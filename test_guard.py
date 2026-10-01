#!/usr/bin/env python3
"""Tests for guard.py with a fake Jev (no key, no network). Run: python3 test_guard.py"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import guard  # noqa: E402

EXAMPLE = os.path.join(HERE, "questions.example.json")
CONFIG = guard.load_config(EXAMPLE)
IN_SCOPE = os.path.expanduser("~/code/some-project")
OUT_OF_SCOPE = "/tmp/elsewhere"
KEY = ("https://example.invalid/v1/systemone", "jev-latest", "k")
POPUPS = []
guard.popup = POPUPS.append  # tests never show real Mac notifications


def fake_jev(performs=0.95, approved=0.99, unrelated=0.05):
    calls = []

    def jev(body, endpoint, key, timeout):
        calls.append(body)
        answers = {}
        for qid in body["questions"]:
            v = performs if qid.endswith("__performs") else unrelated if qid == "change__unrelated" else approved
            answers[qid] = {"type": "noul", "noul": v}
        return {"answers": answers, "usage": {"input_tokens": 300}}
    jev.calls = calls
    return jev


class HardStops(unittest.TestCase):
    def stops(self, cmd):
        return [h["id"] for h in guard.match_hard_stops(cmd, CONFIG)]

    def test_stopped(self):
        for cmd in ["git add -A", "git add .", "cd repo && git add -A && git commit -m x",
                    "git -C /x add --all", "git commit -am 'fix'", "git commit -a -m x"]:
            self.assertTrue(self.stops(cmd), cmd)

    def test_not_stopped(self):
        for cmd in ["git add src/a.js lib/b.js", "git add ./src/file.js", "git commit -m 'add a thing'",
                    "git commit --amend --no-edit", 'echo "never git add -A"', "git status", "npm install"]:
            self.assertEqual(self.stops(cmd), [], cmd)


class Actions(unittest.TestCase):
    def ids(self, cmd):
        return [a["id"] for a in guard.match_actions(cmd, CONFIG)]

    def test_matches(self):
        self.assertEqual(self.ids("git push -u origin feat/x"), ["push"])
        self.assertEqual(self.ids("git push --force-with-lease origin feat/x"), ["force_push"])
        self.assertEqual(self.ids("git push origin +feat/x"), ["force_push"])
        self.assertEqual(self.ids('gh pr create --title "x" --body y'), ["open_pr"])
        self.assertEqual(self.ids("gh pr merge 741 --squash"), ["merge_pr"])
        self.assertEqual(self.ids('git commit -m "fix: thing"'), ["commit"])
        self.assertIn("db_write", self.ids("psql \"$DATABASE_URL\" -c \"DELETE FROM guests WHERE id=1\""))
        self.assertIn("remote_write", self.ids("ssh deploy@example.com 'docker compose restart app'"))
        self.assertIn("paid_api", self.ids("curl -s https://api.openai.com/v1/chat/completions -d @body.json"))

    def test_harmless(self):
        for cmd in ["git status", "git log --oneline -5", "npm test", "gh pr view 741", "gh pr list",
                    "git diff origin/main", "ls -la", "cat package.json"]:
            self.assertEqual(self.ids(cmd), [], cmd)


class Redaction(unittest.TestCase):
    def test_secrets_removed(self):
        cases = {
            "psql postgresql://user:pa55@db.example.com:5432/postgres -c 'select 1'": "pa55",
            "curl -H 'Authorization: Bearer abcdefghijklmnop123' https://x": "abcdefghijklmnop123",
            "OPENAI_API_KEY=sk-proj-abcdefghijklmnop node x.js": "sk-proj-abcdefghijklmnop",
            "export DATABASE_URL=postgres://a:b@c/d": "a:b@c",
            "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U": "eyJhbGciOiJIUzI1NiJ9",
            '{"apiKey": "live_4f9a8b7c6d5e"}': "live_4f9a8b7c6d5e",
            "gh auth token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789": "ghp_ABCDEFGHIJ",
        }
        for text, secret in cases.items():
            self.assertNotIn(secret, guard.redact(text), text)

    def test_pasted_content_stripped(self):
        self.assertEqual(guard._clean('<pasted_content id="x1">\nsome doc text\n</pasted_content> push it'), "push it")

    def test_ordinary_text_kept(self):
        self.assertEqual(guard.redact('git commit -m "fix: scan dates"'), 'git commit -m "fix: scan dates"')


class Transcript(unittest.TestCase):
    def test_messages(self):
        rows = [
            {"type": "user", "message": {"content": "hey, fix the bug"}},
            {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "Base directory for this skill"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixed. Want me to commit?"}]}},
            {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "yes commit it", "origin": {"kind": "human"}}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Rejected. To tell you how to proceed, the user said:\nno, open a PR instead"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "file contents here"}]}},
            {"type": "user", "message": {"content": "<system-reminder>ignore me</system-reminder>merge it"}},
            {"type": "attachment", "attachment": {"type": "queued_command", "origin": {"kind": "human"},
                                                  "prompt": [{"type": "image", "source": {}}, {"type": "text", "text": "see screenshot, push it"}]}},
            {"type": "attachment", "attachment": {"type": "queued_command", "origin": {"kind": "human"}, "prompt": None}},
            {"type": "user", "message": {"content": "<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>"}},
            {"type": "user", "message": {"content": "This session is being continued from a previous conversation. Summary: merge it"}},
            {"type": "user", "message": {"content": [{"type": "text", "text": "<task-notification><status>failed</status>"}]}},
        ]
        msgs = guard.messages_from_rows(rows)
        self.assertEqual([m for w, m in msgs if w == "user"],
                         ["hey, fix the bug", "yes commit it", "no, open a PR instead", "merge it", "see screenshot, push it"])
        user, claude = guard.conversation_context(msgs, 3)
        self.assertEqual(user, ["no, open a PR instead", "merge it", "see screenshot, push it"])
        self.assertEqual(claude, "Fixed. Want me to commit?")


class LongSessions(unittest.TestCase):
    def test_user_messages_found_behind_lots_of_tool_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.jsonl")
            with open(path, "w") as f:
                for text in ("first ask", "commit it when done"):
                    f.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
                blob = "x" * 5000
                for _ in range(200):  # ~1 MB of tool output after the last user message
                    f.write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": blob}]}}) + "\n")
            self.assertEqual(guard.messages_from_rows(guard.read_tail_rows(path)), [])  # the old way saw nothing
            user, _ = guard.conversation_context(guard.recent_messages(path, 3), 3)
            self.assertEqual(user, ["first ask", "commit it when done"])


class Decisions(unittest.TestCase):
    msgs = [("user", "commit it"), ("claude", "Committing now.")]

    def test_mention_only_is_skipped(self):
        r = guard.evaluate('grep -rn "git commit" docs', IN_SCOPE, self.msgs, CONFIG, KEY, fake_jev(performs=0.05, approved=0.0))
        self.assertEqual(r["decision"], "pass")
        self.assertEqual(r["verdicts"]["commit"]["verdict"], "skip")

    def test_approved_runs(self):
        r = guard.evaluate('git commit -m "x"', IN_SCOPE, self.msgs, CONFIG, KEY, fake_jev(approved=0.9))
        self.assertEqual(r["decision"], "pass")  # commit threshold 0.85

    def test_under_threshold_asks(self):
        r = guard.evaluate("gh pr merge 1 --squash", IN_SCOPE, self.msgs, CONFIG, KEY, fake_jev(approved=0.8))
        self.assertEqual(r["decision"], "ask")  # merge needs 0.85
        self.assertIn("merge a pull request", r["reason"])
        self.assertIn("80%", r["reason"])

    def test_unsure_is_logged_not_asked(self):
        r = guard.evaluate("gh pr merge 1 --squash", IN_SCOPE, self.msgs, CONFIG, KEY, fake_jev(performs=0.5, approved=0.0))
        self.assertEqual(r["decision"], "pass")
        self.assertEqual(r["verdicts"]["merge_pr"]["verdict"], "unsure")

    def test_sure_and_unapproved_still_asks(self):
        r = guard.evaluate("gh pr merge 1 --squash", IN_SCOPE, self.msgs, CONFIG, KEY, fake_jev(performs=0.7, approved=0.0))
        self.assertEqual(r["decision"], "ask")

    def test_no_ask_bar_keeps_old_behaviour(self):
        cfg = {k: v for k, v in CONFIG.items() if k != "performs_ask_from"}
        r = guard.evaluate("gh pr merge 1 --squash", IN_SCOPE, self.msgs, cfg, KEY, fake_jev(performs=0.5, approved=0.0))
        self.assertEqual(r["decision"], "ask")

    def test_one_request_all_questions(self):
        jev = fake_jev()
        guard.evaluate('git commit -m x && git push origin feat/x', IN_SCOPE, self.msgs, CONFIG, KEY, jev)
        self.assertEqual(len(jev.calls), 1)
        self.assertEqual(sorted(jev.calls[0]["questions"]),
                         ["commit__approved", "commit__performs", "push__approved", "push__performs"])
        self.assertEqual(jev.calls[0]["state"]["user_recent_messages"], ["commit it"])

    def test_state_is_redacted(self):
        jev = fake_jev()
        guard.evaluate("psql postgres://u:secretpw@h/db -c 'DELETE FROM x'", IN_SCOPE,
                       [("user", "use sk-abcdefghijklmnopqrst to delete")], CONFIG, KEY, jev)
        sent = json.dumps(jev.calls[0])
        self.assertNotIn("secretpw", sent)
        self.assertNotIn("sk-abcdefghijklmnopqrst", sent)

    def test_jev_error_asks(self):
        def broken(*a):
            raise TimeoutError("timed out")
        r = guard.evaluate('git commit -m x', IN_SCOPE, self.msgs, CONFIG, KEY, broken)
        self.assertEqual(r["decision"], "ask")
        self.assertIn("timed out", r["error"])

    def test_no_key_asks(self):
        r = guard.evaluate('git commit -m x', IN_SCOPE, self.msgs, CONFIG, None, fake_jev())
        self.assertEqual(r["decision"], "ask")
        self.assertIn("no Jev key", r["error"])

    def test_missing_answer_asks(self):
        def empty(*a):
            return {"answers": {}}
        r = guard.evaluate('git commit -m x', IN_SCOPE, self.msgs, CONFIG, KEY, empty)
        self.assertEqual(r["decision"], "ask")


class Hook(unittest.TestCase):
    def run_hook(self, command, cwd=IN_SCOPE, mode="watch", jev=None):
        cfg = dict(CONFIG, mode=mode)
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "log.jsonl")
            payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd,
                       "transcript_path": os.path.join(tmp, "none.jsonl"), "session_id": "t"}
            out = io.StringIO()
            with mock.patch.object(guard, "LOG_PATH", log), \
                 mock.patch.object(guard, "load_config", return_value=cfg), \
                 mock.patch.object(guard, "load_key", return_value=KEY), \
                 mock.patch.object(guard, "call_jev", jev or fake_jev(approved=0.0)), \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                 redirect_stdout(out):
                guard.hook()
            logged = []
            if os.path.exists(log):
                with open(log) as f:
                    logged = f.read().splitlines()
            return out.getvalue(), logged

    def test_out_of_scope_silent(self):
        out, logged = self.run_hook("git commit -m x", cwd=OUT_OF_SCOPE, mode="enforce")
        self.assertEqual((out, logged), ("", []))

    def test_harmless_silent_and_unlogged(self):
        out, logged = self.run_hook("git status", mode="enforce")
        self.assertEqual((out, logged), ("", []))

    def test_watch_logs_but_never_interrupts(self):
        out, logged = self.run_hook("git commit -m x", mode="watch")
        self.assertEqual(out, "")
        self.assertEqual(json.loads(logged[0])["decision"], "ask")

    def test_enforce_asks(self):
        out, _ = self.run_hook("git commit -m x", mode="enforce")
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "ask")

    def test_enforce_hard_stop_denies(self):
        out, _ = self.run_hook("git add -A", mode="enforce")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_crash_is_logged_and_asks(self):
        with mock.patch.object(guard, "messages_from_rows", side_effect=TypeError("boom")):
            out, logged = self.run_hook("git commit -m x", mode="enforce")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("guard crash", json.loads(logged[0])["error"])

    def test_never_allow(self):
        out, _ = self.run_hook("git commit -m x", mode="enforce", jev=fake_jev(approved=1.0))
        self.assertEqual(out, "")  # approved: stay silent, never emit "allow"


def git_repo(tmp):
    """A throwaway repo: a.txt and d.txt committed, then c.txt staged, a.txt + d.txt edited, b.txt new."""
    import subprocess
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp, capture_output=True, check=True)
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    def write(name, text):
        with open(os.path.join(tmp, name), "w") as fh:
            fh.write(text)
    for f in ("a.txt", "d.txt", "c.txt"):
        write(f, f"{f} v1\n")
    run("add", "a.txt", "d.txt", "c.txt"); run("commit", "-qm", "init")
    for f in ("a.txt", "d.txt", "c.txt"):
        write(f, f"{f} v2\n")
    write("b.txt", "brand new\n")
    run("add", "c.txt")
    return tmp


class ChangeCheck(unittest.TestCase):
    def test_commit_message_styles(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "msg.txt"), "w") as fh:
                fh.write("from a file on disk\n")
            cases = {
                "git add a && git commit -q -F - <<'EOF' && git log -1\nfix: the heredoc way\n\nbody line\nEOF": "fix: the heredoc way\n\nbody line",
                'git commit -m "$(cat <<\'EOF\'\nfeat: cat heredoc\nEOF\n)"': "feat: cat heredoc",
                'git commit -m "first para" -m "second para"': "first para\n\nsecond para",
                "cat > /tmp/m.txt <<'MSG'\nwritten earlier\nMSG\ngit commit -F /tmp/m.txt": "written earlier",
                "git commit -F msg.txt": "from a file on disk\n",
                "git commit": None,   # opens an editor: unreadable
            }
            for cmd, want in cases.items():
                self.assertEqual(guard.commit_message(cmd, tmp), want, cmd)

    def test_pending_diff_includes_files_added_in_same_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            git_repo(tmp)
            diff = guard.pending_commit_diff("git add a.txt b.txt && git commit -m x", tmp)
            self.assertEqual(sorted(guard.changed_files(diff)), ["a.txt", "b.txt", "c.txt"])  # not d.txt

    def evaluate(self, cmd, **fake):
        with tempfile.TemporaryDirectory() as tmp:
            git_repo(tmp)
            jev = fake_jev(**fake)
            r = guard.evaluate(cmd, tmp, [("user", "commit it")], dict(CONFIG, scope=[tmp]), KEY, jev)
            return r, jev

    def test_unrelated_change_asks_even_when_approved(self):
        r, jev = self.evaluate("git add a.txt && git commit -m 'fix typo in a'", approved=0.99, unrelated=0.9)
        self.assertEqual(r["decision"], "ask")
        self.assertIn("don't match its description", r["reason"])
        self.assertIn("a.txt", r["reason"])
        self.assertIn("change__unrelated", jev.calls[0]["questions"])
        self.assertTrue(jev.calls[0]["state"]["commit"].startswith("fix typo in a"))

    def test_matching_change_passes(self):
        r, _ = self.evaluate("git add a.txt && git commit -m 'fix typo in a'", approved=0.99, unrelated=0.1)
        self.assertEqual(r["decision"], "pass")
        self.assertEqual(r["change"]["unrelated"], 0.1)

    def test_unreadable_message_skips_check(self):
        r, jev = self.evaluate("git commit", approved=0.99)
        self.assertNotIn("change__unrelated", jev.calls[0]["questions"])
        self.assertIn("could not read", r["change"]["skipped"])

    def test_mention_only_never_asks_about_change(self):
        r, _ = self.evaluate('grep -rn "git commit -m x" .', performs=0.05, unrelated=0.99)
        self.assertEqual(r["decision"], "pass")

    def test_commit_message_mentioning_a_merge_is_still_a_commit(self):
        cmd = "git add a.txt && git commit -q -F - <<'EOF'\nfix: guard now checks gh pr merge and gh pr create\nEOF"
        self.assertEqual(guard.change_action_in(cmd, ["merge_pr", "open_pr", "commit"]), "commit")
        self.assertNotIn("gh pr merge", guard.strip_heredoc_bodies(cmd))
        self.assertEqual(guard.change_action_in("git push && gh pr merge 5", ["merge_pr", "push"]), "merge_pr")

    def test_approved_plan_counts_as_user_words(self):
        rows = [{"type": "user", "message": {"content": "yes plan it and build both"}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "content":
                    "User has approved your plan. You can now start coding.\n\n## Approved Plan:\n# Build\n5. Commit and push to the public repo."}]}}]
        user, _ = guard.conversation_context(guard.messages_from_rows(rows), 3)
        self.assertEqual(user[0], "yes plan it and build both")
        self.assertTrue(user[1].startswith("[approved plan] # Build"))
        self.assertIn("Commit and push", user[1])

    def test_merge_reads_the_pr(self):
        def fake_run(folder, *args, timeout=8):
            if args[:3] == ("gh", "pr", "view"):
                return json.dumps({"title": "chore: take Billing out", "body": "frontend only"})
            if args[:3] == ("gh", "pr", "diff"):
                return "diff --git a/backend/automations.js b/backend/automations.js\n-healMissingFirstSync()\n"
            return ""
        with mock.patch.object(guard, "_run", side_effect=fake_run):
            jev = fake_jev(approved=0.99, unrelated=0.95)
            r = guard.evaluate("gh pr merge 333 --squash", IN_SCOPE, [("user", "merge it")], CONFIG, KEY, jev)
        self.assertEqual(r["decision"], "ask")
        self.assertIn("backend/automations.js", r["reason"])
        self.assertTrue(jev.calls[0]["state"]["commit"].startswith("chore: take Billing out"))


class RulesPicker(unittest.TestCase):
    MEMS = {"never-git-add-all": ("Stage by explicit path; git add -A swept a peer's work", "Full rule body about staging."),
            "ask-before-paid-calls": ("Never call a paid provider without a yes", "Full rule body about paid APIs."),
            "currency-from-pms": ("Currency comes from the PMS, never converted", "Full rule body about currency.")}

    def setup_dir(self, tmp):
        os.makedirs(os.path.join(tmp, "memory"))
        for mid, (desc, body) in self.MEMS.items():
            with open(os.path.join(tmp, "memory", f"{mid}.md"), "w") as fh:
                fh.write(f"---\nname: {mid}\ndescription: \"{desc}\"\nmetadata:\n  type: feedback\n---\n\n{body}\n")
        with open(os.path.join(tmp, "memory", "MEMORY.md"), "w") as fh:
            fh.write("- index line\n")
        return os.path.join(tmp, "session.jsonl")

    def fake(self, scores):
        calls = []
        def jev(body, endpoint, key, timeout):
            calls.append(body)
            return {"answers": {q: {"type": "noul", "noul": scores.get(q, 0.1)} for q in body["questions"]}, "usage": {"input_tokens": 99}}
        jev.calls = calls
        return jev

    def run_picker(self, scores, mode="on", key=KEY, jev=None):
        import pick_rules
        with tempfile.TemporaryDirectory() as tmp:
            transcript = self.setup_dir(tmp)
            cfg = dict(CONFIG, rules_picker=dict(CONFIG["rules_picker"], mode=mode))
            payload = {"prompt": "whose paid API key were you using?", "transcript_path": transcript, "cwd": IN_SCOPE, "session_id": "s"}
            jev = jev or self.fake(scores)
            return pick_rules.run(payload, cfg, key, jev) + (jev,)

    def test_picks_full_text_above_bar_best_first(self):
        ctx, entry, jev = self.run_picker({"ask-before-paid-calls": 0.97, "currency-from-pms": 0.88, "never-git-add-all": 0.4})
        self.assertEqual([p[0] for p in entry["picks"]], ["ask-before-paid-calls", "currency-from-pms"])
        self.assertIn("Full rule body about paid APIs.", ctx)
        self.assertLess(ctx.index("paid APIs"), ctx.index("currency"))
        self.assertNotIn("staging", ctx)
        self.assertEqual(len(jev.calls), 1)
        self.assertEqual(sorted(jev.calls[0]["questions"]), sorted(self.MEMS))  # one question per memory, MEMORY.md excluded

    def test_top_k_cap(self):
        _, entry, _ = self.run_picker({m: 0.99 for m in self.MEMS})
        self.assertEqual(len(entry["picks"]), CONFIG["rules_picker"]["top_k"])

    def test_watch_mode_logs_but_adds_nothing(self):
        ctx, entry, _ = self.run_picker({"ask-before-paid-calls": 0.97}, mode="watch")
        self.assertIsNone(ctx)
        self.assertEqual(entry["picks"][0][0], "ask-before-paid-calls")

    def test_error_adds_nothing_and_is_logged(self):
        def broken(*a):
            raise TimeoutError("slow")
        ctx, entry, _ = self.run_picker({}, jev=broken)
        self.assertIsNone(ctx)
        self.assertIn("slow", entry["error"])

    def test_task_notices_are_skipped(self):
        import pick_rules
        with tempfile.TemporaryDirectory() as tmp:
            transcript = self.setup_dir(tmp)
            jev = self.fake({"ask-before-paid-calls": 0.99})
            ctx, entry = pick_rules.run({"prompt": "<task-notification>\n<status>done</status>", "transcript_path": transcript,
                                         "cwd": IN_SCOPE}, CONFIG, KEY, jev)
        self.assertEqual((ctx, entry, jev.calls), (None, None, []))

    def test_no_memory_folder_is_silent(self):
        import pick_rules
        ctx, entry = pick_rules.run({"prompt": "hi", "transcript_path": "/nonexistent/s.jsonl", "cwd": IN_SCOPE}, CONFIG, KEY, self.fake({}))
        self.assertEqual((ctx, entry), (None, None))

    def test_hook_output_shape(self):
        import pick_rules
        with tempfile.TemporaryDirectory() as tmp:
            transcript = self.setup_dir(tmp)
            cfg = dict(CONFIG, rules_picker=dict(CONFIG["rules_picker"], mode="on"))
            payload = {"prompt": "paid call?", "transcript_path": transcript, "cwd": IN_SCOPE}
            out = io.StringIO()
            with mock.patch.object(pick_rules.guard, "load_config", return_value=cfg), \
                 mock.patch.object(pick_rules.guard, "load_key", return_value=KEY), \
                 mock.patch.object(pick_rules.guard, "call_jev", self.fake({"ask-before-paid-calls": 0.95})), \
                 mock.patch.object(pick_rules, "LOG_PATH", os.path.join(tmp, "log.jsonl")), \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
                pick_rules.main()
        hso = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertIn("Full rule body about paid APIs.", hso["additionalContext"])


class PerActionEnforce(unittest.TestCase):
    """Option A: costly actions + change check interrupt; commit/push/PR only log."""
    def cfg(self):
        c = json.loads(json.dumps(CONFIG))
        c["mode"] = "enforce"
        for a in c["actions"]:
            a["enforce"] = a["id"] not in ("commit", "push", "open_pr")
        for h in c["hard_stops"]:
            h["enforce"] = False
        c["change_check"]["enforce"] = True
        return c

    def hook(self, command, jev, cwd=IN_SCOPE, cfg=None):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "log.jsonl")
            payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd, "transcript_path": "/nonexistent"}
            out = io.StringIO()
            with mock.patch.object(guard, "LOG_PATH", log), mock.patch.object(guard, "load_config", return_value=cfg or self.cfg()), \
                 mock.patch.object(guard, "load_key", return_value=KEY), mock.patch.object(guard, "call_jev", jev), \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
                guard.hook()
            with open(log) as f:
                return out.getvalue(), json.loads(f.read().splitlines()[-1])

    def test_watch_only_action_logs_but_does_not_interrupt(self):
        out, entry = self.hook("git push origin feat/x", fake_jev(approved=0.0))
        self.assertEqual(out, "")
        self.assertEqual((entry["decision"], entry["interrupted"]), ("ask", False))

    def test_enforced_action_interrupts(self):
        out, entry = self.hook("gh pr merge 5", fake_jev(approved=0.0))
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertTrue(entry["interrupted"])

    def test_change_check_interrupts_a_watch_only_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            git_repo(tmp)
            cfg = dict(self.cfg(), scope=[tmp])
            out, _ = self.hook("git add a.txt && git commit -m 'fix typo'", fake_jev(approved=0.0, unrelated=0.9), cwd=tmp, cfg=cfg)
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("don't match its description", reason)
        self.assertNotIn("sure you said yes", reason)  # the commit-approval part stays log-only

    def test_watch_only_hard_stop_logs(self):
        out, entry = self.hook("git add -A", fake_jev())
        self.assertEqual((out, entry["decision"], entry["interrupted"]), ("", "deny", False))

    def test_jev_down_interrupts_only_for_enforced(self):
        def broken(*a):
            raise TimeoutError("down")
        self.assertEqual(self.hook("git push origin x", broken)[0], "")
        self.assertIn("could not reach Jev", self.hook("gh pr merge 5", broken)[0])


class Popups(unittest.TestCase):
    """Push and PR pop up on the Mac; they stop only when Jev is sure the user never said yes."""
    def cfg(self, mode="enforce"):
        c = json.loads(json.dumps(CONFIG))
        c["mode"] = mode
        for a in c["actions"]:
            if a["id"] in ("push", "open_pr"):
                a["threshold"] = 0.15
        return c

    def hook(self, command, jev, mode="enforce"):
        POPUPS.clear()
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "log.jsonl")
            payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": IN_SCOPE, "transcript_path": "/nonexistent"}
            out = io.StringIO()
            with mock.patch.object(guard, "LOG_PATH", log), mock.patch.object(guard, "load_config", return_value=self.cfg(mode)), \
                 mock.patch.object(guard, "load_key", return_value=KEY), mock.patch.object(guard, "call_jev", jev), \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
                guard.hook()
            with open(log) as f:
                return out.getvalue(), json.loads(f.read().splitlines()[-1]), list(POPUPS)

    def test_approved_push_pops_up_and_runs(self):
        out, entry, pops = self.hook("git push -q origin HEAD:fix/money", fake_jev(approved=0.4))
        self.assertEqual(out, "")
        self.assertEqual(pops, ["some-project: pushing fix/money"])
        self.assertEqual(entry["popup"], pops[0])

    def test_never_said_yes_stops_and_says_so(self):
        out, _, pops = self.hook('gh pr create --base main --title "Real-time webhooks" --body x', fake_jev(approved=0.05))
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertEqual(pops, ["some-project: opening PR 'Real-time webhooks' (waiting for your OK)"])

    def test_push_and_pr_in_one_popup(self):
        _, _, pops = self.hook('git push -u origin feat/x && gh pr create --title "T"', fake_jev(approved=0.9))
        self.assertEqual(pops, ["some-project: pushing feat/x; opening PR 'T'"])

    def test_unsure_or_mention_no_popup(self):
        self.assertEqual(self.hook("git push origin feat/x", fake_jev(performs=0.5))[2], [])
        self.assertEqual(self.hook('grep -rn "git push" docs', fake_jev(performs=0.05))[2], [])

    def test_watch_mode_no_popup(self):
        self.assertEqual(self.hook("git push origin feat/x", fake_jev(), mode="watch")[2], [])

    def test_merge_does_not_pop_up(self):
        self.assertEqual(self.hook("gh pr merge 5", fake_jev(approved=0.0))[2], [])

    def test_push_target(self):
        self.assertEqual(guard.push_target("git push -q origin HEAD:fix/a 2>&1 | tail -2", IN_SCOPE), "fix/a")
        self.assertEqual(guard.push_target("git push -u origin feat/b", IN_SCOPE), "feat/b")
        self.assertEqual(guard.push_target("git push origin +feat/c", IN_SCOPE), "feat/c")
        with mock.patch.object(guard, "git_branch", return_value="main"):
            self.assertEqual(guard.push_target("git push", IN_SCOPE), "main")


DELETE_SCRIPT = """import pg from 'pg'
const c = new pg.Client({ connectionString: process.env.DATABASE_URL })
await c.connect(); await c.query('BEGIN')
const r = await c.query('DELETE FROM reviews WHERE tenant_id = $1 AND id = ANY($2::uuid[])', [T, ids])
await c.query('COMMIT')
"""
READ_SCRIPT = """import pg from 'pg'
const c = new pg.Client({ connectionString: process.env.DATABASE_URL })
console.log((await c.query('select count(*) from reviews')).rows)
"""


class Scripts(unittest.TestCase):
    """A command that runs a local script is judged on the script's code (the delete-reviews.mjs shape)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        for name, code in (("delete-reviews.mjs", DELETE_SCRIPT), ("count.mjs", READ_SCRIPT),
                           ("ops.mjs", "await sb.from('ops_users').update({ active: false }).eq('id', id)\n"),
                           ("cleanup.test.mjs", DELETE_SCRIPT)):
            with open(os.path.join(self.dir, name), "w") as f:
                f.write(code)
        self.real = f"S={self.dir} && NODE_OPTIONS=--x=1 node --env-file=.env $S/delete-reviews.mjs"

    def tearDown(self):
        self.tmp.cleanup()

    def ids(self, command, cwd=IN_SCOPE):
        return [a["id"] for a in guard.match_actions(command, CONFIG, guard.scripts_run(command, cwd))]

    def test_the_real_shape_is_now_seen(self):
        self.assertEqual(self.ids(self.real), ["db_write"])
        jev = fake_jev(approved=0.0)
        r = guard.evaluate(self.real, IN_SCOPE, [("user", "count the reviews")], CONFIG, KEY, jev)
        self.assertEqual(r["decision"], "ask")
        self.assertIn("change or delete rows", r["reason"])
        sent = jev.calls[0]["state"]["scripts_run"][0]
        self.assertTrue(sent["path"].endswith("delete-reviews.mjs"))
        self.assertIn("DELETE FROM reviews", sent["code"])

    def test_without_the_file_it_was_invisible(self):
        self.assertEqual([a["id"] for a in guard.match_actions(self.real, CONFIG)], [])

    def test_read_only_script_sends_nothing(self):
        self.assertEqual(self.ids(f"node {self.dir}/count.mjs"), [])

    def test_cd_then_relative_path(self):
        self.assertEqual(self.ids(f"cd {self.dir} && node delete-reviews.mjs 2>&1 | tail -3"), ["db_write"])

    def test_supabase_update(self):
        self.assertEqual(self.ids(f"node {self.dir}/ops.mjs"), ["db_write"])

    def test_tests_and_checks_are_skipped(self):
        self.assertEqual(self.ids(f"node --test {self.dir}/cleanup.test.mjs"), [])
        self.assertEqual(self.ids(f"node --check {self.dir}/delete-reviews.mjs"), [])

    def test_unknown_variable_or_missing_file_is_skipped(self):
        self.assertEqual(self.ids("node $NOPE_NOT_SET/delete-reviews.mjs"), [])
        self.assertEqual(self.ids(f"node {self.dir}/gone.mjs"), [])

    def test_heredoc_text_is_not_a_run(self):
        cmd = f"git commit -q -F - <<'EOF'\nfix: node {self.dir}/delete-reviews.mjs no longer leaks\nEOF"
        self.assertEqual(guard.scripts_run(cmd, IN_SCOPE), [])

    def test_hook_stops_it_and_logs_the_script(self):
        cfg = dict(CONFIG, mode="enforce")
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "log.jsonl")
            payload = {"tool_name": "Bash", "tool_input": {"command": self.real}, "cwd": IN_SCOPE, "transcript_path": "/nonexistent"}
            out = io.StringIO()
            with mock.patch.object(guard, "LOG_PATH", log), mock.patch.object(guard, "load_config", return_value=cfg), \
                 mock.patch.object(guard, "load_key", return_value=KEY), mock.patch.object(guard, "call_jev", fake_jev(approved=0.0)), \
                 mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
                guard.hook()
            with open(log) as f:
                entry = json.loads(f.read().splitlines()[-1])
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertTrue(entry["scripts"][0].endswith("delete-reviews.mjs"))


class Config(unittest.TestCase):
    def test_falls_back_to_example(self):
        with mock.patch.object(guard.os.path, "exists", return_value=False):
            self.assertTrue(guard.config_path().endswith("questions.example.json"))

    def test_own_copy_is_valid(self):
        own = os.path.join(HERE, "questions.json")
        if not os.path.exists(own):
            self.skipTest("no personal questions.json")
        cfg = guard.load_config(own)
        for key in ("mode", "scope", "timeout_seconds", "on_error", "performs_skip_below", "recent_messages"):
            self.assertIn(key, cfg)
        self.assertIn(cfg["mode"], ("watch", "enforce"))
        for a in cfg["actions"]:
            for key in ("id", "label", "pattern", "threshold", "performs", "approved"):
                self.assertIn(key, a, a.get("id"))
            self.assertIn("user_recent_messages", a["approved"]["instructions"], a["id"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
