"""回合回复的持久出站账本车道。

钉住 2026-10-05 实盘事故（task 见 docs/architecture/external-agent-api.md「持久
outbox」）：渠道会话 ``ext:qq-official-1903529517:f8a8001865631301`` 的一条网页
发起回合在 00:16:26 产出回复并落进转录，QQ 端什么都没有。判据链：

- pump 只有两个重建点——渠道入站（``bridge.py`` 的 ``on_incoming``）与账本里有
  pending 记录（``_reconcile_pending_pumps``）。网页发起的回合两个都不满足。
- ``reply.final`` 此前只进内存 hub 的环形缓冲，不登记账本，所以两条兜底道
  （服务端 60s 对账 / 桥侧 30s 对账）都看不见它。

现在每条带路由身份的可见回复都先进账本再进 hub，事件携带 ``outbox_id``。同时
账本不再按年龄清理：年龄会把请求方还在等的回答判死，回收改按"还有没有任何会
ack 的桥能看见它"判定。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.core.events import AgentEvent
from g3ku.runtime import external_outbox
from g3ku.runtime.external_events import (
    get_session_event_hub,
    make_session_event_relay,
    reset_session_event_hubs,
    wait_for_external_reply,
)
from g3ku.runtime.external_sessions import ExternalSessionRegistry, reset_external_session_registry
from g3ku.shells import web

SESSION = "ext:qq-official:f8a8001865631301"
ROUTE = "qq:c2c:EB6C1F"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_session_event_hubs()
    reset_external_session_registry()
    yield
    reset_session_event_hubs()
    reset_external_session_registry()


def _relay(**kwargs):
    return make_session_event_relay(SESSION, turn_id="t1", **kwargs)


async def _final_event():
    events = [event for event in get_session_event_hub(SESSION).replay(0) if event["type"] == "reply.final"]
    assert events, "relay 没有发布 reply.final"
    return events[-1]


# --- 1. 登记：先账本后 hub ---------------------------------------------------


@pytest.mark.asyncio
async def test_reply_final_is_registered_before_the_hub():
    await _relay(external_key=ROUTE)(AgentEvent(type="message_end", payload={"text": "下载完成了"}))

    event = await _final_event()
    outbox_id = event["outbox_id"]
    assert outbox_id.startswith("obx-")
    pending = external_outbox.load_pending_outbound()
    assert [record["id"] for record in pending] == [outbox_id]
    record = pending[0]
    # 路由身份必须齐备：桥侧 30s 对账靠 session_key 认出自己名下的会话并为此建 pump。
    assert record["session_key"] == SESSION
    assert record["external_key"] == ROUTE
    assert record["text"] == "下载完成了"
    assert record["event"] == "reply.final"


@pytest.mark.asyncio
async def test_reply_without_route_key_stays_live_only():
    """孤儿转录（注册表里已无条目）拿不到 external_key：只走内存投递，不留死账。"""
    await _relay()(AgentEvent(type="message_end", payload={"text": "只有网页看得见"}))

    event = await _final_event()
    assert "outbox_id" not in event
    assert external_outbox.load_pending_outbound() == []


@pytest.mark.asyncio
async def test_silent_and_empty_replies_never_enter_the_ledger():
    relay = _relay(external_key=ROUTE)
    await relay(AgentEvent(type="message_end", payload={"text": "心跳 ack", "silent_reply": True}))
    await relay(AgentEvent(type="message_end", payload={"text": "内部事件", "heartbeat_internal": True}))
    await relay(AgentEvent(type="message_end", payload={"text": "   "}))

    assert get_session_event_hub(SESSION).replay(0) == []
    assert external_outbox.load_pending_outbound() == []


@pytest.mark.asyncio
async def test_ledger_failure_cannot_eat_the_hub_publish(monkeypatch: pytest.MonkeyPatch):
    """账本写不进去（磁盘满）必须降级成仅内存投递，回复本身不能因此消失。"""

    def _boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    # 按调用方命名空间打桩：relay 走的是 external_events 里绑好的那个名字。
    monkeypatch.setattr("g3ku.runtime.external_events.record_outbound_message", _boom)
    await _relay(external_key=ROUTE)(AgentEvent(type="message_end", payload={"text": "照样要发出去"}))

    event = await _final_event()
    assert event["text"] == "照样要发出去"
    assert "outbox_id" not in event


# --- 2. 销账：每个消费方都必须关自己的账 ------------------------------------


@pytest.mark.asyncio
async def test_in_process_reply_waiter_acks_the_record_it_consumed():
    """OpenAI 兼容端点这类进程内等待方从不 SSE 重放，也没有桥替它 ack；不销账
    就是永久 pending + 每小时一次重注入。"""
    await _relay(external_key=ROUTE)(AgentEvent(type="message_end", payload={"text": "答案"}))
    assert external_outbox.load_pending_outbound()

    outcome = await wait_for_external_reply(
        SESSION, after_seq=0, timeout=1.0, want_turn_id="t1"
    )

    assert outcome.kind == "reply" and outcome.text == "答案"
    assert external_outbox.load_pending_outbound() == []


# --- 3. 回收：年龄不再作理由 --------------------------------------------------


def _write_stale_ts(outbox_id: str, *, ts: str) -> None:
    path = external_outbox._outbox_path()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for line in lines:
        if line.get("id") == outbox_id:
            line["ts"] = ts
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines), encoding="utf-8")


def _reachable_registry(tmp_path: Path, *, enabled: bool = True):
    registry = ExternalSessionRegistry(tmp_path)
    registry.resolve_or_create(bridge_id="qq-official", external_key=ROUTE)
    tokens = {"qq-official": SimpleNamespace(enabled=enabled)}
    config = SimpleNamespace(external_api=SimpleNamespace(enabled=True, tokens=tokens))
    return registry, (config, 1, False)


def test_no_age_exit_left_in_the_ledger() -> None:
    """24h 时效已删：跨一天关机的滞留回复开机后仍要投出去。"""
    assert not hasattr(external_outbox, "expire_stale_pending")
    assert not hasattr(external_outbox, "PENDING_MAX_AGE_SECONDS")

    stale = external_outbox.record_outbound_message(session_key=SESSION, external_key=ROUTE, text="昨天的日报")
    _write_stale_ts(stale, ts=(datetime.now() - timedelta(hours=25)).isoformat())

    assert [record["id"] for record in external_outbox.load_pending_outbound()] == [stale]


def test_retire_keys_on_routability_not_age(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    registry, config = _reachable_registry(tmp_path)
    monkeypatch.setattr(web, "get_external_session_registry", lambda *a, **k: registry)
    monkeypatch.setattr(web, "get_runtime_config", lambda *a, **k: config)

    fresh = external_outbox.record_outbound_message(
        session_key=registry.get_session_key(bridge_id="qq-official", external_key=ROUTE) or "",
        external_key=ROUTE,
        text="能投出去的回复",
    )
    _write_stale_ts(fresh, ts=(datetime.now() - timedelta(hours=30)).isoformat())
    stranded = external_outbox.record_outbound_message(
        session_key="ext:qq-official-deadbeef:0000000000000000", external_key="qq:c2c:gone", text="没人认领的"
    )

    assert web._retire_unreachable_pending() == 1
    assert [record["id"] for record in external_outbox.load_pending_outbound()] == [fresh]

    # 该桥的 token 被停用（换号/整表替换后的旧 bridge_id 就是这个形状）⇒ 终态。
    _reachable_registry(tmp_path, enabled=False)
    registry_off, config_off = _reachable_registry(tmp_path, enabled=False)
    monkeypatch.setattr(web, "get_external_session_registry", lambda *a, **k: registry_off)
    monkeypatch.setattr(web, "get_runtime_config", lambda *a, **k: config_off)
    assert web._retire_unreachable_pending() == 1
    assert external_outbox.load_pending_outbound() == []
    assert stranded


def test_retire_clears_records_that_cannot_be_replayed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ts 畸形的记录在对账侧重放结构上投不出去（年龄过滤直接跳过），留着只会变成
    永远读得到的死行；按不可达销掉并留一行可 grep 的 WARNING。"""
    registry, config = _reachable_registry(tmp_path)
    monkeypatch.setattr(web, "get_external_session_registry", lambda *a, **k: registry)
    monkeypatch.setattr(web, "get_runtime_config", lambda *a, **k: config)

    bad = external_outbox.record_outbound_message(
        session_key=registry.get_session_key(bridge_id="qq-official", external_key=ROUTE) or "",
        external_key=ROUTE,
        text="ts 坏了",
    )
    _write_stale_ts(bad, ts="garbage")

    assert web._retire_unreachable_pending() == 1
    assert external_outbox.load_pending_outbound() == []
