"""Contract tests for ``g3ku/runtime/api/external_v1.py`` — the External Agent
API surface (``/api/v1``), restricted to contract points NOT already covered by
test_external_api_sessions.py / test_external_api_auth.py /
test_external_api_messages.py / test_external_api_events.py:

- endpoint-level auth wiring with the REAL ``require_external_api`` dependency
  (the auth suite exercises the dependency in isolation, never through a route)
- ``POST /sessions`` full get-or-create response shape and re-POST semantics
- ``GET /sessions`` list shape and unknown-key filtering
- ``GET /sessions/{id}/state`` (present in the code, absent from
  ``docs/architecture/external-agent-api.md``): idle shape, ``last_error``
  mapping, ``inflight_turn_id``, and the missing runtime-unavailable guard
- ``POST /sessions/{id}/messages`` request validation: missing text,
  empty/non-list attachments, long text pass-through, inbound text sanitation
- ``DELETE /sessions/{id}`` idempotent clear

Turn execution uses an injected fake ``ExternalTurnService``
(``set_external_turn_service``, restored to ``None`` afterwards); no real LLM,
worker, or network is touched.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from g3ku.runtime.api import external_auth, external_turns, external_v1
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.external_events import reset_session_event_hubs
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)
from g3ku.session.manager import SessionManager

_PRINCIPAL = ExternalApiPrincipal(bridge_id="test-bridge", label="test")


class _FakeSession:
    """Idle-by-default runtime session double (mirrors the messages suite)."""

    def __init__(self, *, running: bool = False):
        self.state = SimpleNamespace(
            is_running=running,
            status="running" if running else "idle",
            queued_follow_up_messages=[],
            last_error=None,
        )
        self.queued: list = []

    async def queue_follow_up_batch(self, messages, *, persist_transcript=True):
        self.queued.extend(messages)
        return list(messages)

    def drain_queued_follow_up_messages(self):
        drained = list(self.queued)
        self.queued.clear()
        return drained


class _FakeBridge:
    """Runtime-bridge double: records submitted messages without executing."""

    def __init__(self, session=None):
        self._session = session
        self.prompts: list = []

    def get_existing_session(self, session_key):
        return self._session

    async def prompt(self, message, **kwargs):
        self.prompts.append(message)
        return SimpleNamespace(output="ok")

    async def prompt_batch(self, messages, **kwargs):
        return SimpleNamespace(output="ok")

    @staticmethod
    def session_is_running(session):
        return bool(session is not None and session.state.is_running)


# -- shared fixtures ---------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_global_state():
    """Isolate process-wide singletons (registry, event hubs, turn service)."""
    reset_external_session_registry()
    reset_session_event_hubs()
    yield
    external_turns.set_external_turn_service(None)
    reset_external_session_registry()
    reset_session_event_hubs()


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def registry(workspace):
    return ExternalSessionRegistry(workspace)


@pytest.fixture
def cleared_artifacts():
    return []


@pytest.fixture
def app_factory(monkeypatch, workspace, registry, cleared_artifacts):
    """Monkeypatch module seams; returns the FastAPI app for the router with
    the fake principal dependency (auth tests build their own app instead)."""
    monkeypatch.setattr(external_v1, "get_external_session_registry", lambda: registry)
    monkeypatch.setattr(external_v1, "_session_manager", lambda: SessionManager(workspace))
    monkeypatch.setattr(external_v1, "workspace_path", lambda: workspace)
    monkeypatch.setattr(
        external_v1,
        "clear_web_ceo_session_artifacts",
        lambda **kwargs: cleared_artifacts.append(kwargs),
    )
    monkeypatch.setattr(external_v1, "peek_global_agent", lambda: None)

    app = FastAPI()
    app.include_router(external_v1.router, prefix="/api/v1")
    app.dependency_overrides[require_external_api] = lambda: _PRINCIPAL
    return app


@pytest.fixture
def harness(app_factory):
    return TestClient(app_factory)


def _install_turn_service(session):
    """Inject a fake ExternalTurnService via the process-wide setter (the same
    mechanism production wiring uses), so external_v1's imported getter returns
    it without monkeypatching."""
    bridge = _FakeBridge(session)
    service = external_turns.ExternalTurnService(runtime_bridge=bridge, register_task=None)
    external_turns.set_external_turn_service(service)
    return service, bridge


async def _wait_terminal(session_key, *, timeout=2.0):
    from g3ku.runtime.external_events import get_session_event_hub

    hub = get_session_event_hub(session_key)
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        terminal = [e for e in hub.replay(0) if e["type"] in {"turn.completed", "turn.failed"}]
        if terminal:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no terminal turn event within timeout")


async def _async_app(monkeypatch, workspace, registry):
    """Same seams as ``harness`` but for pytest-asyncio + ASGITransport tests
    (background turn tasks need a live loop). Returns (app, bridge)."""
    monkeypatch.setattr(external_v1, "get_external_session_registry", lambda: registry)
    monkeypatch.setattr(external_v1, "_session_manager", lambda: SessionManager(workspace))
    monkeypatch.setattr(external_v1, "workspace_path", lambda: workspace)
    monkeypatch.setattr(external_v1, "clear_web_ceo_session_artifacts", lambda **kwargs: None)
    monkeypatch.setattr(external_v1, "peek_global_agent", lambda: None)

    app = FastAPI()
    app.include_router(external_v1.router, prefix="/api/v1")
    app.dependency_overrides[require_external_api] = lambda: _PRINCIPAL
    return app


# -- auth failure paths (endpoint level, real dependency) ---------------------


def _config(*, enabled: bool, tokens: dict | None):
    entries = {
        token_id: SimpleNamespace(
            token=str(payload.get("token") or ""),
            enabled=bool(payload.get("enabled", True)),
            label=str(payload.get("label") or ""),
        )
        for token_id, payload in (tokens or {}).items()
    }
    return SimpleNamespace(external_api=SimpleNamespace(enabled=enabled, tokens=entries))


def _auth_app(monkeypatch, config):
    """App WITHOUT the dependency override: the real Bearer dependency runs."""
    monkeypatch.setattr(external_auth, "get_runtime_config", lambda force=False: (config, None))
    app = FastAPI()
    app.include_router(external_v1.router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def test_missing_token_rejected_401(monkeypatch):
    client = _auth_app(monkeypatch, _config(enabled=True, tokens={"qq": {"token": "sek"}}))
    response = client.post("/api/v1/sessions", json={"external_key": "qq:dm:1"})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_token"


def test_wrong_token_rejected_401(monkeypatch):
    client = _auth_app(monkeypatch, _config(enabled=True, tokens={"qq": {"token": "sek"}}))
    response = client.post(
        "/api/v1/sessions",
        json={"external_key": "qq:dm:1"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_token"


def test_disabled_api_rejected_403(monkeypatch):
    client = _auth_app(monkeypatch, _config(enabled=False, tokens={"qq": {"token": "sek"}}))
    response = client.post(
        "/api/v1/sessions",
        json={"external_key": "qq:dm:1"},
        headers={"Authorization": "Bearer sek"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "external_api_disabled"


def test_disabled_token_entry_rejected_401(monkeypatch):
    client = _auth_app(
        monkeypatch,
        _config(enabled=True, tokens={"qq": {"token": "sek", "enabled": False}}),
    )
    response = client.post(
        "/api/v1/sessions",
        json={"external_key": "qq:dm:1"},
        headers={"Authorization": "Bearer sek"},
    )
    assert response.status_code == 401


def test_valid_token_scopes_principal_to_bridge(monkeypatch):
    client = _auth_app(
        monkeypatch,
        _config(enabled=True, tokens={"qq-bot": {"token": "sek", "label": "QQ"}}),
    )
    created = client.post(
        "/api/v1/sessions",
        json={"external_key": "group:1"},
        headers={"Authorization": "Bearer sek"},
    )
    assert created.status_code == 200
    assert created.json()["session_id"].startswith("ext:qq-bot:")
    listed = client.get("/api/v1/sessions", headers={"Authorization": "Bearer sek"})
    assert listed.json()["bridge_id"] == "qq-bot"


# -- POST /sessions: idempotent get-or-create ---------------------------------


def test_post_sessions_repeat_returns_stable_full_shape(harness):
    first = harness.post("/api/v1/sessions", json={"external_key": "qq:group:1", "title": "群A"})
    assert first.status_code == 200
    first_body = first.json()
    assert set(first_body) == {"ok", "session_id", "external_key", "title", "created_at", "created"}
    assert first_body["ok"] is True
    assert first_body["created"] is True
    assert first_body["title"] == "群A"
    assert first_body["created_at"]

    second = harness.post("/api/v1/sessions", json={"external_key": "qq:group:1", "title": "群A"})
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["created"] is False
    assert second_body["session_id"] == first_body["session_id"]
    assert second_body["external_key"] == "qq:group:1"
    # created_at is the ORIGINAL creation timestamp, not the repeat's
    assert second_body["created_at"] == first_body["created_at"]
    assert second_body["title"] == "群A"


def test_post_sessions_repost_with_new_title_updates_title(harness, registry):
    first = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:2", "title": "旧标题"})
    assert first.json()["created"] is True
    second = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:2", "title": "新标题"})
    assert second.status_code == 200
    assert second.json()["created"] is False
    assert second.json()["title"] == "新标题"
    assert second.json()["session_id"] == first.json()["session_id"]
    # the registry (source of truth) was persisted with the new title
    assert registry.get_by_session_key(first.json()["session_id"]).title == "新标题"


def test_post_sessions_missing_or_blank_external_key_400(harness):
    assert harness.post("/api/v1/sessions", json={}).status_code == 400
    blank = harness.post("/api/v1/sessions", json={"external_key": "   "})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "external_key_required"


# -- GET /sessions ------------------------------------------------------------


def test_list_sessions_shape_and_filter(harness):
    empty = harness.get("/api/v1/sessions")
    assert empty.status_code == 200
    assert empty.json() == {"ok": True, "bridge_id": "test-bridge", "items": []}

    harness.post("/api/v1/sessions", json={"external_key": "qq:dm:1", "title": "一号"})
    harness.post("/api/v1/sessions", json={"external_key": "qq:dm:2"})
    body = harness.get("/api/v1/sessions").json()
    assert body["bridge_id"] == "test-bridge"
    items = body["items"]
    assert len(items) == 2
    assert all(set(item) == {"session_id", "external_key", "title", "created_at"} for item in items)
    by_key = {item["external_key"]: item for item in items}
    assert by_key["qq:dm:1"]["title"] == "一号"
    assert by_key["qq:dm:2"]["title"] == ""  # blank title surfaces as ""
    assert by_key["qq:dm:1"]["created_at"]


def test_list_sessions_unknown_external_key_returns_empty(harness):
    created = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:known"}).json()
    assert created["created"] is True
    filtered = harness.get("/api/v1/sessions", params={"external_key": "qq:dm:unknown"})
    assert filtered.status_code == 200
    assert filtered.json()["items"] == []


# -- GET /sessions/{id}/state -------------------------------------------------


def test_state_idle_session_shape(harness):
    session = _FakeSession()
    _install_turn_service(session)
    created = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:idle"}).json()
    body = harness.get(f"/api/v1/sessions/{created['session_id']}/state").json()

    assert body["ok"] is True
    assert body["session_id"] == created["session_id"]
    assert body["external_key"] == "qq:dm:idle"
    assert body["running"] is False
    assert body["queued_follow_ups"] == 0
    assert body["inflight_turn_id"] is None
    assert body["last_error"] is None


def test_state_reports_last_error_and_inflight_turn(harness):
    session = _FakeSession(running=True)
    session.state.last_error = SimpleNamespace(code="ctx_overflow", message="上下文超限")
    created = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:st"}).json()
    session_id = created["session_id"]
    service, _ = _install_turn_service(session)
    service._turns["t-busy"] = external_turns.TurnRecord(
        turn_id="t-busy",
        session_key=session_id,
        bridge_id="test-bridge",
        external_key="qq:dm:st",
        status="running",
        started_at="",
    )

    body = harness.get(f"/api/v1/sessions/{session_id}/state").json()
    assert body["running"] is True
    assert body["inflight_turn_id"] == "t-busy"
    assert body["last_error"] == {"code": "ctx_overflow", "message": "上下文超限"}


def test_state_missing_runtime_guard_returns_500(monkeypatch, app_factory):
    """Suspicious: the write endpoints translate a RuntimeError from
    get_external_turn_service() into 503 runtime_unavailable; the state
    endpoint has no such guard, so the same failure surfaces as a 500."""
    # raise_server_exceptions must be set at construction: the transport copies
    # the flag and raising-path is exercised while the app is portaled.
    client = TestClient(app_factory, raise_server_exceptions=False)

    def _unavailable():
        raise RuntimeError("runtime_unavailable")

    monkeypatch.setattr(external_v1, "get_external_turn_service", _unavailable)
    created = client.post("/api/v1/sessions", json={"external_key": "qq:dm:rg"}).json()
    assert client.get(f"/api/v1/sessions/{created['session_id']}/state").status_code == 500
    # contrast: the guarded write endpoint returns the documented 503
    assert (
        client.post(f"/api/v1/sessions/{created['session_id']}/messages", json={"text": "hi"}).status_code == 503
    )


# -- POST /sessions/{id}/messages: request validation -------------------------


def test_messages_rejects_non_list_attachments_400(harness):
    created = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:v1"}).json()
    for bad in ("data", {"kind": "image"}):
        response = harness.post(
            f"/api/v1/sessions/{created['session_id']}/messages",
            json={"text": "hi", "attachments": bad},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "attachments_must_be_list"


def test_messages_rejects_empty_attachments_list_without_text_400(harness):
    created = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:v2"}).json()
    response = harness.post(
        f"/api/v1/sessions/{created['session_id']}/messages",
        json={"attachments": []},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "message_required"


def test_messages_rejects_missing_text_400(harness):
    created = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:v3"}).json()
    response = harness.post(f"/api/v1/sessions/{created['session_id']}/messages", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "message_required"

    with_metadata = harness.post(
        f"/api/v1/sessions/{created['session_id']}/messages",
        json={"sender": {"id": "u1"}, "metadata": {"source": "test"}},
    )
    assert with_metadata.status_code == 400


# -- POST /sessions/{id}/messages: text handling with injected turn service ---


@pytest.mark.asyncio
async def test_messages_long_text_passes_through_unchanged(monkeypatch, workspace, registry):
    app = await _async_app(monkeypatch, workspace, registry)
    session = _FakeSession()
    _install_turn_service(session)
    long_text = "查" * 50_000

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:long"})).json()
        response = await client.post(
            f"/api/v1/sessions/{created['session_id']}/messages",
            json={"text": long_text},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "started"
    await _wait_terminal(created["session_id"])
    service = external_turns.get_external_turn_service()
    assert service._runtime_bridge.prompts == [long_text]


@pytest.mark.asyncio
async def test_messages_inbound_text_truncated_at_internal_marker(monkeypatch, workspace, registry):
    """The outbound reply cleaner is applied to INBOUND text: everything from
    ``[SESSION EVENTS]`` onward is dropped before the turn receives it."""
    app = await _async_app(monkeypatch, workspace, registry)
    _install_turn_service(_FakeSession())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:mk"})).json()
        await client.post(
            f"/api/v1/sessions/{created['session_id']}/messages",
            json={"text": "你好\n[SESSION EVENTS]\n内部上下文回显"},
        )
        await _wait_terminal(created["session_id"])

    service = external_turns.get_external_turn_service()
    assert service._runtime_bridge.prompts == ["你好"]


@pytest.mark.asyncio
async def test_messages_internal_marker_only_text_rejected_400(monkeypatch, workspace, registry):
    """Corollary of the above: a payload whose text cleans to empty fails with
    message_required even though the request DID carry a text field."""
    app = await _async_app(monkeypatch, workspace, registry)
    _install_turn_service(_FakeSession())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        created = (await client.post("/api/v1/sessions", json={"external_key": "qq:dm:mk2"})).json()
        response = await client.post(
            f"/api/v1/sessions/{created['session_id']}/messages",
            json={"text": "[SESSION EVENTS]\n只含内部标记"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "message_required"


# -- DELETE /sessions/{id}: clear semantics -----------------------------------


def test_delete_session_idempotent_and_response_shape(harness, workspace, registry, cleared_artifacts):
    created = harness.post("/api/v1/sessions", json={"external_key": "qq:dm:del", "title": "待清"}).json()
    session_id = created["session_id"]

    manager = SessionManager(workspace)
    session = manager.get_or_create(session_id)
    session.add_message("user", "旧上下文")
    manager.save(session)

    for _ in range(2):  # clearing twice must stay a no-op success
        response = harness.delete(f"/api/v1/sessions/{session_id}")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "cleared": True, "session_id": session_id}

    reloaded = SessionManager(workspace).get_or_create(session_id)
    assert len(reloaded.messages) == 0
    assert registry.get_by_session_key(session_id) is not None
    assert cleared_artifacts == [
        {"session_id": session_id},
        {"session_id": session_id},
    ]