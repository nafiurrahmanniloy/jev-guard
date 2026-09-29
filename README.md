# jev-guard

A rule check for [Claude Code](https://claude.com/claude-code). Before Claude
runs a risky shell command, [Jev](https://typesafe.ai) (TypeSafe's fast yes/no
model) checks it against your rules and your recent messages.

Example: Claude is about to run `gh pr create`. Your last message was "batch
these into one PR later". jev-guard asks Jev "did the user clearly tell Claude
to open this PR now?" Jev answers `0.08`, and Claude Code stops and asks you
before anything happens.

Your saved instructions are only reminders. A hook runs every single time.

## How it works

1. **A plain pattern check runs on your machine**, for free, on every Bash
   command. Most commands (`ls`, `npm test`, `git status`) stop here. Nothing
   is read and nothing is sent.
2. **Risky commands** (commit, push, force-push, open or merge a PR, database
   deletes/updates, changes over ssh, paid API calls) go to Jev in **one
   request** with two yes/no questions each:
   - Does this command really do it, or does it just mention it (like a `grep`
     for "git commit")?
   - Did the user clearly say yes to it in their last 3 messages?
3. **Each action has its own bar**, set by what a mistake costs: merge 85%,
   push 80%, commit 70%. Below the bar, Claude Code asks you. At or above it,
   the command runs as normal.
4. **Two things are always stopped** without asking Jev: `git add -A` / `git add .`
   and `git commit -a`, because they can sweep someone else's unfinished work
   into your commit.

If Jev is down, slow (3 s timeout) or the key is missing, it asks you. It
never silently lets a risky command through. And it never auto-approves
anything: Claude Code's normal permission rules still apply on top.

## Does the change match its description?

Before a `git commit`, `gh pr create` or `gh pr merge`, Jev also reads the
message and the diff: the staged changes plus any files `git add`-ed in the
same command, or the PR's diff from GitHub. It asks "does this diff change
code that has nothing to do with what the message says?" At 0.5 or above,
Claude Code asks you, even if you said "commit" or "merge". Approval and
content are separate checks.

This catches a commit titled "hide the Billing page" that also deletes a
backend job, or a `git add` that sweeps someone else's work into your commit.
Tested on one real repo's history: the real incident scored 0.95, planted
unrelated changes were caught 30/30, and 0/30 clean commits were flagged.

## Saved-rules picker (optional)

`pick_rules.py` runs on each message you send. Claude Code only loads the
one-line index of a project's memories. This asks Jev which memories apply
to your message and adds the **full text** of up to 3 (scoring ≥ 0.85) to
Claude's context for that turn. It costs about $0.001 per message and adds
about 0.5 s. In `"mode": "watch"` it only logs its picks to `rules_log.jsonl`;
set `"mode": "on"` in the `rules_picker` block to use them. If Jev fails,
nothing is added. It never blocks your message.

## What gets sent to Jev

Only for risky commands: the command, the folder, the git branch, your last 3
messages, and Claude's last message. For commits and PRs, also the message and
the diff (capped at 60,000 characters). The rules picker sends each message
you type plus Claude's previous message. **Secrets are stripped first**: database
URLs, bearer tokens, API keys, `*_KEY=`/`*_TOKEN=` values, GitHub/Slack tokens,
JWTs, and any long random-looking string. Names and other plain text in your
messages are still sent. Decide if that's OK for your work.

## Install

You need Python 3.8+ (already on macOS and most Linux) and a Jev key. No
`pip install`.

1. Get a key: sign up at [console.typesafe.ai](https://console.typesafe.ai), or
   use a [Vercel AI Gateway](https://vercel.com/docs/ai-gateway) key (same model,
   same price).
2. Clone into your Claude hooks folder:
   ```bash
   git clone https://github.com/nafiurrahmanniloy/jev-guard ~/.claude/hooks/jev-guard
   cd ~/.claude/hooks/jev-guard
   ```
3. Save your key (replace `your-key`). For a Vercel key, use
   `AI_GATEWAY_API_KEY` instead of `TYPESAFE_API_KEY`:
   ```bash
   printf 'TYPESAFE_API_KEY=%s\n' 'your-key' > .env && chmod 600 .env
   ```
4. Make your own copy of the rules (optional but recommended):
   ```bash
   cp questions.example.json questions.json
   ```
5. Check it works:
   ```bash
   python3 test_guard.py
   ```
6. Open `~/.claude/settings.json` and add this inside `"hooks"` (keep any hooks
   you already have):
   ```json
   "PreToolUse": [
     {
       "matcher": "Bash",
       "hooks": [
         {
           "type": "command",
           "command": "python3 \"$HOME/.claude/hooks/jev-guard/guard.py\"",
           "timeout": 15
         }
       ]
     }
   ],
   "UserPromptSubmit": [
     {
       "matcher": "",
       "hooks": [
         {
           "type": "command",
           "command": "python3 \"$HOME/.claude/hooks/jev-guard/pick_rules.py\"",
           "timeout": 5
         }
       ]
     }
   ]
   ```
   Leave out the `UserPromptSubmit` part if you don't want the rules picker.
7. Restart Claude Code.

It starts in **watch mode**: it only writes down what it *would* have done and
never interrupts you.

8. After a day or two, look at what it caught:
   ```bash
   python3 ~/.claude/hooks/jev-guard/guard.py --stats
   ```
9. Happy with it? In `questions.json`, change `"mode": "watch"` to
   `"mode": "enforce"`.

## Tune it on your own history (optional)

```bash
python3 guard.py --replay 200
```

This runs your last in-scope risky commands from past Claude Code sessions
through Jev, each with the messages that came before it, and writes the results
to `replay.jsonl`. Read a sample and check: did it ask where you'd want it to?
Adjust the thresholds in `questions.json` to match. It costs well under a
cent per 100 commands ($0.042 per million input tokens).

## Changing the rules

Everything lives in `questions.json`: which folders are checked (`scope`), the
patterns, the exact questions Jev is asked, and the thresholds. To add a rule,
copy an existing action and change its `pattern`, `label`, questions and
`threshold`. Write both the yes and the no answer out in full, with edge
cases. Vague wording is what makes Jev guess.

## What Jev is bad at

Jev picks from answers you give it. It doesn't write or reason. TypeSafe's
own docs say not to trust it on **math, counting, dates, or exact literal
reading**. So jev-guard never asks it things like "is CI green?". Code or you
handle those.

## Files

| File | What it is |
|---|---|
| `guard.py` | The hook. Standard library only. |
| `questions.example.json` | Starter rules. Copy to `questions.json`. |
| `pick_rules.py` | The optional saved-rules picker hook. |
| `test_guard.py` | Tests with a fake Jev (no key, no network). |
| `questions.json`, `.env`, `log.jsonl`, `rules_log.jsonl`, `replay.jsonl` | Yours only. Git-ignored. |

## Uninstall

Remove the `PreToolUse` and `UserPromptSubmit` blocks from `~/.claude/settings.json`, then delete
`~/.claude/hooks/jev-guard`.

Not affiliated with TypeSafe or Anthropic. MIT license.
