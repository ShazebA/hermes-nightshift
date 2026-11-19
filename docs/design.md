# Design notes

## Extension route in Hermes

Hermes's root `AGENTS.md` sets the order of preference for new capability. It runs from extending existing code, to CLI command + skill, then a service-gated tool, a plugin, an MCP server, and a new core tool as the last resort. Nightshift uses the **CLI command + skill** rung and wires it in with **cron script jobs**. These are the Hermes facts that drove the choice, all confirmed in the Hermes source, not the docs alone:

| Need | Hermes primitive used | Where confirmed |
|---|---|---|
| Run every 15 min without waking the model on quiet nights | `hermes cron create ... --script <file>`; the last stdout line `{"wakeAgent": false}` skips the agent | `hermes_cli/subcommands/cron.py`, `cron/scheduler_prompt.py::_parse_wake_gate` |
| Morning digest with no model at all | `--no-agent`: script stdout is delivered verbatim to `--deliver` | same |
| Scripts must live under `$HERMES_HOME/scripts/`; `.sh` runs with bash, anything else with **Hermes's** Python | `cron/scheduler_script.py::_resolve_script_path`, `_script_argv` |
| Card per incident, no duplicates | `hermes kanban create --initial-status blocked --idempotency-key --json`, `kanban attach` | `hermes_cli/kanban_parser.py`, `kanban.py::_cmd_create` |
| Cards must not be auto-worked | default `--initial-status blocked`; `triage` cards are auto-decomposed into always-assigned children and dispatched when a gateway runs (`kanban.auto_decompose` defaults on) | `gateway/kanban_watchers_dispatcher.py::auto_decompose_tick`, `hermes_cli/kanban_decompose.py` |
| Teach the small model a narrow procedure | Skill in `$HERMES_HOME/skills/devops/nightshift-triage/`; passes `tools/skill_linter.py` and `skill_manager_tool._validate_frontmatter` | `skills/AGENTS.md` |

Consequences:

- The cron scripts are bash shims with absolute paths to `nightshift` and the config, rendered by `scripts/install-hermes.sh`. A `.py` cron script would run under Hermes's interpreter and could not import Nightshift. The gateway's environment may also lack your shell `PATH`.
- Nightshift talks to kanban only through the CLI. It never imports `hermes_cli`, because Hermes's internal module paths are not an API.
- **Memory.** Hermes's `cron/AGENTS.md` says cron sessions skip memory, but `cron/scheduler.py` currently passes `skip_memory=False` with `skip_background_review=True`. Nothing learns automatically from a cron run either way. So lessons live in Nightshift's own per-repo file, which is deterministic, testable and diffable. The skill also asks the model to mirror each lesson into Hermes `memory` so interactive sessions benefit.

## Deterministic first, model last

The model is the least reliable and most expensive component, even when it is local. Everything that can be computed is computed in Python: dedupe, the red streak, last green and first red, the compare range, suspected-file ranking, the repro command and lesson matching. The model gets a short brief and a decision table, and returns one enum, one number and one sentence through `nightshift classify`. That command validates the input, so a confused model cannot corrupt a brief. When the model is unavailable, the heuristic classification still ships.

## Sandbox

`nightshift repro <id> --execute` clones the repo into a temp directory on the host, checks out the first red SHA there, and runs only the rest of the command inside `docker run --rm`. The repo's code never runs on the host. It is off by default (`sandbox.enabled: false`). Hermes's Docker terminal backend is an alternative when the user has set `terminal.backend: docker`, but that is a global agent setting, so Nightshift does not depend on it.

## AetherBus

- Topic `dev.aetherbus.nightshift.brief.v1`. It has to match AetherBus's default stream subjects (`aetherbus.>`, `dev.aetherbus.>`), or JetStream never persists it.
- The `dataref` is `file://…/<id>.json`, not the `.md`. AetherBus's MCP adapter calls `event.resolve()`, which runs `json.loads` on `application/json` datarefs.
- `Bus.publish` returns an id even when the broker is down, because the event is queued to a SQLite outbox. A short-lived cron process would then report success for an event that only exists locally. Nightshift probes the broker first, records `pending`, and retries on the next poll. The outbox path is pinned to the state directory, never the cwd.
- `tests/test_aetherbus_contract.py` runs against the real `aetherbus` package with an in-memory transport. It checks that the envelope has a dataref and no inline data, and that AetherBus's own `Event.resolve()` returns the brief.

## Dedupe model

- **CI.** One incident per (workflow, branch) red streak, keyed on the first red run, so 10 failing pushes produce one brief. When the workflow goes green and later breaks again, that is a new incident.
- **Deployments.** One incident per failing deployment that is the latest decided one in its environment.
- **Issues, PRs, stale branches.** Notices only, reported once in the digest. A stale branch is keyed on its head SHA, so it is reported again if new commits land and it goes stale again.
