from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.session_agent import (
    _TRANSCRIPT_STATE_FAILED,
    _TRANSCRIPT_STATE_PENDING,
    RuntimeAgentSession,
)
from g3ku.session.manager import SessionManager


class _CancelToken:
    def cancel(self, *, reason: str = "") -> None:
        _ = reason


class _HeartbeatController:
    def clear_session(self, session_id: str) -> None:
        _ = session_id


class _RaisingRunner:
    """模拟 provider 400：回合在模型调用处失败，错误进入会话错误路径。"""

    async def run_turn(self, *, user_input, session, on_progress):
        _ = user_input, session, on_progress
        raise RuntimeError("Error code: 400 - provider rejected the request")


def _build_loop(tmp_path: Path, runner: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=runner,
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        web_session_heartbeat=_HeartbeatController(),
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=lambda _session_key: asyncio.sleep(0, result=0),
        _use_rag_memory=lambda: False,
    )


def _seed_failed_pair(manager: SessionManager, key: str) -> None:
    session = manager.get_or_create(key)
    session.add_message(
        "user",
        "找一下真机自动化的方向有没有什么项目",
        metadata={"_transcript_turn_id": "turn-1", "_transcript_state": _TRANSCRIPT_STATE_PENDING},
    )
    session.add_message(
        "assistant",
        "这一轮处理失败：Error code: 400",
        turn_id="turn-1",
        metadata={"source": "runtime_error"},
    )
    manager.save(session)


def _build_session(tmp_path: Path, key: str = "web:ceo-failed") -> RuntimeAgentSession:
    loop = _build_loop(tmp_path)
    return RuntimeAgentSession(loop, session_key=key, channel="web", chat_id="shared")


def test_failed_turn_is_settled_and_never_replayed(tmp_path: Path):
    manager = SessionManager(tmp_path)
    _seed_failed_pair(manager, "web:ceo-failed")

    session = _build_session(tmp_path)

    persisted = session._loop.sessions.get_or_create("web:ceo-failed")
    assert persisted.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_FAILED
    assert session._state.queued_follow_up_messages == []
    # 启动扫描按 pending 粗筛：终态化之后不会再把这条会话挑出来重放
    assert "web:ceo-failed" not in SessionManager(tmp_path).keys_with_pending_user_rows()
    result = asyncio.run(session.dispatch_queued_follow_ups_if_idle(source="test"))
    assert result["dispatched"] == 0
    assert result["reason"] == "empty"


def test_messages_queued_during_failure_stay_deliverable(tmp_path: Path):
    manager = SessionManager(tmp_path)
    _seed_failed_pair(manager, "web:ceo-failed")
    session = _build_session(tmp_path)

    new_item = UserInputMessage(
        content="补充一条",
        metadata={"_transcript_turn_id": "turn-2"},
    )
    session._state.queued_follow_up_messages.append(new_item)

    drained = session.drain_queued_follow_up_messages()
    assert [item.content for item in drained] == ["补充一条"]
    # 失败回合不再出现在任何派发通道里
    assert all("找一下真机自动化" not in item.content for item in drained)


@pytest.mark.asyncio
async def test_prompt_failure_settles_row_keeps_error_and_never_requeues(tmp_path: Path, monkeypatch):
    async def _noop_refresh(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _noop_refresh)
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    loop = _build_loop(tmp_path, _RaisingRunner())
    session = RuntimeAgentSession(
        loop,
        session_key="web:ceo-failed-e2e",
        channel="web",
        chat_id="shared",
    )

    with pytest.raises(RuntimeError):
        await session.prompt("找一下真机自动化的方向")

    persisted = SessionManager(tmp_path).get_or_create("web:ceo-failed-e2e")
    roles = [row.get("role") for row in persisted.messages]
    assert roles[0] == "user"
    assert persisted.messages[0]["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_FAILED
    error_row = next(row for row in persisted.messages if row.get("role") == "assistant")
    assert error_row["metadata"]["source"] == "runtime_error"
    assert session._state.queued_follow_up_messages == []

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")
    assert result["reason"] == "empty"
    assert "web:ceo-failed-e2e" not in SessionManager(tmp_path).keys_with_pending_user_rows()
