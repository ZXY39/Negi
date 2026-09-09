"""Outbox REST tests for the External Agent API.

覆盖持久 outbox 的两个桥侧端点：``GET /outbox/pending``（pump 预热清单，
按 bridge 隔离）与 ``POST /sessions/{id}/outbox/{outbox_id}/ack``（送达销账，
会话作用域防越权）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from g3ku.runtime import external_outbox
from g3ku.runtime.api import external_v1
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)


@pytest.fixture
def env(monkeypatch, tmp_path: Path):
    reset_external_session_registry()
    registry = ExternalSessionRegistry(tmp_path)
    monkeypatch.setattr(external_v1, "get_external_session_registry", lambda: registry)
    external_outbox.configure_external_outbox_root(tmp_path)
    app = FastAPI()
    app.include_router(external_v1.router, prefix="/api/v1")
    app.dependency_overrides[require_external_api] = lambda: ExternalApiPrincipal(
        bridge_id="qq-official", label="test"
    )
    yield registry, app
    external_outbox.configure_external_outbox_root(None)
    reset_external_session_registry()


@pytest.mark.asyncio
async def test_pending_list_is_scoped_to_own_bridge(env) -> None:
    registry, app = env
    own_entry, _ = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:u1")
    other_entry, _ = registry.resolve_or_create(bridge_id="other-bridge", external_key="xx:dm:9")
    own_id = external_outbox.record_outbound_message(
        session_key=own_entry.session_key, external_key="qq:c2c:u1", text="自己的滞留推送"
    )
    external_outbox.record_outbound_message(
        session_key=other_entry.session_key, external_key="xx:dm:9", text="别家桥的推送"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/api/v1/outbox/pending")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert [item["outbox_id"] for item in payload["items"]] == [own_id]
    assert payload["items"][0]["session_id"] == own_entry.session_key
    assert payload["items"][0]["external_key"] == "qq:c2c:u1"
    # 清单只带路由身份，不带消息正文（正文经 SSE 重放投递）。
    assert "text" not in payload["items"][0]


@pytest.mark.asyncio
async def test_ack_marks_pending_entry_delivered(env) -> None:
    registry, app = env
    entry, _ = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:u2")
    outbox_id = external_outbox.record_outbound_message(
        session_key=entry.session_key, external_key="qq:c2c:u2", text="待销账"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(f"/api/v1/sessions/{entry.session_key}/outbox/{outbox_id}/ack")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "acked": True, "outbox_id": outbox_id}
        # 幂等：重复 ack 仍返回 ok。
        response = await http.post(f"/api/v1/sessions/{entry.session_key}/outbox/{outbox_id}/ack")
        assert response.status_code == 200
    assert external_outbox.load_pending_outbound() == []


@pytest.mark.asyncio
async def test_ack_rejects_foreign_session_outbox_id(env) -> None:
    """会话 A 的 ack 不得销掉会话 B 的账（跨会话防护）。"""
    registry, app = env
    entry_a, _ = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:a")
    entry_b, _ = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:b")
    b_id = external_outbox.record_outbound_message(
        session_key=entry_b.session_key, external_key="qq:c2c:b", text="B 的账"
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(f"/api/v1/sessions/{entry_a.session_key}/outbox/{b_id}/ack")
        assert response.status_code == 200
        assert response.json()["acked"] is False
    assert [item["id"] for item in external_outbox.load_pending_outbound()] == [b_id]


@pytest.mark.asyncio
async def test_ack_unknown_session_returns_404(env) -> None:
    _, app = env
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/v1/sessions/ext:ghost:0000/outbox/obx-1/ack")
    assert response.status_code == 404
