"""渠道会话命名契约：群/渠道标题可改，卡片与记忆作用域下拉用同一个新名字。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.api import ceo_sessions
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    get_external_session_registry,
    reset_external_session_registry,
)
from g3ku.runtime.memory_scope_router import MemoryScopeRouter
from g3ku.session.manager import SessionManager


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return app


def _user(content: str) -> dict:
    return {"role": "user", "content": content, "timestamp": "2026-10-31T10:00:00"}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    reset_external_session_registry()
    manager = SessionManager(tmp_path)
    local = manager.get_or_create("web:shared")
    local.messages.append(_user("本地一句"))
    manager.save(local)
    state_store = wcs.WebCeoStateStore(tmp_path)
    state_store.set_active_session_id("web:shared")
    runtime_manager = SimpleNamespace(get=lambda _key: None, remove=lambda _key: None)
    agent = SimpleNamespace(main_task_service=None)

    def _sessions_stub():
        return agent, manager, runtime_manager, state_store

    monkeypatch.setattr(ceo_sessions, "_sessions", _sessions_stub)
    yield SimpleNamespace(
        client=TestClient(_build_app()),
        manager=manager,
        registry=get_external_session_registry(tmp_path),
        workspace=tmp_path,
    )
    reset_external_session_registry()


def _scope_titles(workspace) -> list[str]:
    router = MemoryScopeRouter(workspace=workspace, local_manager=None, config=None)
    return [item["title"] for item in router.list_scopes() if item["kind"] == "group"]


def test_rename_external_group_session_updates_card_and_memory_scope(env):
    entry, _ = env.registry.resolve_or_create(
        bridge_id="qq-official",
        external_key="qq:group:g1",
        chat_type="group",
    )
    session = env.manager.get_or_create(entry.session_key)
    session.messages.append(_user("在吗"))
    env.manager.save(session)

    response = env.client.patch(
        f"/api/ceo/sessions/{entry.session_key}",
        json={"title": "家庭群"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["item"]["title"] == "家庭群"
    assert payload["item"]["rename_title"] == "家庭群"
    assert payload["item"]["can_rename"] is True

    reloaded = ExternalSessionRegistry(env.workspace)
    assert reloaded.get_by_session_key(entry.session_key).title == "家庭群"
    assert env.manager.get_or_create(entry.session_key).metadata.get("title") == "家庭群"
    assert "家庭群" in _scope_titles(env.workspace)


def test_rename_orphan_external_session_returns_404(env):
    response = env.client.patch(
        "/api/ceo/sessions/ext:qq-official:deadbeef00000000",
        json={"title": "无主会话"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "session_not_found"


def test_rename_china_group_session_uses_transcript_metadata(env):
    key = "china:qqbot:default:group:9"
    session = env.manager.get_or_create(key)
    session.messages.append(_user("老档案"))
    env.manager.save(session)

    response = env.client.patch(f"/api/ceo/sessions/{key}", json={"title": "旧群改名"})
    assert response.status_code == 200, response.text
    item = response.json()["item"]
    assert item["session_id"] == key
    assert item["title"] == "旧群改名"
    assert item["rename_title"] == "旧群改名"
    assert item["can_rename"] is True
    assert env.manager.get_or_create(key).metadata.get("title") == "旧群改名"
