"""One pass of the night shift: poll → triage → brief → handoff → ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .brief import build_brief, load_brief, write_brief
from .config import Config
from .detect import detect_repo
from .gh import Gh
from .models import Incident
from .sinks.aetherbus import publish_brief
from .sinks.kanban import create_card
from .state import State, load_lessons
from .triage import triage


@dataclass
class Handled:
    incident: Incident
    brief_json: Path | None = None
    brief_md: Path | None = None
    brief: dict[str, Any] | None = None
    kanban: str = "n/a"
    kanban_task_id: str | None = None
    aetherbus: str = "n/a"
    aetherbus_event_id: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    handled: list[Handled] = field(default_factory=list)
    skipped_seen: int = 0
    errors: list[str] = field(default_factory=list)
    republished: list[str] = field(default_factory=list)

    @property
    def new_failures(self) -> list[Handled]:
        return [h for h in self.handled if h.incident.is_failure]


def run_once(cfg: Config, gh: Gh, *, dry_run: bool = False, briefs_dir: Path | None = None,
             now: datetime | None = None, kanban_run=None) -> Report:
    """``dry_run`` writes briefs (to ``briefs_dir``) but touches no state and no sinks."""
    state = State(cfg.state_dir)
    out_dir = briefs_dir or cfg.briefs_dir
    report = Report()
    if not dry_run:
        _retry_pending(cfg, state, report)
    for repo in cfg.repos:
        incidents, errors = detect_repo(gh, repo, cfg.lookback_runs, now)
        report.errors += errors
        lessons = repo.lessons + load_lessons(cfg.state_dir, repo.name)
        for inc in incidents:
            if not dry_run and state.is_seen(inc.repo, inc.id):
                report.skipped_seen += 1
                continue
            h = Handled(inc)
            if inc.is_failure:
                t = triage(gh, inc, repo, lessons)
                b = build_brief(inc, t, now.strftime("%Y-%m-%dT%H:%M:%SZ") if now else None)
                h.brief_json, h.brief_md = write_brief(b, out_dir)
                h.brief = b
                if not dry_run:
                    _handoff(cfg, h, kanban_run)
            report.handled.append(h)
            if not dry_run:
                state.mark(inc.repo, inc.id, kind=inc.kind, brief_json=_s(h.brief_json),
                           kanban_task_id=h.kanban_task_id, aetherbus=h.aetherbus,
                           aetherbus_event_id=h.aetherbus_event_id)
                state.append_ledger({
                    **{k: getattr(inc, k) for k in ("id", "repo", "kind", "title", "url",
                                                    "detected_at")},
                    "classification": (h.brief or {}).get("classification"),
                    "brief_json": _s(h.brief_json), "brief_md": _s(h.brief_md),
                    "kanban_task_id": h.kanban_task_id,
                })
    if not dry_run:
        state.save()
    return report


def _s(p: Path | None) -> str | None:
    return str(p) if p else None


def _handoff(cfg: Config, h: Handled, kanban_run=None) -> None:
    b = h.brief
    assert b is not None and h.brief_md is not None and h.brief_json is not None
    kw = {"run": kanban_run} if kanban_run else {}
    k = create_card(cfg.kanban, b, h.brief_md, **kw)
    h.kanban, h.kanban_task_id = k.status, k.task_id
    if k.detail:
        h.notes.append(f"kanban: {k.detail}")
    b["handoff"]["kanban_task_id"] = k.task_id
    # Rewrite the JSON so the event's dataref resolves to a brief that names its card.
    write_brief(b, h.brief_json.parent.parent)
    p = publish_brief(cfg.aetherbus, h.brief_json, b, cfg.state_dir)
    h.aetherbus, h.aetherbus_event_id = p.status, p.event_id
    if p.detail:
        h.notes.append(f"aetherbus: {p.detail}")
    if p.event_id:
        b["handoff"]["aetherbus_event_id"] = p.event_id
        write_brief(b, h.brief_json.parent.parent)


def _retry_pending(cfg: Config, state: State, report: Report) -> None:
    for key, rec in state.pending_publish():
        path = Path(rec.get("brief_json") or "")
        if not path.is_file():
            rec["aetherbus"] = "error"
            continue
        b = load_brief(path)
        p = publish_brief(cfg.aetherbus, path, b, cfg.state_dir)
        if p.status == "published":
            rec.update(aetherbus="published", aetherbus_event_id=p.event_id)
            b["handoff"]["aetherbus_event_id"] = p.event_id
            write_brief(b, path.parent.parent)
            report.republished.append(key)
