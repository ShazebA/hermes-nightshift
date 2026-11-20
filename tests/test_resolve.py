"""Resolution: a red night (red-main) followed by the morning after (recovered)."""

import json

from conftest import NOW, RECOVERED, RED_MAIN

from nightshift import cli, pipeline
from nightshift.brief import validate
from nightshift.gh import FixtureGh
from nightshift.pickup import open_briefs
from nightshift.resolve import ci_resolution
from nightshift.state import State

CI, E2E, DEPLOY = "ci-ci-main-9002", "ci-e2e-main-7102", "deploy-production-501"


def _red_then_recovered(c, **kw):
    pipeline.run_once(c, FixtureGh(RED_MAIN), now=NOW, **kw)
    return pipeline.run_once(c, FixtureGh(RECOVERED), now=NOW, **kw)


def test_fix_and_rerun_are_told_apart(cfg):
    c = cfg()
    r = _red_then_recovered(c)
    assert r.handled == []  # nothing new: recovery is not a new incident
    how = {x.id: x.how for x in r.resolved}
    assert how == {CI: "fix", E2E: "rerun", DEPLOY: "fix"}
    by_id = {x.id: x for x in r.resolved}
    assert by_id[CI].sha.startswith("d3adb33") and by_id[CI].url.endswith("/9005")
    assert by_id[DEPLOY].sha.startswith("d3adb33")


def test_resolution_is_written_once_into_brief_state_and_ledger(cfg):
    c = cfg()
    _red_then_recovered(c)
    again = pipeline.run_once(c, FixtureGh(RECOVERED), now=NOW)
    assert again.resolved == []
    st = State(c.state_dir)
    rows = [r for r in st.read_ledger() if r["kind"] == "resolved"]
    assert sorted(r["id"] for r in rows) == sorted([CI, E2E, DEPLOY])
    b = json.loads(next(c.briefs_dir.rglob(f"{CI}.json")).read_text())
    assert b["resolution"]["how"] == "fix" and validate(b) == []
    assert "**Resolved**" in next(c.briefs_dir.rglob(f"{CI}.md")).read_text()
    assert st.seen[f"acme/webapp#{CI}"]["resolved"]  # seen record kept, so dedupe still holds


def test_brief_with_resolution_matches_json_schema(cfg):
    import jsonschema

    from nightshift.brief import SCHEMA_PATH
    c = cfg()
    _red_then_recovered(c)
    schema = json.loads(SCHEMA_PATH.read_text())
    for p in c.briefs_dir.rglob("*.json"):
        jsonschema.validate(json.loads(p.read_text()), schema)


def test_dry_run_resolves_nothing(cfg, tmp_path):
    c = cfg()
    pipeline.run_once(c, FixtureGh(RED_MAIN), now=NOW)
    r = pipeline.run_once(c, FixtureGh(RECOVERED), now=NOW, dry_run=True, briefs_dir=tmp_path / "d")
    assert r.resolved == []
    assert not any(json.loads(p.read_text()).get("resolution") for p in c.briefs_dir.rglob("*.json"))


def test_outage_resolves_nothing(cfg, tmp_path):
    c = cfg()
    pipeline.run_once(c, FixtureGh(RED_MAIN), now=NOW)
    empty = tmp_path / "nofixtures"
    empty.mkdir()
    (empty / "manifest.yaml").write_text("commands: {}\n")
    r = pipeline.run_once(c, FixtureGh(empty), now=NOW)
    assert r.errors and r.resolved == []


def test_still_red_is_not_resolved(cfg):
    c = cfg()
    pipeline.run_once(c, FixtureGh(RED_MAIN), now=NOW)
    assert pipeline.run_once(c, FixtureGh(RED_MAIN), now=NOW).resolved == []


def _brief(run_id=9002, failed_at="2026-11-19T05:00:00Z", sha="9c4d7e2"):
    return {"what_broke": {"workflow": "CI", "branch": "main", "run_id": run_id, "failed_at": failed_at},
            "culprit": {"first_red_sha": sha}}


def _run(i, concl, sha, at):
    return {"databaseId": i, "workflowName": "CI", "headBranch": "main", "status": "completed",
            "conclusion": concl, "headSha": sha, "createdAt": at, "url": f"u/{i}"}


def test_green_from_before_the_failure_does_not_resolve_it():
    runs = [_run(9002, "failure", "9c4d7e2", "2026-11-19T05:00:00Z"),
            _run(9001, "success", "5f2e8a1", "2026-11-19T03:00:00Z")]
    assert ci_resolution(_brief(), runs) is None


def test_failure_scrolled_out_of_window_still_resolves_on_newer_green():
    runs = [_run(9100, "success", "abc1234", "2026-11-20T05:00:00Z")]
    assert ci_resolution(_brief(), runs)["how"] == "fix"


def test_green_on_a_commit_from_later_in_the_streak_is_a_rerun():
    runs = [_run(9002, "failure", "9c4d7e2", "2026-11-19T05:00:00Z"),
            _run(9003, "failure", "e81f3a6", "2026-11-19T06:00:00Z"),
            _run(9004, "success", "e81f3a6", "2026-11-19T06:30:00Z")]
    assert ci_resolution(_brief(), runs)["how"] == "rerun"


def test_kanban_blocked_card_is_closed_claimed_card_only_commented(cfg, fake_hermes):
    c = cfg(kanban={"enabled": True, "board": "nightshift"})
    first = pipeline.run_once(c, FixtureGh(RED_MAIN), now=NOW, kanban_run=fake_hermes)
    cards = {h.incident.id: h.kanban_task_id for h in first.new_failures}
    fake_hermes.status[cards[E2E]] = "ready"  # someone unblocked it
    fake_hermes.calls.clear()
    r = pipeline.run_once(c, FixtureGh(RECOVERED), now=NOW, kanban_run=fake_hermes)
    assert {x.id: x.kanban for x in r.resolved} == {CI: "closed", E2E: "commented", DEPLOY: "closed"}
    completed = [a[a.index("complete") + 1] for a in fake_hermes.calls if "complete" in a]
    assert sorted(completed) == sorted([cards[CI], cards[DEPLOY]])
    fake_hermes.calls.clear()
    pipeline.run_once(c, FixtureGh(RECOVERED), now=NOW, kanban_run=fake_hermes)
    assert fake_hermes.calls == []  # each card is resolved exactly once


def _cfg_file(tmp_path):
    p = tmp_path / "nightshift.yaml"
    p.write_text(f"state_dir: {tmp_path / 'state'}\nrepos:\n  - name: acme/webapp\n"
                 "    repro_setup: npm ci\nkanban:\n  enabled: false\n")
    return str(p)


def test_tick_stays_asleep_when_only_resolutions(tmp_path, capsys):
    c = _cfg_file(tmp_path)
    cli.main(["-c", c, "tick", "--fixtures", str(RED_MAIN)])
    capsys.readouterr()
    assert cli.main(["-c", c, "tick", "--fixtures", str(RECOVERED)]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == '{"wakeAgent": false}'


def test_next_and_claim_skip_resolved_briefs(tmp_path, capsys):
    c = _cfg_file(tmp_path)
    cli.main(["-c", c, "poll", "--fixtures", str(RED_MAIN)])
    briefs = tmp_path / "state" / "briefs"
    assert len(open_briefs(briefs)) == 3
    cli.main(["-c", c, "poll", "--fixtures", str(RECOVERED)])
    assert "3 recovered" in capsys.readouterr().out
    assert open_briefs(briefs) == []
    assert cli.main(["-c", c, "claim", CI, "--agent", "claude-code"]) == 1
    assert "is resolved: fix" in capsys.readouterr().err


def test_digest_reports_recovered_separately(tmp_path, capsys):
    c = _cfg_file(tmp_path)
    cli.main(["-c", c, "poll", "--fixtures", str(RED_MAIN)])
    cli.main(["-c", c, "poll", "--fixtures", str(RECOVERED)])
    capsys.readouterr()
    cli.main(["-c", c, "digest"])
    out = capsys.readouterr().out
    assert "6 new item(s)" in out and "3 failure(s) triaged; 3 recovered" in out
    assert "**Recovered (green again)**" in out
    assert "fixed by `d3adb33`" in out and "re-run of `9c4d7e2`, no code change" in out
    assert "(since recovered: fix)" in out
