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
# Runner/platform failures: infra wherever they appear in the failing step.
INFRA_PATTERNS = [
    r"No space left on device", r"The runner has received a shutdown signal",
    r"lost communication with the server", r"The operation was canceled",
    r"Error: Process completed with exit code 137", r"Unable to locate executable file",
    r"githubstatus\.com", r"failed with 50[234]\b", r"Artifact not found for name",
    r"Unable to download artifact",
]
# Network errors: infra only when no failing test was identified. Test suites print these
# while exercising error paths, so one ECONNRESET among 800 test lines is not an outage.
NETWORK_PATTERNS = [
    r"Could not resolve host", r"ECONNRESET", r"ETIMEDOUT", r"EAI_AGAIN",
    r"toomanyrequests|rate limit exceeded|API rate limit", r"50[234] (Bad Gateway|Service Unavailable|Gateway Time)",
    r"npm ERR! network", r"Internal Server Error.{0,40}(api\.github|ghcr\.io|registry)",
    r"stream error: stream ID \d+",
]
FLAKY_PATTERNS = [
    r"Test timed out in \d+ms", r"Timeout of \d+ms exceeded", r"exceeded timeout of \d+",
    r"socket hang up", r"Navigation timeout", r"Test timeout of \d+ms exceeded",
]
# Roll-up jobs ("all checks passed", re-actors/alls-green) fail only because another job failed.
AGGREGATOR_RE = re.compile(
    r"alls?-green|all[ -]?(required|checks|tests|jobs)|required[ -]checks|check[ -]?status", re.I)
POST_JOB = ("Post job cleanup", "Cleaning up orphan processes", "Terminate orphan process",
            "Removing credentials config")
SIGNATURE_PATTERNS = [
    r"^\s*(FAIL|FAILED)\s+.+", r"\bAssertionError\b.*", r"^\s*(×|✗|✕)\s+.+",
    r"error TS\d+:.*", r"^\s*error(\[E\d+\])?: (?!instrumented|could not compile|aborting).+",
    r"\b(Type|Reference|Syntax)Error:.*", r"^\s*Error:.*",
    r"npm ERR! .*", r"##\[error\](?!Process completed with exit code).*",
]
TEST_LOCATORS = [
    # (regex, runner) — vitest/jest "FAIL path > name", pytest "FAILED path::test"
    (re.compile(r"(?:FAIL|×|✗)\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?)"), "vitest"),
    (re.compile(r"FAILED\s+(\S+\.py::\S+)"), "pytest"),
    # playwright "1) [chromium] › tests/e2e/x.spec.ts:14:5 › title"
    (re.compile(r"\]\s+›\s+(\S+\.(?:test|spec)\.[cm]?[jt]sx?:\d+)"), "playwright"),
]
# GitHub marks each script line of a run step with this colour code; the parser turns it into
# SCRIPT so the step's script survives ANSI stripping.
SCRIPT_MARK = "\x1b[36;1m"
SCRIPT = "\x00"  # not whitespace: rstrip() must not eat it on blank script lines
# `Run owner/action@ref`, a local `./action` or `docker://image`: no script to run locally.
ACTION_RE = re.compile(r"^(?:[\w.-]+/[\w.-]+(?:/[\w./-]+)?@\S+|\./[\w./-]+|docker://\S+)$")
MAX_SCRIPT_LINES = 40
PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z]{1,5})(?::\d+)?")


@dataclass
class LogDigest:
    job: str = ""
    step: str = ""
    command: str = ""  # the step's script (newline-separated) or its Run header
    action: str = ""  # set when the failing step is an action (`uses:`), not a script
    env: list[str] = field(default_factory=list)  # names (not values) from the step's env block
    excerpt: list[str] = field(default_factory=list)
    signature: str = ""  # normalized, stable across runs
    headline: str = ""  # the same line as printed, for people (keeps "60.01 == 60.0")
    error: str = ""
    aggregator: bool = False  # only a roll-up job failed; the real failure is elsewhere in the run
    segment: list[str] = field(default_factory=list)  # failing step's lines, cut at the error
    test_target: str = ""
    runner: str = ""
    paths: list[str] = field(default_factory=list)


def parse_failed_log(text: str) -> LogDigest:
    """Parse `gh run view --log-failed` output (``job<TAB>step<TAB>timestamp line``).

    Recent `gh` versions label every line ``UNKNOWN STEP`` and include the whole job, so steps
    are recovered from the ``##[group]Run <cmd>`` headers GitHub prints at the start of each
    step. The failing step is the one holding the job's first ``##[error]``; roll-up jobs
    (alls-green, "all required checks") are only used when nothing else failed.
    """
    jobs: dict[str, list[tuple[str, str]]] = {}
    for raw in text.splitlines():
        parts = raw.split("\t", 2)
        if len(parts) < 3:
            continue
        job, step, line = parts
        line = TS.sub("", line)
        if line.startswith(SCRIPT_MARK):
            line = SCRIPT + line[len(SCRIPT_MARK):]
        jobs.setdefault(job, []).append((step, ANSI.sub("", line).rstrip()))
    if not jobs:
        return LogDigest()
    failing = [(j, _failing_step(rows)) for j, rows in jobs.items()]
    failing = [(j, st) for j, st in failing if st is not None]
    if not failing:
        last_job = list(jobs)[-1]
        failing = [(last_job, ("", [ln for _, ln in jobs[last_job]]))]
    real = [f for f in failing if not _is_aggregator(f[0], f[1][1])]
    job, (step, lines) = (real or failing)[0]
    d = LogDigest(job=job, step=step)
    d.command, d.action, d.env = _step_command(lines)
    d.aggregator = not real
    body = [ln.removeprefix(SCRIPT) for ln in _drop_groups(lines)
            if not ln.startswith(("##[start-action", "##[end-action"))]
    d.segment = body
    d.excerpt = _excerpt(body)
    d.headline = signature_line(body)
    d.signature = normalize(d.headline)
    d.error = first_error(body)
    for rx, runner in TEST_LOCATORS:
        m = next((rx.search(ln) for ln in body if rx.search(ln)), None)
        if m:
            d.test_target, d.runner = m.group(1).replace("\\", "/"), runner
            break
    d.paths = extract_paths(body)
    return d


def _failing_step(rows: list[tuple[str, str]]) -> tuple[str, list[str]] | None:
    """(step name, lines up to and including the first ##[error]) or None if the job has none."""
    err = next((i for i, (_, ln) in enumerate(rows) if ln.startswith("##[error]")), None)
    if err is None:
        return None
    step = rows[err][0]
    if step and step != "UNKNOWN STEP":
        lines = [ln for i, (s, ln) in enumerate(rows) if s == step and i <= err]
        return step, lines
    start = next((i for i in range(err, -1, -1) if rows[i][1].startswith("##[group]Run ")), 0)
    lines = [ln for _, ln in rows[start:err + 1]]
    name = lines[0].removeprefix("##[group]") if lines and lines[0].startswith("##[group]Run ") else step
    return name, lines


def _is_aggregator(job: str, lines: list[str]) -> bool:
    return bool(AGGREGATOR_RE.search(job) or (lines and AGGREGATOR_RE.search(lines[0])))


def _drop_groups(lines: list[str]) -> list[str]:
    """Drop ``##[group]...##[endgroup]`` blocks (step script, shell, env): noise for triage."""
    out, inside = [], False
    for ln in lines:
        if ln.startswith("##[group]"):
            inside = True
        elif ln.startswith("##[endgroup]"):
            inside = False
        elif inside and ln.strip() and not ln.startswith((" ", "\t", "##[")):
            # Group content is indented; an unindented line means the endgroup marker was lost
            # (truncated or trimmed log). Don't let one missing marker swallow the whole step.
            inside = False
            out.append(ln)
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


def _step_command(lines: list[str]) -> tuple[str, str, list[str]]:
    """(command, action, env names) of the failing step, from GitHub's step header.

    Current logs print ``##[group]Run <first line>``, then every script line marked with a
    colour code (SCRIPT here), then ``shell:``/``env:``/``with:`` at column 0 with indented
    values. Older logs indent the script by two spaces instead. An action step has ``with:``
    inputs and no script.
    """
    for i, ln in enumerate(lines):
        if not ln.startswith("##[group]Run "):
            continue
        header = ln.removeprefix("##[group]Run ").strip()
        rest = lines[i + 1:i + 400]
        script = []
        for nxt in rest:
            if not nxt.startswith(SCRIPT):
                break
            script.append(nxt[len(SCRIPT):])
        if not script:  # older format: two-space indented script
            for nxt in rest:
                if (nxt.startswith("##[endgroup]") or not nxt.startswith(" ")
                        or re.match(r"^\s+(shell|env|with):", nxt)):
                    break
                script.append(nxt[2:] if nxt.startswith("  ") else nxt.strip())
        while script and not script[-1].strip():
            script.pop()
        env: list[str] = []
        in_env = False
        for nxt in rest:
            if nxt.startswith("##[endgroup]"):
                break
            if re.match(r"^\s*env:\s*$", nxt):
                in_env = True
                continue
            m = re.match(r"^\s+([A-Za-z_][A-Za-z0-9_]*):", nxt)
            if in_env and m:
                env.append(m.group(1))
            elif in_env:
                in_env = False
        action = header if not script and ACTION_RE.match(header) else ""
        return ("\n".join(script) if script else header), action, env
    return "", "", []


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
    s = re.sub(r"(?<![A-Za-z\d])\d+(\.\d+)?(ms|s)?\b", "N", s)
    return re.sub(r"\s+", " ", s).strip()[:160]


def signature_line(lines: list[str]) -> str:
    """The line that best says what failed, as printed (whitespace collapsed)."""
    for pat in SIGNATURE_PATTERNS:
        rx = re.compile(pat)
        for ln in lines:
            m = rx.search(ln)
            if m:
                return re.sub(r"\s+", " ", m.group(0)).strip()[:200]
    # Only the generic "Process completed with exit code N": use the last real line before it.
    meaningful = [ln for ln in lines if ln.strip() and not ln.startswith("##[")
                  and not re.fullmatch(r"[\s=\-_*#]+", ln) and not ln.startswith(POST_JOB)]
    if meaningful:
        return re.sub(r"\s+", " ", meaningful[-1]).strip()[:200]
    return re.sub(r"\s+", " ", lines[-1]).strip()[:200] if lines else ""


def signature(lines: list[str]) -> str:
    """Normalized signature (numbers and SHAs masked) for dedupe and lesson matching."""
    return normalize(signature_line(lines))


ERROR_RE = re.compile(r"\b(\w*Error|\w*Exception|error TS\d+):\s.*")


def first_error(lines: list[str]) -> str:
    """The first concrete error message (``Error: ...``, ``TypeError: ...``), unnormalised."""
    for ln in lines:
        m = ERROR_RE.search(ln)
        if m and "##[error]" not in ln:
            return m.group(0).strip()[:200]
    return ""


# ------------------------------------------------------------------ classification


def classify(log_text: str, incident: Incident, culprit_commits: int,
             aggregator: bool = False, test_target: str = "", failure_text: str = "") -> dict[str, Any]:
    """Heuristic classification over the failing step's text. Returns {category, confidence,
    source, rationale}.

    Once a failing test is identified, network and timing patterns only count when they are in
    the failure itself (``failure_text``: signature and first error). A big test log prints
    ECONNRESET or "timeout exceeded" while exercising error paths, and that is not the failure.
    """
    soft_text = failure_text if test_target else log_text
    data = incident.data
    if incident.kind == "ci_failure" and data.get("green_on_red_sha"):
        return _c("flaky", 0.8, "The same commit both failed and passed this workflow (re-run "
                  "went green).")
    for pat in INFRA_PATTERNS:
        m = re.search(pat, log_text, re.I)
        if m:
            return _c("infra", 0.75, f"Log matches infrastructure pattern: {m.group(0)!r}.")
    for pat in NETWORK_PATTERNS:
        m = re.search(pat, soft_text, re.I)
        if m:
            return _c("infra", 0.7, f"The failure matches network error pattern: {m.group(0)!r}.")
    if is_github_managed(incident):
        return _c("unknown", 0.4, "GitHub-managed workflow (Dependabot, CodeQL default setup and "
                  "similar run with event 'dynamic'): repo commits rarely cause these; check the "
                  "workflow's own log and its configuration.")
    if re.search(r"\b\d+ flaky\b", log_text) and not re.search(r"\b\d+ failed\b", log_text):
        return _c("flaky", 0.7, "The test runner itself reports flaky tests and no hard failures.")
    for pat in FLAKY_PATTERNS:
        m = re.search(pat, soft_text, re.I)
        if m:
            return _c("flaky", 0.55, f"Log matches timing/flake pattern: {m.group(0)!r}.")
    if not log_text.strip():
        return _c("unknown", 0.2, "No failing-step log was available.")
    if aggregator:
        return _c("unknown", 0.3, "Only a roll-up job (required checks / alls-green) failed in the "
                  "fetched log; open the run to see which upstream job broke.")
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
    name = re.sub(r"(\.(test|spec))?\.[^.]+$", "", name)
    return re.sub(r"^test_|_test$", "", name)  # pytest/go style: test_pricing.py <-> pricing.py


def is_github_managed(incident: Incident) -> bool:
    return incident.kind == "ci_failure" and (incident.data.get("first_red") or {}).get("event") == "dynamic"


def repro_command(digest: LogDigest, sha: str, repo: RepoConfig, run_id: Any = None) -> str:
    setup = f"{repo.repro_setup} && " if repo.repro_setup else ""
    view = f"gh run view {run_id} -R {repo.name} --log-failed" if run_id else ""
    rerun = f"gh run rerun {run_id} --failed -R {repo.name}" if run_id else "re-run the job on GitHub"
    script = digest.command.splitlines()
    if not digest.runner and view and (digest.action or len(script) > MAX_SCRIPT_LINES):
        why = (f"the failing step is the action {digest.action}, which only runs inside GitHub Actions"
               if digest.action else f"the failing step is a {len(script)}-line script; read it in the log")
        return f"{view}  # {why}. To re-run it: {rerun}"
    if digest.runner == "vitest" and "jest" in digest.command:
        target = f"npx jest {digest.test_target}"
    elif digest.runner == "vitest":
        target = f"npx vitest run {digest.test_target}"
    elif digest.runner == "pytest":
        target = f"python -m pytest {digest.test_target}"
    elif digest.runner == "playwright":
        target = f"npx playwright test {digest.test_target}"
    elif len(script) > 1:
        # Run it the way GitHub does (bash -e) without joining lines into broken shell.
        env = [f"# step env (set these first): {' '.join(digest.env)}"] if digest.env else []
        body = "\n".join([*env, *script])
        tag = "NIGHTSHIFT_STEP"
        while tag in script:  # a script line equal to the delimiter would end the heredoc early
            tag += "_"
        return f"git checkout {sha} && {setup}bash -e <<'{tag}'\n{body}\n{tag}"
    else:
        target = digest.command or "<failing step command not found in log>"
        if digest.env:
            target += f"  # step env (set these first): {' '.join(digest.env)}"
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


def find_last_green(gh: Gh, repo: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """Targeted lookup when the shared lookback window held no green run for this workflow
    (busy repos and nightly workflows push it out). Only runs created before the first red
    count, so a later fix can't pose as the last green."""
    wf = data["first_red"].get("workflowDatabaseId") or data.get("workflow")
    runs = gh.json(["run", "list", "-R", repo, "--workflow", str(wf), "--branch", data["branch"],
                    "--status", "success", "-L", "10", "--json", "databaseId,headSha,url,createdAt"],
                   repo) or []
    before = [r for r in runs if r["createdAt"] < data["first_red"]["createdAt"]]
    return max(before, key=lambda r: r["createdAt"]) if before else None


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
        if not d.get("last_green"):
            try:
                d["last_green"] = find_last_green(gh, incident.repo, d)
            except GhError as e:
                errors.append(f"last green: {e}")
        base = (d.get("last_green") or {}).get("headSha")
        head = d["first_red"]["headSha"]
    else:
        log_text = d.get("description", "")
        base, head = d.get("last_success_sha"), d["sha"]
    digest = parse_failed_log(log_text) if incident.kind == "ci_failure" else LogDigest(
        excerpt=[log_text] if log_text else [], signature=normalize(log_text),
        headline=re.sub(r"\s+", " ", log_text).strip()[:200],
        paths=extract_paths([log_text]))
    try:
        # GitHub-managed workflows don't build the repo's code, so a commit range would blame noise.
        commits, files, compare_url = (([], [], None) if is_github_managed(incident)
                                       else _compare(gh, incident.repo, base, head))
    except GhError as e:
        errors.append(f"compare: {e}")
        commits, files, compare_url = [], [], None
    text_for_rules = "\n".join(digest.excerpt) + "\n" + digest.signature
    step_text = "\n".join(digest.segment) if digest.segment else log_text
    cls = classify(step_text, incident, len(commits), aggregator=digest.aggregator,
                   test_target=digest.test_target,
                   failure_text=f"{digest.signature}\n{digest.error}")
    return Triage(
        digest=digest, log_text=log_text, classification=cls, commits=commits,
        changed_files=files, compare_url=compare_url,
        suspected=suspected_files(files, digest.paths, digest.test_target),
        repro=repro_command(digest, head, repo, d["latest_red"]["databaseId"])
        if incident.kind == "ci_failure"
        else build_repro(head, repo),
        lessons=matching_lessons(lessons, text_for_rules),
        errors=errors,
    )
