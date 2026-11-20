"""AetherBus sink (optional): publish one CloudEvent per brief whose ``dataref`` is a
``file://`` URI to the brief JSON (pointer, not payload).

Why JSON and not the Markdown: AetherBus's MCP adapter resolves the dataref with the default
``datacontenttype=application/json`` and ``json.loads`` the bytes, so a ``.md`` dataref would
fail to resolve. The JSON carries ``handoff.brief_path`` pointing at the Markdown.

Why probe first: ``Bus.publish`` never raises on a dead broker; it queues to a SQLite outbox
and returns an id. A short-lived cron process would then report "published" for an event that
only exists in a local outbox. We probe TCP first and record ``pending`` instead, and the next
poll retries.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..config import AetherBusConfig


@dataclass
class PublishResult:
    status: str  # published | pending | disabled | unavailable | error
    event_id: str | None = None
    detail: str = ""


def broker_reachable(url: str, timeout: float = 1.5) -> bool:
    u = urlparse(url if "://" in url else f"nats://{url}")
    try:
        with socket.create_connection((u.hostname or "localhost", u.port or 4222), timeout=timeout):
            return True
    except OSError:
        return False


def event_headers(brief: dict[str, Any]) -> dict[str, str]:
    c = brief["classification"]
    return {"nightshift-repo": brief["repo"], "nightshift-kind": brief["kind"],
            "nightshift-category": c["category"], "nightshift-schema": brief["schema"]}


async def _publish(cfg: AetherBusConfig, json_path: Path, brief: dict[str, Any],
                   outbox: Path) -> str:
    from aetherbus import Bus  # optional dependency, imported lazily
    from aetherbus.config import Settings

    settings = Settings(transport_url=cfg.url, source=cfg.source, outbox_path=str(outbox))
    async with Bus(settings) as bus:
        return await bus.publish(cfg.topic, dataref=json_path.resolve().as_uri(),
                                 correlation_id=brief["id"], headers=event_headers(brief))


def publish_brief(cfg: AetherBusConfig, json_path: Path, brief: dict[str, Any],
                  state_dir: Path, publisher=None) -> PublishResult:
    if not cfg.enabled:
        return PublishResult("disabled")
    try:
        import aetherbus  # noqa: F401
    except ImportError:
        return PublishResult("unavailable", detail="aetherbus is not installed "
                             "(pip install -e /path/to/aetherbus)")
    if not broker_reachable(cfg.url):
        return PublishResult("pending", detail=f"broker not reachable at {cfg.url}")
    try:
        pub = publisher or _publish
        eid = asyncio.run(pub(cfg, json_path, brief, state_dir / "aetherbus-outbox.sqlite"))
        return PublishResult("published", event_id=eid)
    except Exception as e:  # broker/protocol errors must not lose the brief
        return PublishResult("pending", detail=f"{type(e).__name__}: {e}")
