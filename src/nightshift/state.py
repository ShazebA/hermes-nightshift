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
    return [Lesson(pattern=x["pattern"], note=x["note"], id=x["id"], source=x.get("source", "learned"))
            for x in yaml.safe_load(p.read_text()) or []]


def add_lesson(state_dir: Path, repo: str, pattern: str, note: str, source: str = "learned",
               incident: str | None = None) -> Lesson:
    re.compile(pattern)
    if not note.strip():
        raise ValueError("lesson note is empty")
    p = lessons_path(state_dir, repo)
    rows = (yaml.safe_load(p.read_text()) or []) if p.exists() else []
    for r in rows:  # same pattern → update the note instead of duplicating
        if r["pattern"] == pattern:
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
