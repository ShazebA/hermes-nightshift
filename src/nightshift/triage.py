"""Deterministic triage: failing-log extraction, culprit range, suspected files, repro command
and a heuristic classification. The model only refines ``classification`` afterwards (see
``nightshift classify``); everything here must work with no model at all."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from .config import Lesson, RepoConfig
from .gh import Gh, GhError
from .models import Incident

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
EXCERPT_LINES = 60

# (pattern, category, confidence, why). First match wins, so order is priority.
INFRA_PATTERNS = [
    r"No space left on device", r"The runner has received a shutdown signal",
    r"lost communication with the server", r"The operation was canceled",
    r"Could not resolve host", r"ECONNRESET", r"ETIMEDOUT", r"EAI_AGAIN",
    r"toomanyrequests|rate limit exceeded|API rate limit", r"50[234] (Bad Gateway|Service Unavailable|Gateway Time)",
    r"npm ERR! network", r"Unable to locate executable file", r"Error: Process completed with exit code 137",
]
FLAKY_PATTERNS = [
    r"Test timed out in \d+ms", r"Timeout of \d+ms exceeded", r"exceeded timeout of \d+",
    r"flaky", r"socket hang up", r"Navigation timeout",
]
SIGNATURE_PATTERNS = [
    r"^\s*(FAIL|FAILED)\s+.+", r"\bAssertionError\b.*", r"^\s*(×|✗|✕)\s+.+",
    r"error TS\d+:.*", r"\b(Type|Reference|Syntax)Error:.*", r"^\s*Error:.*",
    r"npm ERR! .*", r"##\[error\].*",
]
TEST_LOCATORS = [
    # (regex, runner) — vitest/jest "FAIL path > name", pytest "FAILED path::test"
    (re.compile(r"(?:FAIL|×|✗)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)"), "vitest"),
    (re.compile(r"FAILED\s+(\S+\.py::\S+)"), "pytest"),
    # playwright "1) [chromium] › tests/e2e/x.spec.ts:14:5 › title"
    (re.compile(r"\]\s+›\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?:\d+)"), "playwright"),
]
PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z]{1,5})(?::\d+)?")


@dataclass
class LogDigest:
    job: str = ""
    step: str = ""
    command: str = ""
    excerpt: list[str] = field(default_factory=list)
    signature: str = ""
    error: str = ""
    test_target: str = ""
    runner: str = ""
    paths: list[str] = field(default_factory=list)


def parse_failed_log(text: str) -> LogDigest:
    """Parse `gh run view --log-failed` output (``job<TAB>step<TAB>timestamp line``)."""
    rows = []
    for raw in text.splitlines():
        parts = raw.split("\t", 2)
        if len(parts) < 3:
            continue
        job, step, line = parts
        rows.append((job, step, ANSI.sub("", TS.sub("", line)).rstrip()))
    if not rows:
        return LogDigest()
    # The failing step is the one that holds the ##[error] marker; default to the last step.
    err_rows = [r for r in rows if "##[error]" in r[2]]
    job, step = (err_rows[-1] if err_rows else rows[-1])[:2]
    lines = [ln for j, s, ln in rows if j == job and s == step]
    d = LogDigest(job=job, step=step, command=_run_command(lines))
    body = _drop_groups(lines)
    d.excerpt = _excerpt(body)
    d.signature = signature(body)
    d.error = first_error(body)
    for rx, runner in TEST_LOCATORS:
        m = next((rx.search(ln) for ln in body if rx.search(ln)), None)
        if m:
            d.test_target, d.runner = m.group(1), runner
            break
    d.paths = extract_paths(body)
    return d


def _drop_groups(lines: list[str]) -> list[str]:
    """Drop ``##[group]...##[endgroup]`` blocks (step script, shell, env): noise for triage."""
    out, inside = [], False
    for ln in lines:
        if ln.startswith("##[group]"):
            inside = True
        elif ln.startswith("##[endgroup]"):
            inside = False
        elif not inside:
            out.append(ln)
    return out


def extract_paths(lines: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for ln in lines:
        for m in PATH_RE.finditer(ln):
            p = m.group(1).removeprefix("./")
            if not p.startswith(("node_modules/", "http", "usr/", "opt/", "home/runner")):
                seen.setdefault(p, None)
    return list(seen)[:30]


def _run_command(lines: list[str]) -> str:
    """The script of a run step. GitHub prints ``##[group]Run <first line>`` and then the full
    script indented by two spaces, followed by ``shell:``/``env:`` lines."""
    for i, ln in enumerate(lines):
        if ln.startswith("##[group]Run "):
            script = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("##[endgroup]") or not nxt.startswith(" "):
                    break
                if re.match(r"^\s+(shell|env):", nxt):
                    break
                if nxt.strip():
                    script.append(nxt.strip())
            return " && ".join(script) if script else ln.removeprefix("##[group]Run ").strip()
    return ""


def _excerpt(lines: list[str]) -> list[str]:
    """Error-dense window: everything from 5 lines before the first error-ish line, capped."""
    sig_rx = [re.compile(p) for p in SIGNATURE_PATTERNS[:-1]]
    first = next((i for i, ln in enumerate(lines) if any(rx.search(ln) for rx in sig_rx)), None)
    start = max(0, first - 5) if first is not None else max(0, len(lines) - EXCERPT_LINES)
    window = [ln for ln in lines[start:] if ln.strip()]
    if len(window) > EXCERPT_LINES:
        window = window[: EXCERPT_LINES - 10] + ["…"] + window[-9:]
    return window


def normalize(line: str) -> str:
    s = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", line)
    s = re.sub(r"\d+(\.\d+)?(ms|s)?\b", "N", s)
    return re.sub(r"\s+", " ", s).strip()[:160]


def signature(lines: list[str]) -> str:
    for pat in SIGNATURE_PATTERNS:
        rx = re.compile(pat)
        for ln in lines:
            m = rx.search(ln)
            if m:
                return normalize(m.group(0))
    return normalize(lines[-1]) if lines else ""


ERROR_RE = re.compile(r"\b(\w*Error|Exception|error TS\d+)\b:?\s.*")


def first_error(lines: list[str]) -> str:
    """The first concrete error message (``Error: ...``, ``TypeError: ...``), unnormalised."""
    for ln in lines:
        m = ERROR_RE.search(ln)
        if m and "##[error]" not in ln:
            return m.group(0).strip()[:200]
    return ""


# ------------------------------------------------------------------ classification


def classify(log_text: str, incident: Incident, culprit_commits: int) -> dict[str, Any]:
    """Heuristic classification. Returns {category, confidence, source, rationale}."""
    data = incident.data
    if incident.kind == "ci_failure" and data.get("green_on_red_sha"):
        return _c("flaky", 0.8, "The same commit both failed and passed this workflow (re-run "
                  "went green).")
    for pat in INFRA_PATTERNS:
        m = re.search(pat, log_text, re.I)
        if m:
            return _c("infra", 0.75, f"Log matches infrastructure pattern: {m.group(0)!r}.")
    for pat in FLAKY_PATTERNS:
        m = re.search(pat, log_text, re.I)
        if m:
            return _c("flaky", 0.55, f"Log matches timing/flake pattern: {m.group(0)!r}.")
    if not log_text.strip():
        return _c("unknown", 0.2, "No failing-step log was available.")
    if culprit_commits:
        return _c("real", 0.7, f"Deterministic failure with {culprit_commits} commit(s) between "
                  "last green and first red.")
    return _c("real", 0.5, "Deterministic-looking failure but no commit range to blame.")


def _c(category: str, confidence: float, rationale: str) -> dict[str, Any]:
    return {"category": category, "confidence": confidence, "source": "heuristic",
            "rationale": rationale}


# ------------------------------------------------------------------ culprit


def suspected_files(changed: list[str], log_paths: list[str], test_target: str) -> list[dict]:
    """Rank files changed in the culprit range by how strongly the log points at them."""
    target_file = re.sub(r":\d+$", "", test_target.split("::")[0]) if test_target else ""
    mentioned = set(log_paths) | ({target_file} if target_file else set())
    stems = {_stem(p) for p in mentioned}
    dirs = {str(PurePosixPath(p).parent) for p in mentioned}
    scored = []
    for f in changed:
        if f in mentioned or any(m.endswith(f) or f.endswith(m) for m in mentioned):
            scored.append((3, f, "named in the failing log"))
        elif _stem(f) in stems:
            scored.append((2, f, "source/test pair of a file named in the failing log"))
        elif str(PurePosixPath(f).parent) in dirs:
            scored.append((1, f, "same directory as a file named in the failing log"))
        else:
            scored.append((0, f, "changed between last green and first red"))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [{"path": f, "reason": why, "score": s} for s, f, why in scored[:8]]


def _stem(p: str) -> str:
    name = PurePosixPath(p.split("::")[0]).name
    return re.sub(r"(\.(test|spec))?\.[^.]+$", "", name)


def repro_command(digest: LogDigest, sha: str, repo: RepoConfig) -> str:
    setup = f"{repo.repro_setup} && " if repo.repro_setup else ""
    if digest.runner == "vitest" and "jest" in digest.command:
        target = f"npx jest {digest.test_target}"
    elif digest.runner == "vitest":
        target = f"npx vitest run {digest.test_target}"
    elif digest.runner == "pytest":
        target = f"python -m pytest {digest.test_target}"
    elif digest.runner == "playwright":
        target = f"npx playwright test {digest.test_target}"
    else:
        target = digest.command or "<failing step command not found in log>"
    return f"git checkout {sha} && {setup}{target}"


def build_repro(sha: str, repo: RepoConfig) -> str:
    setup = f"{repo.repro_setup} && " if repo.repro_setup else ""
    build = repo.build_command or "<set repos[].build_command in nightshift.yaml>"
    return f"git checkout {sha} && {setup}{build}"


def matching_lessons(lessons: list[Lesson], text: str) -> list[dict[str, str]]:
    return [{"id": le.id, "pattern": le.pattern, "note": le.note, "source": le.source}
            for le in lessons if re.search(le.pattern, text, re.I | re.M)]


# ------------------------------------------------------------------ driver


@dataclass
class Triage:
    digest: LogDigest
    log_text: str
    classification: dict[str, Any]
    commits: list[dict[str, Any]]
    changed_files: list[str]
    compare_url: str | None
    suspected: list[dict[str, Any]]
    repro: str
    lessons: list[dict[str, str]]
    errors: list[str]


def _compare(gh: Gh, repo: str, base: str | None, head: str | None):
    if not base or not head or base == head:
        return [], [], None
    data = gh.json(["api", f"repos/{repo}/compare/{base}...{head}"], repo) or {}
    commits = [{
        "sha": c["sha"], "title": (c["commit"]["message"] or "").splitlines()[0][:120],
        "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
        "url": c.get("html_url"),
    } for c in data.get("commits", [])]
    files = [f["filename"] for f in data.get("files", [])]
    return commits, files, data.get("html_url")


def triage(gh: Gh, incident: Incident, repo: RepoConfig, lessons: list[Lesson]) -> Triage:
    errors: list[str] = []
    d = incident.data
    log_text = ""
    if incident.kind == "ci_failure":
        run_id = str(d["latest_red"]["databaseId"])
        try:
            log_text = gh.text(["run", "view", run_id, "-R", incident.repo, "--log-failed"],
                               incident.repo)
        except GhError as e:
            errors.append(f"log: {e}")
        base = (d.get("last_green") or {}).get("headSha")
        head = d["first_red"]["headSha"]
    else:
        log_text = d.get("description", "")
        base, head = d.get("last_success_sha"), d["sha"]
    digest = parse_failed_log(log_text) if incident.kind == "ci_failure" else LogDigest(
        excerpt=[log_text] if log_text else [], signature=normalize(log_text),
        paths=extract_paths([log_text]))
    try:
        commits, files, compare_url = _compare(gh, incident.repo, base, head)
    except GhError as e:
        errors.append(f"compare: {e}")
        commits, files, compare_url = [], [], None
    text_for_rules = "\n".join(digest.excerpt) + "\n" + digest.signature
    cls = classify(log_text, incident, len(commits))
    return Triage(
        digest=digest, log_text=log_text, classification=cls, commits=commits,
        changed_files=files, compare_url=compare_url,
        suspected=suspected_files(files, digest.paths, digest.test_target),
        repro=repro_command(digest, head, repo) if incident.kind == "ci_failure"
        else build_repro(head, repo),
        lessons=matching_lessons(lessons, text_for_rules),
        errors=errors,
    )
