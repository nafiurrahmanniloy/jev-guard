#!/usr/bin/env python3
r"""PreToolUse hook: Jev rule check for risky Bash commands in Claude Code.

Jev (TypeSafe's decision model) answers two yes/no questions per risky action
before Claude runs a command: does this command really do it, and did the user
clearly say yes to it in their recent messages? Everything tunable (scope,
patterns, questions, thresholds, mode) lives in questions.json next to this file
(falls back to questions.example.json when you have not made your own copy).

Flow per Bash command:
  outside scope            -> exit 0 silently, nothing read or sent
  hard stop pattern        -> deny (enforce) / log only (watch)
  no risky pattern         -> exit 0 silently, nothing sent
  risky pattern            -> ONE Jev request, all questions at once, secrets
                              stripped; each action needs approved >= threshold
  Jev unreachable/errors   -> on_error ("ask")

Modes: "watch" logs what it would have done and never interrupts; "enforce"
emits a PreToolUse permissionDecision (ask / deny). Never emits "allow", so the
normal permission system still applies to everything.

Receives JSON on stdin:
{"session_id","transcript_path","cwd","hook_event_name","tool_name","tool_input"}

CLI:  guard.py --stats          summary of log.jsonl
      guard.py --replay [N]     run past in-scope commands through Jev (needs key)
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
LOG_PATH = os.path.join(HERE, "log.jsonl")
REPLAY_LOG_PATH = os.path.join(HERE, "replay.jsonl")
TRANSCRIPTS_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")

MAX_MESSAGE_CHARS = 600    # per user message sent to Jev
MAX_CLAUDE_CHARS = 800     # Claude's last message
MAX_COMMAND_CHARS = 3000
TAIL_BYTES = 400_000       # read only the end of the transcript

ENDPOINTS = {
    "TYPESAFE_API_KEY": ("https://api.typesafe.ai/v1/systemone", "jev-latest"),
    "AI_GATEWAY_API_KEY": ("https://ai-gateway.vercel.sh/typesafe/v1/systemone", "typesafe-ai/jev"),
}

# ── secrets never leave this machine ─────────────────────────────────────────
_REDACTIONS = [
    (re.compile(r"\b(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s'\"]+", re.I), r"\1://[REDACTED]"),
    (re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1 [REDACTED]"),
    (re.compile(r"\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|DATABASE_URL)[A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)"), r"\1=[REDACTED]"),
    (re.compile(r"([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|authorization|apikey|x-api-key)[\"']?\s*[:=]\s*)([\"'][^\"']+[\"']|[^\s,&}]+)", re.I), r"\1[REDACTED]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{10,}"), "[REDACTED]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED-JWT]"),
    (re.compile(r"(?<![A-Za-z0-9/._-])[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9/._-])"), "[REDACTED]"),
]


def redact(text):
    if not text:
        return text
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# ── config / key ─────────────────────────────────────────────────────────────
def config_path():
    own = os.path.join(HERE, "questions.json")
    return own if os.path.exists(own) else os.path.join(HERE, "questions.example.json")


def load_config(path=None):
    with open(path or config_path(), encoding="utf-8") as f:
        return json.load(f)


def load_key():
    """Returns (endpoint, model, key) or None. Env vars win over the .env file."""
    values = {}
    try:
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    for name, (endpoint, model) in ENDPOINTS.items():
        key = os.environ.get(name) or values.get(name)
        if key:
            return endpoint, model, key
    return None


def scope_roots(config):
    return [os.path.realpath(os.path.expanduser(s)) for s in config["scope"]]


def in_scope(cwd, config):
    cwd = os.path.realpath(os.path.expanduser(cwd or ""))
    return any(cwd == root or cwd.startswith(root.rstrip(os.sep) + os.sep) for root in scope_roots(config))


# ── transcript → what the user and Claude last said ─────────────────────────
_REJECTION = re.compile(r"the user said:\s*\n(.+)", re.S)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _clean(text):
    return _REMINDER.sub("", text or "").strip()


def messages_from_rows(rows):
    """[(who, text)] in order. who is 'user' or 'claude'. Skips skill/meta injections and tool output."""
    out = []
    for r in rows:
        t = r.get("type")
        if t == "user" and not r.get("isMeta"):
            content = (r.get("message") or {}).get("content")
            if isinstance(content, str):
                text = _clean(content)
                if text and not text.startswith("<command-") and not text.startswith("<local-command"):
                    out.append(("user", text))
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        text = _clean(c.get("text"))
                        if text:
                            out.append(("user", text))
                    elif c.get("type") == "tool_result":
                        body = c.get("content")
                        if isinstance(body, list):
                            body = " ".join(x.get("text", "") for x in body if isinstance(x, dict))
                        m = _REJECTION.search(body or "")
                        if m:  # the user's words typed while rejecting a tool call
                            out.append(("user", _clean(m.group(1))))
        elif t == "attachment":
            a = r.get("attachment") or {}
            if a.get("type") == "queued_command" and (a.get("origin") or {}).get("kind") == "human":
                text = _clean(a.get("prompt"))
                if text:
                    out.append(("user", text))
        elif t == "assistant":
            for c in (r.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text", "").strip():
                    out.append(("claude", c["text"].strip()))
    return out


def read_tail_rows(path):
    rows = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > TAIL_BYTES:
                f.seek(size - TAIL_BYTES)
                f.readline()  # drop the partial first line
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def conversation_context(messages, n_recent):
    user = [m for who, m in messages if who == "user"][-n_recent:]
    claude = [m for who, m in messages if who == "claude"]
    return (
        [redact(m)[:MAX_MESSAGE_CHARS] for m in user],
        redact(claude[-1])[:MAX_CLAUDE_CHARS] if claude else None,
    )


# ── the check itself ─────────────────────────────────────────────────────────
def effective_folder(command, cwd):
    """A leading `cd X &&` or `git -C X` moves the command; follow it for folder + branch."""
    m = re.match(r"\s*cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*&&", command) or \
        re.search(r"\bgit\s+-C\s+(\"[^\"]+\"|'[^']+'|\S+)", command)
    if m:
        target = os.path.expanduser(m.group(1).strip("'\""))
        return target if os.path.isabs(target) else os.path.join(cwd, target)
    return cwd


def git_branch(folder):
    try:
        out = subprocess.run(["git", "-C", folder, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=1)
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def match_hard_stops(command, config):
    return [h for h in config["hard_stops"] if re.search(h["pattern"], command, re.I | re.M)]


def match_actions(command, config):
    hits = [a for a in config["actions"] if re.search(a["pattern"], command, re.I | re.M)]
    overridden = {o for a in hits for o in a.get("overrides", [])}
    return [a for a in hits if a["id"] not in overridden]


def build_request(actions, state, model):
    questions = {}
    for a in actions:
        questions[f"{a['id']}__performs"] = {"type": "noul", **a["performs"]}
        questions[f"{a['id']}__approved"] = {"type": "noul", **a["approved"]}
    return {"model": model, "state": state, "questions": questions}


def call_jev(body, endpoint, key, timeout):
    req = urllib.request.Request(
        endpoint, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def noul(answers, qid):
    a = (answers or {}).get(qid) or {}
    v = a.get("noul", a.get("probability"))
    if not isinstance(v, (int, float)):
        raise ValueError(f"no noul value for {qid}")
    return float(v)


def decide(actions, answers, config):
    """Per action: skip / ok / ask. Returns (verdicts, needs_ask list)."""
    verdicts, needs_ask = {}, []
    for a in actions:
        performs = noul(answers, f"{a['id']}__performs")
        approved = noul(answers, f"{a['id']}__approved")
        if performs < config["performs_skip_below"]:
            verdict = "skip"
        elif approved >= a["threshold"]:
            verdict = "ok"
        else:
            verdict = "ask"
            needs_ask.append((a, approved))
        verdicts[a["id"]] = {"performs": round(performs, 3), "approved": round(approved, 3),
                             "threshold": a["threshold"], "verdict": verdict}
    return verdicts, needs_ask


def evaluate(command, cwd, messages, config, key_info, jev=None):
    """Core shared by the hook and --replay. Returns a result dict."""
    jev = jev or call_jev
    result = {"hard_stops": [], "actions": [], "decision": "pass", "reason": None,
              "verdicts": {}, "error": None, "ms": None, "tokens": None}
    stops = match_hard_stops(command, config)
    if stops:
        result.update(hard_stops=[h["id"] for h in stops], decision="deny",
                      reason="Jev rule check (hard stop): " + " ".join(h["reason"] for h in stops))
        return result

    actions = match_actions(command, config)
    result["actions"] = [a["id"] for a in actions]
    if not actions:
        return result

    folder = effective_folder(command, cwd)
    root = scope_roots(config)[0]
    user, claude_last = conversation_context(messages, config["recent_messages"])
    state = {
        "command": redact(command)[:MAX_COMMAND_CHARS],
        "folder": os.path.relpath(os.path.realpath(folder), root) if in_scope(folder, config) else os.path.basename(folder),
        "git_branch": git_branch(folder),
        "user_recent_messages": user,
        "claude_last_message": claude_last,
    }
    if key_info is None:
        result["error"] = "no Jev key (put TYPESAFE_API_KEY or AI_GATEWAY_API_KEY in .env)"
    else:
        endpoint, model, key = key_info
        started = time.monotonic()
        try:
            resp = jev(build_request(actions, state, model), endpoint, key, config["timeout_seconds"])
            result["ms"] = int((time.monotonic() - started) * 1000)
            result["tokens"] = (resp.get("usage") or {}).get("input_tokens")
            verdicts, needs_ask = decide(actions, resp.get("answers"), config)
            result["verdicts"] = verdicts
            if needs_ask:
                parts = [f"{a['label']} (Jev is {round(p * 100)}% sure you said yes; needs {round(a['threshold'] * 100)}%)"
                         for a, p in needs_ask]
                result.update(decision="ask", reason="Jev rule check: about to " + "; ".join(parts) + ".")
            return result
        except urllib.error.HTTPError as e:
            result["error"] = f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
        except Exception as e:  # timeout, DNS, bad JSON, missing answer
            result["error"] = f"{type(e).__name__}: {e}"
        result["ms"] = int((time.monotonic() - started) * 1000)

    if config["on_error"] == "ask":
        labels = ", ".join(a["label"] for a in actions)
        result.update(decision="ask", reason=f"Jev rule check could not reach Jev ({result['error'][:120]}). About to {labels}. Approve?")
    return result


def append_log(path, entry):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── entry points ─────────────────────────────────────────────────────────────
def hook():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    cwd = payload.get("cwd") or os.getcwd()
    try:
        config = load_config()
    except (OSError, ValueError):
        return 0  # a broken rules file must never block work
    if not in_scope(cwd, config) or not command.strip():
        return 0
    if not match_hard_stops(command, config) and not match_actions(command, config):
        return 0  # most commands: nothing read, nothing sent, nothing logged

    messages = messages_from_rows(read_tail_rows(payload.get("transcript_path") or ""))
    result = evaluate(command, cwd, messages, config, load_key())
    append_log(LOG_PATH, {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "session": payload.get("session_id"),
        "mode": config["mode"], "folder": os.path.relpath(os.path.realpath(cwd), scope_roots(config)[0]),
        "command": redact(command)[:300],
        **{k: result[k] for k in ("hard_stops", "actions", "verdicts", "decision", "error", "ms", "tokens")},
    })
    if config["mode"] != "enforce" or result["decision"] == "pass":
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": result["decision"],  # "ask" or "deny", never "allow"
        "permissionDecisionReason": result["reason"],
    }}))
    return 0


def stats():
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except OSError:
        rows = []
    if not rows:
        print("No checks logged yet.")
        return 0
    by = {}
    for r in rows:
        by[r["decision"]] = by.get(r["decision"], 0) + 1
    errors = [r for r in rows if r.get("error")]
    timed = sorted(r["ms"] for r in rows if r.get("ms") is not None and not r.get("error"))
    print(f"Checks: {len(rows)}  (first {rows[0]['ts']}, last {rows[-1]['ts']})")
    print("Decisions: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    print(f"Jev errors: {len(errors)}" + (f"  (last: {errors[-1]['ts']} {errors[-1]['error'][:100]})" if errors else ""))
    if timed:
        print(f"Jev time: median {timed[len(timed) // 2]} ms, slowest {timed[-1]} ms")
    for r in rows[-5:]:
        print(f"  {r['ts']}  {r['mode']:7}  {r['decision']:4}  {r['command'][:80]}")
    return 0


def replay(limit=None):
    """Run every past in-scope risky command through Jev with the messages that preceded it."""
    config, key_info = load_config(), load_key()
    if key_info is None:
        print("No Jev key yet: put TYPESAFE_API_KEY or AI_GATEWAY_API_KEY in", ENV_PATH)
        return 1
    done = 0
    for path in sorted(glob.glob(TRANSCRIPTS_GLOB)):
        rows = []
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rows.append(row)
                if row.get("type") != "assistant":
                    continue
                for c in (row.get("message") or {}).get("content") or []:
                    if not (isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash"):
                        continue
                    command, cwd = (c.get("input") or {}).get("command") or "", row.get("cwd") or ""
                    if not in_scope(cwd, config):
                        continue
                    if not match_hard_stops(command, config) and not match_actions(command, config):
                        continue
                    messages = messages_from_rows(rows)
                    result = evaluate(command, cwd, messages, config, key_info)
                    user, claude_last = conversation_context(messages, config["recent_messages"])
                    append_log(REPLAY_LOG_PATH, {
                        "transcript": os.path.basename(path), "ts": row.get("timestamp"),
                        "command": redact(command)[:300], "user": user, "claude_last": claude_last,
                        **{k: result[k] for k in ("hard_stops", "actions", "verdicts", "decision", "error", "ms", "tokens")},
                    })
                    done += 1
                    if limit and done >= limit:
                        print(f"Replayed {done} commands -> {REPLAY_LOG_PATH}")
                        return 0
    print(f"Replayed {done} commands -> {REPLAY_LOG_PATH}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--stats":
        sys.exit(stats())
    if len(sys.argv) > 1 and sys.argv[1] == "--replay":
        sys.exit(replay(int(sys.argv[2]) if len(sys.argv) > 2 else None))
    try:
        sys.exit(hook())
    except Exception:
        sys.exit(0)  # the guard must never break a session
