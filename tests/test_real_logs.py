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
