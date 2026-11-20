"""Opt-in sandboxed repro: check out the first-red SHA into a throwaway directory and run the
brief's repro command inside a disposable Docker container.

This deliberately does not depend on Hermes's terminal backend setting (switching
`terminal.backend` to docker is a global change to the user's agent). When the user HAS set
`terminal.backend: docker`, the nightshift-triage skill can instead run the same command
through Hermes's `terminal` tool; both paths record their result with `nightshift repro`.
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import RepoConfig, SandboxConfig
from .triage import ANSI


def docker_argv(cfg: SandboxConfig, image: str, workdir: Path, command: str) -> list[str]:
    return ["docker", "run", "--rm", f"--network={cfg.network}", "--cpus=2", "--memory=4g",
            "-v", f"{workdir}:/work", "-w", "/work", image, "sh", "-lc", command]


def strip_checkout(command: str) -> tuple[str | None, str]:
    """Split ``git checkout <sha> && rest`` into (sha, rest): the checkout happens on the host."""
    head, sep, rest = command.partition(" && ")
    parts = head.split()
    if sep and len(parts) == 3 and parts[:2] == ["git", "checkout"]:
        return parts[2], rest
    return None, command


def run_repro(brief: dict[str, Any], repo: RepoConfig, cfg: SandboxConfig,
              run=subprocess.run) -> dict[str, Any]:
    sha, inner = strip_checkout(brief["repro"]["command"])
    if sha is None:
        raise ValueError("repro command does not start with `git checkout <sha> && `; "
                         "it is not a local repro")
    source = repo.path or f"https://github.com/{repo.name}.git"
    image = repo.sandbox_image or cfg.image
    with tempfile.TemporaryDirectory(prefix="nightshift-repro-") as tmp:
        wd = Path(tmp) / "src"
        for argv in (["git", "clone", "--quiet", "--no-checkout", source, str(wd)],
                     ["git", "-C", str(wd), "checkout", "--quiet", sha]):
            p = run(argv, capture_output=True, text=True, timeout=600)
            if p.returncode != 0:
                raise RuntimeError(f"{shlex.join(argv)} failed: {p.stderr.strip()[:300]}")
        try:
            # One merged stream keeps test output and npm noise in their real order.
            p = run(docker_argv(cfg, image, wd, inner), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=cfg.timeout_seconds)
            code, out = p.returncode, p.stdout or ""
        except subprocess.TimeoutExpired as e:
            code, out = None, f"timed out after {cfg.timeout_seconds}s\n{e.stdout or ''}"
    return {
        "image": image,
        "exit_code": code,
        # A repro "reproduces" a red build when the command fails the same way CI did.
        "reproduced": None if code is None else code != 0,
        "ran_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "output_tail": [ANSI.sub("", ln) for ln in str(out).splitlines() if ln.strip()][-40:],
    }
