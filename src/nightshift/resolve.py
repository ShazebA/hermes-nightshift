"""Resolution: notice when an open failure has gone green again, and say how.

Pure functions over the same `gh` data detection already fetched (``detect_repo(observed=)``),
so resolving costs no extra calls. Only positive evidence resolves: a green run or successful
deployment that is newer than the failure. A watcher error, a missing workflow or a run that
scrolled out of the lookback window leaves the incident open.

``how`` is ``fix`` when the green run is on a different commit than every red run (that commit
is the likely fix) and ``rerun`` when the same commit went green (the failure went away without
a code change, which points at flakiness or infrastructure).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import GREEN_CONCLUSIONS, RED_CONCLUSIONS


def _key(ts: str | None, ident: Any) -> tuple[str, int]:
    return (ts or "", int(ident or 0))


def ci_resolution(brief: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    wb, cu = brief["what_broke"], brief["culprit"]
    wf, branch, red_id = wb.get("workflow"), wb.get("branch"), wb.get("run_id")
    if not wf or not red_id:
        return None
    mine = [r for r in runs if r.get("workflowName") == wf and r.get("headBranch") == branch
            and r.get("status") == "completed"]
    red_key = _key(wb.get("failed_at"), red_id)
    red_ids = {red_id}
    red_shas = {s for s in (cu.get("first_red_sha"),) if s}
    for r in mine:
        if r["databaseId"] == red_id:
            red_shas.add(r["headSha"])
    greens = []
    for r in sorted(mine, key=lambda r: _key(r.get("createdAt"), r.get("databaseId"))):
        k = _key(r.get("createdAt"), r.get("databaseId"))
        if r.get("conclusion") in RED_CONCLUSIONS and k >= red_key:
            red_shas.add(r["headSha"])  # the streak may have grown since the brief was written
        # A re-run keeps its id and createdAt, so the red run itself turning green counts.
        if r.get("conclusion") in GREEN_CONCLUSIONS and (k > red_key or r["databaseId"] in red_ids):
            greens.append(r)
            break
    if not greens:
        return None
    g = greens[0]
    return {"how": "rerun" if g["headSha"] in red_shas else "fix", "sha": g["headSha"],
            "url": g.get("url"), "run_id": g["databaseId"], "green_at": g.get("createdAt")}


def deploy_resolution(brief: dict[str, Any], deployments: list[dict[str, Any]],
                      status_of: Callable[[int], list[dict[str, Any]]],
                      max_walk: int = 10) -> dict[str, Any] | None:
    """The newest successful deployment to the same environment, newer than the failed one."""
    wb, cu = brief["what_broke"], brief["culprit"]
    env = wb.get("environment")
    try:
        failed_id = int(brief["id"].rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None
    failed = next((d for d in deployments if d.get("id") == failed_id), None)
    failed_key = _key(failed["created_at"], failed_id) if failed else _key(wb.get("failed_at"), failed_id)
    newer = sorted((d for d in deployments if (d.get("environment") or "default") == env
                    and _key(d.get("created_at"), d.get("id")) > failed_key),
                   key=lambda d: _key(d.get("created_at"), d.get("id")), reverse=True)
    for d in newer[:max_walk]:
        st = (status_of(d["id"]) or [{}])[0]
        if st.get("state") == "success":
            return {"how": "rerun" if d.get("sha") == cu.get("first_red_sha") else "fix",
                    "sha": d.get("sha"), "url": st.get("log_url") or st.get("target_url") or d.get("url"),
                    "run_id": d["id"], "green_at": d.get("created_at")}
    return None


def find_resolution(brief: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any] | None:
    if brief.get("resolution"):
        return None
    if brief["kind"] == "ci_failure":
        runs = (observed.get("ci") or {}).get(brief["what_broke"].get("branch"))
        return ci_resolution(brief, runs) if runs is not None else None
    if brief["kind"] == "deploy_failure" and "deployments" in observed:
        deps, status_of = observed["deployments"]
        return deploy_resolution(brief, deps, status_of)
    return None
