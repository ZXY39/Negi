from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.core.messages import UserInputMessage
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import SessionManager


class _CancelToken:
    def cancel(self, *, reason: str = "") -> None:
        _ = reason


class _HeartbeatController:
    def clear_session(self, session_id: str) -> None:
        _ = session_id


class _StreamingThenBlockingRunner:
    """先把可见正文流出去（与真前门同一回调），然后卡在收尾之前。

    实盘丢行的形状就是这一种：最后一跳 provider 已 ``outcome=completed``，正文已到界面，
    回合还没走到 `_persist_turn_transcript` 就被取消了。
    """

    def __init__(self, *, reply_text: str, stream: bool = True) -> None:
        self.reply_text = reply_text
        self.stream = stream
        self.started = asyncio.Event()

    async def run_turn(self, *, user_input, session, on_progress):
        _ = user_input
        if self.stream:
            await session._handle_assistant_text_delta(self.reply_text)
        self.started.set()
        await asyncio.Future()


class _AnswerRunner:
    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.seen: list[str] = []

    async def run_turn(self, *, user_input, session, on_progress):
        _ = on_progress
        self.seen.append(session._history_text(user_input.content))
        return self.reply_text


class _DrainingRunner(_AnswerRunner):
    """内部回合：在 call_model 处把排队的补充消息接走（真前门同一动作）。"""

    async def run_turn(self, *, user_input, session, on_progress):
        await session.take_follow_up_batch_for_call_model()
        _ = on_progress
        for item in session._current_user_batch_inputs(user_input):
            self.seen.append(session._history_text(item.content))
        return self.reply_text


def _build_loop(tmp_path: Path, runner: object, turn_task_ref: dict) -> SimpleNamespace:
    async def _cancel_session_tasks(_session_key: str) -> int:
        task = turn_task_ref.get("task")
        if task is None:
            return 0
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return 1

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
        cancel_session_tasks=_cancel_session_tasks,
        _use_rag_memory=lambda: False,
    )


def _row_by_text(messages: list[dict], text: str) -> dict:
    for row in messages:
        if str(row.get("content") or "") == text:
            return row
    raise AssertionError(f"row not found: {text}")


async def _noop_refresh(*, force: bool = False, reason: str = "") -> None:
    _ = force, reason
    return None


@pytest.fixture(autouse=True)
def _patch_web_runtime_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _noop_refresh)


@pytest.mark.asyncio
async def test_cancellation_after_a_streamed_reply_archives_a_durable_assistant_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """取消落在收尾窗口时，已流出的可见正文必须留在转录里。

    基线形状：这一支只发控制帧就 raise，正文只剩内存里的 latest_message，刷新后气泡消失。
    """
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}
    runner = _StreamingThenBlockingRunner(reply_text="任务已创建：task:41ae566dccde")
    loop = _build_loop(tmp_path, runner, turn_task_ref)
    session = RuntimeAgentSession(loop, session_key="web:ceo-cancel-tail", channel="web", chat_id="cancel-tail")

    turn_task = asyncio.create_task(session.prompt("把这次审计派成异步任务"))
    turn_task_ref["task"] = turn_task
    await runner.started.wait()
    turn_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn_task

    messages = SessionManager(tmp_path).get_or_create("web:ceo-cancel-tail").messages
    assert [row["role"] for row in messages] == ["user", "assistant"]
    archived = messages[1]
    assert archived["content"] == "任务已创建：task:41ae566dccde"
    assert archived["status"] == "paused"
    assert archived["turn_id"] == messages[0]["metadata"]["_transcript_turn_id"]
    assert archived["metadata"]["source"] == "manual_pause_archive"
    assert archived["metadata"]["history_visible"] is False


@pytest.mark.asyncio
async def test_cancellation_without_any_visible_text_leaves_the_row_recoverable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """没出过正文就不该伪造一行答复：用户行留在 pending，重建时才会被接回重投。"""
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}
    runner = _StreamingThenBlockingRunner(reply_text="", stream=False)
    loop = _build_loop(tmp_path, runner, turn_task_ref)
    session = RuntimeAgentSession(loop, session_key="web:ceo-cancel-silent", channel="web", chat_id="cancel-silent")

    turn_task = asyncio.create_task(session.prompt("这条提问会被取消"))
    turn_task_ref["task"] = turn_task
    await runner.started.wait()
    turn_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn_task

    messages = SessionManager(tmp_path).get_or_create("web:ceo-cancel-silent").messages
    assert [row["role"] for row in messages] == ["user"]
    assert messages[0]["metadata"]["_transcript_state"] == "pending"


@pytest.mark.asyncio
async def test_later_internal_turn_does_not_retire_an_orphan_pending_user_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """实盘第二半：孤儿 pending 行不许被无关回合退役成 completed，并且仍是可接回的。

    基线里退役判据只看"不在这轮队列里"，于是任何一轮成功回合都会把别轮遗留的 pending
    行升成终态——那条提问既没回答也不再重投（08:57:22 的
    ``Retired 1 consumed pending transcript user message(s)`` 就是这一次）。
    """
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}
    runner = _StreamingThenBlockingRunner(reply_text="", stream=False)
    loop = _build_loop(tmp_path, runner, turn_task_ref)
    session = RuntimeAgentSession(loop, session_key="web:ceo-orphan-retire", channel="web", chat_id="orphan-retire")

    turn_task = asyncio.create_task(session.prompt("被取消的那条提问"))
    turn_task_ref["task"] = turn_task
    await runner.started.wait()
    turn_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn_task

    # 唯一的复活通道：durable pending 行仍然算"从未回答"。
    assert session._rehydrate_queued_follow_ups() == 1
    assert [
        session._history_text(item.content)
        for item in list(session._state.queued_follow_up_messages or [])
    ] == ["被取消的那条提问"]
    session._state.queued_follow_up_messages.clear()

    heartbeat_reply = _AnswerRunner("心跳回合只看自己的事件束。")
    loop.multi_agent_runner = heartbeat_reply
    await session.prompt(
        UserInputMessage(
            content="This is a background heartbeat.",
            metadata={"heartbeat_internal": True, "heartbeat_reason": "task_terminal"},
        )
    )

    messages = SessionManager(tmp_path).get_or_create("web:ceo-orphan-retire").messages
    orphan = _row_by_text(messages, "被取消的那条提问")
    assert orphan["metadata"]["_transcript_state"] == "pending"
    assert "被取消的那条提问" not in heartbeat_reply.seen


@pytest.mark.asyncio
async def test_internal_turn_still_retires_the_follow_up_it_drained(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """退役判据收窄后，内部回合真正 drain 走的补充行仍然要落终态（原漏点不复发）。"""
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}
    runner = _StreamingThenBlockingRunner(reply_text="", stream=False)
    loop = _build_loop(tmp_path, runner, turn_task_ref)
    session = RuntimeAgentSession(loop, session_key="web:ceo-drained-retire", channel="web", chat_id="drained-retire")

    turn_task = asyncio.create_task(session.prompt("正在跑的回合"))
    turn_task_ref["task"] = turn_task
    await runner.started.wait()
    await session.queue_follow_up_batch([UserInputMessage(content="排队里的补充")])
    turn_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn_task

    loop.multi_agent_runner = _DrainingRunner("补充已经被这一轮消费。")
    await session.prompt(
        UserInputMessage(
            content="This is a background heartbeat.",
            metadata={"heartbeat_internal": True, "heartbeat_reason": "task_terminal"},
        )
    )

    messages = SessionManager(tmp_path).get_or_create("web:ceo-drained-retire").messages
    assert _row_by_text(messages, "排队里的补充")["metadata"]["_transcript_state"] == "completed"


@pytest.mark.asyncio
async def test_resume_frontdoor_interrupt_persists_its_reply(tmp_path: Path, monkeypatch) -> None:
    """批准续跑回合的回复过去只发 message_end：转录里永远没有那一行。"""
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _noop_refresh)

    turn_task_ref: dict[str, asyncio.Task[object] | None] = {"task": None}

    class _InterruptingRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def run_turn(self, *, user_input, session, on_progress):
            _ = user_input, on_progress
            await session._handle_assistant_text_delta("批准前已经流出去的一段。")
            self.started.set()
            await asyncio.Future()

        async def resume_turn(self, *, session, resume_value, on_progress):
            _ = session, resume_value, on_progress
            return "批准后跑完的回复正文。"

    runner = _InterruptingRunner()
    loop = _build_loop(tmp_path, runner, turn_task_ref)
    session = RuntimeAgentSession(loop, session_key="web:ceo-interrupt-resume", channel="web", chat_id="interrupt-resume")

    turn_task = asyncio.create_task(session.prompt("需要批准的那条请求"))
    turn_task_ref["task"] = turn_task
    await runner.started.wait()
    turn_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn_task

    interim = SessionManager(tmp_path).get_or_create("web:ceo-interrupt-resume").messages
    assert [row["role"] for row in interim] == ["user", "assistant"]
    assert interim[1]["metadata"]["source"] == "manual_pause_archive"

    result = await session.resume_frontdoor_interrupt(resume_value={"approved": True})
    assert result.output == "批准后跑完的回复正文。"

    messages = SessionManager(tmp_path).get_or_create("web:ceo-interrupt-resume").messages
    # 归档行被就地替换：一个请求只留一行回复，不在尾部再补一行。
    assert [row["role"] for row in messages] == ["user", "assistant"]
    final_row = messages[1]
    assert final_row["content"] == "批准后跑完的回复正文。"
    assert final_row["turn_id"] == messages[0]["metadata"]["_transcript_turn_id"]
    assert final_row.get("status") is None
    assert final_row["metadata"].get("source") != "manual_pause_archive"
    assert messages[0]["metadata"]["_transcript_state"] == "completed"
