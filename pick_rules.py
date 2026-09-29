#!/usr/bin/env python3
r"""UserPromptSubmit hook: Jev picks which saved rules (Claude Code memories) apply to this message.

Claude Code loads only the one-line index of a project's memories (memory/MEMORY.md). This hook
asks Jev one yes/no per memory ("does this rule apply to this message?"), with the user's message
and Claude's previous message as context, and adds the FULL text of the best few to Claude's
context for this turn.

Settings: the `rules_picker` block in questions.json.
  mode "watch": log the picks, add nothing.   mode "on": add them.
Never blocks a message: any error means nothing is added (and the error is logged).

Receives JSON on stdin: {"session_id","transcript_path","cwd","hook_event_name","prompt"}
CLI:  pick_rules.py --try "message text" [transcript.jsonl]   show what would be picked
"""
import glob
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import guard  # noqa: E402

LOG_PATH = os.path.join(HERE, "rules_log.jsonl")


def load_memories(memory_dir):
    """[{id, description, body}] from memory/*.md (frontmatter `description:` + the text after it)."""
    mems = []
    for path in sorted(glob.glob(os.path.join(memory_dir, "*.md"))):
        if os.path.basename(path) == "MEMORY.md":
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
        if not m:
            continue
        desc = re.search(r"^description:\s*(.+)$", m.group(1), re.M)
        if desc:
            mems.append({"id": os.path.basename(path)[:-3], "description": desc.group(1).strip().strip('"')[:220],
                         "body": m.group(2).strip()})
    return mems


def build_questions(mems):
    return {m["id"]: {"type": "noul",
                      "instructions": f"Saved rule/fact: \"{m['description']}\". Should Claude keep this in mind while handling `message` (given `claude_previous_message`)?",
                      "criteria": {"true": "It applies directly: same system, tool, provider, data, or the kind of action the message asks for.",
                                   "false": "Unrelated to what the message is about, or only shares a common word."}}
            for m in mems}


def pick(prompt, claude_last, mems, settings, key_info, jev=None):
    """[(score, memory)] best first: at most top_k, each >= min_score."""
    jev = jev or guard.call_jev
    endpoint, model, key = key_info
    state = {"message": guard.redact(prompt)[:1500], "claude_previous_message": guard.redact(claude_last or "")[:1200]}
    resp = jev({"model": model, "state": state, "questions": build_questions(mems)}, endpoint, key, settings["timeout_seconds"])
    scored = sorted(((guard.noul(resp.get("answers"), m["id"]), m) for m in mems), key=lambda sm: -sm[0])
    return [(s, m) for s, m in scored if s >= settings["min_score"]][:settings["top_k"]], resp


def context_text(picks, max_chars):
    parts = ["Saved rules that likely apply to this message (picked by the Jev rules picker; full text from memory/):"]
    for score, m in picks:
        body = m["body"] if len(m["body"]) <= max_chars else m["body"][:max_chars] + " …"
        parts.append(f"## {m['id']} (Jev {round(score * 100)}%)\n{body}")
    return "\n\n".join(parts)


def run(payload, config, key_info, jev=None):
    """Returns (additional_context or None, log_entry or None)."""
    settings = config.get("rules_picker") or {}
    prompt = (payload.get("prompt") or "").strip()
    if not settings.get("enabled") or not prompt or not guard.in_scope(payload.get("cwd") or "", config):
        return None, None
    transcript = payload.get("transcript_path") or ""
    mems = load_memories(os.path.join(os.path.dirname(transcript), "memory")) if transcript else []
    if not mems:
        return None, None
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "session": payload.get("session_id"), "mode": settings["mode"],
             "prompt": guard.redact(prompt)[:160], "memories": len(mems), "picks": [], "error": None, "ms": None, "tokens": None}
    if key_info is None:
        entry["error"] = "no Jev key"
        return None, entry
    _, claude_last = guard.conversation_context(guard.messages_from_rows(guard.read_tail_rows(transcript)), 1)
    started = time.monotonic()
    try:
        picks, resp = pick(prompt, claude_last, mems, settings, key_info, jev)
    except Exception as e:
        entry.update(error=f"{type(e).__name__}: {e}"[:300], ms=int((time.monotonic() - started) * 1000))
        return None, entry
    entry.update(picks=[[m["id"], round(s, 3)] for s, m in picks], ms=int((time.monotonic() - started) * 1000),
                 tokens=(resp.get("usage") or {}).get("input_tokens"))
    if not picks or settings["mode"] != "on":
        return None, entry
    return context_text(picks, settings["max_chars_each"]), entry


def main():
    try:
        payload = json.load(sys.stdin)
        config = guard.load_config()
    except (OSError, ValueError):
        return 0
    context, entry = run(payload, config, guard.load_key())
    if entry:
        guard.append_log(LOG_PATH, entry)
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))
    return 0


def try_it(message, transcript=None):
    config = guard.load_config()
    settings = dict(config["rules_picker"], mode="on", enabled=True)
    transcript = transcript or max(glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")), key=os.path.getmtime)
    payload = {"prompt": message, "transcript_path": transcript, "cwd": os.path.expanduser("~")}
    context, entry = run(payload, dict(config, rules_picker=settings, scope=["~"]), guard.load_key())
    print(json.dumps(entry, indent=1)[:1500])
    print(context or "(nothing picked)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--try":
        sys.exit(try_it(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None))
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)  # never block a message
