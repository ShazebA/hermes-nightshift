"""Config file loading. One YAML file lists the repos and the sinks."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATHS = ("nightshift.yaml", "~/.config/nightshift/nightshift.yaml")
DEFAULT_TOPIC = "dev.aetherbus.nightshift.brief.v1"
KANBAN_STATUSES = ("blocked", "triage", "ready")
WATCH_KINDS = ("ci", "deployments", "issues", "pull_requests", "stale_branches")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    pass


@dataclass
class Lesson:
    pattern: str
    note: str
    id: str = ""
    source: str = "config"


@dataclass
class RepoConfig:
    name: str
    branches: list[str] = field(default_factory=lambda: ["main"])
    watch: list[str] = field(default_factory=lambda: list(WATCH_KINDS))
    path: str | None = None  # local clone, used for sandbox repro
    repro_setup: str | None = None  # e.g. "npm ci"
    build_command: str | None = None  # used to reproduce failed deployments
    sandbox_image: str | None = None
    stale_branch_days: int = 30
    lessons: list[Lesson] = field(default_factory=list)
    # Workflow names or shell-style globs (case-insensitive) whose failures are not briefed,
    # e.g. "Dependabot Updates" or "Diagnose *".
    ignore_workflows: list[str] = field(default_factory=list)

    def ignores(self, workflow: str) -> bool:
        return any(fnmatch.fnmatchcase(workflow.lower(), p.lower()) for p in self.ignore_workflows)

    @property
    def slug(self) -> str:
        return self.name.replace("/", "__")


@dataclass
class KanbanConfig:
    enabled: bool = True
    board: str | None = None
    hermes_bin: str = "hermes"
    assignee: str | None = None
    # blocked: waits for a human (safe default). triage: Hermes's gateway auto-decomposes triage
    # cards (kanban.auto_decompose, default on) into assigned child tasks that get dispatched.
    # ready: dispatched as soon as kanban.default_assignee or `assignee` is set.
    status: str = "blocked"


@dataclass
class AetherBusConfig:
    enabled: bool = False
    url: str = "nats://localhost:4222"
    topic: str = DEFAULT_TOPIC
    source: str = "aetherbus://nightshift"


@dataclass
class SandboxConfig:
    enabled: bool = False
    image: str = "node:22-bookworm"
    network: str = "bridge"
    timeout_seconds: int = 900


@dataclass
class Config:
    repos: list[RepoConfig]
    state_dir: Path
    briefs_dir: Path
    kanban: KanbanConfig = field(default_factory=KanbanConfig)
    aetherbus: AetherBusConfig = field(default_factory=AetherBusConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    lookback_runs: int = 30
    source_path: Path | None = None

    def repo(self, name: str) -> RepoConfig:
        for r in self.repos:
            if r.name == name:
                return r
        known = ", ".join(r.name for r in self.repos)
        where = f" ({self.source_path})" if self.source_path else ""
        raise ConfigError(f"repo {name!r} is not in the config{where}; configured repos: {known}")


def _expand(p: str, base: Path) -> Path:
    path = Path(os.path.expanduser(os.path.expandvars(p)))
    return path if path.is_absolute() else (base / path).resolve()


def _lessons(raw: Any) -> list[Lesson]:
    out = []
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict) or "pattern" not in item or "note" not in item:
            raise ConfigError(f"lessons[{i}] needs 'pattern' and 'note'")
        re.compile(item["pattern"])  # fail early on bad regex
        out.append(Lesson(pattern=item["pattern"], note=item["note"], id=item.get("id", f"cfg-{i}")))
    return out


def parse_config(data: dict[str, Any], base: Path) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    repos_raw = data.get("repos") or []
    if not repos_raw:
        raise ConfigError("config needs at least one entry under 'repos'")
    repos = []
    for i, r in enumerate(repos_raw):
        if isinstance(r, str):
            r = {"name": r}
        name = r.get("name", "")
        if not _REPO_RE.match(name):
            raise ConfigError(f"repos[{i}].name must look like owner/repo, got {name!r}")
        watch = r.get("watch", list(WATCH_KINDS))
        bad = set(watch) - set(WATCH_KINDS)
        if bad:
            raise ConfigError(f"repos[{i}].watch has unknown kinds {sorted(bad)}; use {WATCH_KINDS}")
        ignore = r.get("ignore_workflows") or []
        if not isinstance(ignore, list) or not all(isinstance(x, str) and x.strip() for x in ignore):
            raise ConfigError(f"repos[{i}].ignore_workflows must be a list of workflow names or globs")
        repos.append(
            RepoConfig(
                name=name,
                branches=list(r.get("branches", ["main"])),
                watch=list(watch),
                path=str(_expand(r["path"], base)) if r.get("path") else None,
                repro_setup=r.get("repro_setup"),
                build_command=r.get("build_command"),
                sandbox_image=r.get("sandbox_image"),
                stale_branch_days=int(r.get("stale_branch_days", 30)),
                lessons=_lessons(r.get("lessons")),
                ignore_workflows=[x.strip() for x in ignore],
            )
        )
    k = data.get("kanban") or {}
    if k.get("status", "blocked") not in KANBAN_STATUSES:
        raise ConfigError(f"kanban.status must be one of {KANBAN_STATUSES}")
    a = data.get("aetherbus") or {}
    s = data.get("sandbox") or {}
    state_dir = _expand(data.get("state_dir", "~/.local/share/nightshift"), base)
    return Config(
        repos=repos,
        state_dir=state_dir,
        briefs_dir=_expand(data["briefs_dir"], base) if data.get("briefs_dir") else state_dir / "briefs",
        kanban=KanbanConfig(
            enabled=bool(k.get("enabled", True)),
            board=k.get("board"),
            hermes_bin=k.get("hermes_bin", "hermes"),
            assignee=k.get("assignee"),
            status=k.get("status", "blocked"),
        ),
        aetherbus=AetherBusConfig(
            enabled=bool(a.get("enabled", False)),
            url=a.get("url", "nats://localhost:4222"),
            topic=a.get("topic", DEFAULT_TOPIC),
            source=a.get("source", "aetherbus://nightshift"),
        ),
        sandbox=SandboxConfig(
            enabled=bool(s.get("enabled", False)),
            image=s.get("image", "node:22-bookworm"),
            network=s.get("network", "bridge"),
            timeout_seconds=int(s.get("timeout_seconds", 900)),
        ),
        lookback_runs=int(data.get("lookback_runs", 30)),
    )


def load_config(path: str | None = None) -> Config:
    candidates = [path] if path else [os.environ.get("NIGHTSHIFT_CONFIG"), *DEFAULT_CONFIG_PATHS]
    for c in candidates:
        if not c:
            continue
        p = Path(os.path.expanduser(c))
        if p.is_file():
            cfg = parse_config(yaml.safe_load(p.read_text()) or {}, p.parent.resolve())
            cfg.source_path = p.resolve()
            return cfg
    tried = ", ".join(str(c) for c in candidates if c)
    raise ConfigError(f"no config file found (tried: {tried}); copy config.example.yaml")
