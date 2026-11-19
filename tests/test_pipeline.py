"""End-to-end pass over fixtures: dedupe, sinks, AetherBus pending/retry, learn, digest."""

import json
import sys
import types

import pytest
from conftest import NOW

from nightshift import cli, pipeline
from nightshift.sinks import aetherbus as ab
from nightshift.state import State


def test_second_pass_dedupes(cfg, gh):
    c = cfg()
    first = pipeline.run_once(c, gh, now=NOW)
    assert len(first.new_failures) == 3 and len(first.handled) == 6
    second = pipeline.run_once(c, gh, now=NOW)
    assert second.handled == [] and second.skipped_seen == 6
    assert len(State(c.state_dir).read_ledger()) == 6


def test_dry_run_touches_no_state_or_sinks(cfg, gh, tmp_path, fake_hermes):
    c = cfg(kanban={"enabled": True})
    r = pipeline.run_once(c, gh, dry_run=True, briefs_dir=tmp_path / "dry", now=NOW, kanban_run=fake_hermes)
    assert len(r.new_failures) == 3
    assert fake_hermes.calls == []
    assert not (c.state_dir / "state.json").exists()
    assert len(list((tmp_path / "dry").rglob("*.json"))) == 3


def test_kanban_card_per_failure_with_idempotency_key(cfg, gh, fake_hermes):
    c = cfg(kanban={"enabled": True, "board": "nightshift"})
    r = pipeline.run_once(c, gh, now=NOW, kanban_run=fake_hermes)
    creates = [a for a in fake_hermes.calls if "create" in a]
    attaches = [a for a in fake_hermes.calls if "attach" in a]
    assert len(creates) == 3 and len(attaches) == 3
    for argv in creates:
        assert argv[:4] == ["hermes", "kanban", "--board", "nightshift"]
        assert "--triage" in argv and "--json" in argv
        assert argv[argv.index("--idempotency-key") + 1].startswith("nightshift:acme/webapp:")
    for h in r.new_failures:
        assert h.kanban == "created"
        assert json.loads(h.brief_json.read_text())["handoff"]["kanban_task_id"] == h.kanban_task_id


def test_kanban_failure_keeps_the_brief(cfg, gh):
    c = cfg(kanban={"enabled": True, "hermes_bin": "/nonexistent/hermes"})
    r = pipeline.run_once(c, gh, now=NOW)
    assert all(h.kanban == "error" and h.brief_json.is_file() for h in r.new_failures)


def test_aetherbus_not_installed_is_reported_not_fatal(cfg, gh, monkeypatch):
    monkeypatch.setitem(sys.modules, "aetherbus", None)  # makes `import aetherbus` raise
    r = pipeline.run_once(cfg(aetherbus={"enabled": True}), gh, now=NOW)
    assert {h.aetherbus for h in r.new_failures} == {"unavailable"}


@pytest.fixture
def fake_aetherbus(monkeypatch):
    monkeypatch.setitem(sys.modules, "aetherbus", types.ModuleType("aetherbus"))
    published = []

    async def fake_publish(cfg, json_path, brief, outbox):
        published.append((cfg.topic, json_path.resolve().as_uri(), brief["id"]))
        return f"evt-{len(published)}"

    monkeypatch.setattr(ab, "_publish", fake_publish)
    return published


def test_unreachable_broker_is_pending_then_retried(cfg, gh, monkeypatch, fake_aetherbus):
    c = cfg(aetherbus={"enabled": True})
    monkeypatch.setattr(ab, "broker_reachable", lambda url: False)
    r = pipeline.run_once(c, gh, now=NOW)
    assert {h.aetherbus for h in r.new_failures} == {"pending"} and fake_aetherbus == []

    monkeypatch.setattr(ab, "broker_reachable", lambda url: True)
    r2 = pipeline.run_once(c, gh, now=NOW)
    assert len(r2.republished) == 3
    topics = {t for t, _, _ in fake_aetherbus}
    assert topics == {"dev.aetherbus.nightshift.brief.v1"}
    # pointer, not payload: dataref is a file:// URI to the brief JSON
    for _, uri, bid in fake_aetherbus:
        assert uri.startswith("file://") and uri.endswith(f"/{bid}.json")
    assert State(c.state_dir).pending_publish() == []


def test_topic_is_inside_aetherbus_default_stream_subjects(cfg):
    topic = cfg().aetherbus.topic
    assert topic.startswith(("aetherbus.", "dev.aetherbus."))


# ------------------------------------------------------------------ CLI-level


def _write_cfg(tmp_path, extra=""):
    p = tmp_path / "nightshift.yaml"
    p.write_text(f"""state_dir: {tmp_path / 'state'}
repos:
  - name: acme/webapp
    repro_setup: npm ci
kanban:
  enabled: false
{extra}""")
    return str(p)


FIX = ["--fixtures", str(__import__("conftest").RED_MAIN)]


def test_tick_wakes_agent_only_for_new_failures(tmp_path, capsys):
    c = _write_cfg(tmp_path)
    assert cli.main(["-c", c, "tick", *FIX]) == 0
    out = capsys.readouterr().out
    assert out.startswith("NIGHTSHIFT: 3 new failure brief(s)")
    assert "wakeAgent" not in out
    assert cli.main(["-c", c, "tick", *FIX]) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == '{"wakeAgent": false}'


def test_learned_lesson_shows_up_in_later_briefs(tmp_path, capsys):
    c = _write_cfg(tmp_path)
    assert cli.main(["-c", c, "learn", "acme/webapp", "--pattern", r"sealed_at does not exist",
                     "--note", "Run supabase migrations before vitest."]) == 0
    assert cli.main(["-c", c, "poll", "--dry-run", "--briefs-dir", str(tmp_path / "b"), *FIX]) == 0
    brief = json.loads(next((tmp_path / "b").rglob("ci-ci-main-9002.json")).read_text())
    assert [le["note"] for le in brief["lessons"]] == ["Run supabase migrations before vitest."]


def test_classify_overrides_heuristic_and_keeps_it(tmp_path, capsys):
    c = _write_cfg(tmp_path)
    cli.main(["-c", c, "poll", *FIX])
    assert cli.main(["-c", c, "classify", "ci-e2e-main-7102", "--category", "real", "--confidence", "0.6",
                     "--rationale", "Timeout is caused by the new sealed_at query."]) == 0
    b = json.loads(next((tmp_path / "state").rglob("ci-e2e-main-7102.json")).read_text())
    assert b["classification"]["source"] == "model" and b["classification"]["category"] == "real"
    assert b["classification"]["heuristic"]["category"] == "flaky"
    assert "Fix the root cause" in b["handoff"]["prompt"]
    assert cli.main(["-c", c, "classify", "ci-e2e-main-7102", "--category", "real", "--confidence", "7",
                     "--rationale", "x"]) == 2


def test_digest_groups_by_kind_and_advances_marker(tmp_path, capsys):
    c = _write_cfg(tmp_path)
    cli.main(["-c", c, "poll", *FIX])
    capsys.readouterr()
    assert cli.main(["-c", c, "digest"]) == 0
    out = capsys.readouterr().out
    for section in ("**CI failures**", "**Failed deployments**", "**PRs awaiting review**",
                    "**New issues**", "**Stale branches**"):
        assert section in out
    assert (tmp_path / "state" / "digests").is_dir()
    cli.main(["-c", c, "digest"])
    assert "quiet night" in capsys.readouterr().out
