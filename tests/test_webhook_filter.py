"""The Hermes webhook route filter, run the way Hermes runs it: a separate interpreter, the
payload as JSON on stdin, kept only on exit 0 with a non-empty, non-[SILENT] JSON-object stdout
(gateway/platforms/webhook_filters.py `run_route_script`)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILTER = ROOT / "hermes" / "scripts" / "nightshift-webhook-filter.py.tmpl"
PAYLOADS = Path(__file__).parent / "fixtures" / "webhooks"

KEPT = {
    "workflow_run-failure",
    "workflow_run-timed_out",
    "workflow_run-startup_failure",
    "deployment_status-failure",
    "deployment_status-error",
    "deployment_status-failure-actions",
}


def run_filter(stdin: str) -> tuple[bool, dict | None, subprocess.CompletedProcess]:
    # -I -S: no site-packages and no cwd on sys.path, so any non-stdlib import fails here.
    p = subprocess.run([sys.executable, "-I", "-S", str(FILTER)], input=stdin, capture_output=True,
                       text=True, timeout=30, cwd=FILTER.parent)
    out = p.stdout.strip()
    if p.returncode != 0 or not out or out == "[SILENT]":
        return False, None, p
    try:
        obj = json.loads(out)
    except json.JSONDecodeError:
        return True, None, p  # Hermes would keep text output; the filter never prints text
    return isinstance(obj, dict), obj, p


def test_fixture_set_is_complete():
    names = {p.stem for p in PAYLOADS.glob("*.json")}
    assert KEPT <= names and len(names) >= 15


@pytest.mark.parametrize("path", sorted(PAYLOADS.glob("*.json")), ids=lambda p: p.stem)
def test_filter_keeps_only_failures(path):
    kept, obj, proc = run_filter(path.read_text())
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert kept is (path.stem in KEPT)
    if kept:
        assert obj["repo"] == "acme/webapp"
        assert obj["event"] == path.stem.split("-")[0]


def test_kept_payload_is_minimal():
    kept, obj, _ = run_filter((PAYLOADS / "workflow_run-failure.json").read_text())
    assert kept
    assert obj == {
        "event": "workflow_run", "repo": "acme/webapp", "conclusion": "failure", "workflow": "CI",
        "branch": "main", "sha": "9c4d7e2a1b3f5d6e8a0b2c4d6e8f0a1b3c5d7e9f",
        "url": "https://github.com/acme/webapp/actions/runs/9003",
    }
    # free text from the payload (commit messages, emails) never reaches the model prompt
    assert "instructions" not in json.dumps(obj) and "example.com" not in json.dumps(obj)


def test_deployment_status_wins_over_embedded_workflow_run():
    # GitHub embeds the deploying workflow_run in deployment_status payloads; the deploy state decides.
    kept, obj, _ = run_filter((PAYLOADS / "deployment_status-failure-actions.json").read_text())
    assert kept and obj["event"] == "deployment_status" and obj["state"] == "failure"
    assert obj["environment"] == "Production"


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", "null", '{"workflow_run": "x"}'])
def test_garbage_is_dropped_quietly(stdin):
    kept, _, proc = run_filter(stdin)
    assert not kept and proc.returncode == 0 and proc.stdout == ""
