"""跨桥会话作用域（externalApi.crossSession）的 REST 判据。

一条车道两套作用域，最容易写坏的是"放宽读口时顺手放宽了生命周期"：改名/清空/
outbox 销账必须仍然只认本桥名下。这里把界线的两侧都钉住。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime.api import external_v1
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)
from g3ku.session.manager import SessionManager


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


@pytest.fixture
def registry(workspace):
    reset_external_session_registry()
    return ExternalSessionRegistry(workspace)


@pytest.fixture
def submits():
    return []


class _RuntimeState:
    is_running = False
    status = ""


class _RuntimeManager:
    """runtime_manager.get(key) 的形状桩：读运行档位用。"""

    def get(self, session_key):
        return SimpleNamespace(state=_RuntimeState)


class _CatalogStore:
    """只当哨兵用：断言目录构建拿到的就是运行时这一份 store。

    车道向 store 问两份路径事实（安全名、转录在不在），所以两个都得答——真
    `SessionManager` 就是这两个都实现。
    """

    def get_path(self, key):
        return SessionManager(Path.cwd()).get_path(key)

    def has_transcript(self, key):
        return self.get_path(key).exists()


_RUNTIME_STORE = _CatalogStore()


@pytest.fixture
def client(monkeypatch, workspace, registry, submits):
    manager = SessionManager(workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(external_v1, "get_external_session_registry", lambda: registry)
    monkeypatch.setattr(external_v1, "_session_manager", lambda: manager)
    monkeypatch.setattr(external_v1, "workspace_path", lambda: workspace)
    monkeypatch.setattr(external_v1, "peek_global_agent", lambda: SimpleNamespace(sessions=_RUNTIME_STORE))
    monkeypatch.setattr(external_v1, "get_runtime_manager", lambda agent: _RuntimeManager())

    class _Bridge:
        def get_existing_session(self, session_key):
            return None

    class _Service:
        _runtime_bridge = _Bridge()

        async def submit(self, *, entry, user_message, idempotency_key=None):
            submits.append(
                {
                    "bridge_id": entry.bridge_id,
                    "session_key": entry.session_key,
                    "external_key": entry.external_key,
                    "text": getattr(user_message, "content", user_message),
                }
            )
            return {"turn_id": "t-1", "status": "started"}

    monkeypatch.setattr(external_v1, "get_external_turn_service", lambda: _Service())

    app = FastAPI()
    app.include_router(external_v1.router, prefix="/api/v1")
    app.dependency_overrides[require_external_api] = lambda: ExternalApiPrincipal(
        bridge_id="poster", label="poster"
    )
    return TestClient(app)


def _as_principal(client, principal):
    client.app.dependency_overrides[require_external_api] = lambda: principal


def _cross_session_client(client):
    _as_principal(client, ExternalApiPrincipal(bridge_id="poster", label="poster", cross_session=True))
    return client


def _own_foreign_session(registry, *, bridge_id="other-bridge", external_key="qq:group:9"):
    return registry.resolve_or_create(bridge_id=bridge_id, external_key=external_key, title="别人的群")[0]


def test_scope_all_denied_without_cross_session(client):
    _own_foreign_session(external_v1._registry())
    response = client.get("/api/v1/sessions", params={"scope": "all"})
    assert response.status_code == 403
    assert response.json()["detail"] == "cross_session_scope_required"
    # 默认作用域仍然只给本桥名下
    own = client.get("/api/v1/sessions")
    assert own.status_code == 200
    assert own.json()["items"] == []


def test_scope_all_lists_preview_and_transcript_path(client, monkeypatch, workspace):
    _cross_session_client(client)
    foreign = _own_foreign_session(external_v1._registry())
    # 本地网页会话不进注册表，靠"有转录文件"这条事实被寻址
    manager = SessionManager(workspace)
    session = manager.get_or_create("web:ceo-local")
    session.add_message("user", "本地会话的第一条")
    manager.save(session)

    seen = {}

    async def _fake_catalog(session_manager, *, active_session_id, is_running_resolver, status_resolver, **kwargs):
        seen["manager_is_runtime_store"] = session_manager is _RUNTIME_STORE
        seen["active_session_id"] = active_session_id
        seen["is_running"] = is_running_resolver("web:ceo-local")
        seen["status"] = status_resolver("web:ceo-local")
        # 目录把渠道会话放在 channel_groups，"全部"必须把两侧合起来
        return {
            "items": [
                {
                    "session_id": foreign.session_key,
                    "external_key": foreign.external_key,
                    "title": foreign.title,
                    "preview_text": "别人群里最新的一句",
                    "message_count": 7,
                    "updated_at": "2026-10-10T09:00:00",
                    "is_running": False,
                    "status": "idle",
                    "session_family": "channel",
                    "session_origin": "qq-official",
                    "can_message": True,
                },
                {
                    "session_id": "web:ceo-local",
                    "title": "本地会话",
                    "preview_text": "本地会话的第一条",
                    "message_count": 1,
                    "updated_at": "2026-10-10T09:01:00",
                    "is_running": False,
                    "status": "idle",
                    "session_family": "local",
                    "session_origin": "web",
                    "can_message": True,
                },
            ],
            "channel_groups": [
                {
                    "channel_id": "qq-official",
                    "items": [
                        {
                            "session_id": "qq:group:7",
                            "title": "QQ 群",
                            "preview_text": "群里最后一句",
                            "message_count": 4,
                            "session_family": "channel",
                            "session_origin": "qq-official",
                            "can_message": True,
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr(external_v1, "build_ceo_session_catalog_async", _fake_catalog)
    response = client.get("/api/v1/sessions", params={"scope": "all"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["scope"] == "all"
    by_id = {item["session_id"]: item for item in payload["items"]}
    # 渠道侧那一条也在，且只出现一次
    assert set(by_id) == {foreign.session_key, "web:ceo-local", "qq:group:7"}
    assert seen["manager_is_runtime_store"] is True, "必须用运行时那份 store，第二份实例会整表静默跳过"
    assert by_id[foreign.session_key]["preview_text"] == "别人群里最新的一句"
    assert by_id["qq:group:7"]["transcript_path"].endswith("qq_group_7.jsonl")
    assert by_id["web:ceo-local"]["transcript_path"] == str(SessionManager(workspace).get_path("web:ceo-local"))
    # 并进来的每一行都要带"转录在不在"：只给路径等于把判断推给调用方去拼文件名。
    assert by_id["web:ceo-local"]["has_transcript"] is True
    assert by_id["qq:group:7"]["has_transcript"] is False
    assert by_id[foreign.session_key]["has_transcript"] is False
    assert seen["active_session_id"] == "ext-api:poster"
    assert seen["is_running"] is False and seen["status"] == ""


def test_cross_session_post_addresses_foreign_and_local_keys(client, submits):
    _cross_session_client(client)
    foreign = _own_foreign_session(external_v1._registry())
    response = client.post(f"/api/v1/sessions/{foreign.session_key}/messages", json={"text": "跨桥第一条"})
    assert response.status_code == 200, response.json()
    assert response.json()["session_id"] == foreign.session_key
    # 回合归属记在发起方：回复路由与幂等位都按发起方算
    assert submits[-1]["bridge_id"] == "poster"
    assert submits[-1]["session_key"] == foreign.session_key

    missing = client.post("/api/v1/sessions/web:ceo-nope/messages", json={"text": "凭空造键"})
    assert missing.status_code == 404
    assert missing.json()["detail"] == "session_not_found"


def test_default_scope_still_rejects_foreign_sessions(client, submits):
    foreign = _own_foreign_session(external_v1._registry())
    response = client.post(f"/api/v1/sessions/{foreign.session_key}/messages", json={"text": "越权"})
    assert response.status_code == 404
    assert response.json()["detail"] == "session_not_found"
    assert submits == []


def test_cross_session_does_not_open_rename_clear_or_ack(client):
    _cross_session_client(client)
    foreign = _own_foreign_session(external_v1._registry())
    renamed = client.patch(f"/api/v1/sessions/{foreign.session_key}", json={"title": "抢名字"})
    assert renamed.status_code == 404
    cleared = client.delete(f"/api/v1/sessions/{foreign.session_key}")
    assert cleared.status_code == 404
    acked = client.post(f"/api/v1/sessions/{foreign.session_key}/outbox/obx-1/ack", json={"status": "delivered"})
    assert acked.status_code == 404
    assert external_v1._registry().get_by_session_key(foreign.session_key).title == "别人的群"
