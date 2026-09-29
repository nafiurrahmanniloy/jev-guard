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


def fake_jev(performs=0.95, approved=0.99):
    calls = []

    def jev(body, endpoint, key, timeout):
        calls.append(body)
        answers = {}
        for qid in body["questions"]:
            answers[qid] = {"type": "noul", "noul": performs if qid.endswith("__performs") else approved}
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
        ]
        msgs = guard.messages_from_rows(rows)
        self.assertEqual([m for w, m in msgs if w == "user"],
                         ["hey, fix the bug", "yes commit it", "no, open a PR instead", "merge it"])
        user, claude = guard.conversation_context(msgs, 3)
        self.assertEqual(user, ["yes commit it", "no, open a PR instead", "merge it"])
        self.assertEqual(claude, "Fixed. Want me to commit?")


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
        r = guard.evaluate("gh pr merge 1 --squash", IN_SCOPE, self.msgs, CONFIG, KEY, fake_jev(approved=0.9))
        self.assertEqual(r["decision"], "ask")  # merge needs 0.95
        self.assertIn("merge a pull request", r["reason"])
        self.assertIn("90%", r["reason"])

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

    def test_never_allow(self):
        out, _ = self.run_hook("git commit -m x", mode="enforce", jev=fake_jev(approved=1.0))
        self.assertEqual(out, "")  # approved: stay silent, never emit "allow"


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
