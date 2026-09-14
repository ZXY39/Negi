"""编辑重发/Fork REST 端点的契约测试(TestClient + stub 运行时)。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from g3ku.runtime import web_ceo_history_edit as history_edit
from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.api import ceo_sessions
from g3ku.session.manager import SessionManager


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    return app


def _user(turn_id: str, content: str = "u") -> dict:
    return {
        "role": "user",
        "content": content,
        "timestamp": f"2026-09-14T10:00:0{turn_id}",
        "metadata": {"_transcript_turn_id": turn_id},
    }


def _assistant(turn_id: str, content: str = "a", **metadata) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "timestamp": f"2026-09-14T10:01:0{turn_id}",
        "turn_id": turn_id,
        "metadata": dict(metadata),
    }


def _continuity_payload() -> dict:
    return {
        "frontdoor_request_body_messages": [{"role": "user", "content": "第一条"}],
        "frontdoor_history_shrink_reason": "",
        "source_reason": "finalize",
    }


class _RuntimeSessionStub:
    def __init__(self, *, is_running: bool = False, queued=(), lock: asyncio.Lock | None = "default"):
        self.state = SimpleNamespace(
            is_running=is_running,
            status="running" if is_running else "completed",
            paused=False,
            pending_interrupts=[],
            queued_follow_up_messages=list(queued),
        )
        self.applied: list[tuple] = []
        if lock == "default":
            self._turn_lock = asyncio.Lock()
        elif lock is not None:
            self._turn_lock = lock

    def has_blocking_tool_execution(self) -> bool:
        return False

    def apply_history_truncation_state(self, payload, *, removed_turn_ids=None):
        self.applied.append((payload, removed_turn_ids))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(history_edit, "workspace_path", lambda: tmp_path)
    manager = SessionManager(tmp_path)
    key = "web:ceo-api"
    session = manager.get_or_create(key)
    for message in (
        _user("t1", "第一条"),
        _assistant("t1", "回复一"),
        _user("t2", "第二条"),
        _assistant("t2", "回复二"),
    ):
        session.messages.append(dict(message))
    session.metadata = {"title": "API 测试会话"}
    manager.save(session)
    wcs.write_turn_boundary_snapshot(key, "t1", _continuity_payload())

    runtime_stub = _RuntimeSessionStub()
    runtime_manager = SimpleNamespace(get=lambda _key: runtime_stub, remove=lambda _key: None)
    state_store = wcs.WebCeoStateStore(tmp_path)
    agent = SimpleNamespace(main_task_service=None)

    def _sessions_stub():
        return agent, manager, runtime_manager, state_store

    monkeypatch.setattr(ceo_sessions, "_sessions", _sessions_stub)
    client = TestClient(_build_app())
    return SimpleNamespace(
        client=client,
        manager=manager,
        key=key,
        runtime=runtime_stub,
        state_store=state_store,
        workspace=tmp_path,
    )


def test_truncate_endpoint_success(env):
    response = env.client.post(f"/api/ceo/sessions/{env.key}/truncate", json={"turn_id": "t2"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["session_id"] == env.key
    assert payload["boundary_turn_id"] == "t2"
    assert payload["removed_message_count"] == 2
    assert payload["continuity_source"] == "turn_boundary"
    # 转录已截断 + 内存会话对象就地变更。
    assert len(env.manager.get_or_create(env.key).messages) == 2
    assert len(env.runtime.applied) == 1
    assert env.runtime.applied[0][1] == ["t2"]
    sidecar = wcs.read_completed_continuity_snapshot(env.key)
    assert sidecar["frontdoor_history_shrink_reason"] == "user_edit_truncation"


def test_truncate_requires_turn_id(env):
    response = env.client.post(f"/api/ceo/sessions/{env.key}/truncate", json={})
    assert response.status_code == 400
    assert response.json()["detail"] == "turn_id_required"


def test_truncate_unknown_turn_404(env):
    response = env.client.post(f"/api/ceo/sessions/{env.key}/truncate", json={"turn_id": "missing"})
    assert response.status_code == 404
    assert response.json()["detail"] == "turn_not_found"


def test_truncate_blocked_while_running(env):
    env.runtime.state.is_running = True
    env.runtime.state.status = "running"
    response = env.client.post(f"/api/ceo/sessions/{env.key}/truncate", json={"turn_id": "t2"})
    assert response.status_code == 409
    assert response.json()["detail"] == "ceo_turn_in_progress"
    assert len(env.manager.get_or_create(env.key).messages) == 4


def test_truncate_blocked_with_queued_follow_ups(env):
    env.runtime.state.queued_follow_up_messages.append("pending follow-up")
    response = env.client.post(f"/api/ceo/sessions/{env.key}/truncate", json={"turn_id": "t2"})
    assert response.status_code == 409
    assert response.json()["detail"] == "ceo_turn_in_progress"


def test_truncate_blocked_by_task_gate(env):
    # t1 回复轮创建过任务 → t2 不可截断(严格判定:门槛复验在服务端)。
    messages = env.manager.get_or_create(env.key).messages
    messages[1]["metadata"]["task_ids"] = ["task:aaa"]
    env.manager.save(env.manager.get_or_create(env.key))
    response = env.client.post(f"/api/ceo/sessions/{env.key}/truncate", json={"turn_id": "t2"})
    assert response.status_code == 409
    assert response.json()["detail"] == "edit_fork_blocked_by_async_task"


def test_truncate_channel_session_readonly(env):
    response = env.client.post("/api/ceo/sessions/ext:qq:123/truncate", json={"turn_id": "t2"})
    assert response.status_code == 409
    assert response.json()["detail"] == "channel_session_readonly"


def test_truncate_boundary_snapshot_missing_409(env):
    wcs.clear_turn_boundary_snapshots(env.key)
    response = env.client.post(f"/api/ceo/sessions/{env.key}/truncate", json={"turn_id": "t2"})
    assert response.status_code == 409
    assert response.json()["detail"] == "boundary_unavailable"


def test_fork_endpoint_success(env):
    source_messages_before = [
        dict(m) for m in env.manager.get_or_create(env.key).messages
    ]
    response = env.client.post(f"/api/ceo/sessions/{env.key}/fork", json={"turn_id": "t2"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    fork = payload["fork"]
    new_key = fork["session_id"]
    assert new_key.startswith("web:ceo-")
    assert fork["source_session_id"] == env.key
    assert fork["copied_message_count"] == 2
    assert fork["continuity_source"] == "turn_boundary"
    assert fork["composer"]["text"] == "第二条"
    assert fork["composer"]["uploads"] == []
    # 目录载荷:active 切到新会话。
    assert payload["active_session_id"] == new_key
    assert env.state_store.get_active_session_id() == new_key
    # 新会话转录 + 连续性 sidecar 落盘;源会话零变更。
    new_session = env.manager.get_or_create(new_key)
    assert [m["role"] for m in new_session.messages] == ["user", "assistant"]
    assert new_session.metadata["title"] == "API 测试会话 · Fork"
    assert wcs.read_completed_continuity_snapshot(new_key) is not None
    assert wcs.read_turn_boundary_snapshot(new_key, "t1") is not None
    # 源会话消息零变更(_assert_known_session 的 metadata 规范化不属于消息变更)。
    assert [dict(m) for m in env.manager.get_or_create(env.key).messages] == source_messages_before
    assert len(env.manager.get_or_create(env.key).messages) == 4


def test_fork_blocked_by_task_gate(env):
    messages = env.manager.get_or_create(env.key).messages
    messages[1]["metadata"]["task_ids"] = ["task:aaa"]
    env.manager.save(env.manager.get_or_create(env.key))
    response = env.client.post(f"/api/ceo/sessions/{env.key}/fork", json={"turn_id": "t2"})
    assert response.status_code == 409
    assert response.json()["detail"] == "edit_fork_blocked_by_async_task"


def test_assert_edit_fork_runtime_idle_rejects_pending_interrupts():
    runtime = _RuntimeSessionStub()
    runtime.state.pending_interrupts = [{"id": "approval"}]
    with pytest.raises(HTTPException) as excinfo:
        ceo_sessions._assert_edit_fork_runtime_idle(runtime)
    assert excinfo.value.status_code == 409
    runtime.state.pending_interrupts = []
    ceo_sessions._assert_edit_fork_runtime_idle(runtime)  # 不抛
    ceo_sessions._assert_edit_fork_runtime_idle(None)  # 无 live 对象放行
