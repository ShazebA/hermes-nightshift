"""Contract test against the real `aetherbus` package (skipped when it isn't installed).

No broker needed: AetherBus's transport factory is swapped for an in-memory one, so this checks
that Nightshift's publish call matches the real Bus API, produces a valid CloudEvents envelope
with a pointer (dataref) and no inline payload, and that AetherBus's own `Event.resolve()` (what
`aetherbus-mcp` calls on resources/read) turns the dataref back into the brief.
"""

import asyncio
import json

import pytest

aetherbus = pytest.importorskip("aetherbus")
from aetherbus import bus as ab_bus  # noqa: E402
from aetherbus.envelope import Envelope  # noqa: E402
from aetherbus.event import Event  # noqa: E402
from conftest import NOW  # noqa: E402

from nightshift import pipeline  # noqa: E402
from nightshift.sinks import aetherbus as sink  # noqa: E402


class RecordingTransport:
    def __init__(self):
        self.published = []

    async def connect(self):
        pass

    async def close(self):
        pass

    async def ensure_stream(self, name, subjects):
        self.subjects = subjects

    async def publish(self, topic, payload, headers):
        self.published.append((topic, payload, headers))


def test_publish_round_trips_through_real_aetherbus(cfg, gh, monkeypatch):
    transport = RecordingTransport()
    monkeypatch.setattr(ab_bus, "_build_transport", lambda settings: transport)
    monkeypatch.setattr(sink, "broker_reachable", lambda url: True)
    c = cfg(aetherbus={"enabled": True})

    r = pipeline.run_once(c, gh, now=NOW)

    assert {h.aetherbus for h in r.new_failures} == {"published"}
    assert len(transport.published) == 3
    # topic must be captured by AetherBus's default JetStream stream or it is never persisted
    assert any(c.aetherbus.topic.startswith(s.rstrip(">")) for s in transport.subjects)
    for (topic, raw, headers), h in zip(transport.published, r.new_failures, strict=True):
        env = Envelope.from_bytes(raw)
        assert topic == env.type == c.aetherbus.topic
        assert env.data is None and env.dataref == h.brief_json.resolve().as_uri()
        assert env.aetherbuscorrelationid == h.incident.id
        assert headers["nightshift-category"] in ("flaky", "real", "infra", "unknown")
        resolved = asyncio.run(Event(env).resolve())
        assert resolved == json.loads(h.brief_json.read_text())
        assert resolved["handoff"]["aetherbus_event_id"] == env.id
    # nothing was parked in AetherBus's outbox, and it never lands in the cwd
    assert not list(c.state_dir.parent.glob(".outbox.sqlite"))
