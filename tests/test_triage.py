"""Deterministic triage: log parsing, classification, culprit ranking, repro, lessons."""

from conftest import NOW, RED_MAIN, REPO

from nightshift.config import Lesson, RepoConfig
from nightshift.detect import detect_repo
from nightshift.models import Incident
from nightshift.triage import (
    classify,
    matching_lessons,
    parse_failed_log,
    repro_command,
    suspected_files,
    triage,
)


def ci_log():
    return (RED_MAIN / "log-9003.txt").read_text()


def test_log_parse_finds_failing_step_test_and_error():
    d = parse_failed_log(ci_log())
    assert (d.job, d.step) == ("test", "Run npm test")
    assert d.command == "npm test"
    assert d.test_target == "src/lib/entries.test.ts" and d.runner == "vitest"
    assert d.error == "Error: column entries.archived_at does not exist"
    assert d.signature.startswith("FAIL src/lib/entries.test.ts")
    assert "src/lib/entries.ts" in d.paths


def test_excerpt_drops_step_header_and_env_and_ansi():
    d = parse_failed_log(ci_log())
    text = "\n".join(d.excerpt)
    assert "SUPABASE_URL" not in text and "shell:" not in text and "\x1b[" not in text
    assert "column entries.archived_at does not exist" in text
    assert len(d.excerpt) <= 60


def test_excerpt_is_capped_on_huge_logs():
    lines = "".join(f"j\tRun x\t2026-11-19T00:00:00.0Z line {i}\n" for i in range(5000))
    assert len(parse_failed_log(lines + "j\tRun x\t2026-11-19T00:00:01Z Error: boom\n").excerpt) <= 60


def _inc(**data):
    return Incident(id="x", repo=REPO, kind="ci_failure", title="t", url="u", detected_at="n", data=data)


def test_classification_categories():
    assert classify("##[error]No space left on device", _inc(), 3)["category"] == "infra"
    assert classify("Test timed out in 5000ms", _inc(), 3)["category"] == "flaky"
    assert classify("anything", _inc(green_on_red_sha=True), 3)["category"] == "flaky"
    assert classify("", _inc(), 0)["category"] == "unknown"
    real = classify(ci_log(), _inc(), 2)
    assert real["category"] == "real" and real["source"] == "heuristic"
    # a commit range to blame makes a real failure more certain than none
    assert real["confidence"] > classify(ci_log(), _inc(), 0)["confidence"]


def test_files_named_in_the_log_outrank_other_changes():
    ranked = suspected_files(["src/app/page.tsx", "src/lib/entries.ts", "docs/x.md"],
                             ["src/lib/entries.ts"], "src/lib/entries.test.ts")
    assert ranked[0]["path"] == "src/lib/entries.ts"
    assert [f["path"] for f in ranked].index("docs/x.md") > 0


def test_source_test_pair_is_suspected():
    ranked = suspected_files(["src/lib/other.ts", "src/lib/entries.ts"], [], "src/lib/entries.test.ts")
    assert ranked[0]["path"] == "src/lib/entries.ts" and ranked[0]["score"] > ranked[1]["score"]


def test_repro_targets_the_failing_test_at_the_first_red_sha():
    d = parse_failed_log(ci_log())
    cmd = repro_command(d, "abc123", RepoConfig(name=REPO, repro_setup="npm ci"))
    assert cmd == "git checkout abc123 && npm ci && npx vitest run src/lib/entries.test.ts"


def test_repro_falls_back_to_step_command():
    d = parse_failed_log("j\tRun make\t2026-11-19T00:00:00Z ##[group]Run make check\n"
                         "j\tRun make\t2026-11-19T00:00:00Z   make check\n"
                         "j\tRun make\t2026-11-19T00:00:01Z ##[endgroup]\n"
                         "j\tRun make\t2026-11-19T00:00:02Z ##[error]Process completed with exit code 2.\n")
    assert repro_command(d, "s", RepoConfig(name=REPO)) == "git checkout s && make check"


def test_lessons_match_on_log_text():
    lessons = [Lesson(pattern=r"(column|relation) \S+ does not exist", note="apply migrations", id="L1"),
               Lesson(pattern=r"ENOSPC", note="disk", id="L2")]
    hits = matching_lessons(lessons, "\n".join(parse_failed_log(ci_log()).excerpt))
    assert [h["id"] for h in hits] == ["L1"]


def test_full_triage_from_fixtures(gh):
    found, _ = detect_repo(gh, RepoConfig(name=REPO, watch=["ci", "deployments"], repro_setup="npm ci"), now=NOW)
    ci = next(i for i in found if i.id == "ci-ci-main-9002")
    t = triage(gh, ci, RepoConfig(name=REPO, repro_setup="npm ci"),
               [Lesson(pattern="does not exist", note="migrations", id="L1")])
    assert t.errors == []
    assert [c["title"] for c in t.commits] == ["Refactor entries query helpers", "Read archived_at from entries"]
    assert t.suspected[0]["path"] == "src/lib/entries.ts"
    assert t.repro.startswith(f"git checkout {ci.data['first_red']['headSha']} ")
    assert t.lessons and t.lessons[0]["id"] == "L1"
    dep = next(i for i in found if i.kind == "deploy_failure")
    td = triage(gh, dep, RepoConfig(name=REPO, build_command="npm run build"), [])
    assert td.repro.endswith("npm run build") and td.suspected[0]["path"] == "src/lib/entries.ts"


def test_last_green_outside_lookback_is_found_with_a_targeted_query(tmp_path):
    import json

    import yaml

    from nightshift.gh import FixtureGh, fixture_key
    first_red = {"databaseId": 50, "headSha": "b" * 40, "url": "u50", "createdAt": "2026-11-19T05:00:00Z",
                 "workflowDatabaseId": 7}
    inc = _inc(workflow="Nightly", branch="main", first_red=first_red, latest_red=first_red, last_green=None)
    args = ["run", "list", "-R", REPO, "--workflow", "7", "--branch", "main", "--status", "success",
            "-L", "10", "--json", "databaseId,headSha,url,createdAt"]
    (tmp_path / "green.json").write_text(json.dumps([
        {"databaseId": 60, "headSha": "c" * 40, "url": "u60", "createdAt": "2026-11-19T06:00:00Z"},  # after: a fix
        {"databaseId": 40, "headSha": "a" * 40, "url": "u40", "createdAt": "2026-11-18T05:00:00Z"},
    ]))
    (tmp_path / "log.txt").write_text("")
    (tmp_path / "compare.json").write_text(json.dumps({"commits": [], "files": [], "html_url": "c"}))
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump({"commands": {
        fixture_key(args, REPO): "green.json",
        fixture_key(["run", "view", "50", "-R", REPO, "--log-failed"], REPO): "log.txt",
        fixture_key(["api", f"repos/{REPO}/compare/{'a' * 40}...{'b' * 40}"], REPO): "compare.json",
    }}))
    inc.repo = REPO
    t = triage(FixtureGh(tmp_path), inc, RepoConfig(name=REPO), [])
    assert inc.data["last_green"]["databaseId"] == 40
    assert "last green" not in " ".join(t.errors)
