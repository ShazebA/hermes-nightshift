---
name: nightshift-triage
description: Classify overnight CI failures and record repo lessons.
version: 0.1.0
author: Shazeb (ShazebA), Hermes Agent
license: Apache-2.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [ci, github-actions, triage, devops, handoff]
    category: devops
    related_skills: [systematic-debugging, github]
---

# Nightshift Triage Skill

Runs inside the `nightshift-watch` cron job. The job's script has already polled GitHub,
written one brief per new failure, opened a kanban card and (optionally) published an
AetherBus event. Your only jobs are to check each brief's classification and to record what
you learn. You never edit the target repo, push, or open PRs: stronger coding agents do the
fix from the brief.

## When to Use

- The cron prompt starts with `NIGHTSHIFT: N new failure brief(s)`.
- A human asks you to re-check or reclassify a Nightshift brief by id.

## Prerequisites

- Nightshift installed and wired in with `scripts/install-hermes.sh` from the Nightshift repo.
- You do not need `nightshift` on `PATH`: the prompt prints the full command for every brief.

## How to Run

Each brief in the prompt looks like this:

```
- id=ci-ci-main-9002 repo=owner/repo heuristic=real/0.70 brief=/path/to/ci-ci-main-9002.md card=t_0001 bus=published
    classify: /path/to/python -m nightshift -c /path/to/nightshift.yaml classify ci-ci-main-9002 --category CATEGORY --confidence NUMBER --rationale "SENTENCE"
    learn: /path/to/python -m nightshift -c /path/to/nightshift.yaml learn owner/repo --pattern 'REGEX' --note 'FIX' --incident ci-ci-main-9002
```

Handle the briefs one at a time, in order. Run the `classify:` and `learn:` lines with
`terminal` exactly as printed. Change only the CAPITALISED placeholders. Keep each answer short.

## Quick Reference

| Need | How |
|---|---|
| Read a brief | `read_file` on the `brief=` path |
| Record your classification | the printed `classify:` line; CATEGORY is one of `flaky`, `real`, `infra`, `unknown`; NUMBER is 0 to 1 |
| Save a repo lesson | the printed `learn:` line; REGEX matches the error, FIX is one line |

## Procedure

1. `read_file` the brief. Read only these sections: the table, **What broke**, **Log excerpt**,
   **Suspected culprit**, **Known lessons**.
2. Decide ONE category using this table. Stop at the first row that fits.

   | If | Category |
   |---|---|
   | A known lesson in the brief explains the error | the category that lesson implies (usually `real`) |
   | Log shows runner/network/disk/rate-limit/registry problems | `infra` |
   | Same commit passed on another run, or the error is a timeout in a UI/E2E test | `flaky` |
   | An assertion, type error, missing column/module, or build error that a listed commit plausibly caused | `real` |
   | None of the above | `unknown` |

3. Confidence: `0.8` when the log states the cause outright, `0.6` when it is a reasonable
   inference, `0.4` when you are guessing. Never above `0.9`.
4. If your category differs from the `heuristic=` value, OR your confidence differs by more
   than 0.15, run the `classify:` line. Otherwise do nothing for this step.
5. Learn: if the root cause is something that will recur in this repo (a setup step, a missing
   migration, an env var, a known flaky test), run the `learn:` line with a REGEX that matches
   the stable part of the error line and a one-line note with the fix. Put `\S+` where the
   error names a specific table, column, file or module, so the lesson covers the whole class:
   `column \S+ does not exist`, not `column entries\.archived_at does not exist`. No SHAs,
   numbers or timestamps. If the command answers `lesson not saved`, fix the REGEX it names
   and run it once more; then move on. Also save the same fact with `memory` so interactive sessions know it. Skip this step
   when nothing generalises.
6. Final reply: one line per brief, `<id>: <category> <confidence> — <six-word reason>`.
   That reply is what gets delivered to the user's chat.

## Pitfalls

- Do not run the repro command yourself unless the user configured `terminal.backend: docker`;
  repo code must not run on the host. Use `nightshift repro <id> --execute` only when asked.
- Do not rewrite the brief file with `patch`. Only the `classify:` command changes a brief, so
  the JSON stays valid against `nightshift.brief/v1`.
- The rationale is capped at 400 characters; one sentence is enough.
- A lesson regex that is too broad (for example `error`) will attach to every future brief.
  Prefer a distinctive phrase such as `column \S+ does not exist`.

## Verification

- `read_file` the brief again: the table shows your category with source `model`.
- The `learn:` command prints `lesson L<n> saved`; the next brief that matches lists it.
