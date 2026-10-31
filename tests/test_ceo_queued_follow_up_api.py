"""撤回待发送补充的 REST 端点契约（TestClient + stub 运行时）。"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.api import ceo_sessions
from g3ku.session.manager import SessionManager


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return app


class _RuntimeStub:
    def __init__(self, *, result: bool):
        self.result = result
        self.withdrawn: list[str] = []
        self.emitted = 0

    def withdraw_queued_follow_up(self, turn_id: str) -> bool:
        self.withdrawn.append(str(turn_id or ""))
        return self.result

    async def _emit_state_snapshot(self) -> None:
        self.emitted += 1


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    manager = SessionManager(tmp_path)
    key = "web:ceo-queue"
    session = manager.get_or_create(key)
    session.messages.append({
        "role": "user",
        "content": "在排队的补充",
        "timestamp": "2026-09-23T15:00:00",
        "metadata": {"_transcript_turn_id": "t-1", "_transcript_state": "pending"},
    })
    manager.save(session)
    state_store = wcs.WebCeoStateStore(tmp_path)
    agent = SimpleNamespace(main_task_service=None)

    holder: dict[str, _RuntimeStub | None] = {"session": None}
    runtime_manager = SimpleNamespace(
        get=lambda _key: holder["session"],
        get_or_create=lambda **_kwargs: holder["session"],
        remove=lambda _key: None,
    )
    monkeypatch.setattr(
        ceo_sessions, "_sessions", lambda: (agent, manager, runtime_manager, state_store)
    )
    return SimpleNamespace(
        client=TestClient(_build_app()),
        key=key,
        holder=holder,
        manager=manager,
    )


def test_withdraw_endpoint_calls_the_session_and_emits_state(env):
    runtime = _RuntimeStub(result=True)
    env.holder["session"] = runtime

    response = env.client.post(
        f"/api/ceo/sessions/{env.key}/queued-follow-ups/withdraw", json={"turn_id": "t-1"}
    )

    assert response.status_code == 200, response.text
    assert runtime.withdrawn == ["t-1"]
    # 撤回要随帧广播，否则同会话的其他标签页仍画着那条候选。
    assert runtime.emitted == 1


def test_withdraw_endpoint_rejects_consumed_turn(env):
    env.holder["session"] = _RuntimeStub(result=False)

    response = env.client.post(
        f"/api/ceo/sessions/{env.key}/queued-follow-ups/withdraw", json={"turn_id": "t-9"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "follow_up_not_queued"


def test_withdraw_endpoint_accepts_channel_session(env):
    """渠道会话同样有队列条目：撤回口不能再按 web: 前缀把整族会话判成不存在。"""
    channel_key = "ext:qq-official-1903529517:abc123"
    session = env.manager.get_or_create(channel_key)
    session.messages.append({
        "role": "user",
        "content": "渠道里排队的一条",
        "timestamp": "2026-11-01T10:00:00",
        "metadata": {"_transcript_turn_id": "t-channel-1", "_transcript_state": "pending"},
    })
    env.manager.save(session)
    runtime = _RuntimeStub(result=True)
    env.holder["session"] = runtime

    response = env.client.post(
        f"/api/ceo/sessions/{quote(channel_key, safe='')}/queued-follow-ups/withdraw",
        json={"turn_id": "t-channel-1"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["session_id"] == channel_key
    assert runtime.withdrawn == ["t-channel-1"]
    assert runtime.emitted == 1


def test_withdraw_endpoint_requires_turn_id(env):
    response = env.client.post(f"/api/ceo/sessions/{env.key}/queued-follow-ups/withdraw", json={})

    assert response.status_code == 400
    assert response.json()["detail"] == "turn_id_required"
