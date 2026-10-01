# jev-guard

**A seatbelt for Claude Code.** Before Claude runs a risky command, like a merge, a
force-push, a database delete or a paid API call, a fast yes/no AI model called
[Jev](https://typesafe.ai) checks two things:

1. **Did you actually say yes to this?** It reads your last few messages.
2. **Does the change match its description?** It reads the commit or PR message
   and the diff.

If either answer is shaky, Claude Code stops and asks you. Everything else runs
as normal.

> Example: Claude is about to run `gh pr merge 333`. The PR says "hide the Billing
> page" but its diff also deletes an unrelated backend job. Jev is 96% sure the
> change doesn't match its description, so Claude Code asks you first.
> This is a real incident, replayed. The original was merged, and the deleted
> job had to be restored later.

---

## What it does

jev-guard is three small [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks)
in plain Python (standard library only, no `pip install`).

### 1. Approval check: "did you say yes?"

Runs before every Bash command.

- **A free pattern check on your machine comes first.** Most commands (`ls`,
  `npm test`, `git status`) stop here. Nothing is read and nothing is sent.
- **Risky commands** (commit, push, force-push, open or merge a PR, database
  deletes/updates, changes over `ssh`, paid API calls) go to Jev in **one
  request**, with two yes/no questions per action:
  - Does the command really do this, or does it just mention it (a `grep` for
    "git commit")?
  - Has the user asked for it, or agreed to a plan that includes it? Jev reads
    your last 3 messages, Claude's last message, and any plan you approved.
- **Each action has its own bar, set by what a mistake costs:** merge,
  force-push, database and paid calls need 85%; push, PR and server 80%;
  commit 70%. Below the bar, Claude Code asks you.
- **Scripts are read too.** `node delete-rows.mjs` hides its `DELETE` inside
  the file. When a command runs a local script (`node`, `python3`, `tsx`,
  `bash`, `sh`), the guard reads the file. If its code matches an action's
  `script_pattern` (SQL deletes/updates, Supabase `.delete()`/`.update()`,
  paid API hosts), Jev gets the code and judges what it does. Scripts that
  only read cost nothing. It follows a leading `cd` and `S=… && node $S/x.mjs`
  in the same command. Test runs (`--test`, `*.test.*`), `node --check` and a
  `run-migrations.mjs` are skipped.
- **Unsure means no stop.** If Jev is less than 70% sure the command really
  does the risky thing, it only goes in the log (`performs_ask_from`). Without
  this, half-sure guesses about `curl` and test runs were the biggest source of
  stops.
- **Pop-ups instead of stops (macOS).** Set `notify` on an action and you get a
  Mac notification whenever Jev is sure it really happens, for example
  "my-app: pushing fix/login" or "my-app: opening PR 'Fix login'". The session
  keeps going. Push and open-PR have it on in the starter file. Pair it with a
  low `threshold` (say 0.15) to stop only when Jev is sure you never said yes.

### 2. Change check: "does the change match its description?"

Runs before `git commit`, `gh pr create` and `gh pr merge`. Jev reads the message
and the diff. That's the staged changes plus any files `git add`-ed in the same
command, or the PR's diff from GitHub. At 50% or higher that the diff contains
unrelated changes, Claude Code asks you. **This runs even when you did say
"commit" or "merge".** Approval and content are separate questions.

It catches:
- A commit titled "remove the Billing page" that also deletes backend logic.
- A `git add` that sweeps someone else's unfinished work into your commit.
- A "fix README typo" commit with a real code change hidden inside.

### 3. Saved-rules picker (optional)

Runs on every message you send. Claude Code loads only a one-line index of a
project's saved memories (`memory/MEMORY.md`), so the detail behind each rule
is easy to miss. The picker asks Jev which memories apply to *this* message and
adds the **full text** of up to 3 (scoring ≥ 85%) to Claude's context for that
turn.

> You ask "whose API key did you use for that?" and Claude gets your saved rule
> "never call a paid provider with a client's key without asking" in full.

### Always stopped, no AI needed

`git add -A` / `git add .` and `git commit -a` sweep every changed file into a
commit, including work that isn't part of this change. They're stopped by
pattern alone. In a shared checkout, that's how other people's work ends up in
your PR.

---

## Why you'd use it

- **Your written instructions are only suggestions.** `CLAUDE.md` rules and saved
  memories are followed most of the time, not every time. A hook runs every time.
- **Permission prompts are all or nothing.** Either you approve every
  `git push` by hand, or none. jev-guard only asks when you *didn't* clearly say
  yes, so "merge it" goes through and an unprompted merge doesn't.
- **Long sessions and auto mode drift.** After hours of work, "fix the bug" can
  quietly turn into commit, push and merge. On one real history, about half of
  the commits it flagged came right after messages like "fix X", with no
  "commit" in them.
- **The diff is where the damage hides.** Commit titles describe the intent;
  diffs contain the accidents. Reading every diff against its message is
  tedious for a person and costs Jev a third of a second.
- **It's cheap and fast.** Replaying 2,585 real risky commands cost **$0.10**.
  The median answer took **0.33 s**, and there were zero errors. Commands that
  aren't risky cost nothing and add no delay.
- **It fails safe.** If Jev is down, slow (3 s timeout), or the key is missing,
  it asks you. It never silently lets a risky command through, and it never
  auto-approves anything: Claude Code's own permission rules still apply on top.

## Why you might not want it

Be honest with yourself about these before installing:

- **It sends data to a third party.** For risky commands it sends the command,
  folder, branch, your last 3 messages and Claude's last message. For commits
  and PRs it also sends the diff (capped at 60,000 characters). The rules picker
  sends every message you type. Secrets are stripped first (database URLs,
  bearer tokens, API keys, `*_KEY=`/`*_TOKEN=` values, GitHub/Slack tokens, JWTs,
  long random strings). **Names, business details and code are not stripped.**
  If your work can't leave your machine, don't use it.
- **Jev is a closed, hosted model.** No self-hosting, no published paper. You
  need a key from TypeSafe or a Vercel AI Gateway key. As of September 2026,
  TypeSafe had paused new signups, and some people saw 429 errors through
  Vercel.
- **The approval check misses casual yeses.** On real history it let through 54
  of 80 clear approvals ("merge it", "ship it") and missed the rest ("go go",
  "then raise a PR"). So if you enforce it on commits, expect to be asked more
  often than you'd like. That's why you can switch on per action (see below)
  and start with the costly ones.
- **It adds a little time.** About 0.3–0.5 s on risky commands, 1–2 s on merges
  (it reads the PR from GitHub), and about 0.5 s per message for the rules
  picker.
- **It's a seatbelt, not a lock.** The first filter is a pattern match. A command
  written in an unusual way can get past the pattern. It stops honest mistakes,
  not a determined bypass. Reading scripts does **not** catch:
  - a delete inside a module the script imports (only the file itself is read)
  - SQL built from strings while the script runs
  - a path in a variable set by an earlier command (`$J/x.mjs` with no `J=`)
  - `npm run …` scripts
  - git or ssh calls made from inside a script (only database and paid-API
    patterns are checked in scripts)
  - commands you type yourself with `!` in Claude Code: hooks never see them.
- **Jev is weak at math, counting, dates and literal reading** (TypeSafe's own
  docs). So jev-guard never asks it things like "is CI green?" or "is this
  number right?".
- **It was tuned on one person's history.** The numbers below come from one
  developer's real sessions and repo. Run the replay on your own history before
  trusting the thresholds.

---

## How well it works (real tests, not demos)

Everything below was measured on real Claude Code history: 111 session files,
one production repo's git log, and 152 saved memories. The total cost was about
$0.30.

| Test | Result |
|---|---|
| Change check on a **real incident** ("take Billing out" commit that deleted unrelated backend code) | Jev 95% sure it didn't match (96% via the PR) |
| Change check: an unrelated deletion planted into 30 normal commits | **30 / 30 caught** |
| Change check: small unrelated changes from other parts of the code planted into 30 commits (file list removed so it had to read the diff) | **30 / 30 caught** at 50% (26 / 30 at 70%) |
| Change check: the same 30 commits, untouched | **0 / 30 false alarms** |
| Approval check: 80 cases where the user clearly asked | 54 let through at the 85% bar (the old "this exact PR" wording: 8) |
| Approval check: 80 cases where the user never asked | 2 let through, and reading them showed both *were* real approvals the labels missed |
| Rules picker: first pick relevant, hand-judged on 25 real messages | ~17 / 25 (keyword matching: ~5 / 25) |
| Full replay: 2,585 risky commands | $0.10, median 0.33 s, 0 errors |

### What we tried and dropped

The same test harness said no to these, so they're not in the repo:

- **Skill picker** (which Claude Code skill fits this message?): matched the
  skill actually used only 7 / 27 times.
- **Plain-English reply check** (flag Claude's replies that are too technical):
  a plain word count predicted the user's complaints better (0.70 vs Jev's 0.64).
- **File finder** (which file should Claude edit?): right file first 23 / 60 vs
  keywords 13 / 60. Better, but not enough to skip opening the files anyway.

---

## Install

You need Python 3.8+ (already on macOS and most Linux) and a Jev key.

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
4. Make your own copy of the rules (it's git-ignored, so your edits stay yours):
   ```bash
   cp questions.example.json questions.json
   ```
5. Check it works (uses a fake Jev: no key, no network):
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

8. After a day or two, see what it caught:
   ```bash
   python3 ~/.claude/hooks/jev-guard/guard.py --stats
   ```
9. Switch it on. In `questions.json`, set `"mode": "enforce"`. To start with only
   the costly actions, set `"enforce": false` on the `commit`, `push` and
   `open_pr` actions. They'll keep being logged but won't interrupt. For the
   rules picker, set `"mode": "on"` in the `rules_picker` block.

## Tune it on your own history

```bash
python3 guard.py --replay 200        # your last 200 risky commands, each with the messages before it
python3 pick_rules.py --try "your message here"   # what the rules picker would add
```

`--replay` writes `replay.jsonl`. Read a sample and ask yourself whether it
asked where you'd want it to. Then adjust the thresholds. It costs well under a
cent per 100 commands.

## Changing the rules

Everything lives in `questions.json`:

- `scope`: which folders are checked (default: your whole home folder).
- `actions`: for each one, a `pattern` (does Jev get asked at all?), the two
  questions Jev is asked, a `threshold`, an `enforce` switch, and an optional
  `notify` switch (Mac pop-up).
- `performs_skip_below` / `performs_ask_from`: below the first, the command
  only mentions the action and is ignored; between the two, Jev is unsure and
  it is logged without stopping you.
- `hard_stops`: stopped by pattern alone.
- `change_check`: the question, the 50% bar, the diff size cap.
- `rules_picker`: on/off, top 3, 85% bar.

To add a rule, copy an existing action and change its `pattern`, `label`,
questions and `threshold`. **Write out both the yes and the no answer in full,
with edge cases.** Vague wording is what makes Jev guess. The biggest single
improvement in testing came from rewording one question: "did the user approve
this exact PR?" became "has the user asked for, or agreed to, this?"

## Files

| File | What it is |
|---|---|
| `guard.py` | The approval check and change check (`PreToolUse` hook). Also `--stats` and `--replay`. |
| `pick_rules.py` | The saved-rules picker (`UserPromptSubmit` hook). Also `--try`. |
| `questions.example.json` | Starter rules. Copy to `questions.json`. |
| `test_guard.py` | 47 tests with a fake Jev (no key, no network). |
| `questions.json`, `.env`, `log.jsonl`, `rules_log.jsonl`, `replay.jsonl` | Yours only. Git-ignored. |

## Uninstall

Remove the `PreToolUse` and `UserPromptSubmit` blocks from
`~/.claude/settings.json`, then delete `~/.claude/hooks/jev-guard`.

---

Not affiliated with TypeSafe or Anthropic. MIT license.
