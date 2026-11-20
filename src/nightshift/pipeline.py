"""One pass of the night shift: poll → triage → brief → handoff → ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .brief import build_brief, load_brief, write_brief
from .config import Config
from .detect import detect_repo
from .gh import Gh
from .models import FAILURE_KINDS, Incident
from .resolve import find_resolution
from .sinks.aetherbus import publish_brief
from .sinks.kanban import create_card, resolve_card
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
class Resolved:
    repo: str
    id: str
    title: str
    how: str  # fix | rerun
    sha: str
    url: str | None
    kanban: str = "n/a"  # closed | commented | error | n/a
    brief_md: Path | None = None


@dataclass
class Report:
    handled: list[Handled] = field(default_factory=list)
    skipped_seen: int = 0
    errors: list[str] = field(default_factory=list)
    republished: list[str] = field(default_factory=list)
    resolved: list[Resolved] = field(default_factory=list)
    watchers: int = 0  # watcher calls attempted (one per repo x kind, CI once per branch)

    @property
    def blind(self) -> bool:
        """Every watcher failed (expired gh auth, rate limit, outage): nothing was observed."""
        return self.watchers > 0 and len(self.errors) >= self.watchers

    @property
    def new_failures(self) -> list[Handled]:
        return [h for h in self.handled if h.incident.is_failure]


def run_once(cfg: Config, gh: Gh, *, dry_run: bool = False, briefs_dir: Path | None = None,
             now: datetime | None = None, kanban_run=None) -> Report:
    """``dry_run`` writes briefs (to ``briefs_dir``) but touches no state and no sinks."""
    state = State(cfg.state_dir)
    out_dir = briefs_dir or cfg.briefs_dir
    report = Report()
    for repo in cfg.repos:
        observed: dict[str, Any] = {}
        incidents, errors = detect_repo(gh, repo, cfg.lookback_runs, now, observed=observed)
        report.errors += errors
        report.watchers += sum(len(repo.branches) if k == "ci" else 1 for k in repo.watch)
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
            _resolve_open(cfg, state, repo.name, observed, report, now, kanban_run)
    if not dry_run:
        # After resolving, so a brief that went green while the broker was down is never
        # announced; briefs first tried in this pass wait for the next one.
        fresh = {State.key(h.incident.repo, h.incident.id) for h in report.handled}
        _retry_pending(cfg, state, report, skip=fresh)
        stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for e in report.errors:  # surfaced by the digest so a broken watcher can't go unnoticed
            state.append_ledger({"id": "watch-error", "repo": e.split(" ", 1)[0], "kind": "watch_error",
                                 "title": e, "url": "", "detected_at": stamp})
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


def _retry_pending(cfg: Config, state: State, report: Report, skip: set[str] = frozenset()) -> None:
    for key, rec in state.pending_publish():
        if key in skip:
            continue
        if rec.get("resolved"):
            # Went green before the broker came back: announcing it now would look like new work.
            rec["aetherbus"] = "skipped-resolved"
            continue
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


def _resolve_open(cfg: Config, state: State, repo: str, observed: dict[str, Any], report: Report,
                  now: datetime | None, kanban_run=None) -> None:
    """Mark open failures of ``repo`` that have gone green: state, brief, ledger row, card."""
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefix = f"{repo}#"
    for key, rec in state.seen.items():
        if not key.startswith(prefix) or rec.get("kind") not in FAILURE_KINDS or rec.get("resolved"):
            continue
        path = Path(rec.get("brief_json") or "")
        if not path.is_file():
            continue
        b = load_brief(path)
        res = find_resolution(b, observed)
        if not res:
            continue
        res = {"resolved_at": stamp, **res}
        b["resolution"] = res
        b["updated_at"] = stamp
        _, md = write_brief(b, path.parent.parent)
        r = Resolved(repo, b["id"], b["title"], res["how"], res["sha"], res.get("url"), brief_md=md)
        task_id = b["handoff"].get("kanban_task_id")
        if cfg.kanban.enabled and task_id:
            what = ("fixed by commit " if res["how"] == "fix" else
                    "went green on a re-run of the same commit (no code change) at ") + res["sha"][:7]
            kw = {"run": kanban_run} if kanban_run else {}
            text = f"Nightshift: {b['title']} {what}. {res.get('url') or ''}".strip()
            r.kanban, detail = resolve_card(cfg.kanban, task_id, text,
                                            claimed=bool(b["handoff"].get("claimed_by")), **kw)
            if detail:
                rec["kanban_resolution_detail"] = detail
        rec.update(resolved=stamp, resolution=res["how"], resolved_sha=res["sha"], kanban_resolution=r.kanban)
        state.append_ledger({"id": b["id"], "repo": repo, "kind": "resolved", "title": b["title"],
                             "url": res.get("url") or "", "detected_at": stamp,
                             "failure_kind": b["kind"], "how": res["how"], "sha": res["sha"],
                             "brief_md": str(md), "kanban_task_id": task_id})
        report.resolved.append(r)
