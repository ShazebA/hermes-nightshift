"""Parser regression tests on real (trimmed) public CI logs. See fixtures/real-logs/SOURCES.md.

Modern `gh` labels every line UNKNOWN STEP and returns whole jobs, so these pin down step
recovery, roll-up job skipping, signature choice and infra detection on logs we did not write.
"""

from pathlib import Path

import pytest

from nightshift.models import Incident
from nightshift.triage import classify, parse_failed_log

LOGS = Path(__file__).parent / "fixtures" / "real-logs"

CASES = [
    # file, failing command contains, signature contains, test target, category
    ("fastapi-pytest", "scripts/test-cov.sh", "FAILED tests/test_sse.py::test_keepalive_ping_sync",
     "tests/test_sse.py::test_keepalive_ping_sync", "real"),
    ("fastapi-missing-artifact", "actions/download-artifact", "Artifact not found", "", "infra"),
    ("nextjs-api-502", "", "failed with", "", "infra"),
    ("hermes-pages-outage", "actions/deploy-pages", "githubstatus.com", "", "infra"),
    ("hermes-nix-build", "", "error: Cannot build", "", "real"),
    ("ruff-clippy", "cargo clippy", "error[E0599]: no variant", "", "real"),
    ("playwright-android", "npm run atest", "Error: expect(received).toContain(expected)",
     "tests/page/elementhandle-scroll-into-view.spec.ts:147", "real"),
]


def _inc():
    return Incident("x", "o/r", "ci_failure", "t", "u", "n", {})


@pytest.mark.parametrize("name,cmd,sig,target,category", CASES, ids=[c[0] for c in CASES])
def test_real_log(name, cmd, sig, target, category):
    d = parse_failed_log((LOGS / f"{name}.log").read_text())
    assert cmd in d.command
    assert sig in d.signature
    assert d.test_target == target
    assert not d.aggregator
    # the excerpt stops at the failure: no post-job cleanup noise
    assert d.excerpt[-1].startswith("##[error]")
    assert classify("\n".join(d.segment), _inc(), 1, d.aggregator)["category"] == category


def test_rollup_job_is_skipped_for_the_job_that_broke():
    d = parse_failed_log((LOGS / "fastapi-pytest.log").read_text())
    assert "alls-green" not in d.job and d.job.startswith("test (")


def test_only_rollup_job_is_unknown_not_real():
    text = "\n".join(ln for ln in (LOGS / "fastapi-pytest.log").read_text().splitlines()
                     if ln.startswith("test-alls-green"))
    d = parse_failed_log(text)
    assert d.aggregator
    assert classify("\n".join(d.segment), _inc(), 1, d.aggregator)["category"] == "unknown"


# ------------------------------------------------- step commands and repro on real logs

def _repro(name, run_id=123):
    from nightshift.config import RepoConfig
    from nightshift.triage import repro_command
    d = parse_failed_log((LOGS / f"{name}.log").read_text())
    return d, repro_command(d, "abc1234", RepoConfig(name="o/r"), run_id)


def test_colour_marked_script_is_read_whole_including_blank_lines():
    d, _ = _repro("uv-long-script")
    lines = d.command.splitlines()
    assert len(lines) == 61 and lines[0].startswith('if ! [[ "$RUN_ID"') and "" in lines
    assert {"RUN_ID", "HEAD_SHA", "GH_TOKEN"} <= set(d.env)
    assert not any("\x00" in ln for ln in d.segment + d.excerpt)  # marker never leaks


def test_multi_line_script_repro_is_valid_bash():
    import subprocess
    d, repro = _repro("hermes-nix-build")
    assert len(d.command.splitlines()) > 1 and "<<'NIGHTSHIFT_STEP'" in repro
    assert repro.startswith("git checkout abc1234 && bash -e <<'NIGHTSHIFT_STEP'\n")
    assert subprocess.run(["bash", "-n"], input=repro, text=True).returncode == 0


def test_script_too_long_to_inline_points_at_the_log():
    _, repro = _repro("uv-long-script")
    assert repro.startswith("gh run view 123 -R o/r --log-failed  #") and "61-line script" in repro


@pytest.mark.parametrize("name,action", [
    ("pydantic-action-step", "dawidd6/action-download-artifact@eab87c9830c39eff17e5a6eadb20bfb4bc880477"),
    ("fastapi-missing-artifact", "actions/download-artifact@v8.0.2"),
])
def test_action_step_repro_reads_the_log_and_only_suggests_rerun(name, action):
    d, repro = _repro(name)
    assert d.action == action
    cmd, _, comment = repro.partition("  #")
    assert cmd == "gh run view 123 -R o/r --log-failed"  # read-only; the rerun is only a hint
    assert "gh run rerun 123 --failed -R o/r" in comment


def test_network_and_timeout_text_in_test_output_is_not_the_failure():
    """ECONNRESET is in test *names* and one test timed out, but the failure is a protocol
    error in a browser that doesn't support the command."""
    d = parse_failed_log((LOGS / "playwright-bidi.log").read_text())
    text = "\n".join(d.segment)
    assert "ECONNRESET" in text and "Test timeout of 15000ms exceeded" in text
    assert d.test_target == "tests/library/beforeunload.spec.ts:20"
    c = classify(text, _inc(), 0, d.aggregator, d.test_target, f"{d.signature}\n{d.error}")
    assert c["category"] == "real"


def test_network_error_that_is_the_failure_is_infra():
    d = parse_failed_log((LOGS / "uv-long-script.log").read_text())
    c = classify("\n".join(d.segment), _inc(), 0, d.aggregator, d.test_target, f"{d.signature}\n{d.error}")
    assert c["category"] == "infra" and "stream error" in c["rationale"]


def test_github_managed_workflow_is_not_blamed_on_commits():
    inc = Incident("x", "o/r", "ci_failure", "t", "u", "n", {"first_red": {"event": "dynamic"}})
    c = classify("##[error]Dependabot encountered an error performing the update", inc, 17)
    assert c["category"] == "unknown" and "GitHub-managed" in c["rationale"]


def test_heredoc_repro_parses_under_sh_as_the_sandbox_runs_it():
    import subprocess

    from nightshift.sandbox import strip_checkout
    _, repro = _repro("hermes-nix-build")
    sha, inner = strip_checkout(repro)
    assert sha == "abc1234"
    assert subprocess.run(["sh", "-n"], input=inner, text=True).returncode == 0


def test_kanban_card_fences_a_multi_line_repro(tmp_path):
    from nightshift.sinks.kanban import card_body
    _, repro = _repro("hermes-nix-build")
    brief = {"what_broke": {"summary": "s"}, "repro": {"command": repro}, "handoff": {"prompt": "p"},
             "classification": {"category": "real", "confidence": 0.7, "source": "heuristic"}}
    assert "```bash\n" + repro + "\n```" in card_body(brief, tmp_path / "b.md")
