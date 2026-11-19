"""Brief: the fixed-schema handoff document (``nightshift.brief/v1``).

JSON is canonical and is what the AetherBus ``dataref`` points at; the Markdown file is a
deterministic rendering of the same data for humans and for coding agents that prefer prose.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import BRIEF_SCHEMA_ID
from .models import Incident
from .triage import Triage, normalize

CATEGORIES = ("flaky", "real", "infra", "unknown")
SOURCES = ("heuristic", "model", "human")
SCHEMA_PATH = Path(__file__).parent / "schema" / "brief.v1.schema.json"


def confidence_label(c: float) -> str:
    return "high" if c >= 0.75 else "medium" if c >= 0.5 else "low"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_brief(incident: Incident, t: Triage, now: str | None = None) -> dict[str, Any]:
    d = incident.data
    ci = incident.kind == "ci_failure"
    first_red = d.get("first_red") or {}
    last_green = d.get("last_green") or {}
    cls = dict(t.classification)
    cls["label"] = confidence_label(cls["confidence"])
    where = d.get("workflow") if ci else f"deploy to {d.get('environment')}"
    summary = (f"{where} is red on {d.get('branch') or d.get('ref')}"
               + (f" ({d.get('red_streak')} failing run(s) in a row)" if ci else "")
               + (f": {t.digest.signature.rstrip('.')}" if t.digest.signature else "")
               + (f" ({t.digest.error.rstrip('.')})" if t.digest.error
                  and normalize(t.digest.error) not in t.digest.signature else "")) + "."
    brief: dict[str, Any] = {
        "schema": BRIEF_SCHEMA_ID,
        "id": incident.id,
        "repo": incident.repo,
        "kind": incident.kind,
        "title": incident.title,
        "created_at": now or _now(),
        "classification": cls,
        "what_broke": {
            "summary": summary,
            "workflow": d.get("workflow"),
            "environment": d.get("environment"),
            "job": t.digest.job or None,
            "step": t.digest.step or None,
            "branch": d.get("branch") or d.get("ref"),
            "run_id": (d.get("latest_red") or {}).get("databaseId") if ci else None,
            "run_url": incident.url,
            "failed_at": (d.get("latest_red") or {}).get("createdAt") if ci else d.get("created_at"),
            "red_streak": d.get("red_streak") if ci else None,
            "signature": t.digest.signature,
            "error": t.digest.error or None,
            "test_target": t.digest.test_target or None,
            "log_excerpt": t.digest.excerpt,
        },
        "repro": {"command": t.repro, "verified": False, "sandbox": None},
        "culprit": {
            "last_green_sha": last_green.get("headSha") if ci else d.get("last_success_sha"),
            "last_green_url": last_green.get("url") if ci else None,
            "first_red_sha": first_red.get("headSha") if ci else d["sha"],
            "first_red_url": first_red.get("url") if ci else incident.url,
            "compare_url": t.compare_url,
            "commits": t.commits,
            "suspected_files": t.suspected,
        },
        "lessons": t.lessons,
        "links": _links(incident, t),
        "handoff": {"prompt": "", "kanban_task_id": None, "aetherbus_event_id": None},
    }
    if t.errors:
        brief["errors"] = t.errors
    return brief


def _links(incident: Incident, t: Triage) -> list[dict[str, str]]:
    links = [{"label": "Failing run" if incident.kind == "ci_failure" else "Deployment log",
              "url": incident.url}]
    lg = (incident.data.get("last_green") or {}).get("url")
    if lg:
        links.append({"label": "Last green run", "url": lg})
    if t.compare_url:
        links.append({"label": "Culprit range (compare)", "url": t.compare_url})
    return links


def handoff_prompt(brief: dict[str, Any], md_path: Path) -> str:
    cat = brief["classification"]["category"]
    caution = {
        "flaky": "It looks flaky: confirm it reproduces before changing code, and prefer "
                 "stabilising the test over changing behaviour.",
        "infra": "It looks like infrastructure, not code: confirm before changing anything in "
                 "the repo.",
    }.get(cat, "Fix the root cause with the smallest change and add or adjust a test.")
    return (f"Fix the {brief['kind'].replace('_', ' ')} in {brief['repo']} described in the "
            f"Nightshift brief at {md_path}. Start by running the repro command from the brief. "
            f"{caution} Do not touch unrelated files.")


# ------------------------------------------------------------------ validation


def validate(brief: dict[str, Any]) -> list[str]:
    """Dependency-free check of the parts of the schema that consumers rely on. The JSON Schema
    file is the full contract; tests check that both agree."""
    errs = []
    req = ["schema", "id", "repo", "kind", "title", "created_at", "classification",
           "what_broke", "repro", "culprit", "lessons", "links", "handoff"]
    errs += [f"missing {k}" for k in req if k not in brief]
    if errs:
        return errs
    if brief["schema"] != BRIEF_SCHEMA_ID:
        errs.append(f"schema must be {BRIEF_SCHEMA_ID}")
    if brief["kind"] not in ("ci_failure", "deploy_failure"):
        errs.append("kind must be ci_failure or deploy_failure")
    c = brief["classification"]
    if c.get("category") not in CATEGORIES:
        errs.append(f"classification.category must be one of {CATEGORIES}")
    if not isinstance(c.get("confidence"), int | float) or not 0 <= c["confidence"] <= 1:
        errs.append("classification.confidence must be a number in [0, 1]")
    if c.get("source") not in SOURCES:
        errs.append(f"classification.source must be one of {SOURCES}")
    if len(c.get("rationale", "")) > 400:
        errs.append("classification.rationale must be at most 400 characters")
    if not brief["repro"].get("command"):
        errs.append("repro.command is empty")
    if not brief["culprit"].get("first_red_sha"):
        errs.append("culprit.first_red_sha is empty")
    claimed_by = brief["handoff"].get("claimed_by")
    if claimed_by is not None and (not isinstance(claimed_by, str) or not claimed_by):
        errs.append("handoff.claimed_by must be a non-empty string or null")
    return errs


# ------------------------------------------------------------------ rendering


def render_markdown(b: dict[str, Any]) -> str:
    c, w, r, cu = b["classification"], b["what_broke"], b["repro"], b["culprit"]
    out = [
        f"# {b['title']}",
        "",
        f"<!-- {b['schema']} id={b['id']} -->",
        "",
        "| Field | Value |", "|---|---|",
        f"| Repo | `{b['repo']}` |",
        f"| Kind | {b['kind']} |",
        f"| Classification | **{c['category']}** ({c.get('label', confidence_label(c['confidence']))}"
        f", {c['confidence']:.2f}, {c['source']}) |",
        f"| Branch | `{w.get('branch')}` |",
        f"| Last green | {_sha(cu.get('last_green_sha'))} |",
        f"| First red | {_sha(cu.get('first_red_sha'))} |",
        f"| Created | {b['created_at']} |",
        "",
        "## What broke", "", w["summary"], "",
    ]
    for label, key in (("Workflow", "workflow"), ("Environment", "environment"), ("Job", "job"),
                       ("Step", "step"), ("Failing test", "test_target"), ("Error", "error"),
                       ("Run", "run_url")):
        if w.get(key):
            out.append(f"- {label}: `{w[key]}`" if key != "run_url" else f"- {label}: {w[key]}")
    out += ["", "**Why this classification:** " + c["rationale"], ""]
    if w["log_excerpt"]:
        out += ["### Log excerpt", "", "```text", *w["log_excerpt"], "```", ""]
    out += ["## Repro", "", "```bash", r["command"], "```", ""]
    sb = r.get("sandbox")
    if sb:
        out.append(f"Sandbox ({sb.get('image')}): exit {sb.get('exit_code')}, "
                   f"reproduced={sb.get('reproduced')} at {sb.get('ran_at')}.")
    else:
        out.append("Not yet run in a sandbox (`nightshift repro " + b["id"] + " --execute`).")
    out += ["", "## Suspected culprit", ""]
    if cu["commits"]:
        out.append("Commits between last green and first red:")
        out += [f"- `{x['sha'][:7]}` {x['title']} ({x.get('author') or '?'})" for x in cu["commits"]]
    else:
        out.append("No commit range available (no earlier green run in the lookback window).")
    if cu["suspected_files"]:
        out += ["", "Suspected files:"]
        out += [f"- `{f['path']}`: {f['reason']}" for f in cu["suspected_files"]]
    out += ["", "## Known lessons for this repo", ""]
    out += [f"- {le['note']} (`{le['id']}`)" for le in b["lessons"]] or ["- None matched."]
    out += ["", "## Links", ""] + [f"- [{x['label']}]({x['url']})" for x in b["links"]]
    out += ["", "## Handoff", "", b["handoff"]["prompt"] or "(filled in when the brief is written)"]
    if b["handoff"].get("claimed_by"):
        out += ["", f"Claimed by `{b['handoff']['claimed_by']}` at {b['handoff'].get('claimed_at')}."]
    if b.get("errors"):
        out += ["", "## Triage gaps", ""] + [f"- {e}" for e in b["errors"]]
    return "\n".join(out) + "\n"


def _sha(s: str | None) -> str:
    return f"`{s[:7]}`" if s else "unknown"


# ------------------------------------------------------------------ files


def brief_paths(brief: dict[str, Any], briefs_dir: Path) -> tuple[Path, Path]:
    base = briefs_dir / brief["repo"].replace("/", "__") / brief["id"]
    return base.with_suffix(".json"), base.with_suffix(".md")


def write_brief(brief: dict[str, Any], briefs_dir: Path) -> tuple[Path, Path]:
    errs = validate(brief)
    if errs:
        raise ValueError("brief failed validation: " + "; ".join(errs))
    jp, mp = brief_paths(brief, briefs_dir)
    jp.parent.mkdir(parents=True, exist_ok=True)
    brief["handoff"]["brief_path"] = str(mp)
    if not brief["handoff"]["prompt"]:
        brief["handoff"]["prompt"] = handoff_prompt(brief, mp)
    md = render_markdown(brief)
    payload = json.dumps(brief, indent=2, sort_keys=False) + "\n"
    # Write-then-rename so a consumer resolving the dataref never sees a half-written file.
    for path, text in ((mp, md), (jp, payload)):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text)
        tmp.replace(path)
    return jp, mp


def load_brief(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def find_brief(briefs_dir: Path, brief_id: str) -> Path:
    hits = sorted(briefs_dir.glob(f"*/{brief_id}.json"))
    if not hits:
        raise FileNotFoundError(f"no brief with id {brief_id!r} under {briefs_dir}")
    return hits[0]
