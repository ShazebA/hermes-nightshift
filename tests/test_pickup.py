"""Consumer side: `nightshift next` lists unclaimed briefs, `nightshift claim` picks one up."""

import json
import subprocess

import jsonschema
import pytest
from conftest import NOW, RED_MAIN

from nightshift import cli, pipeline
from nightshift.brief import SCHEMA_PATH, load_brief, validate
from nightshift.pickup import AlreadyClaimed, claim, open_briefs

SCHEMA = json.loads(SCHEMA_PATH.read_text())
FIX = ["--fixtures", str(RED_MAIN)]
WHEN = "2026-11-19T08:00:00Z"


def _restamp(c, stamps):
    """Give briefs distinct created_at values so ordering doesn't depend on ties."""
    for bid, ts in stamps.items():
        p = next(c.briefs_dir.rglob(f"{bid}.json"))
        b = load_brief(p)
        b["created_at"] = ts
        p.write_text(json.dumps(b))


def test_next_lists_unclaimed_newest_first(cfg, gh):
    c = cfg()
    pipeline.run_once(c, gh, now=NOW)
    rows = open_briefs(c.briefs_dir)
    assert len(rows) == 3
    ids = [r["id"] for r in rows]
    _restamp(c, {ids[0]: "2026-11-18T01:00:00Z", ids[1]: "2026-11-19T03:00:00Z",
                 ids[2]: "2026-11-18T12:00:00Z"})
    assert [r["id"] for r in open_briefs(c.briefs_dir)] == [ids[1], ids[2], ids[0]]
    assert open_briefs(c.briefs_dir, "acme/other") == []
    r = open_briefs(c.briefs_dir, "acme/webapp")[0]
    assert r["brief_md"].endswith(".md") and r["repro"].startswith("git checkout ")


def test_tied_created_at_order_is_deterministic(cfg, gh):
    c = cfg()
    pipeline.run_once(c, gh, now=NOW)
    first = [r["id"] for r in open_briefs(c.briefs_dir)]
    assert first == [r["id"] for r in open_briefs(c.briefs_dir)]


def test_claim_writes_schema_valid_brief_and_hides_it_from_next(cfg, gh):
    c = cfg()
    pipeline.run_once(c, gh, now=NOW)
    top = open_briefs(c.briefs_dir)[0]
    res = claim(c, top["id"], "claude-code", now=WHEN)
    b = load_brief(res.brief_json)
    assert b["handoff"]["claimed_by"] == "claude-code" and b["handoff"]["claimed_at"] == WHEN
    assert b["updated_at"] == WHEN
    assert validate(b) == []
    jsonschema.validate(b, SCHEMA)
    assert "Claimed by `claude-code`" in res.brief_md.read_text()
    assert res.kanban == "disabled"
    assert top["id"] not in [r["id"] for r in open_briefs(c.briefs_dir)]
    assert len(open_briefs(c.briefs_dir)) == 2


def test_schema_rejects_bad_claim_fields(cfg, gh):
    c = cfg()
    pipeline.run_once(c, gh, now=NOW)
    b = load_brief(next(c.briefs_dir.rglob("*.json")))
    b["handoff"]["claimed_by"] = 7
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(b, SCHEMA)
    assert validate(b)


def test_second_agent_cannot_steal_a_claim(cfg, gh):
    c = cfg()
    pipeline.run_once(c, gh, now=NOW)
    bid = open_briefs(c.briefs_dir)[0]["id"]
    claim(c, bid, "claude-code", now=WHEN)
    with pytest.raises(AlreadyClaimed):
        claim(c, bid, "codex")
    again = claim(c, bid, "claude-code")
    assert again.already_yours and again.brief["handoff"]["claimed_at"] == WHEN


def test_claim_comments_and_unblocks_the_card(cfg, gh, fake_hermes):
    c = cfg(kanban={"enabled": True, "board": "nightshift"})
    r = pipeline.run_once(c, gh, now=NOW, kanban_run=fake_hermes)
    h = r.new_failures[0]
    fake_hermes.calls.clear()
    res = claim(c, h.incident.id, "claude-code", run=fake_hermes, now=WHEN)
    assert res.kanban == "updated" and res.notes == []
    comment, unblock = fake_hermes.calls
    base = ["hermes", "kanban", "--board", "nightshift"]
    assert comment[:6] == base + ["comment", h.kanban_task_id]
    assert "claude-code" in comment[6] and str(res.brief_md) in comment[6]
    assert comment[7:] == ["--author", "nightshift"]
    assert unblock == base + ["unblock", h.kanban_task_id]


def test_kanban_failure_on_claim_keeps_the_claim(cfg, gh, fake_hermes):
    c = cfg(kanban={"enabled": True})
    r = pipeline.run_once(c, gh, now=NOW, kanban_run=fake_hermes)
    bid = r.new_failures[0].incident.id

    def failing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such task")

    res = claim(c, bid, "codex", run=failing)
    assert res.kanban == "error" and "comment failed: no such task" in res.notes[0]
    assert load_brief(res.brief_json)["handoff"]["claimed_by"] == "codex"


def test_claim_without_card_id_is_a_note_not_a_crash(cfg, gh, fake_hermes):
    c = cfg(kanban={"enabled": True, "hermes_bin": "/nonexistent/hermes"})
    pipeline.run_once(c, gh, now=NOW)  # card creation fails, so no kanban_task_id
    bid = open_briefs(c.briefs_dir)[0]["id"]
    res = claim(c, bid, "cursor", run=fake_hermes)
    assert res.kanban == "no-card" and fake_hermes.calls == []
    assert load_brief(res.brief_json)["handoff"]["claimed_by"] == "cursor"


# ------------------------------------------------------------------ CLI-level


def _write_cfg(tmp_path):
    p = tmp_path / "nightshift.yaml"
    p.write_text(f"""state_dir: {tmp_path / 'state'}
repos:
  - name: acme/webapp
kanban:
  enabled: false
""")
    return str(p)


def test_cli_next_then_claim(tmp_path, capsys):
    c = _write_cfg(tmp_path)
    assert cli.main(["-c", c, "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    cli.main(["-c", c, "poll", *FIX])
    capsys.readouterr()

    assert cli.main(["-c", c, "next", "--repo", "acme/webapp", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 3
    top = rows[0]["id"]

    assert cli.main(["-c", c, "claim", top, "--agent", "claude-code", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["claimed_by"] == "claude-code" and out["kanban"] == "disabled"
    assert out["brief_md"].endswith(f"{top}.md")

    assert cli.main(["-c", c, "claim", top, "--agent", "codex"]) == 1
    assert "claimed by claude-code" in capsys.readouterr().err

    assert cli.main(["-c", c, "next"]) == 0
    assert top not in capsys.readouterr().out
    assert cli.main(["-c", c, "next", "--repo", "acme/unknown"]) == 2
