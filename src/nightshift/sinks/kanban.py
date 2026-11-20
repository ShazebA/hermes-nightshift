"""Hermes kanban sink: one card per brief, via the public `hermes kanban` CLI (no imports from
the Hermes tree, so Nightshift survives Hermes refactors).

Flags confirmed against hermes_cli/kanban_parser.py: `create <title> --body --triage
--initial-status --idempotency-key --created-by --assignee --json`, `attach <task_id> <path>`,
and the top-level `--board <slug>`.

Default card status is `blocked`: with a gateway running, Hermes auto-decomposes `triage` cards
on every board (gateway/kanban_watchers_dispatcher.py::auto_decompose_tick) into children that
always get an assignee and are then dispatched, which would let a local worker start "fixing" a
repo nobody asked it to touch. A brief is input for a human or a chosen coding agent.
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


def _base(cfg: KanbanConfig) -> list[str]:
    # --board is a top-level kanban flag, so it goes before the subcommand.
    return [cfg.hermes_bin, "kanban"] + (["--board", cfg.board] if cfg.board else [])


def claim_card(cfg: KanbanConfig, task_id: str, agent: str, md_path: Path,
               run=subprocess.run) -> tuple[bool, str]:
    """Record a claim on the card, then unblock it.

    Flags confirmed against hermes_cli/kanban_parser.py: `comment <task_id> <text...> --author`
    and `unblock [--reason] <task_ids...>`. Unblock moves the card to `ready` (or `todo` while
    parents are open); if the card has an assignee, a running gateway may dispatch it, so the
    comment says the work is happening outside Hermes.
    """
    base = _base(cfg)
    text = (f"Claimed by {agent} via `nightshift claim`. The fix is happening outside Hermes; "
            f"do not dispatch a worker for this card. Brief: {md_path}")
    steps = [("comment", base + ["comment", str(task_id), text, "--author", "nightshift"]),
             ("unblock", base + ["unblock", str(task_id)])]
    for name, argv in steps:
        try:
            proc = run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"kanban {name} failed: {e}"
        if proc.returncode != 0:
            return False, f"kanban {name} failed: {(proc.stderr or proc.stdout).strip()[:300]}"
    return True, ""


def create_card(cfg: KanbanConfig, brief: dict[str, Any], md_path: Path,
                run=subprocess.run) -> KanbanResult:
    if not cfg.enabled:
        return KanbanResult("disabled")
    base = _base(cfg)
    title = f"[nightshift] {brief['repo']}: {brief['title']}"
    args = base + ["create", title, "--body", card_body(brief, md_path),
                   "--idempotency-key", f"nightshift:{brief['repo']}:{brief['id']}",
                   "--created-by", "nightshift", "--json"]
    if cfg.status == "triage":
        args.append("--triage")
    elif cfg.status == "blocked":
        args += ["--initial-status", "blocked"]
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


def resolve_card(cfg: KanbanConfig, task_id: str, text: str,
                 run=subprocess.run) -> tuple[str, str]:
    """Tell the card its failure went green. Returns (status, detail).

    Only a card still `blocked` (nobody picked it up) is completed. A card someone unblocked,
    claimed or a worker is running gets a comment and is left alone. Flags confirmed with
    `hermes kanban show --json` (``task.status``), `comment --author` and `complete --result`.
    Status: closed | commented | error.
    """
    base = _base(cfg)

    def call(name, argv):
        try:
            proc = run(argv, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"kanban {name} failed: {e}"
        if proc.returncode != 0:
            return None, f"kanban {name} failed: {(proc.stderr or proc.stdout).strip()[:300]}"
        return proc, ""

    proc, err = call("show", base + ["show", str(task_id), "--json"])
    if proc is None:
        return "error", err
    try:
        shown = json.loads(proc.stdout)
        status = (shown.get("task") or shown).get("status")
    except (json.JSONDecodeError, AttributeError):
        return "error", f"unparseable kanban show output: {proc.stdout[:200]}"
    proc, err = call("comment", base + ["comment", str(task_id), text, "--author", "nightshift"])
    if proc is None:
        return "error", err
    if status != "blocked":
        return "commented", f"card is {status}; left open"
    proc, err = call("complete", base + ["complete", str(task_id), "--result", text[:300]])
    return ("closed", "") if proc is not None else ("error", err)
