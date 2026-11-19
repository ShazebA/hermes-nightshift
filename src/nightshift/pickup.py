"""Consumer side of the handoff: list unclaimed briefs and claim one for a coding agent.

A claim is recorded in the brief itself (`handoff.claimed_by` / `handoff.claimed_at`), written
through `write_brief` so the JSON stays schema-valid and the Markdown is re-rendered. If the
kanban sink is on, the card gets a comment and is unblocked, through the `hermes kanban` CLI.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .brief import brief_paths, find_brief, load_brief, write_brief
from .config import Config
from .sinks.kanban import claim_card


class AlreadyClaimed(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iter_briefs(briefs_dir: Path, repo: str | None = None):
    """Every brief JSON under briefs_dir (optionally one repo's), skipping half-written files."""
    pattern = f"{repo.replace('/', '__')}/*.json" if repo else "*/*.json"
    for path in sorted(briefs_dir.glob(pattern)):
        try:
            yield path, load_brief(path)
        except (OSError, ValueError):
            continue


def open_briefs(briefs_dir: Path, repo: str | None = None) -> list[dict[str, Any]]:
    """Unclaimed briefs, newest first. Ties on created_at (one poll writes several briefs in the
    same second) fall back to the failure time, then the id, so the order is deterministic."""
    rows = []
    for path, b in iter_briefs(briefs_dir, repo):
        h = b.get("handoff") or {}
        if h.get("claimed_by"):
            continue
        rows.append({
            "id": b["id"], "repo": b["repo"], "kind": b["kind"], "title": b["title"],
            "created_at": b["created_at"],
            "failed_at": (b.get("what_broke") or {}).get("failed_at") or "",
            "category": b["classification"]["category"],
            "confidence": b["classification"]["confidence"],
            "repro": b["repro"]["command"],
            "brief_json": str(path), "brief_md": str(path.with_suffix(".md")),
            "kanban_task_id": h.get("kanban_task_id"),
        })
    rows.sort(key=lambda r: (r["created_at"], r["failed_at"], r["id"]), reverse=True)
    return rows


@dataclass
class ClaimResult:
    brief: dict[str, Any]
    brief_json: Path
    brief_md: Path
    already_yours: bool = False
    kanban: str = "disabled"  # disabled | no-card | updated | error
    notes: list[str] = field(default_factory=list)


def claim(cfg: Config, brief_id: str, agent: str, run=subprocess.run,
          now: str | None = None) -> ClaimResult:
    agent = agent.strip()
    if not agent:
        raise ValueError("--agent must not be empty")
    path = find_brief(cfg.briefs_dir, brief_id)
    b = load_brief(path)
    h = b["handoff"]
    holder = h.get("claimed_by")
    if holder and holder != agent:
        raise AlreadyClaimed(f"brief {brief_id} was claimed by {holder} at {h.get('claimed_at')}")
    if holder == agent:
        jp, mp = brief_paths(b, path.parent.parent)
        return ClaimResult(b, jp, mp, already_yours=True, kanban="skipped")
    ts = now or _now()
    h["claimed_by"], h["claimed_at"] = agent, ts
    b["updated_at"] = ts
    jp, mp = write_brief(b, path.parent.parent)
    res = ClaimResult(b, jp, mp)
    if not cfg.kanban.enabled:
        return res
    task_id = h.get("kanban_task_id")
    if not task_id:
        res.kanban = "no-card"
        res.notes.append("kanban is enabled but this brief has no card id; nothing to unblock")
        return res
    ok, detail = claim_card(cfg.kanban, task_id, agent, mp, run=run)
    res.kanban = "updated" if ok else "error"
    if detail:
        res.notes.append(detail)
    return res
