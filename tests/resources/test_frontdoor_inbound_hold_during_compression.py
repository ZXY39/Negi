"""压缩在途的入站闸门契约：手动上下文压缩期间任何车道都不得起新回合。

钉住的合同（docs/FIX_PLAN_frontdoor-inbound-hold-during-compression.md）：

- `frontdoor_inbound_hold` 是 `session_is_running` 的超集：有回合在跑 → `turn_running`；
  手动压缩在跑 → `manual_context_compression`；自动压缩（跑在回合内）不算额外状态。
- 渠道车道（`/api/v1`，QQ 官方/onebot/openai-compat 都回环到这里）在 hold 期间
  必须走既有的排队分支：`{"status": "queued", "receipt": ...}` + `queue_follow_up_batch`，
  且一次 `prompt` 都不许发生；空闲后照常起回合。
- 心跳车道（`heartbeat/session_service` 的"忙则改期"分支）读同一个判定；该分支在私有
  协程内部，这里不单独钉，靠谓词与真会话两条用例保证它拿到的值正确。

实盘事故（2026-09-21，ext:qq-official:f8a8001865631301）：13:05:31 点压缩，13:06:01
的渠道消息照常起回合，13:06:28 落地的 17,956 tok 摘要被 13:06:46 的 115,338 tok
旧基线覆盖，摘要里那份收口水位线选择器一起消失 —— 现在这条路径必须被闸门挡住。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.core.state import AgentState
from g3ku.runtime.api.external_turns import (
    QUEUED_RECEIPT_TEXT,
    ExternalTurnService,
)
from g3ku.runtime.bridge import SessionRuntimeBridge
from g3ku.runtime.external_events import get_session_event_hub, reset_session_event_hubs
from g3ku.runtime.external_sessions import ExternalSessionEntry
from g3ku.runtime.session_agent import (
    MANUAL_COMPRESSION_RUNNING,
    MANUAL_COMPRESSION_STATE_ATTR,
    RuntimeAgentSession,
)
from g3ku.session.manager import SessionManager


@pytest.fixture(autouse=True)
def _clean_hubs():
    reset_session_event_hubs()
    yield
    reset_session_event_hubs()


def _host(state: AgentState, manual: dict | None = None) -> SimpleNamespace:
    """只借用 RuntimeAgentSession 上的那个判定方法，避免拉起完整回合运行时。"""
    host = SimpleNamespace(_state=state)
    host.frontdoor_inbound_hold = lambda: RuntimeAgentSession.frontdoor_inbound_hold(host)
    if manual is not None:
        setattr(host, MANUAL_COMPRESSION_STATE_ATTR, manual)
    return host


def _state(*, running: bool, session_key: str = "ext:test-bridge:hold") -> AgentState:
    return AgentState(
        session_key=session_key,
        is_running=running,
        status="running" if running else "idle",
    )


# -- a) 谓词本身 --------------------------------------------------------------


def test_idle_session_without_compression_has_no_hold() -> None:
    assert _host(_state(running=False)).frontdoor_inbound_hold() == ""


def test_running_turn_holds_inbound_as_turn_running() -> None:
    assert _host(_state(running=True)).frontdoor_inbound_hold() == "turn_running"


def test_manual_compression_holds_inbound_while_session_looks_idle() -> None:
    """这条就是事故的形状：pause_first=False 时 is_running 全程是 false。"""
    host = _host(
        _state(running=False),
        {
            "status": MANUAL_COMPRESSION_RUNNING,
            "source": "manual",
            "started_at": "2026-09-21T13:05:31",
        },
    )

    assert host.frontdoor_inbound_hold() == "manual_context_compression"


@pytest.mark.parametrize(
    "status",
    ["completed", "paused", "not_needed", "failed", "", None],
)
def test_terminal_compression_state_releases_the_hold(status: str | None) -> None:
    host = _host(_state(running=False), {"status": status})

    assert host.frontdoor_inbound_hold() == ""


def test_auto_compression_generation_alone_does_not_hold() -> None:
    """自动压缩跑在回合内，只看手动状态字段：不能因为代际存在就挡住入站。"""
    host = _host(_state(running=False))
    host._active_frontdoor_compression_generation = 7

    assert host.frontdoor_inbound_hold() == ""


# -- b) 车道共用的静态入口 -----------------------------------------------------


def test_bridge_static_hold_is_none_safe() -> None:
    assert SessionRuntimeBridge.frontdoor_inbound_hold(None) == ""


def test_bridge_static_falls_back_to_running_check_for_duck_sessions() -> None:
    """渠道/前端可能拿到的是鸭子类型会话（如测试替身）：没有该方法时仍必须保住
    running 那一半语义，不能因为加宽判定而放松旧契约。"""

    class _Legacy:
        state = SimpleNamespace(is_running=True, status="running")

    assert SessionRuntimeBridge.frontdoor_inbound_hold(_Legacy()) == "turn_running"

    class _Idle:
        state = SimpleNamespace(is_running=False, status="idle")

    assert SessionRuntimeBridge.frontdoor_inbound_hold(_Idle()) == ""


def test_bridge_static_delegates_to_the_session_predicate() -> None:
    host = _host(_state(running=False), {"status": MANUAL_COMPRESSION_RUNNING})

    assert SessionRuntimeBridge.frontdoor_inbound_hold(host) == "manual_context_compression"


# -- c) 渠道车道：/api/v1（QQ 官方消息走的就是这条） ---------------------------


class _ChannelSession:
    """`ExternalTurnService` 用到的会话面：running 字段 + 排队三件套 + hold 判定。"""

    def __init__(self, *, hold: str = "", running: bool = False) -> None:
        self.state = SimpleNamespace(
            is_running=running,
            status="running" if running else "idle",
        )
        self._hold = hold
        self.queued: list = []
        self.queue_persist_flags: list[bool] = []

    def frontdoor_inbound_hold(self) -> str:
        if self._hold:
            return self._hold
        return "turn_running" if self.state.is_running else ""

    async def queue_follow_up_batch(self, messages, *, persist_transcript=True):
        self.queue_persist_flags.append(persist_transcript)
        self.queued.extend(messages)
        return list(messages)

    def drain_queued_follow_up_messages(self):
        drained = list(self.queued)
        self.queued.clear()
        return drained


class _ChannelBridge:
    def __init__(self, session) -> None:
        self._session = session
        self.prompts: list = []
        self.batches: list = []

    def get_existing_session(self, session_key: str):
        _ = session_key
        return self._session

    def get_session(self, **kwargs):
        _ = kwargs
        return self._session

    async def prompt(self, message, **kwargs):
        _ = kwargs
        self.prompts.append(message)
        return SimpleNamespace(output="ok")

    async def prompt_batch(self, messages, **kwargs):
        _ = kwargs
        self.batches.append(list(messages))
        return SimpleNamespace(output="ok")


def _entry(session_key: str) -> ExternalSessionEntry:
    return ExternalSessionEntry(
        bridge_id="test-bridge",
        external_key="qq:c2c:holder",
        session_key=session_key,
        created_at="2026-09-21T13:00:00",
        title="",
    )


@pytest.mark.asyncio
async def test_channel_message_during_compression_queues_instead_of_starting_turn() -> None:
    session_key = "ext:test-bridge:hold-d1"
    session = _ChannelSession(hold="manual_context_compression")
    bridge = _ChannelBridge(session)
    service = ExternalTurnService(runtime_bridge=bridge, register_task=None)

    result = await service.submit(
        entry=_entry(session_key),
        user_message="告诉我你的上下文结构",
        idempotency_key="k-d1",
    )

    assert result == {"turn_id": None, "status": "queued", "receipt": QUEUED_RECEIPT_TEXT}
    assert session.queued == ["告诉我你的上下文结构"]
    assert session.queue_persist_flags == [True]
    assert bridge.prompts == [] and bridge.batches == []


@pytest.mark.asyncio
async def test_channel_message_still_queues_when_a_turn_is_running() -> None:
    """加宽判定不得放松原有的忙时排队契约。"""
    session_key = "ext:test-bridge:hold-d2"
    session = _ChannelSession(running=True)
    bridge = _ChannelBridge(session)
    service = ExternalTurnService(runtime_bridge=bridge, register_task=None)

    result = await service.submit(entry=_entry(session_key), user_message="m1")

    assert result["status"] == "queued"
    assert bridge.prompts == []


@pytest.mark.asyncio
async def test_channel_message_starts_normally_once_idle_again() -> None:
    session_key = "ext:test-bridge:hold-d3"
    session = _ChannelSession()
    bridge = _ChannelBridge(session)
    service = ExternalTurnService(runtime_bridge=bridge, register_task=None)

    result = await service.submit(entry=_entry(session_key), user_message="m1")

    assert result["status"] == "started"
    for _ in range(40):
        if bridge.prompts:
            break
        await asyncio.sleep(0.05)
    assert bridge.prompts == ["m1"]


# -- d) 真会话上的谓词 --------------------------------------------------------


def test_real_session_predicate_round_trips(tmp_path: Path) -> None:
    """真 RuntimeAgentSession 上同一个判定可用（property state 与 _state 不分叉）。"""
    session = _real_session(tmp_path, "web:ceo-hold-real")

    assert session.frontdoor_inbound_hold() == ""
    setattr(session, MANUAL_COMPRESSION_STATE_ATTR, {"status": MANUAL_COMPRESSION_RUNNING})
    assert session.frontdoor_inbound_hold() == "manual_context_compression"
    session.state.is_running = True
    assert session.frontdoor_inbound_hold() == "turn_running"


# -- e) 队列的 durable 那一半：pending 行可重放 --------------------------------


def _loop(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(workspace),
        multi_agent_runner=None,
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _key: None,
        release_session_cancellation_token=lambda _key, _token: None,
        _use_rag_memory=lambda: False,
    )


def _real_session(workspace: Path, key: str) -> RuntimeAgentSession:
    return RuntimeAgentSession(
        _loop(workspace),
        session_key=key,
        channel="ext",
        chat_id=key.rsplit(":", 1)[-1],
    )


def _transcript_rows(workspace: Path, key: str) -> list[dict]:
    return list(SessionManager(workspace).get_or_create(key).messages or [])


@pytest.mark.asyncio
async def test_queued_message_survives_a_restart_in_transcript_order(tmp_path: Path) -> None:
    """重启=新 SessionManager + 新会话对象：队列必须从盘上接回来，顺序不变。"""
    key = "ext:test-bridge:queue-restart"
    session = _real_session(tmp_path, key)

    await session.queue_follow_up_batch(["第一条在排队", "第二条在排队"], persist_transcript=True)
    assert [str(item.content) for item in session._state.queued_follow_up_messages] == [
        "第一条在排队",
        "第二条在排队",
    ]

    reopened = _real_session(tmp_path, key)
    queued = reopened._state.queued_follow_up_messages

    assert [str(item.content) for item in queued] == ["第一条在排队", "第二条在排队"]
    rows = [
        row
        for row in _transcript_rows(tmp_path, key)
        if str(row.get("role") or "") == "user"
    ]
    assert [str(row.get("content") or "") for row in rows] == ["第一条在排队", "第二条在排队"]


@pytest.mark.asyncio
async def test_rehydrated_items_keep_their_turn_id_so_the_row_flips_once_sent(
    tmp_path: Path,
) -> None:
    """接回来必须带原 turn_id：派发后 `_persist_turn_transcript` 按同一 turn_id 升
    completed，不会在转录里再插一条重复的用户消息。"""
    key = "ext:test-bridge:queue-turn-id"
    session = _real_session(tmp_path, key)
    await session.queue_follow_up_batch(["保持 turn_id"], persist_transcript=True)
    original_turn_id = str(
        (session._state.queued_follow_up_messages[0].metadata or {}).get("_transcript_turn_id") or ""
    ).strip()
    assert original_turn_id

    reopened = _real_session(tmp_path, key)
    item = reopened._state.queued_follow_up_messages[0]

    assert str((item.metadata or {}).get("_transcript_turn_id") or "").strip() == original_turn_id
    assert str((item.metadata or {}).get("_transcript_state") or "") == "pending"


@pytest.mark.asyncio
async def test_pending_row_whose_turn_already_answered_is_not_requeued(tmp_path: Path) -> None:
    """崩溃在状态翻转之前的那一轮已经答过：重发等于把同一个问题再答一遍。"""
    key = "ext:test-bridge:queue-answered"
    session = _real_session(tmp_path, key)
    await session.queue_follow_up_batch(["已经答过了"], persist_transcript=True)
    turn_id = str(
        (session._state.queued_follow_up_messages[0].metadata or {}).get("_transcript_turn_id") or ""
    ).strip()

    manager = SessionManager(tmp_path)
    answered = manager.get_or_create(key)
    answered.messages = list(answered.messages)
    answered.messages.append(
        {
            "role": "assistant",
            "content": "答完了",
            "timestamp": "2026-09-21T13:07:13.308040",
            "turn_id": turn_id,
            "metadata": {},
        }
    )
    manager.save(answered)

    reopened = _real_session(tmp_path, key)

    assert list(reopened._state.queued_follow_up_messages or []) == []


@pytest.mark.asyncio
async def test_requeue_of_the_same_message_does_not_stack_duplicates(tmp_path: Path) -> None:
    """同一条渠道消息重投（幂等位之外的场景）：转录按 turn_id 原地更新，队列不叠两份。"""
    key = "ext:test-bridge:queue-idem"
    session = _real_session(tmp_path, key)
    queued = await session.queue_follow_up_batch(["同一条"], persist_transcript=True)
    session._state.queued_follow_up_messages.clear()
    session._state.queued_follow_up_messages.extend(list(queued))

    await session.queue_follow_up_batch(list(queued), persist_transcript=True)
    reopened = _real_session(tmp_path, key)

    assert [str(row.get("content") or "") for row in _transcript_rows(tmp_path, key) if str(row.get("role")) == "user"] == ["同一条"]
    assert len(reopened._state.queued_follow_up_messages) == 1


# -- f) 回到空闲后的派发 ------------------------------------------------------


class _RecordingSession:
    """真谓词 + 可观察的派发：只替换 `prompt_batch`，其余走 RuntimeAgentSession 本体。"""

    def __init__(self, session: RuntimeAgentSession) -> None:
        self._session = session
        self.calls: list[list[str]] = []
        self.hold_at_call: list[str] = []
        self.listeners_at_call: list[int] = []
        self.fail_with: Exception | None = None

    async def _prompt_batch(self, messages, **kwargs):
        _ = kwargs
        self.hold_at_call.append(self._session.frontdoor_inbound_hold())
        self.listeners_at_call.append(len(getattr(self._session, "_listeners", ())))
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append([str(getattr(item, "content", item)) for item in messages])
        return SimpleNamespace(output="答复")


@pytest.mark.asyncio
async def test_dispatch_sends_the_queue_in_order_when_idle(tmp_path: Path) -> None:
    key = "ext:test-bridge:dispatch-idle"
    session = _real_session(tmp_path, key)
    recorder = _RecordingSession(session)
    session.prompt_batch = recorder._prompt_batch  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["先到的", "后到的"], persist_transcript=True)

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    assert recorder.calls == [["先到的", "后到的"]]
    assert result["dispatched"] == 2
    assert list(session._state.queued_follow_up_messages or []) == []
    assert recorder.hold_at_call == [""]


@pytest.mark.asyncio
async def test_dispatch_refuses_while_compression_holds(tmp_path: Path) -> None:
    key = "ext:test-bridge:dispatch-held"
    session = _real_session(tmp_path, key)
    recorder = _RecordingSession(session)
    session.prompt_batch = recorder._prompt_batch  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["在排队"], persist_transcript=True)
    setattr(session, MANUAL_COMPRESSION_STATE_ATTR, {"status": MANUAL_COMPRESSION_RUNNING})

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    assert recorder.calls == []
    assert result["reason"] == "held:manual_context_compression"
    assert [str(item.content) for item in session._state.queued_follow_up_messages] == ["在排队"]


@pytest.mark.asyncio
async def test_dispatch_waits_for_a_pending_tool_approval(tmp_path: Path) -> None:
    """审批中的回合会把基线一起改掉，派发必须排在它后面。"""
    key = "ext:test-bridge:dispatch-approval"
    session = _real_session(tmp_path, key)
    recorder = _RecordingSession(session)
    session.prompt_batch = recorder._prompt_batch  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["在排队"], persist_transcript=True)
    session._state.pending_interrupts = [{"id": "i-1"}]

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    assert recorder.calls == []
    assert result["reason"] == "tool_approval_pending"
    assert len(session._state.queued_follow_up_messages) == 1


@pytest.mark.asyncio
async def test_failed_dispatch_returns_the_items_to_the_front_of_the_queue(tmp_path: Path) -> None:
    key = "ext:test-bridge:dispatch-fail"
    session = _real_session(tmp_path, key)
    recorder = _RecordingSession(session)
    recorder.fail_with = RuntimeError("provider 503")
    session.prompt_batch = recorder._prompt_batch  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["旧的一条"], persist_transcript=True)
    fresh = await session.queue_follow_up_batch(["新到的一条"], persist_transcript=True)
    _ = fresh

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    assert result["reason"] == "dispatch_failed"
    assert [str(item.content) for item in session._state.queued_follow_up_messages] == [
        "旧的一条",
        "新到的一条",
    ]


@pytest.mark.asyncio
async def test_dispatched_message_keeps_its_turn_id_so_the_row_flips_not_duplicates(
    tmp_path: Path,
) -> None:
    """派发后那一轮按同一 turn_id 升 completed：转录里不能出现第二条同样的用户消息。"""
    key = "ext:test-bridge:dispatch-flip"
    session = _real_session(tmp_path, key)
    recorder = _RecordingSession(session)
    session.prompt_batch = recorder._prompt_batch  # type: ignore[method-assign]
    queued = await session.queue_follow_up_batch(["保持 turn_id"], persist_transcript=True)
    turn_id = str((queued[0].metadata or {}).get("_transcript_turn_id") or "").strip()

    await session.dispatch_queued_follow_ups_if_idle(source="test")

    sent_item = recorder.calls[0]
    assert sent_item == ["保持 turn_id"]
    await session._persist_turn_transcript(
        user_input=queued[0],
        user_text="保持 turn_id",
        assistant_text="答复",
        interaction_flow=[],
        internal_source=None,
        route_kind="",
    )
    rows = [
        row
        for row in _transcript_rows(tmp_path, key)
        if str(row.get("role") or "") == "user"
    ]
    assert len(rows) == 1
    assert str((rows[0].get("metadata") or {}).get("_transcript_state") or "") == "completed"
    assert str((rows[0].get("metadata") or {}).get("_transcript_turn_id") or "") == turn_id
    # 翻转之后，重启不再接回这条。
    assert _real_session(tmp_path, key)._state.queued_follow_up_messages == []


@pytest.mark.asyncio
async def test_dispatch_relay_is_subscribed_only_for_its_own_turn(tmp_path: Path) -> None:
    """relay 订阅必须随派发结束：留在 _listeners 里，之后每个回合的事件都会往这个已终局
    的 turn_id 上灌（ruff 的 F841 就是在这一处抓到的）。"""
    key = "ext:test-bridge:dispatch-listeners"
    session = _real_session(tmp_path, key)
    recorder = _RecordingSession(session)
    session.prompt_batch = recorder._prompt_batch  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["在排队"], persist_transcript=True)
    before = len(session._listeners)

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    assert result["dispatched"] == 1
    assert recorder.listeners_at_call == [before + 1]
    assert len(session._listeners) == before


@pytest.mark.asyncio
async def test_dispatch_publishes_exactly_one_hub_terminal_event(tmp_path: Path) -> None:
    """渠道的 SSE pump 靠终局事件收口：派发出去的回合也要遵守同一条回合契约。"""
    key = "ext:test-bridge:dispatch-hub"
    session = _real_session(tmp_path, key)
    recorder = _RecordingSession(session)
    session.prompt_batch = recorder._prompt_batch  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["在排队"], persist_transcript=True)

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    events = get_session_event_hub(key).replay(0)
    assert [str(event["type"]) for event in events] == ["turn.started", "turn.completed"]
    assert {str(event.get("turn_id")) for event in events} == {str(result["turn_id"])}
    assert len(session._listeners) == 0


@pytest.mark.asyncio
async def test_failed_dispatch_still_closes_the_hub_turn(tmp_path: Path) -> None:
    key = "ext:test-bridge:dispatch-hub-fail"
    session = _real_session(tmp_path, key)
    recorder = _RecordingSession(session)
    recorder.fail_with = RuntimeError("provider 503")
    session.prompt_batch = recorder._prompt_batch  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["在排队"], persist_transcript=True)

    await session.dispatch_queued_follow_ups_if_idle(source="test")

    events = get_session_event_hub(key).replay(0)
    assert [str(event["type"]) for event in events] == ["turn.started", "turn.failed"]
    assert len(session._listeners) == 0


# -- g) 压缩收尾这个缝确实接上了 ----------------------------------------------

class _EndpointSession:
    """手动压缩端点用的替身：真谓词 + 记录派发时刻的 hold 状态。"""

    def __init__(self) -> None:
        self.state = _state(running=False, session_key="web:shared")
        self._state = self.state
        self._compression_state: dict = {}
        self._active_frontdoor_compression_generation = None
        self.dispatch_calls: list[tuple[str, str]] = []
        self.pause_calls: list[bool] = []

    frontdoor_inbound_hold = RuntimeAgentSession.frontdoor_inbound_hold

    def _emit_state_snapshot(self):
        return asyncio.sleep(0)

    def _sync_completed_continuity_snapshot(self, **kwargs):
        _ = kwargs
        return None

    def append_context_compression_marker(self, **kwargs):
        _ = kwargs
        return True

    def _cancel_active_frontdoor_compression_generation(self):
        return None

    async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict:
        # 关键断言点：派发发生在压缩已终局之后，否则 hold 还在，队列原地不动。
        self.dispatch_calls.append((source, self.frontdoor_inbound_hold()))
        return {"dispatched": 0, "reason": "empty", "source": source}


@pytest.mark.asyncio
async def test_compression_finish_seam_dispatches_after_the_hold_is_released(
    tmp_path: Path, monkeypatch
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from g3ku.runtime.api import ceo_sessions
    from g3ku.session.manager import SessionManager

    class _Runner:
        async def compress_session_context(self, *, session):
            _ = session
            return {
                "applied": True,
                "reason": "",
                "pre_tokens": 116_036,
                "post_tokens": 17_956,
                "provider_model": "openai:glm-5.2",
                "context_window_tokens": 390_000,
                "compression_mode": "llm",
            }

    runtime_session = _EndpointSession()
    session_manager = SessionManager(tmp_path)
    stored = session_manager.get_or_create("web:shared")
    session_manager.save(stored)
    runtime_manager = SimpleNamespace(
        get=lambda session_id: runtime_session if session_id == "web:shared" else None,
        get_or_create=lambda **kwargs: runtime_session
        if kwargs.get("session_key") == "web:shared"
        else None,
    )
    agent = SimpleNamespace(
        sessions=session_manager,
        multi_agent_runner=_Runner(),
        memory_manager=None,
    )
    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (agent, session_manager, runtime_manager, SimpleNamespace()),
    )

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix="/api")
    client = TestClient(app)
    response = client.post("/api/ceo/sessions/web:shared/compress-context")
    assert response.status_code == 200
    for _ in range(60):
        if runtime_session.dispatch_calls:
            break
        await asyncio.sleep(0.05)

    assert runtime_session.dispatch_calls == [("manual_compression_finished", "")]
    view = client.get("/api/ceo/sessions/web:shared/compress-context").json()
    assert view["status"] == "completed"
    assert view["post_tokens"] == 17_956


# -- h) 重启后的启动重放 ------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_scan_lists_only_sessions_with_a_queued_row(tmp_path: Path) -> None:
    """粗筛要有区分度：排队中的进、正常答完的不进——否则启动时会把所有会话都构造一遍。"""
    key = "ext:test-bridge:boot-scan"
    session = _real_session(tmp_path, key)
    await session.queue_follow_up_batch(["重启时还在排队"], persist_transcript=True)

    manager = SessionManager(tmp_path)
    answered = manager.get_or_create("ext:test-bridge:boot-answered")
    answered.messages = list(answered.messages)
    answered.messages.append(
        {
            "role": "user",
            "content": "正常答完的一条",
            "timestamp": "2026-09-21T13:11:03.029342",
            "metadata": {"_transcript_turn_id": "t-answered", "_transcript_state": "completed"},
        }
    )
    answered.messages.append(
        {
            "role": "assistant",
            "content": "答完了",
            "timestamp": "2026-09-21T13:11:21.628361",
            "turn_id": "t-answered",
            "metadata": {},
        }
    )
    manager.save(answered)

    keys = SessionManager(tmp_path).keys_with_pending_user_rows()

    assert keys == [key]


@pytest.mark.asyncio
async def test_boot_replay_dispatches_each_scanned_session_in_order(tmp_path: Path) -> None:
    from g3ku.shells import web

    calls: list[tuple[str, str]] = []

    class _Sessions:
        def keys_with_pending_user_rows(self) -> list[str]:
            return ["ext:qq-official:boot-a", "web:ceo-boot-b"]

    class _DispatchingSession:
        def __init__(self, key: str) -> None:
            self._key = key

        async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict:
            calls.append((self._key, source))
            return {"dispatched": 1 if self._key.endswith("boot-a") else 0, "reason": ""}

    class _Manager:
        def get_or_create(self, **kwargs):
            return _DispatchingSession(str(kwargs.get("session_key") or ""))

    agent = SimpleNamespace(sessions=_Sessions())

    replayed = await web.replay_queued_follow_ups(agent, _Manager())

    assert calls == [
        ("ext:qq-official:boot-a", "boot_replay"),
        ("web:ceo-boot-b", "boot_replay"),
    ]
    assert replayed == 1


@pytest.mark.asyncio
async def test_boot_replay_survives_one_session_failing_to_construct(tmp_path: Path) -> None:
    from g3ku.shells import web

    calls: list[str] = []

    class _Sessions:
        def keys_with_pending_user_rows(self) -> list[str]:
            return ["ext:qq-official:broken", "ext:qq-official:good"]

    class _Good:
        async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict:
            calls.append(source)
            return {"dispatched": 1, "reason": ""}

    class _Manager:
        def get_or_create(self, **kwargs):
            if str(kwargs.get("session_key") or "").endswith("broken"):
                raise RuntimeError("transcript unreadable")
            return _Good()

    replayed = await web.replay_queued_follow_ups(SimpleNamespace(sessions=_Sessions()), _Manager())

    assert calls == ["boot_replay"]
    assert replayed == 1
