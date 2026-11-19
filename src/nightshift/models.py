"""Incident: one thing the night shift noticed. Failures get triaged and briefed;
notices (issues, PRs awaiting review, stale branches) only go to the digest."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

FAILURE_KINDS = ("ci_failure", "deploy_failure")
NOTICE_KINDS = ("new_issue", "pr_awaiting_review", "stale_branch")
RED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}
GREEN_CONCLUSIONS = {"success"}


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "x"


@dataclass
class Incident:
    id: str  # stable fingerprint, also the dedupe key and kanban idempotency key
    repo: str
    kind: str
    title: str
    url: str
    detected_at: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_failure(self) -> bool:
        return self.kind in FAILURE_KINDS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Incident:
        return cls(**{k: d[k] for k in ("id", "repo", "kind", "title", "url", "detected_at")},
                   data=d.get("data", {}))
