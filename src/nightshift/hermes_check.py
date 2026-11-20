"""Health checks for the Hermes side of a Nightshift install, used by `nightshift doctor`.

Everything goes through the public `hermes` CLI or files Hermes documents for users
(`$HERMES_HOME/config.yaml`, `$HERMES_HOME/skills/`), never Hermes's Python modules. The CLI
text matched here (`will NOT fire`, `Name:`) comes from hermes_cli/cron.py.
"""

from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

import yaml

JOBS = ("nightshift-watch", "nightshift-digest")
Check = tuple[str, bool, str]


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _cli(run: Callable, hermes_bin: str, *args: str) -> str:
    try:
        p = run([hermes_bin, *args], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"<failed: {e}>"
    return (p.stdout or "") + (p.stderr or "")


def model_server_up(base_url: str, timeout: float = 3) -> bool:
    """Any HTTP answer counts as up (a managed llama-server answers 401 without its key)."""
    try:
        urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=timeout)
        return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def hermes_checks(hermes_bin: str = "hermes", home: Path | None = None, run: Callable = subprocess.run,
                  probe: Callable[[str], bool] = model_server_up) -> list[Check]:
    home = home or hermes_home()
    checks: list[Check] = []

    status = _cli(run, hermes_bin, "cron", "status")
    checks.append(("Hermes scheduler running (gateway up)",
                   "will NOT fire" not in status and not status.startswith("<failed"),
                   "cron jobs only fire while the gateway runs: `hermes gateway install` "
                   "(login service) or open the Hermes desktop app"))

    listing = _cli(run, hermes_bin, "cron", "list")
    for job in JOBS:
        checks.append((f"cron job {job} registered", bool(re.search(rf"Name:\s+{job}\b", listing)),
                       "run scripts/install-hermes.sh and the `hermes cron create` lines it prints"))

    skill = list((home / "skills").glob("*/nightshift-triage/SKILL.md"))
    checks.append(("skill nightshift-triage installed", bool(skill), "scripts/install-hermes.sh --apply"))

    try:
        model = (yaml.safe_load((home / "config.yaml").read_text()) or {}).get("model") or {}
    except OSError:
        model = {}
    base_url = model.get("base_url")
    if base_url:
        checks.append((f"model server reachable ({base_url})", probe(base_url),
                       "the triage model is down; for Hermes's managed llama.cpp runtime, open the "
                       "desktop app (it boots the server). Briefs are still written without it."))
    return checks
