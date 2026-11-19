"""Brief schema v1: generation from fixtures, validation, rendering."""

import copy
import json

import jsonschema
import pytest
from conftest import NOW, REPO

from nightshift import BRIEF_SCHEMA_ID
from nightshift.brief import SCHEMA_PATH, build_brief, render_markdown, validate, write_brief
from nightshift.config import RepoConfig
from nightshift.detect import detect_repo
from nightshift.triage import triage

SCHEMA = json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def briefs(gh):
    repo = RepoConfig(name=REPO, watch=["ci", "deployments"], repro_setup="npm ci", build_command="npm run build")
    found, _ = detect_repo(gh, repo, now=NOW)
    return {i.id: build_brief(i, triage(gh, i, repo, repo.lessons), "2026-11-19T07:00:00Z")
            for i in found if i.is_failure}


def test_every_generated_brief_satisfies_the_json_schema(briefs, tmp_path):
    assert set(briefs) == {"ci-ci-main-9002", "ci-e2e-main-7102", "deploy-production-501"}
    for b in briefs.values():
        write_brief(b, tmp_path)  # fills handoff fields
        jsonschema.validate(b, SCHEMA)
        assert validate(b) == []
        assert b["schema"] == BRIEF_SCHEMA_ID == SCHEMA["properties"]["schema"]["const"]


def test_ci_brief_content(briefs):
    b = briefs["ci-ci-main-9002"]
    assert b["classification"]["category"] == "real"
    assert b["culprit"]["last_green_sha"].startswith("5f2e8a1")
    assert b["culprit"]["first_red_sha"].startswith("9c4d7e2")
    assert b["what_broke"]["test_target"] == "src/lib/entries.test.ts"
    assert "archived_at does not exist" in b["what_broke"]["summary"]
    assert {link["label"] for link in b["links"]} >= {"Failing run", "Last green run", "Culprit range (compare)"}


def test_flaky_brief_has_no_culprit_range(briefs):
    b = briefs["ci-e2e-main-7102"]
    assert b["classification"]["category"] == "flaky"
    assert b["culprit"]["commits"] == []


BAD = [
    ("classification", "category", "maybe"),
    ("classification", "confidence", 1.5),
    ("classification", "source", "oracle"),
    (None, "kind", "new_issue"),
    (None, "schema", "nightshift.brief/v0"),
]


@pytest.mark.parametrize("section,key,value", BAD)
def test_validator_and_json_schema_agree_on_rejections(briefs, section, key, value):
    b = copy.deepcopy(briefs["ci-ci-main-9002"])
    b["handoff"]["prompt"] = "x"
    (b[section] if section else b)[key] = value
    assert validate(b)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(b, SCHEMA)


def test_write_refuses_invalid_brief(briefs, tmp_path):
    b = copy.deepcopy(briefs["ci-ci-main-9002"])
    b["classification"]["category"] = "maybe"
    with pytest.raises(ValueError):
        write_brief(b, tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def test_markdown_carries_everything_an_agent_needs(briefs, tmp_path):
    b = briefs["ci-ci-main-9002"]
    jp, mp = write_brief(b, tmp_path)
    md = mp.read_text()
    assert md == render_markdown(json.loads(jp.read_text()))
    for needle in (b["repro"]["command"], b["culprit"]["first_red_sha"][:7], "src/lib/entries.ts",
                   b["what_broke"]["run_url"], str(mp)):
        assert needle in md
    for heading in ("## What broke", "## Repro", "## Suspected culprit", "## Known lessons", "## Links", "## Handoff"):
        assert heading in md
    assert jp.parent.name == "acme__webapp" and jp.stem == b["id"]
