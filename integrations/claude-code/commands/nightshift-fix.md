---
description: Pick up the newest Nightshift failure brief, reproduce it, and fix it
argument-hint: "[OWNER/REPO]"
allowed-tools: Bash(nightshift:*), Bash(git:*), Read, Edit, Write, Grep, Glob
---

You are picking up a CI or deploy failure that Hermes Nightshift has already triaged. The brief is finished homework: what broke, a repro command, the suspected commits and files, and lessons for this repo. Start from it rather than from scratch.

1. **Find the brief.** Run `nightshift next --json` (add `--repo $ARGUMENTS` if an argument was given). It prints a JSON array of unclaimed briefs, newest first. If the array is empty, say there is nothing to pick up and stop. Otherwise take the first entry. If that brief's `repo` doesn't match this working directory's `git remote get-url origin`, look further down the list for one that matches; if none do, tell the user which repo the top brief is for and stop.

2. **Claim it.** Run `nightshift claim <id> --agent claude-code --json`. Exit code 1 means another agent claimed it first: run `next` again and take the next one. The claim also comments on the brief's Hermes kanban card and unblocks it when kanban is enabled. Report any `notes` from the output, but a kanban note is not a reason to stop.

3. **Read the brief.** Read the Markdown file at `brief_md` in full. Pay attention to the classification and its rationale, the log excerpt, the suspected culprit commits and files, and "Known lessons for this repo". The Handoff section tells you how cautious to be: for `flaky`, confirm it reproduces before changing code; for `infra`, confirm it is not infrastructure before touching the repo.

4. **Reproduce.** The repro command has the form `git checkout <sha> && <command>`. Check that `git status --porcelain` is clean first; if it isn't, stop and ask the user. Run the whole repro command and confirm it fails the way the brief says. If it doesn't fail, say so, report what you saw, and stop before changing code. Then return to the default branch (`git switch -`).

5. **Fix.** Create a branch off the up-to-date default branch: `git switch -c nightshift/<id>`. Do not work on the detached HEAD from step 4. Make the smallest change that fixes the root cause, and add or adjust a test. Leave unrelated files alone.

6. **Verify.** Run the repro command's test or build part again (without the `git checkout <sha> &&` prefix) on your branch, and confirm it now passes. Commit with a message that names the brief id.

7. **Report.** Summarize the root cause, the fix, how you verified it, and the branch name. If you learned something that should help the next triage of this repo, suggest a `nightshift learn OWNER/REPO --pattern 'REGEX' --note 'FIX'` command for the user to run.
