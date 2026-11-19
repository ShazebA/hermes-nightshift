"""Hermes kanban sink: one card per brief, via the public `hermes kanban` CLI (no imports from
the Hermes tree, so Nightshift survives Hermes refactors).

Flags confirmed against hermes_cli/kanban_parser.py: `create <title> --body --triage
--idempotency-key --created-by --assignee --json`, `attach <task_id> <path>`, and the
top-level `--board <slug>`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import KanbanConfig


@dataclass
class KanbanResult:
    status: str  # created | exists | disabled | error
    task_id: str | None = None
    detail: str = ""


def card_body(brief: dict[str, Any], md_path: Path) -> str:
    c = brief["classification"]
    return (
        f"{brief['what_broke']['summary']}\n\n"
        f"Classification: {c['category']} ({c['confidence']:.2f}, {c['source']})\n"
        f"Repro: `{brief['repro']['command']}`\n"
        f"Brief: {md_path}\n\n"
        f"{brief['handoff']['prompt']}"
    )


def create_card(cfg: KanbanConfig, brief: dict[str, Any], md_path: Path,
                run=subprocess.run) -> KanbanResult:
    if not cfg.enabled:
        return KanbanResult("disabled")
    base = [cfg.hermes_bin, "kanban"] + (["--board", cfg.board] if cfg.board else [])
    title = f"[nightshift] {brief['repo']}: {brief['title']}"
    args = base + ["create", title, "--body", card_body(brief, md_path),
                   "--idempotency-key", f"nightshift:{brief['repo']}:{brief['id']}",
                   "--created-by", "nightshift", "--json"]
    if cfg.triage:
        args.append("--triage")
    if cfg.assignee:
        args += ["--assignee", cfg.assignee]
    try:
        proc = run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return KanbanResult("error", detail=str(e))
    if proc.returncode != 0:
        return KanbanResult("error", detail=(proc.stderr or proc.stdout).strip()[:500])
    try:
        task = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return KanbanResult("error", detail=f"unparseable kanban output: {proc.stdout[:200]}")
    task_id = task.get("id") or task.get("task_id")
    att = run(base + ["attach", str(task_id), str(md_path)], capture_output=True, text=True,
              timeout=60)
    detail = "" if att.returncode == 0 else f"attach failed: {att.stderr.strip()[:200]}"
    return KanbanResult("created", task_id=str(task_id), detail=detail)
