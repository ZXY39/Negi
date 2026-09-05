"""Turn-execution REST tests for the External Agent API.

Covers the highest-priority contract carried over from the legacy China
transport: exactly one terminal event per turn on every path, running-session
follow-up queueing with the drain-loop continuation, idempotent resubmission,
and attachment message construction.

These tests run on a live asyncio loop (pytest-asyncio + ASGITransport)
because the turn executor spawns background tasks that must keep progressing
while the test polls for terminal events — the sync TestClient portal only
runs the loop during requests.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.api import external_turns, external_v1
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.external_events import get_session_event_hub, reset_session_event_hubs
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)
from g3ku.session.manager import SessionManager


class _FakeSession:
    def __init__(self, *, running: bool = False):
        self.state = SimpleNamespace(
            is_running=running,
            status="running" if running else "idle",
            queued_follow_up_messages=[],
            last_error=None,
        )
        self.queued: list = []
        self.drained_calls = 0

    async def queue_follow_up_batch(self, messages, *, persist_transcript=True):
        self.queued.extend(messages)
        return list(messages)

    def drain_queued_follow_up_messages(self):
        self.drained_calls += 1
        drained = list(self.queued)
        self.queued.clear()
        return drained

    async def archive_follow_up_chain_transition(self, *, pending_follow_up_turn_ids=None):
        return None


class _FakeBridge:
    def __init__(self, session=None, *, fail_with=None, inject_follow_up=False):
        self._session = session
        self.fail_with = fail_with
        self.inject_follow_up = inject_follow_up
        self._injected = False
        self.prompts: list = []
        self.batches: list = []
        self.pause_calls: list = []
        self.cancel_calls: list = []

    def get_existing_session(self, session_key):
        return self._session

    @staticmethod
    def session_is_running(session):
        return bool(session is not None and session.state.is_running)

    async def prompt(self, message, **kwargs):
        self.prompts.append(message)
        if self.fail_with is not None:
            raise self.fail_with
        if self.inject_follow_up and not self._injected:
            self._injected = True
            await self._session.queue_follow_up_batch([UserInputMessage(content="mid-turn follow-up")])
            await asyncio.sleep(0.05)
        return SimpleNamespace(output="ok")

    async def prompt_batch(self, messages, **kwargs):
        self.batches.append(list(messages))
        return SimpleNamespace(output="ok")

    async def pause(self, session_key, *, manual=True):
        self.pause_calls.append(session_key)
        return 1

    async def cancel(self, session_key, *, reason=""):
        self.cancel_calls.append((session_key, reason))
        return 2


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def registry(workspace):
    reset_external_session_registry()
    reset_session_event_hubs()
    reg = ExternalSessionRegistry(workspace)
    yield reg
    reset_external_session_registry()
    reset_session_event_hubs()


@pytest.fixture
def harness(monkeypatch, workspace, registry):
    """Monkeypatch module seams; returns a builder producing (app, bridge)."""

    monkeypatch.setattr(external_v1, "get_external_session_registry", lambda: registry)
    monkeypatch.setattr(external_v1, "_session_manager", lambda: SessionManager(workspace))
    monkeypatch.setattr(external_v1, "workspace_path", lambda: workspace)
    monkeypatch.setattr(external_v1, "clear_web_ceo_session_artifacts", lambda **kwargs: None)

    def build(session=None, *, fail_with=None, inject_follow_up=False):
        bridge = _FakeBridge(session, fail_with=fail_with, inject_follow_up=inject_follow_up)
        service = external_turns.ExternalTurnService(runtime_bridge=bridge, register_task=None)
        external_turns.set_external_turn_service(service)
        monkeypatch.setattr(external_v1, "get_external_turn_service", lambda: service)

        app = FastAPI()
        app.include_router(external_v1.router, prefix="/api/v1")
        app.dependency_overrides[require_external_api] = lambda: ExternalApiPrincipal(
            bridge_id="test-bridge", label="test"
        )
        return app, bridge

    yield build
    external_turns.set_external_turn_service(None)


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _wait_terminal(session_key, *, timeout=5.0):
    hub = get_session_event_hub(session_key)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        terminal = [e for e in hub.replay(0) if e["type"] in {"turn.completed", "turn.failed"}]
        if terminal:
            return hub.replay(0)
        await asyncio.sleep(0.02)
    raise AssertionError("no terminal turn event within timeout")


@pytest.mark.asyncio
async def test_idle_message_produces_exactly_one_terminal(harness, registry):
    app, bridge = harness(session=_FakeSession())
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:1"})).json()
        session_id = created["session_id"]

        response = await client.post(f"/api/v1/sessions/{session_id}/messages", json={"text": "你好"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "started"
        turn_id = body["turn_id"]

        events = await _wait_terminal(session_id)

    types = [e["type"] for e in events]
    assert types.count("turn.started") == 1
    assert types.count("turn.completed") == 1
    assert types.count("turn.failed") == 0
    assert events[-1]["type"] == "turn.completed"
    assert events[-1]["turn_id"] == turn_id
    assert len(bridge.prompts) == 1
    assert bridge.prompts[0] == "你好"


@pytest.mark.asyncio
async def test_failed_turn_emits_readable_error(harness, registry):
    app, _ = harness(session=_FakeSession(), fail_with=ValueError("模型链不可用"))
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:2"})).json()
        session_id = created["session_id"]
        await client.post(f"/api/v1/sessions/{session_id}/messages", json={"text": "hi"})
        events = await _wait_terminal(session_id)

    failed = [e for e in events if e["type"] == "turn.failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "模型链不可用"
    assert "模型链不可用" in failed[0]["detail"]
    assert not [e for e in events if e["type"] == "turn.completed"]


@pytest.mark.asyncio
async def test_running_session_queues_follow_up_with_receipt(harness, registry):
    session = _FakeSession(running=True)
    app, _ = harness(session=session)
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:group:7"})).json()
        session_id = created["session_id"]
        body = (await client.post(f"/api/v1/sessions/{session_id}/messages", json={"text": "加一句"})).json()

    assert body["status"] == "queued"
    assert body["turn_id"] is None
    assert body["receipt"] == "收到，将在当前任务中一并处理。"
    assert len(session.queued) == 1


@pytest.mark.asyncio
async def test_follow_ups_queued_during_turn_are_drained(harness, registry):
    session = _FakeSession()
    app, bridge = harness(session=session, inject_follow_up=True)
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:group:8"})).json()
        session_id = created["session_id"]
        await client.post(f"/api/v1/sessions/{session_id}/messages", json={"text": "长任务"})
        events = await _wait_terminal(session_id)

    terminal = [e for e in events if e["type"] in {"turn.completed", "turn.failed"}]
    assert len(terminal) == 1 and terminal[0]["type"] == "turn.completed"
    assert len(bridge.batches) == 1
    drained = bridge.batches[0][0]
    assert isinstance(drained, UserInputMessage)
    assert drained.content == "mid-turn follow-up"


@pytest.mark.asyncio
async def test_idempotency_key_dedupes(harness, registry):
    app, bridge = harness(session=_FakeSession())
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:3"})).json()
        session_id = created["session_id"]

        first = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"text": "once"},
            headers={"Idempotency-Key": "evt-001"},
        )
        await _wait_terminal(session_id)
        second = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"text": "once"},
            headers={"Idempotency-Key": "evt-001"},
        )

    assert second.json()["status"] == "duplicate"
    assert second.json()["turn_id"] == first.json()["turn_id"]
    assert len(bridge.prompts) == 1


@pytest.mark.asyncio
async def test_pause_turn_routes_to_bridge(harness, registry):
    app, bridge = harness(session=_FakeSession())
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:4"})).json()
        session_id = created["session_id"]
        turn_id = (await client.post(f"/api/v1/sessions/{session_id}/messages", json={"text": "x"})).json()["turn_id"]
        await _wait_terminal(session_id)

        response = await client.post(f"/api/v1/turns/{turn_id}/pause")
        missing = await client.post("/api/v1/turns/nope/pause")

    assert response.status_code == 200
    assert response.json()["paused"] is True
    assert bridge.pause_calls == [session_id]
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_cancel_session(harness, registry):
    app, bridge = harness(session=_FakeSession())
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:6"})).json()
        session_id = created["session_id"]
        response = await client.post(f"/api/v1/sessions/{session_id}/cancel")

    assert response.status_code == 200
    assert response.json()["cancelled"] == 2
    assert bridge.cancel_calls == [(session_id, "external_api_cancel")]


@pytest.mark.asyncio
async def test_state_endpoint_reports_queue_and_inflight(harness, registry):
    session = _FakeSession(running=True)
    app, _ = harness(session=session)
    session.state.queued_follow_up_messages.append(UserInputMessage(content="waiting"))
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:state"})).json()
        body = (await client.get(f"/api/v1/sessions/{created['session_id']}/state")).json()

    assert body["running"] is True
    assert body["queued_follow_ups"] == 1
    assert body["external_key"] == "qq:dm:state"


@pytest.mark.asyncio
async def test_empty_message_rejected(harness, registry):
    app, _ = harness(session=_FakeSession())
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:9"})).json()
        response = await client.post(f"/api/v1/sessions/{created['session_id']}/messages", json={"text": ""})

    assert response.status_code == 400
    assert response.json()["detail"] == "message_required"


@pytest.mark.asyncio
async def test_image_attachment_builds_image_url_block(harness, workspace, registry):
    app, bridge = harness(session=_FakeSession())
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:img"})).json()
        session_id = created["session_id"]

        png_bytes = b"\x89PNG\r\n\x1a\nfake-image-bytes"
        response = await client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "text": "看这张图",
                "attachments": [
                    {
                        "kind": "image",
                        "name": "shot.png",
                        "mime_type": "image/png",
                        "data_base64": base64.b64encode(png_bytes).decode("ascii"),
                    }
                ],
                "sender": {"id": "u9", "name": "用户九"},
            },
        )
        assert response.status_code == 200
        await _wait_terminal(session_id)

    message = bridge.prompts[0]
    assert isinstance(message, UserInputMessage)
    blocks = message.content
    assert any(b.get("type") == "text" and "Channel attachments:" in b.get("text", "") for b in blocks)
    image_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert message.metadata.get("external_attachments")[0]["kind"] == "image"
    assert message.metadata.get("external_sender") == {"id": "u9", "name": "用户九"}

    stored = Path(message.metadata["external_attachments"][0]["path"])
    assert stored.exists() and stored.read_bytes() == png_bytes
    assert str(workspace) in str(stored)


@pytest.mark.asyncio
async def test_oversized_attachment_rejected(harness, registry):
    app, _ = harness(session=_FakeSession())
    async with _client(app) as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:big"})).json()
        too_big = base64.b64encode(b"x" * (5 * 1024 * 1024 + 1)).decode("ascii")
        response = await client.post(
            f"/api/v1/sessions/{created['session_id']}/messages",
            json={"text": "big", "attachments": [{"kind": "image", "name": "big.png", "data_base64": too_big}]},
        )

    assert response.status_code == 413
