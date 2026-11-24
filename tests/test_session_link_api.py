"""Session-link REST contract: symmetric writes, target validation, delete cleanup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.api import ceo_sessions
from g3ku.runtime.external_sessions import (
    get_external_session_registry,
    reset_external_session_registry,
)
from g3ku.session.manager import SessionManager
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return app


def _user(content: str) -> dict:
    return {"role": "user", "content": content, "timestamp": "2026-11-18T10:00:00"}


def _make_service(tmp_path) -> tuple[MainRuntimeService, list]:
    service = MainRuntimeService.__new__(MainRuntimeService)
    service.store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    actions: list = []

    async def _startup():
        return None

    service.startup = _startup
    service.record_operator_action = lambda **kwargs: actions.append(kwargs)
    return service, actions


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    reset_external_session_registry()
    manager = SessionManager(tmp_path)
    for key in ("web:ceo-a", "web:ceo-b"):
        session = manager.get_or_create(key)
        session.messages.append(_user("一句"))
        manager.save(session)
    state_store = wcs.WebCeoStateStore(tmp_path)
    state_store.set_active_session_id("web:ceo-a")
    runtime_manager = SimpleNamespace(get=lambda _key: None, remove=lambda _key: None)
    service, actions = _make_service(tmp_path)
    agent = SimpleNamespace(main_task_service=service)

    def _sessions_stub():
        return agent, manager, runtime_manager, state_store

    monkeypatch.setattr(ceo_sessions, "_sessions", _sessions_stub)
    yield SimpleNamespace(
        client=TestClient(_build_app()),
        manager=manager,
        registry=get_external_session_registry(tmp_path),
        workspace=tmp_path,
        service=service,
        actions=actions,
        agent=agent,
        runtime_manager=runtime_manager,
    )
    service.store.close()
    reset_external_session_registry()


def test_get_links_empty(env):
    response = env.client.get("/api/ceo/sessions/web:ceo-a/links")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session_id"] == "web:ceo-a"
    assert payload["linked"] == []
    # The dialog renders the model-facing short id next to each candidate title.
    assert payload["short_ids"]["web:ceo-b"] == env.service.session_short_id("web:ceo-b")
    assert "web:ceo-a" not in payload["short_ids"]


def test_put_links_symmetric_and_short_ids(env):
    response = env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": ["web:ceo-b"]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["added"] == ["web:ceo-b"]
    assert payload["linked"] == ["web:ceo-b"]
    # Symmetric: the peer side sees the edge without its own write.
    peer = env.client.get("/api/ceo/sessions/web:ceo-b/links").json()
    assert peer["linked"] == ["web:ceo-a"]
    assert env.service.session_short_id("web:ceo-a") == "a"
    assert env.service.session_short_id("web:ceo-b") == "b"
    # Operator attribution recorded once for the change, in model-facing ids.
    assert len(env.actions) == 1
    assert env.actions[0]["scope"] == "session:web:ceo-a"
    assert " b" in env.actions[0]["action"]


def test_put_links_noop_records_nothing(env):
    env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": ["web:ceo-b"]})
    env.actions.clear()
    response = env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": ["web:ceo-b"]})
    assert response.status_code == 200
    assert response.json()["added"] == []
    assert env.actions == []


def test_put_links_removal_clears_both_sides(env):
    env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": ["web:ceo-b"]})
    response = env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": []})
    assert response.json()["removed"] == ["web:ceo-b"]
    assert env.client.get("/api/ceo/sessions/web:ceo-b/links").json()["linked"] == []
    # Identifier rows survive unlinking.
    assert env.service.session_short_id("web:ceo-b") == "b"


@pytest.mark.parametrize(
    "target,reason",
    [
        ("web:ceo-a", "session_not_linkable"),  # self
        ("internal:persona_photo", "session_not_linkable"),
        ("china:qqbot:default:group:9", "session_not_linkable"),
        ("web:ceo-missing", "session_missing"),
        ("ext:bridge:deadbeef00000000", "session_missing"),
    ],
)
def test_put_links_rejects_bad_targets(env, target, reason):
    response = env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": [target]})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail == {"reason": reason, "target": target}
    assert env.client.get("/api/ceo/sessions/web:ceo-a/links").json()["linked"] == []


def test_put_links_requires_list(env):
    response = env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": "web:ceo-b"})
    assert response.status_code == 400
    assert response.json()["detail"] == "links_list_required"


def test_links_unknown_session_404(env):
    assert env.client.get("/api/ceo/sessions/web:nope/links").status_code == 404
    assert env.client.put("/api/ceo/sessions/web:nope/links", json={"links": []}).status_code == 404


def test_ext_target_linkable_when_registered_visible(env):
    entry, _ = env.registry.resolve_or_create(
        bridge_id="qq-official", external_key="qq:group:g1", chat_type="group"
    )
    response = env.client.put(
        "/api/ceo/sessions/web:ceo-a/links", json={"links": [entry.session_key]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["linked"] == [entry.session_key]
    # Hidden entries are not candidates.
    env.registry.set_hidden(entry.session_key, True)
    response = env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": []})
    assert response.status_code == 200
    response = env.client.put(
        "/api/ceo/sessions/web:ceo-a/links", json={"links": [entry.session_key]}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "session_not_linkable"


def _delete(env, monkeypatch, *, session_key: str, is_channel: bool, forget: bool = False, hides: bool = False):
    if hides:
        monkeypatch.setattr(ceo_sessions, "channel_session_hides_on_delete", lambda _entry: True)
    monkeypatch.setattr(ceo_sessions, "get_web_heartbeat_service", lambda _agent: None)
    monkeypatch.setattr(ceo_sessions, "delete_web_ceo_session_artifacts", lambda **_kwargs: None)

    async def _cancel(_key):
        return None

    monkeypatch.setattr(env.agent, "cancel_session_tasks", _cancel, raising=False)
    return asyncio.run(
        ceo_sessions._delete_single_ceo_session(
            env.agent,
            env.manager,
            env.runtime_manager,
            env.service,
            session_key=session_key,
            is_channel_session=is_channel,
            delete_task_records=False,
            forget_identity=forget,
        )
    )


def test_local_delete_purges_links_and_mailbox(env, monkeypatch):
    from main.models import FrontdoorSessionMessage

    env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": ["web:ceo-b"]})
    env.service.store.upsert_frontdoor_session_message(
        FrontdoorSessionMessage(
            message_id="sm:1",
            session_id="web:ceo-a",
            source_session_key="web:ceo-b",
            content="hi",
            created_at="t0",
        )
    )
    result = _delete(env, monkeypatch, session_key="web:ceo-a", is_channel=False)
    assert result["deleted"] is True
    assert env.client.get("/api/ceo/sessions/web:ceo-b/links").json()["linked"] == []
    assert env.service.store.list_pending_frontdoor_session_messages() == []
    # Identifier rows stay (never reused, never deleted).
    assert env.service.session_short_id("web:ceo-a") == "a"


def test_channel_hide_keeps_links(env, monkeypatch):
    entry, _ = env.registry.resolve_or_create(
        bridge_id="qq-official", external_key="qq:group:g2", chat_type="group"
    )
    env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": [entry.session_key]})
    result = _delete(env, monkeypatch, session_key=entry.session_key, is_channel=True, hides=True)
    assert result["identity_hidden"] is True
    assert env.client.get("/api/ceo/sessions/web:ceo-a/links").json()["linked"] == [entry.session_key]


def test_channel_forget_purges_links(env, monkeypatch):
    entry, _ = env.registry.resolve_or_create(
        bridge_id="qq-official", external_key="qq:group:g3", chat_type="group"
    )
    env.client.put("/api/ceo/sessions/web:ceo-a/links", json={"links": [entry.session_key]})
    result = _delete(env, monkeypatch, session_key=entry.session_key, is_channel=True, forget=True)
    assert result["identity_forgotten"] is True
    assert env.client.get("/api/ceo/sessions/web:ceo-a/links").json()["linked"] == []
