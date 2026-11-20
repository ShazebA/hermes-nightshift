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
RECOVERED = FIXTURES / "recovered"
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
        self.status: dict[str, str] = {}

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if "create" in argv:
            self.n += 1
            tid = f"t_{self.n:04d}"
            self.status[tid] = "blocked"
            out = json.dumps({"id": tid, "status": "blocked"})
        elif "show" in argv:
            tid = argv[argv.index("show") + 1]
            out = json.dumps({"task": {"id": tid, "status": self.status.get(tid, "blocked")}})
        else:
            if "complete" in argv:
                self.status[argv[argv.index("complete") + 1]] = "done"
            if "unblock" in argv:
                self.status[argv[-1]] = "ready"
            out = "ok"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture
def fake_hermes():
    return FakeHermes()
