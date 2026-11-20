# Contributing

Thanks for helping. Nightshift is small on purpose: deterministic Python does the work, and a small model makes one narrow judgement.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

To run the AetherBus contract test, install the extra (`pip install -e ".[dev,aetherbus]"`). Without it the test is skipped.

## Ground rules

- **No live network in tests.** GitHub reads go through `nightshift.gh.Gh`; tests use `FixtureGh`, which replays recorded `gh` output from `tests/fixtures/<scenario>/`.
- **Test behaviour, not snapshots.** Assert relationships, for example "the file named in the log ranks first" or "the validator and the JSON Schema reject the same briefs". Do not freeze rendered Markdown.
- **Keep the model's job narrow.** If something can be computed, compute it in Python. Prompts and the skill give the model a decision table and a fixed output command, not open-ended analysis.
- **Do not import from the Hermes tree.** Talk to Hermes through its CLI (`hermes kanban`, `hermes cron`) so Nightshift survives Hermes refactors.
- **Brief schema changes** follow [docs/brief-schema.md § Versioning](docs/brief-schema.md#versioning). Update the JSON Schema, `brief.validate`, and the renderer in the same PR.

## Adding a fixture scenario

1. Create `tests/fixtures/<scenario>/`.
2. Record each `gh` call the pipeline makes, for example `gh run list -R owner/repo --branch main -L 30 --json <fields> > runs-main.json`. The exact argv is in `nightshift/detect.py` and `nightshift/triage.py`. A failing test prints `no fixture recorded for: gh <key>` for anything missing.
3. Replace the owner and repo in the files with `{repo}`, `{owner}` and `{name}`, and **scrub anything private**: names, emails, tokens, internal URLs.
4. Add `manifest.yaml`, mapping each key (the argv with the repo replaced by `{repo}`, GraphQL documents as `query=<graphql>`) to a file. See `tests/fixtures/red-main/manifest.yaml`.

## Adding a log pattern

Infra and flaky patterns live in `nightshift/triage.py`. Add the pattern, plus a test line in `tests/test_triage.py::test_classification_categories` that shows it classifying.

## Changing the Hermes skill

`hermes/skills/nightshift-triage/SKILL.md` follows Hermes's skill authoring standards: a description of at most 60 characters, the fixed section order, and native tool names in backticks. Check it with Hermes's linter:

```bash
cd ~/.hermes/hermes-agent && venv/bin/python -c "from pathlib import Path; from tools.skill_linter import lint_skill, format_findings; print(format_findings(lint_skill(Path('/path/to/hermes-nightshift/hermes/skills/nightshift-triage/SKILL.md'))) or 'clean')"
```

## Security

Never commit tokens, `.env` files, real `nightshift.yaml` configs or unscrubbed logs. Repro commands run repository code, so they only ever run inside the Docker sandbox and only when `sandbox.enabled: true`.
