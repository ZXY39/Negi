"""渠道会话（``ext:``）接受网页输入的车道合同。

三条必须钉住的事实（设计与取舍见 docs/FIX_PLAN_channel-session-web-input.md）：

1. 可写判定 = 外部注册表里有这条会话。``china:*`` 归档与注册表丢失的孤儿 ``ext:``
   转录仍然只读 —— 这不是新增的开关位，而是 ``ExternalTurnService.submit`` 的必填
   ``entry``（回复出站要靠它路由）。
2. 网页输入的那条消息经外部车道提交后，回复会以 ``reply.final`` 落到该会话的 hub，
   渠道 pump 因此投得到 QQ。WS 原生 prompt 路径不挂 relay，绕过它就会造出
   「网页看得到回复、渠道端什么都没有」的形状。
3. 排队（会话正忙）时当帧必须补 ``ceo.state``：``queue_follow_up_batch`` 不发会话
   事件，而这条车道没有本地回合去顺带刷新候选发送条。

第 2/3 条的分支在 ``ceo_websocket`` 的闭包里，跑不起真 websocket，按同目录
``test_websocket_ceo_lane_hardening.py`` 的先例锁代码形状；判定本体（第 1 条）与
出站事件（第 2 条的行为侧）走真调用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.core.events import AgentEvent
from g3ku.runtime.api import websocket_ceo
from g3ku.runtime.api.external_turns import ExternalTurnService
from g3ku.runtime.external_events import get_session_event_hub, reset_session_event_hubs
from g3ku.runtime.external_sessions import (
    EXTERNAL_OUTBOUND_CHANNEL,
    ExternalSessionEntry,
    ExternalSessionRegistry,
    reset_external_session_registry,
)

SOURCE = Path(websocket_ceo.__file__).read_text(encoding="utf-8")
EXT_KEY = "ext:qq-official:f8a8001865631301"


def _block(start_marker: str, end_marker: str) -> str:
    start = SOURCE.index(start_marker)
    return SOURCE[start : SOURCE.index(end_marker, start)]


def _entry(session_key: str = EXT_KEY) -> ExternalSessionEntry:
    return ExternalSessionEntry(
        bridge_id="qq-official",
        external_key="qq:c2c:EB6C1F",
        session_key=session_key,
        created_at="2026-09-23T00:00:00",
    )


class _RelayBridge:
    """SessionRuntimeBridge 替身：把 ``listeners`` 收到的 relay 真的叫一遍，
    因此这条测试验的是「提交形状 → 出站事件」整条链，不只是 submit 的返回值。"""

    def __init__(self, session=None, *, reply_text: str = "已看完，两处要改。"):
        self._session = session
        self.reply_text = reply_text
        self.prompts: list = []
        self.prompt_kwargs: list[dict] = []

    def get_existing_session(self, session_key):
        return self._session

    async def prompt(self, message, **kwargs):
        self.prompts.append(message)
        self.prompt_kwargs.append(kwargs)
        relay = (kwargs.get("listeners") or [None])[0]
        if relay is not None:
            await relay(
                AgentEvent(
                    type="message_end",
                    payload={"text": self.reply_text, "source": "user"},
                )
            )
        return SimpleNamespace(output=self.reply_text)

    async def prompt_batch(self, messages, **kwargs):
        self.prompts.append(list(messages))
        self.prompt_kwargs.append(kwargs)
        return SimpleNamespace(output=self.reply_text)


class _BusySession:
    def __init__(self):
        self.state = SimpleNamespace(is_running=True, status="running")
        self.queued: list = []

    async def queue_follow_up_batch(self, messages, *, persist_transcript=True):
        self.queued.extend(messages)
        return list(messages)

    def drain_queued_follow_up_messages(self):
        drained = list(self.queued)
        self.queued.clear()
        return drained


@pytest.fixture(autouse=True)
def _clean_state():
    reset_session_event_hubs()
    reset_external_session_registry()
    yield
    reset_session_event_hubs()
    reset_external_session_registry()


# --- 1. 可写判定：注册表驱动 --------------------------------------------------


def test_gate_resolves_entry_only_for_registered_external_sessions(monkeypatch, tmp_path):
    registry = ExternalSessionRegistry(tmp_path)
    entry, _created = registry.resolve_or_create(bridge_id="qq-official", external_key="qq:c2c:EB6C1F")
    monkeypatch.setattr(websocket_ceo, "get_external_session_registry", lambda *a, **k: registry)

    resolved = websocket_ceo._external_entry_for_channel_input(entry.session_key)
    assert resolved is not None and resolved.external_key == "qq:c2c:EB6C1F"
    # 归档与本地会话没有 entry：输入闸门保持原样。
    assert websocket_ceo._external_entry_for_channel_input("china:qqbot:default:dm:user-a") is None
    assert websocket_ceo._external_entry_for_channel_input("web:ceo-1") is None
    # 孤儿 ext 转录（注册表丢了条目）也不可写。
    assert websocket_ceo._external_entry_for_channel_input("ext:qq-official:deadbeef00000000") is None


def test_gate_keeps_session_readonly_when_registry_lookup_fails(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(websocket_ceo, "get_external_session_registry", _boom)
    assert websocket_ceo._external_entry_for_channel_input(EXT_KEY) is None


# --- 2. 提交形状决定回复能否出站 ---------------------------------------------


@pytest.mark.asyncio
async def test_web_submitted_message_publishes_channel_reply_final():
    bridge = _RelayBridge()
    service = ExternalTurnService(runtime_bridge=bridge, register_task=None)
    # 网页构造的消息形状（websocket_ceo._build_user_message）与渠道 HTTP 构造的形状
    # 必须经同一条 submit 得到同样的出站事件。
    message = websocket_ceo._build_user_message("帮我看下这份报告", [])

    result = await service.submit(entry=_entry(), user_message=message)
    await asyncio.sleep(0)  # 让 _execute_turn 收尾

    assert result["status"] == "started"
    hub = get_session_event_hub(EXT_KEY)
    published = [(event["type"], event.get("text")) for event in hub.replay(0)]
    assert ("turn.started", None) in published
    assert ("reply.final", "已看完，两处要改。") in published
    assert ("turn.completed", None) in published
    # 出站路由身份：drain/pump 靠这两个值把回复送回原目标。
    kwargs = bridge.prompt_kwargs[0]
    assert kwargs["channel"] == EXTERNAL_OUTBOUND_CHANNEL
    assert kwargs["chat_id"] == "qq:c2c:EB6C1F"


@pytest.mark.asyncio
async def test_busy_channel_session_queues_web_message_without_new_turn():
    session = _BusySession()
    bridge = _RelayBridge(session=session)
    service = ExternalTurnService(runtime_bridge=bridge, register_task=None)

    result = await service.submit(entry=_entry(), user_message="等当前任务跑完再看这条")
    await asyncio.sleep(0)

    assert result["status"] == "queued"
    assert result["receipt"]
    assert bridge.prompts == []
    assert len(session.queued) == 1
    # 排队不起回合 ⇒ hub 上不该有 turn.* 事件。
    assert get_session_event_hub(EXT_KEY).replay(0) == []


# --- 3. 车道接线形状（闭包跑不起真 websocket，锁形状）--------------------------


def test_channel_input_branch_uses_the_external_lane_only():
    body = _block("async def _handle_channel_user_input(", "async def sender(")

    assert "await service.submit(entry=external_entry" in body, "网页输入未走外部车道"
    # 绕过 submit 就等于绕过 relay：QQ 端收不到回复，正是本计划要修的形状。
    # 只判调用形状，函数自己的说明文字里会提到这些名字。
    assert "_invoke_user_turn(" not in body
    assert "session.prompt(" not in body
    # 回合 task 归外部车道（注册在 None 键），WS 不得把它当自己的回合。
    assert "current_turn_task =" not in body


def test_queued_channel_input_pushes_state_frame_same_turn():
    body = _block("async def _handle_channel_user_input(", "async def sender(")

    assert "if any_queued:" in body, "排队未补帧：候选发送条要等渠道侧下一次事件才更新"
    assert "_push_stream_event('ceo.state'" in body


def test_channel_input_branch_never_reads_the_channel_transcript():
    body = _block("async def _handle_channel_user_input(", "async def sender(")

    # 渠道转录可达数十 MB：这里一次都不许读它（同 _channel_runtime_session 的理由）。
    assert "get_or_create" not in body
    assert "_pending_tool_approval_interrupts(session, session_id, None)" in body


def test_channel_branch_runs_before_the_web_lane_transcript_read():
    branch = SOURCE.index("await _handle_channel_user_input(user_messages)")
    # 最后一次出现才是主循环里的 web 车道读取（前面几处在 relay/patch 闭包里）。
    web_read = SOURCE.rindex("persisted = transcript_store.get_or_create(session_id)")
    assert branch < web_read, "渠道分支必须在 web 车道的转录读取之前分叉"


def test_readonly_gate_now_keys_on_missing_registry_entry():
    assert "if is_channel_session and external_entry is None:" in SOURCE
    # 错误合同不变：前端 org_graph_app.js:5535 与 REST 侧沿用同一个 code。
    assert "'code': 'channel_session_readonly'" in SOURCE


def test_history_edit_stays_refused_for_channel_sessions():
    # 只读语义收窄为「历史不可改」：编辑/Fork 门槛不得跟着输入闸门一起放宽。
    gates = _block("def _session_edit_fork_gates(", "def _edit_fork_eligible_turn_ids(")
    assert "if is_channel_session or not key.startswith(\"web:\"):" in gates
    assert gates.index("if is_channel_session") < gates.index("return None")
