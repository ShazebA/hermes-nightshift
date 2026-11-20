# Brief schema: `nightshift.brief/v1`

A brief is the unit of handoff: one per failure incident (CI or deployment). The JSON file is canonical and is validated against [`brief.v1.schema.json`](../src/nightshift/schema/brief.v1.schema.json) (JSON Schema 2020-12). The Markdown file next to it is a deterministic rendering of the same data: `nightshift brief show <id>` prints it, and coding agents can read either.

Files live at `<briefs_dir>/<owner>__<repo>/<id>.json` and `.md`. Both are written write-then-rename, so a reader resolving an AetherBus `dataref` never sees a half-written file.

## Versioning

- `schema` is the literal `nightshift.brief/v1`.
- **Additive** changes, such as new optional fields or new link labels, keep `v1`. Consumers must ignore fields they don't know.
- **Breaking** changes, such as renaming or removing a field, changing a type, or adding an enum value a strict consumer would reject, bump to `v2`. They ship a new schema file, and `v1` stays readable for one minor release.
- `resolution` was added this way: optional, so v1 consumers that don't know it keep working. Resolving rewrites the brief file in place (as `claim` does), so an AetherBus `dataref` resolves to the current state.
- AetherBus events carry the version in the `nightshift-schema` header, so subscribers can filter.

## Fields

| Field | Type | Notes |
|---|---|---|
| `schema` | const | `nightshift.brief/v1` |
| `id` | string | Incident fingerprint, for example `ci-ci-main-9002` (`ci-<workflow>-<branch>-<first red run id>`) or `deploy-<env>-<deployment id>`. Stable, and doubles as the kanban idempotency key |
| `repo` | string | `owner/repo` |
| `kind` | enum | `ci_failure`, `deploy_failure` |
| `title` | string | One line |
| `created_at`, `updated_at` | date-time | UTC |
| `classification.category` | enum | `flaky`, `real`, `infra`, `unknown` |
| `classification.confidence` | 0–1 | `label` is derived: high ≥ 0.75, medium ≥ 0.5, else low |
| `classification.source` | enum | `heuristic` (Nightshift), `model` (the triage skill), `human` |
| `classification.rationale` | string ≤ 400 | One sentence |
| `classification.heuristic` | object | The original heuristic verdict, kept when a model or human overrides it |
| `what_broke.summary` | string | One line that combines the workflow, branch, failing test and error |
| `what_broke.{workflow,environment,job,step,branch,run_id,run_url,failed_at,red_streak}` | | Where it broke |
| `what_broke.signature` | string | Normalized first error line (numbers and SHAs masked); stable across reruns |
| `what_broke.error` | string? | First concrete `Error:`-style line, verbatim |
| `what_broke.test_target` | string? | Failing test file (vitest/jest), `file::test` (pytest) or `file:line` (playwright) |
| `what_broke.log_excerpt` | string[] ≤ 80 | Error-dense window of the failing step, with the step script and env stripped |
| `repro.command` | string | `git checkout <first red sha> && <setup> && <narrowest command>`. The narrowest command is the failing test when one is found, else the step's script (multi-line scripts run as `bash -e <<'NIGHTSHIFT_STEP'`, with the step's env var names, never values, in a comment). When the step is an action (`uses:`) or a script over 40 lines, it is a read-only `gh run view <id> --log-failed` with the re-run command in a comment |
| `repro.verified` | bool | True only after a sandbox run reproduced the failure |
| `repro.sandbox` | object? | `image, exit_code, reproduced, ran_at, output_tail` |
| `culprit.last_green_sha` / `first_red_sha` | string | From the same workflow and branch |
| `culprit.commits[]` | `{sha,title,author,url}` | `compare last_green...first_red` |
| `culprit.suspected_files[]` | `{path,reason,score}` | Changed files ranked by how strongly the log names them (3 named, 2 source/test pair, 1 same directory, 0 just changed) |
| `culprit.compare_url` | string? | GitHub compare page |
| `lessons[]` | `{id,pattern,note,source}` | Repo lessons whose regex matched this failure |
| `links[]` | `{label,url}` | Failing run, last green run, compare |
| `handoff.prompt` | string | Ready-to-paste instruction for a coding agent |
| `handoff.brief_path` | string | Absolute path of the Markdown brief |
| `handoff.kanban_task_id` | string? | Hermes kanban card |
| `handoff.aetherbus_event_id` | string? | CloudEvent id (UUIDv7) |
| `handoff.claimed_by` | string? | Coding agent that picked the brief up (`nightshift claim`); unclaimed briefs are what `nightshift next` lists |
| `handoff.claimed_at` | date-time? | When it was claimed |
| `errors[]` | string[] | Triage gaps, for example a log that could not be fetched |
| `resolution` | object? | Absent while the failure is open. Set once by a later poll that sees it green again |
| `resolution.resolved_at` | date-time | When Nightshift noticed |
| `resolution.how` | `fix` \| `rerun` | `fix`: a different commit went green (likely the fix). `rerun`: a commit that was red went green with no code change |
| `resolution.sha` | string | Commit of the green run or successful deployment |
| `resolution.url` | string? | That run or deployment |
| `resolution.run_id` | integer? | Run id, or deployment id for `deploy_failure` |
| `resolution.green_at` | date-time? | When the green run or deployment started |

## Classification heuristic

The first matching rule wins:

1. The same SHA both failed and passed this workflow → `flaky` (0.8)
2. Log matches a runner, disk or GitHub platform pattern → `infra` (0.75)
3. A network or rate-limit pattern → `infra` (0.7)
4. A GitHub-managed workflow (event `dynamic`: Dependabot, CodeQL default setup) → `unknown` (0.4), and no culprit commit range is computed
5. The test runner reports flaky tests and no failures → `flaky` (0.7); a timeout or flake pattern → `flaky` (0.55)
6. No log → `unknown` (0.2)
7. Otherwise `real`: 0.7 with a culprit commit range, 0.5 without

Once a failing test is identified, the network patterns (rule 3) and the timeout patterns (rule 5) only look at the failure itself (signature and first error), not the whole log: test suites print `ECONNRESET` or "timeout exceeded" while exercising error paths, and a real Playwright run did exactly that.

The model refines this through `nightshift classify`. The prompt gives it a decision table, not open-ended reasoning, so a small local model can do it reliably.
