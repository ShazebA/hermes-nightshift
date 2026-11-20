"""Durable local state: which incidents were already handled (dedupe), an append-only incident
ledger for the digest, and the per-repo lessons file the Learn step writes to."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .config import Lesson


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class State:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.path = state_dir / "state.json"
        self.ledger = state_dir / "incidents.jsonl"
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.seen: dict[str, dict[str, Any]] = data.get("seen", {})
        self.last_digest: str | None = data.get("last_digest")
        # Ledger rows already digested. An offset, not a timestamp: rows written in the same
        # second as a digest must not be reported twice or skipped.
        self.digest_offset: int = int(data.get("digest_offset", 0))

    @staticmethod
    def key(repo: str, incident_id: str) -> str:
        return f"{repo}#{incident_id}"

    def is_seen(self, repo: str, incident_id: str) -> bool:
        return self.key(repo, incident_id) in self.seen

    def mark(self, repo: str, incident_id: str, **fields: Any) -> dict[str, Any]:
        rec = self.seen.setdefault(self.key(repo, incident_id), {"first_seen": _now()})
        rec.update(fields)
        return rec

    def pending_publish(self) -> list[tuple[str, dict[str, Any]]]:
        return [(k, v) for k, v in self.seen.items() if v.get("aetherbus") == "pending"]

    def save(self) -> None:
        _atomic_write(self.path, json.dumps(
            {"seen": self.seen, "last_digest": self.last_digest, "digest_offset": self.digest_offset},
            indent=2) + "\n")

    def append_ledger(self, entry: dict[str, Any]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_ledger(self, since: str | None = None, offset: int = 0) -> list[dict[str, Any]]:
        if not self.ledger.exists():
            return []
        rows = [json.loads(ln) for ln in self.ledger.read_text().splitlines() if ln.strip()][offset:]
        return [r for r in rows if not since or r.get("detected_at", "") >= since]

    def ledger_size(self) -> int:
        if not self.ledger.exists():
            return 0
        return sum(1 for ln in self.ledger.read_text().splitlines() if ln.strip())


# ------------------------------------------------------------------ lessons


def lessons_path(state_dir: Path, repo: str) -> Path:
    return state_dir / "lessons" / f"{repo.replace('/', '__')}.yaml"


def load_lessons(state_dir: Path, repo: str) -> list[Lesson]:
    p = lessons_path(state_dir, repo)
    if not p.exists():
        return []
    return [Lesson(pattern=x["pattern"], note=x["note"], id=x["id"], source=x.get("source", "learned"),
                   incident=x.get("incident"))
            for x in yaml.safe_load(p.read_text()) or []]


# Failure output every repo produces. A lesson that matches any of these would attach to most
# future briefs, so it is too broad no matter how long the regex is.
GENERIC_FAILURES = (
    "assert 1 == 2", "E       assert 60.01 == 60.0", "AssertionError: assert False",
    "FAILED tests/test_app.py::test_create - assert 1 == 2", "1 failed, 2 passed in 0.02s",
    "Error: expect(received).toBe(expected) // Object.is equality",
    "AssertionError: expected 1 to equal 2", "FAIL src/app.test.ts > renders",
    "TypeError: Cannot read properties of undefined (reading 'id')",
    "ReferenceError: foo is not defined", "SyntaxError: Unexpected token",
    "ModuleNotFoundError: No module named 'foo'", "npm ERR! code ELIFECYCLE",
    "npm ERR! Test failed.  See above for more details.", "error[E0308]: mismatched types",
    "error: could not compile `app` due to 2 previous errors", "--- FAIL: TestCreate (0.00s)",
    "panic: runtime error: index out of range", "Traceback (most recent call last):",
    "Error: Process completed with exit code 1.", "##[error]Process completed with exit code 1.",
    "Tests:       1 failed, 3 passed, 4 total", "make: *** [Makefile:12: test] Error 2",
)


def lesson_problems(pattern: str, incident_text: str | None = None) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a proposed lesson regex. Errors block saving: a lesson that
    matches everything pollutes every brief, and one that misses its own incident never fires."""
    errors, warnings = [], []
    try:
        rx = re.compile(pattern, re.I | re.M)
    except re.error as e:
        return [f"not a valid regex: {e}"], []
    if rx.search("") is not None or len(pattern.strip()) < 6:
        errors.append("too broad: it matches (almost) anything; use a distinctive phrase from the error")
    else:
        hit = next((g for g in GENERIC_FAILURES if rx.search(g)), None)
        if hit:
            errors.append(f"too broad: it matches generic failure output every repo prints ({hit!r}); "
                          "a lesson needs a phrase specific to this repo's recurring cause")
    if incident_text is not None and not rx.search(incident_text):
        errors.append("does not match the failing log of the incident it came from")
    if re.search(r"[0-9a-f]{7,}|\d{4,}|\d{2}:\d{2}", pattern):
        warnings.append("contains a SHA, timestamp or long number that will differ next time")
    if re.search(r"[A-Za-z]\w*\\?\.[A-Za-z]\w*|[a-z]+_[a-z]+", pattern):
        warnings.append("names a specific identifier; consider \\S+ in its place so the lesson "
                        "covers the whole class of failure")
    return errors, warnings


def add_lesson(state_dir: Path, repo: str, pattern: str, note: str, source: str = "learned",
               incident: str | None = None) -> Lesson:
    re.compile(pattern)
    if not note.strip():
        raise ValueError("lesson note is empty")
    p = lessons_path(state_dir, repo)
    rows = (yaml.safe_load(p.read_text()) or []) if p.exists() else []
    for r in rows:  # same pattern, or a retry for the same incident → update in place
        if r["pattern"] == pattern or (incident and r.get("incident") == incident):
            r["pattern"] = pattern
            r.update(note=note.strip(), updated_at=_now())
            _atomic_write(p, yaml.safe_dump(rows, sort_keys=False))
            return Lesson(pattern=pattern, note=r["note"], id=r["id"], source=r.get("source", source))
    lid = f"L{len(rows) + 1}"
    row = {"id": lid, "pattern": pattern, "note": note.strip(), "source": source,
           "added_at": _now()}
    if incident:
        row["incident"] = incident
    rows.append(row)
    _atomic_write(p, yaml.safe_dump(rows, sort_keys=False))
    return Lesson(pattern=pattern, note=row["note"], id=lid, source=source)
