"""Thin wrapper around the `gh` CLI, plus a fixture replayer for tests and dry runs.

Every GitHub read goes through ``Gh.text``/``Gh.json`` so a recorded fixture set can stand in
for the network. Fixture keys are the command with the repo slug replaced by ``{repo}`` (and
owner/name by ``{owner}``/``{name}``), so one fixture set replays against any configured repo.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import yaml


class GhError(RuntimeError):
    pass


class Gh:
    def __init__(self, binary: str = "gh", timeout: int = 120):
        self.binary = binary
        self.timeout = timeout

    def text(self, args: list[str], repo: str) -> str:
        proc = subprocess.run(
            [self.binary, *args], capture_output=True, text=True, timeout=self.timeout
        )
        if proc.returncode != 0:
            raise GhError(f"gh {shlex.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
        return proc.stdout

    def json(self, args: list[str], repo: str) -> Any:
        out = self.text(args, repo)
        return json.loads(out) if out.strip() else None


def fixture_key(args: list[str], repo: str) -> str:
    owner, name = repo.split("/", 1)
    # GraphQL documents are long and noisy; key them by a placeholder.
    args = ["query=<graphql>" if a.startswith("query=") else a for a in args]
    key = shlex.join(args)
    return key.replace(repo, "{repo}").replace(f"owner={owner}", "owner={owner}").replace(
        f"name={name}", "name={name}"
    )


class FixtureGh(Gh):
    """Replays recorded `gh` output. ``manifest.yaml`` maps fixture keys to files.

    Fixture files may contain ``{repo}``/``{owner}``/``{name}`` placeholders, which are filled
    with the repo being polled so one scenario replays against any configured repo."""

    def __init__(self, fixture_dir: str | Path):
        super().__init__()
        self.dir = Path(fixture_dir)
        manifest = yaml.safe_load((self.dir / "manifest.yaml").read_text()) or {}
        self.map: dict[str, str] = manifest.get("commands", {})
        self.calls: list[str] = []

    def text(self, args: list[str], repo: str) -> str:
        key = fixture_key(args, repo)
        self.calls.append(key)
        if key not in self.map:
            raise GhError(f"no fixture recorded for: gh {key}")
        owner, name = repo.split("/", 1)
        text = (self.dir / self.map[key]).read_text()
        return text.replace("{repo}", repo).replace("{owner}", owner).replace("{name}", name)
