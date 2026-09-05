"""Event hub, SSE stream, and AgentEvent relay tests for the External Agent API.

The SSE endpoint is exercised by invoking the endpoint coroutine directly and
iterating the returned ``StreamingResponse.body_iterator``: neither this
Starlette TestClient build (waits for full response completion) nor httpx
ASGITransport (same) can stream incremental chunks. Real over-the-wire SSE is
covered by the mock-bridge E2E script (rebuild Step 3).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from g3ku.core.events import AgentEvent
from g3ku.runtime.api import external_v1
from g3ku.runtime.api.external_auth import ExternalApiPrincipal
from g3ku.runtime.external_events import (
    SessionEventHub,
    get_session_event_hub,
    make_session_event_relay,
    reset_session_event_hubs,
)
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)

_PRINCIPAL = ExternalApiPrincipal(bridge_id="test-bridge", label="test")


@pytest.fixture(autouse=True)
def clean_hubs():
    reset_session_event_hubs()
    yield
    reset_session_event_hubs()


def test_hub_assigns_monotonic_seq_and_replays():
    hub = SessionEventHub("ext:b:1", buffer_size=64)
    hub.publish("turn.started", turn_id="t1")
    hub.publish("reply.delta", turn_id="t1", text="hello")
    hub.publish("turn.completed", turn_id="t1")

    events = hub.replay(0)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [e["type"] for e in events] == ["turn.started", "reply.delta", "turn.completed"]
    assert hub.replay(2) == [events[2]]
    assert hub.replay(3) == []


def test_hub_buffer_evicts_oldest_when_full():
    hub = SessionEventHub("ext:b:2", buffer_size=16)
    for index in range(20):
        hub.publish("progress", text=str(index))
    events = hub.replay(0)
    assert len(events) == 16
    assert events[0]["seq"] == 5  # oldest 4 evicted
    assert events[-1]["seq"] == 20


def test_relay_maps_session_events():
    reset_session_event_hubs()
    session_key = "ext:test-bridge:relay"
    relay = make_session_event_relay(session_key, turn_id="t9")

    import asyncio

    async def dispatch(events):
        for event in events:
            await relay(event)

    asyncio.run(
        dispatch(
            [
                AgentEvent(type="assistant_stream_delta", payload={"turn_id": "t9", "text": "流式文本", "source": "user"}),
                AgentEvent(type="message_delta", payload={"kind": "progress", "text": "阶段推进"}),
                AgentEvent(type="tool_execution_start", payload={"tool_name": "shell"}),
                AgentEvent(type="tool_execution_end", payload={"is_error": True, "text": "工具失败"}),
                AgentEvent(
                    type="message_end",
                    payload={"turn_id": "t9", "text": "最终答复 [SESSION EVENTS]\ninternal", "source": "user"},
                ),
            ]
        )
    )

    hub = get_session_event_hub(session_key)
    events = hub.replay(0)
    types = [e["type"] for e in events]
    assert types == ["reply.delta", "progress", "progress", "progress", "reply.final"]

    delta = events[0]
    assert delta["text"] == "流式文本" and delta["turn_id"] == "t9"

    progress_kinds = [e["kind"] for e in events[1:4]]
    assert progress_kinds == ["milestone", "tool", "tool_error"]
    assert events[2]["text"].startswith("🔧 ")
    assert events[3]["text"].startswith("⚠️ ")

    final = events[4]
    assert final["text"] == "最终答复"  # sanitized internal tail removed
    assert final["turn_id"] == "t9"


def test_relay_skips_internal_ack_message_end():
    session_key = "ext:test-bridge:ack"
    relay = make_session_event_relay(session_key, turn_id="t1")

    import asyncio

    asyncio.run(relay(AgentEvent(type="message_end", payload={"heartbeat_internal": True, "text": "HEARTBEAT_OK"})))
    assert get_session_event_hub(session_key).replay(0) == []


@pytest.fixture
def sse_env(monkeypatch, tmp_path):
    reset_external_session_registry()
    registry = ExternalSessionRegistry(tmp_path)
    monkeypatch.setattr(external_v1, "get_external_session_registry", lambda: registry)
    return registry


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


async def _open_stream(session_key: str, *, last_event_id: str | None = None):
    response = await external_v1.stream_external_events(
        session_key,
        _FakeRequest(),
        principal=_PRINCIPAL,
        last_event_id=last_event_id,
    )
    assert response.media_type == "text/event-stream"
    return response.body_iterator


async def _collect_lines(iterator, *, want_data_lines: int, timeout: float = 5.0) -> list[str]:
    lines: list[str] = []
    data_count = 0

    async def pump() -> None:
        nonlocal data_count
        async for chunk in iterator:
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8")
            for line in text.splitlines():
                lines.append(line)
                if line.startswith("data:"):
                    data_count += 1
            if data_count >= want_data_lines:
                return

    await asyncio.wait_for(pump(), timeout=timeout)
    return lines


@pytest.mark.asyncio
async def test_sse_replays_backlog_with_last_event_id(sse_env):
    registry = sse_env
    entry, _ = registry.resolve_or_create(bridge_id="test-bridge", external_key="qq:dm:sse")
    hub = get_session_event_hub(entry.session_key)
    hub.publish("turn.started", turn_id="t1")
    hub.publish("reply.delta", turn_id="t1", text="abc")
    hub.publish("turn.completed", turn_id="t1")

    iterator = await _open_stream(entry.session_key)
    lines = await _collect_lines(iterator, want_data_lines=3)
    await iterator.aclose()

    data_lines = [l for l in lines if l.startswith("data:")]
    assert len(data_lines) == 3
    assert '"turn.started"' in data_lines[0]
    assert '"turn.completed"' in data_lines[2]
    id_lines = [l for l in lines if l.startswith("id:")]
    assert id_lines[:3] == ["id: 1", "id: 2", "id: 3"]

    # Replay honors Last-Event-ID: only events after seq 2 arrive.
    iterator2 = await _open_stream(entry.session_key, last_event_id="2")
    lines2 = await _collect_lines(iterator2, want_data_lines=1)
    await iterator2.aclose()

    data_lines2 = [l for l in lines2 if l.startswith("data:")]
    assert len(data_lines2) == 1
    assert '"turn.completed"' in data_lines2[0]


@pytest.mark.asyncio
async def test_sse_unknown_session_404(sse_env):
    with pytest.raises(HTTPException) as exc:
        await external_v1.stream_external_events(
            "ext:test-bridge:nope", _FakeRequest(), principal=_PRINCIPAL, last_event_id=None
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_sse_foreign_bridge_session_404(sse_env):
    registry = sse_env
    registry.resolve_or_create(bridge_id="other-bridge", external_key="qq:dm:x")
    foreign = registry.get_session_key(bridge_id="other-bridge", external_key="qq:dm:x")
    with pytest.raises(HTTPException) as exc:
        await external_v1.stream_external_events(
            foreign, _FakeRequest(), principal=_PRINCIPAL, last_event_id=None
        )
    assert exc.value.status_code == 404
