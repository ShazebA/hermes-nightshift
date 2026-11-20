"""Morning digest: a plain-Markdown summary of everything the night shift saw."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

KIND_ORDER = ("watch_error", "ci_failure", "deploy_failure", "resolved", "pr_awaiting_review", "new_issue",
              "stale_branch")
KIND_TITLE = {
    "watch_error": "Watcher errors (Nightshift could not see this repo)",
    "ci_failure": "CI failures", "deploy_failure": "Failed deployments",
    "resolved": "Recovered (green again)",
    "pr_awaiting_review": "PRs awaiting review", "new_issue": "New issues",
    "stale_branch": "Stale branches",
}


def render_digest(rows: list[dict[str, Any]], since: str | None, until: str) -> str:
    window = f"since {since}" if since else "all recorded"
    if not rows:
        return f"Nightshift digest ({window}): quiet night, nothing new.\n"
    failures = sum(1 for r in rows if r["kind"] in ("ci_failure", "deploy_failure"))
    recovered = sum(1 for r in rows if r["kind"] == "resolved")
    rows_ = [r for r in rows if r["kind"] not in ("watch_error", "resolved")]
    out = [f"# Nightshift digest, {until[:10]}", "",
           f"{len(rows_)} new item(s) {window}; {failures} failure(s) triaged"
           + (f"; {recovered} recovered" if recovered else "") + ".", ""]
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_repo[r["repo"]].append(r)
    for repo in sorted(by_repo):
        out += [f"## {repo}", ""]
        items = by_repo[repo]
        for kind in KIND_ORDER:
            ks = [r for r in items if r["kind"] == kind]
            if not ks:
                continue
            out.append(f"**{KIND_TITLE[kind]}**")
            if kind == "watch_error":
                counts: dict[str, int] = {}
                for r in ks:
                    counts[r["title"]] = counts.get(r["title"], 0) + 1
                out += [f"- {t} (x{n})" for t, n in counts.items()] + [""]
                continue
            if kind == "resolved":
                for r in ks:
                    how = (f"fixed by `{(r.get('sha') or '')[:7]}`" if r.get("how") == "fix" else
                           f"green on a re-run of `{(r.get('sha') or '')[:7]}`, no code change (flaky?)")
                    line = f"- {r['title']}: {how}" + (f" ({r['url']})" if r.get("url") else "")
                    if r.get("kanban_task_id"):
                        line += f"  card: {r['kanban_task_id']}"
                    out.append(line)
                out.append("")
                continue
            for r in ks:
                line = f"- {r['title']} ({r['url']})"
                if r.get("classification"):
                    c = r["classification"]
                    line += f" — {c['category']} {c['confidence']:.2f}"
                if r.get("resolution"):
                    line += f" (since recovered: {r['resolution']['how']})"
                if r.get("brief_md"):
                    line += f"\n  brief: {r['brief_md']}"
                if r.get("kanban_task_id"):
                    line += f"  card: {r['kanban_task_id']}"
                out.append(line)
            out.append("")
    return "\n".join(out).rstrip() + "\n"
