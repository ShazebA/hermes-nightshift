from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nightshift.config import parse_config
from nightshift.gh import FixtureGh

FIXTURES = Path(__file__).parent / "fixtures"
RED_MAIN = FIXTURES / "red-main"
NOW = datetime(2026, 11, 19, 7, 0, tzinfo=UTC)
REPO = "acme/webapp"


@pytest.fixture
def gh():
    return FixtureGh(RED_MAIN)


@pytest.fixture
def cfg(tmp_path):
    def make(**overrides):
        data = {
            "state_dir": str(tmp_path / "state"),
            "repos": [{
                "name": REPO,
                "repro_setup": "npm ci",
                "build_command": "npm run build",
                "lessons": [{"pattern": r"(column|relation) \S+ does not exist",
                             "note": "Supabase migrations are not applied in CI."}],
            }],
            "kanban": {"enabled": False},
        }
        data.update(overrides)
        return parse_config(data, tmp_path)
    return make


class FakeHermes:
    """Stands in for `hermes kanban ...` and records argv."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.n = 0

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if "create" in argv:
            self.n += 1
            out = json.dumps({"id": f"t_{self.n:04d}", "status": "triage"})
        else:
            out = "ok"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture
def fake_hermes():
    return FakeHermes()
