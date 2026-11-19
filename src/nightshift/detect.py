"""Incident detection: turn `gh` JSON into Incidents. Pure functions over gh output plus a
``detect_repo`` driver that issues the gh calls. Polling only; a webhook receiver can feed the
same pure functions later (see docs/webhooks.md)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import RepoConfig
from .gh import Gh, GhError
from .models import GREEN_CONCLUSIONS, RED_CONCLUSIONS, Incident, slugify

RUN_FIELDS = (
    "databaseId,name,workflowName,workflowDatabaseId,status,conclusion,headSha,headBranch,"
    "event,createdAt,url,attempt,displayTitle"
)
PR_FIELDS = "number,title,author,createdAt,url,isDraft,reviewDecision,headRefName"
ISSUE_FIELDS = "number,title,author,createdAt,url,labels"
STALE_QUERY = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
    "refs(refPrefix:\"refs/heads/\",first:100){nodes{name target{... on Commit{oid committedDate}}}}}}"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- CI


def ci_incidents_from_runs(repo: str, branch: str, runs: list[dict[str, Any]],
                           now: datetime | None = None) -> list[Incident]:
    """One incident per (workflow, branch) whose most recent decided run is red.

    The incident is keyed on the FIRST red run of the current streak, so repeated failures of
    the same breakage dedupe to one brief. Cancelled/skipped/in-progress runs are ignored.
    """
    now = now or _now()
    by_wf: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        if r.get("headBranch") != branch or r.get("status") != "completed":
            continue
        by_wf.setdefault(r["workflowName"], []).append(r)
    out = []
    for wf, wf_runs in sorted(by_wf.items()):
        decided = [r for r in wf_runs if r.get("conclusion") in RED_CONCLUSIONS | GREEN_CONCLUSIONS]
        decided.sort(key=lambda r: (r["createdAt"], r["databaseId"]), reverse=True)
        if not decided or decided[0]["conclusion"] not in RED_CONCLUSIONS:
            continue
        streak = []
        last_green = None
        for r in decided:
            if r["conclusion"] in RED_CONCLUSIONS:
                streak.append(r)
            else:
                last_green = r
                break
        first_red, latest_red = streak[-1], streak[0]
        red_shas = {r["headSha"] for r in streak}
        # A green run on a SHA that also went red (re-run passed) is the classic flake signal.
        green_on_red_sha = any(
            r["conclusion"] in GREEN_CONCLUSIONS and r["headSha"] in red_shas for r in wf_runs
        )
        out.append(Incident(
            id=f"ci-{slugify(wf)}-{slugify(branch)}-{first_red['databaseId']}",
            repo=repo,
            kind="ci_failure",
            title=f"{wf} failing on {branch}",
            url=latest_red["url"],
            detected_at=_iso(now),
            data={
                "workflow": wf,
                "branch": branch,
                "first_red": _run_ref(first_red),
                "latest_red": _run_ref(latest_red),
                "last_green": _run_ref(last_green) if last_green else None,
                "red_streak": len(streak),
                "green_on_red_sha": green_on_red_sha,
            },
        ))
    return out


def _run_ref(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in ("databaseId", "headSha", "url", "createdAt", "attempt",
                                  "displayTitle", "event", "conclusion")}


# ------------------------------------------------------------------ deployments


def deploy_incidents(repo: str, deployments: list[dict[str, Any]],
                     statuses: Mapping[int, list[dict[str, Any]]] | Callable[[int], list[dict[str, Any]]],
                     now: datetime | None = None, max_walk: int = 10) -> list[Incident]:
    """Latest decided deployment per environment is failure/error → incident.

    Vercel (and most providers) report through GitHub deployment statuses, so this covers
    Vercel without a Vercel token. ``statuses`` gives a deployment's statuses, newest first; as a
    callable it is only invoked while walking each environment back to its last success, so a
    poll costs one status call per environment in the common all-green case.
    """
    now = now or _now()
    status_of = statuses if callable(statuses) else (lambda i: statuses.get(i))
    by_env: dict[str, list[dict[str, Any]]] = {}
    for d in deployments:
        by_env.setdefault(d.get("environment") or "default", []).append(d)
    out = []
    for env, deps in sorted(by_env.items()):
        deps.sort(key=lambda d: (d["created_at"], d["id"]), reverse=True)
        latest_fail, last_ok = None, None
        for d in deps[:max_walk]:
            st = (status_of(d["id"]) or [{}])[0]
            state = st.get("state")
            if state in ("failure", "error"):
                if latest_fail is None and last_ok is None:
                    latest_fail = (d, st)
            elif state == "success":
                last_ok = (d, st)
                break
        if latest_fail is None:
            continue
        d, st = latest_fail
        out.append(Incident(
            id=f"deploy-{slugify(env)}-{d['id']}",
            repo=repo,
            kind="deploy_failure",
            title=f"Deployment to {env} failed",
            url=st.get("log_url") or st.get("target_url") or d.get("url", ""),
            detected_at=_iso(now),
            data={
                "environment": env,
                "deployment_id": d["id"],
                "sha": d["sha"],
                "ref": d.get("ref"),
                "creator": (st.get("creator") or {}).get("login"),
                "state": st.get("state"),
                "description": st.get("description") or "",
                "log_url": st.get("log_url") or st.get("target_url"),
                "created_at": d["created_at"],
                "last_success_sha": last_ok[0]["sha"] if last_ok else None,
            },
        ))
    return out


# ------------------------------------------------------------------ notices


def issue_incidents(repo: str, issues: list[dict[str, Any]], now: datetime | None = None):
    now = now or _now()
    return [Incident(
        id=f"issue-{i['number']}", repo=repo, kind="new_issue",
        title=f"#{i['number']} {i['title']}", url=i["url"], detected_at=_iso(now),
        data={"number": i["number"], "author": (i.get("author") or {}).get("login"),
              "created_at": i["createdAt"], "labels": [x["name"] for x in i.get("labels") or []]},
    ) for i in issues]


def pr_incidents(repo: str, prs: list[dict[str, Any]], now: datetime | None = None):
    now = now or _now()
    return [Incident(
        id=f"pr-{p['number']}", repo=repo, kind="pr_awaiting_review",
        title=f"PR #{p['number']} {p['title']}", url=p["url"], detected_at=_iso(now),
        data={"number": p["number"], "author": (p.get("author") or {}).get("login"),
              "created_at": p["createdAt"], "branch": p.get("headRefName"),
              "review_decision": p.get("reviewDecision") or ""},
    ) for p in prs if not p.get("isDraft") and p.get("reviewDecision") not in
        ("APPROVED", "CHANGES_REQUESTED")]


def stale_branch_incidents(repo: str, graphql: dict[str, Any], keep: list[str], days: int,
                           now: datetime | None = None):
    now = now or _now()
    cutoff = now - timedelta(days=days)
    nodes = (((graphql.get("data") or {}).get("repository") or {}).get("refs") or {}).get("nodes")
    out = []
    for n in nodes or []:
        tgt = n.get("target") or {}
        if n["name"] in keep or not tgt.get("committedDate"):
            continue
        last = _parse(tgt["committedDate"])
        if last < cutoff:
            out.append(Incident(
                id=f"stale-{slugify(n['name'], 60)}-{tgt['oid'][:7]}", repo=repo,
                kind="stale_branch",
                title=f"Branch {n['name']} idle for {(now - last).days} days",
                url=f"https://github.com/{repo}/tree/{n['name']}", detected_at=_iso(now),
                data={"branch": n["name"], "last_commit": tgt["oid"],
                      "last_commit_at": tgt["committedDate"], "idle_days": (now - last).days},
            ))
    return out


# ------------------------------------------------------------------ driver


def detect_repo(gh: Gh, repo: RepoConfig, lookback: int = 30,
                now: datetime | None = None) -> tuple[list[Incident], list[str]]:
    """Run every enabled watcher for one repo. Returns (incidents, errors); one failing
    watcher never hides the others."""
    found: list[Incident] = []
    errors: list[str] = []
    name = repo.name

    def guarded(label, fn):
        try:
            found.extend(fn())
        except (GhError, KeyError, TypeError, ValueError) as e:
            errors.append(f"{name} {label}: {e}")

    if "ci" in repo.watch:
        for b in repo.branches:
            guarded(f"ci[{b}]", lambda b=b: ci_incidents_from_runs(name, b, gh.json(
                ["run", "list", "-R", name, "--branch", b, "-L", str(lookback),
                 "--json", RUN_FIELDS], name) or [], now))
    if "deployments" in repo.watch:
        def deploys():
            deps = gh.json(["api", f"repos/{name}/deployments?per_page=20"], name) or []
            return deploy_incidents(name, deps, lambda i: gh.json(
                ["api", f"repos/{name}/deployments/{i}/statuses?per_page=5"], name) or [], now)
        guarded("deployments", deploys)
    if "issues" in repo.watch:
        guarded("issues", lambda: issue_incidents(name, gh.json(
            ["issue", "list", "-R", name, "--state", "open", "--limit", "30",
             "--json", ISSUE_FIELDS], name) or [], now))
    if "pull_requests" in repo.watch:
        guarded("pull_requests", lambda: pr_incidents(name, gh.json(
            ["pr", "list", "-R", name, "--state", "open", "--limit", "30",
             "--json", PR_FIELDS], name) or [], now))
    if "stale_branches" in repo.watch:
        owner, rname = name.split("/", 1)
        guarded("stale_branches", lambda: stale_branch_incidents(name, gh.json(
            ["api", "graphql", "-f", f"query={STALE_QUERY}", "-F", f"owner={owner}",
             "-F", f"name={rname}"], name) or {}, repo.branches, repo.stale_branch_days, now))
    return found, errors
