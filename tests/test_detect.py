"""Incident detection from recorded gh JSON (no network)."""

import json

from conftest import NOW, RED_MAIN, REPO

from nightshift.config import RepoConfig
from nightshift.detect import (
    ci_incidents_from_runs,
    deploy_incidents,
    detect_repo,
    pr_incidents,
    stale_branch_incidents,
)
from nightshift.gh import FixtureGh


def runs():
    return json.loads((RED_MAIN / "runs-main.json").read_text().replace("{repo}", REPO))


def by_id(incidents):
    return {i.id: i for i in incidents}


def test_red_streak_is_one_incident_keyed_on_first_red_run():
    inc = by_id(ci_incidents_from_runs(REPO, "main", runs(), NOW))
    ci = inc["ci-ci-main-9002"]
    assert ci.kind == "ci_failure"
    assert ci.data["first_red"]["databaseId"] == 9002
    assert ci.data["latest_red"]["databaseId"] == 9003
    assert ci.data["red_streak"] == 2
    # cancelled run 9010 sits between green and red and must not reset or join the streak
    assert ci.data["last_green"]["databaseId"] == 9001
    assert ci.data["green_on_red_sha"] is False


def test_green_and_in_progress_workflows_raise_nothing():
    ids = by_id(ci_incidents_from_runs(REPO, "main", runs(), NOW))
    assert not any(i.startswith("ci-lint") for i in ids)


def test_recovered_workflow_raises_nothing():
    rs = runs()
    rs.append({**rs[0], "databaseId": 9999, "conclusion": "success", "createdAt": "2026-11-19T06:59:00Z"})
    assert "ci-ci-main-9002" not in by_id(ci_incidents_from_runs(REPO, "main", rs, NOW))


def test_red_and_green_on_same_sha_flags_flake():
    e2e = by_id(ci_incidents_from_runs(REPO, "main", runs(), NOW))["ci-e2e-main-7102"]
    assert e2e.data["green_on_red_sha"] is True


def test_other_branches_are_ignored():
    assert ci_incidents_from_runs(REPO, "release", runs(), NOW) == []


def test_deploy_failure_finds_previous_success_per_environment():
    deps = json.loads((RED_MAIN / "deployments.json").read_text())
    statuses = {i: json.loads((RED_MAIN / f"status-{i}.json").read_text()) for i in (500, 501, 502)}
    deps = [d for d in deps if d["id"] != 499]
    inc = deploy_incidents(REPO, deps, statuses, NOW)
    assert [i.id for i in inc] == ["deploy-production-501"]
    assert inc[0].data["last_success_sha"] == deps[2]["sha"]
    assert inc[0].url.startswith("https://vercel.com/")


def test_drafts_and_decided_prs_are_not_awaiting_review():
    prs = json.loads((RED_MAIN / "prs.json").read_text())
    assert [i.data["number"] for i in pr_incidents(REPO, prs, NOW)] == [1]


def test_stale_branches_respect_age_and_watched_branches():
    g = json.loads((RED_MAIN / "branches.json").read_text())
    inc = stale_branch_incidents(REPO, g, keep=["main"], days=30, now=NOW)
    assert [i.data["branch"] for i in inc] == ["feature/old-export"]
    assert stale_branch_incidents(REPO, g, keep=["main"], days=365, now=NOW) == []


def test_one_broken_watcher_does_not_hide_the_others(tmp_path):
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    for f in RED_MAIN.iterdir():
        (fixtures / f.name).write_bytes(f.read_bytes())
    manifest = (fixtures / "manifest.yaml").read_text()
    (fixtures / "manifest.yaml").write_text(
        "\n".join(ln for ln in manifest.splitlines() if "issues.json" not in ln))
    found, errors = detect_repo(FixtureGh(fixtures), RepoConfig(name=REPO), now=NOW)
    assert any("issues" in e for e in errors)
    kinds = {i.kind for i in found}
    assert {"ci_failure", "deploy_failure", "pr_awaiting_review", "stale_branch"} <= kinds
    assert "new_issue" not in kinds


def test_watch_list_limits_gh_calls(gh):
    found, errors = detect_repo(gh, RepoConfig(name=REPO, watch=["pull_requests"]), now=NOW)
    assert errors == []
    assert {i.kind for i in found} == {"pr_awaiting_review"}
    assert all(c.startswith("pr list") for c in gh.calls)


def test_fixture_placeholders_follow_the_polled_repo():
    gh = FixtureGh(RED_MAIN)
    found, _ = detect_repo(gh, RepoConfig(name="someone/else", watch=["ci"]), now=NOW)
    assert found and all("someone/else" in i.url for i in found)


def test_deploy_walk_stops_at_last_success(gh):
    # deployment 499 is older than Production's last success and has no recorded statuses:
    # fetching it would raise "no fixture recorded".
    found, errors = detect_repo(gh, RepoConfig(name=REPO, watch=["deployments"]), now=NOW)
    assert errors == [] and [i.id for i in found] == ["deploy-production-501"]
    assert not any("/deployments/499/" in c for c in gh.calls)


def test_ignored_workflows_get_no_incident_but_stay_visible_for_resolution(gh):
    observed = {}
    repo = RepoConfig(name=REPO, watch=["ci"], ignore_workflows=["e2E", "Lint*"])
    found, errors = detect_repo(gh, repo, now=NOW, observed=observed)
    assert errors == [] and {i.id for i in found} == {"ci-ci-main-9002"}
    assert any(r["workflowName"] == "E2E" for r in observed["ci"]["main"])


def test_ignore_workflows_must_be_a_list_of_names():
    from pathlib import Path

    import pytest

    from nightshift.config import ConfigError, parse_config
    with pytest.raises(ConfigError, match="ignore_workflows"):
        parse_config({"repos": [{"name": "o/r", "ignore_workflows": "Dependabot Updates"}]}, Path("."))
    cfg = parse_config({"repos": [{"name": "o/r", "ignore_workflows": ["Dependabot *"]}]}, Path("."))
    assert cfg.repos[0].ignores("dependabot updates") and not cfg.repos[0].ignores("CI")
