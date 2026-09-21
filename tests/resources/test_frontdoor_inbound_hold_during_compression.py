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
from g3ku.runtime.external_events import reset_session_event_hubs
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
    loop = SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=None,
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        create_session_cancellation_token=lambda _key: None,
        release_session_cancellation_token=lambda _key, _token: None,
        _use_rag_memory=lambda: False,
    )
    session = RuntimeAgentSession(
        loop,
        session_key="web:ceo-hold-real",
        channel="web",
        chat_id="ceo-hold-real",
    )

    assert session.frontdoor_inbound_hold() == ""
    setattr(session, MANUAL_COMPRESSION_STATE_ATTR, {"status": MANUAL_COMPRESSION_RUNNING})
    assert session.frontdoor_inbound_hold() == "manual_context_compression"
    session.state.is_running = True
    assert session.frontdoor_inbound_hold() == "turn_running"
