"""Tests for the web shell outbound drain (bus -> external event hubs).

Regression coverage for the silent-drain-death failure mode: the drain used
to catch only TimeoutError/CancelledError, so any other exception killed the
task silently and all later outbound messages (cron reminders, heartbeat
replies) were stranded in the bus queue forever.

The China channel subsystem has been removed, so the drain routes a single
channel family:
- ``channel == "ext"`` -> external session registry lookup -> per-session
  event hub ``outbound.created`` event
Any other channel has no consumer and is skipped with a warning; the skip
must not kill the drain.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta

import pytest
from loguru import logger

from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.runtime import external_outbox
from g3ku.runtime.external_events import get_session_event_hub, reset_session_event_hubs
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)
from g3ku.shells import web as web_shell
from g3ku.shells.web import _start_outbound_drain


@pytest.fixture
def ext_registry(monkeypatch, tmp_path):
    reset_external_session_registry()
    reset_session_event_hubs()
    registry = ExternalSessionRegistry(tmp_path)
    monkeypatch.setattr(web_shell, "get_external_session_registry", lambda: registry)
    # drain 现在会把每条 ext 出站登记进持久 outbox：测试必须重定向到 tmp，
    # 否则会写进真实工作区的 .g3ku/external-outbox/。
    external_outbox.configure_external_outbox_root(tmp_path)
    yield registry
    external_outbox.configure_external_outbox_root(None)
    reset_external_session_registry()
    reset_session_event_hubs()


async def _wait_until(condition, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_drain_skips_non_external_channel_message(ext_registry) -> None:
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:1")
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        # Legacy/poisoned channels have no consumer after the China subsystem
        # removal; the skip must not kill the drain.
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="no consumer")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="ext", chat_id=entry.session_key, content="still delivered")
        )
        hub = get_session_event_hub(entry.session_key)
        await _wait_until(lambda: hub.last_seq >= 1)
        events = hub.replay(0)
        assert events[0]["type"] == "outbound.created"
        assert events[0]["text"] == "still delivered"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_routes_ext_message_to_event_hub(ext_registry) -> None:
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:1")
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(
                channel="ext",
                chat_id=entry.session_key,
                content="proactive hello",
                metadata={"dedupe_key": "k1"},
            )
        )
        hub = get_session_event_hub(entry.session_key)
        await _wait_until(lambda: hub.last_seq >= 1)
        events = hub.replay(0)
        assert events[0]["type"] == "outbound.created"
        assert events[0]["text"] == "proactive hello"
        assert events[0]["external_key"] == "qq:dm:1"
        assert events[0]["session_key"] == entry.session_key
        assert events[0]["dedupe_key"] == "k1"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_routes_ext_message_by_external_key(ext_registry) -> None:
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:group:9")
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="ext", chat_id="qq:group:9", content="by external key")
        )
        hub = get_session_event_hub(entry.session_key)
        await _wait_until(lambda: hub.last_seq >= 1)
        events = hub.replay(0)
        assert events[0]["type"] == "outbound.created"
        assert events[0]["text"] == "by external key"
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_drops_ext_message_with_unknown_target(ext_registry) -> None:
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="ext", chat_id="ext:ghost:0000", content="nobody home")
        )
        await _wait_until(lambda: bus.outbound.empty())
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_acks_internal_only_ext_text(ext_registry) -> None:
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:2")
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="ext", chat_id=entry.session_key, content="[SESSION EVENTS]\ninternal")
        )
        await _wait_until(lambda: bus.outbound.empty())
        hub = get_session_event_hub(entry.session_key)
        assert hub.replay(0) == []
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_registers_durable_outbox_and_reuses_replay_id(ext_registry) -> None:
    """drain 在发布 hub 前登记持久 outbox（事件携带 outbox_id 供桥 ack 销账）；
    启动重放消息自带 outbox_id 时直接复用，绝不重复登记。"""
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:7")
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="ext", chat_id=entry.session_key, content="durable push")
        )
        hub = get_session_event_hub(entry.session_key)
        await _wait_until(lambda: hub.last_seq >= 1)
        event = hub.replay(0)[0]
        outbox_id = event["outbox_id"]
        assert outbox_id
        pending = external_outbox.load_pending_outbound()
        assert [item["id"] for item in pending] == [outbox_id]
        assert pending[0]["text"] == "durable push"
        assert pending[0]["external_key"] == "qq:dm:7"

        # 模拟启动重放：带原 id 重新入总线，不得二次登记。
        await bus.publish_outbound(
            OutboundMessage(
                channel="ext",
                chat_id=entry.session_key,
                content="durable push",
                metadata={"source": "outbox_replay", "outbox_id": outbox_id},
            )
        )
        await _wait_until(lambda: hub.last_seq >= 2)
        assert hub.replay(0)[1]["outbox_id"] == outbox_id
        assert len(external_outbox.load_pending_outbound()) == 1

        # 桥 ack 后不再 pending（重启重放不会再投）。
        assert external_outbox.ack_outbound_message(outbox_id, session_key=entry.session_key) is True
        assert external_outbox.load_pending_outbound() == []
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_startup_replay_republishes_pending_outbox(ext_registry, monkeypatch) -> None:
    """启动重放：pending 条目带原 outbox_id 重新注入总线 → drain → hub，
    供桥 pump 预热后经 SSE 重放补投；不重复登记账本。"""
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:8")
    outbox_id = external_outbox.record_outbound_message(
        session_key=entry.session_key, external_key="qq:dm:8", text="重启滞留的提醒"
    )
    bus = MessageBus()
    monkeypatch.setattr(web_shell, "_global_bus", bus)
    task = _start_outbound_drain(bus)
    try:
        await web_shell._replay_pending_external_outbox()
        hub = get_session_event_hub(entry.session_key)
        await _wait_until(lambda: hub.last_seq >= 1)
        event = hub.replay(0)[0]
        assert event["type"] == "outbound.created"
        assert event["text"] == "重启滞留的提醒"
        assert event["outbox_id"] == outbox_id
        assert [item["id"] for item in external_outbox.load_pending_outbound()] == [outbox_id]
    finally:
        await _stop(task)


def _age_records(ages_seconds: dict[str, float]) -> None:
    """手工把指定 msg 记录的 ts 调老（仿 test_external_outbox 的手法）。"""
    path = external_outbox._outbox_path()
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for record in lines:
        if record.get("kind") == "msg" and str(record.get("id")) in ages_seconds:
            record["ts"] = (datetime.now() - timedelta(seconds=ages_seconds[record["id"]])).isoformat()
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in lines), encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_route_warns_when_hub_has_no_subscriber(ext_registry) -> None:
    """发布进 hub 时无订阅者 = live 投递必然蒸发（2026-09-14 事故的静默失败
    点）：必须升级 WARNING 供 grep；有订阅者时维持 INFO。"""
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:warn1")
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    warnings: list[str] = []
    sink_id = logger.add(lambda m: warnings.append(str(m.record["message"])), level="WARNING")
    hub = get_session_event_hub(entry.session_key)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="ext", chat_id=entry.session_key, content="没人消费的推送")
        )
        await _wait_until(lambda: any("no live subscriber" in w for w in warnings))
        assert hub.last_seq >= 1  # 事件仍进 ring buffer，供后续 pump 回放

        # 对照组：有订阅者时不再出现该 WARNING。
        queue = hub.subscribe()
        try:
            warnings.clear()
            await bus.publish_outbound(
                OutboundMessage(channel="ext", chat_id=entry.session_key, content="有消费者的推送")
            )
            await _wait_until(lambda: hub.last_seq >= 2)
            await asyncio.sleep(0.05)
            assert not any("no live subscriber" in w for w in warnings)
        finally:
            hub.unsubscribe(queue)
    finally:
        logger.remove(sink_id)
        await _stop(task)


@pytest.mark.asyncio
async def test_reconcile_republishes_only_aged_unsubscribed_records(ext_registry, monkeypatch) -> None:
    """周期对账只重放「足够老且 hub 无订阅者」的记录：有订阅者说明 pump 在线
    （ring buffer 回放已兜底），太新的留给 live 链路。"""
    entry_a, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:rA")
    entry_b, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:rB")
    entry_c, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:rC")
    id_a = external_outbox.record_outbound_message(
        session_key=entry_a.session_key, external_key="qq:dm:rA", text="滞留A"
    )
    id_b = external_outbox.record_outbound_message(
        session_key=entry_b.session_key, external_key="qq:dm:rB", text="滞留B"
    )
    external_outbox.record_outbound_message(
        session_key=entry_c.session_key, external_key="qq:dm:rC", text="新鲜C"
    )
    _age_records({id_a: 200.0, id_b: 200.0})
    monkeypatch.setattr(web_shell, "_outbox_republish_backoff", {})
    bus = MessageBus()
    monkeypatch.setattr(web_shell, "_global_bus", bus)
    task = _start_outbound_drain(bus)
    hub_b = get_session_event_hub(entry_b.session_key)
    queue_b = hub_b.subscribe()  # B 模拟在线 pump
    try:
        republished, expired = await web_shell._reconcile_external_outbox_once()
        assert (republished, expired) == (1, 0)
        hub_a = get_session_event_hub(entry_a.session_key)
        await _wait_until(lambda: hub_a.last_seq >= 1)
        event = hub_a.replay(0)[0]
        assert event["type"] == "outbound.created"
        assert event["text"] == "滞留A"
        assert event["outbox_id"] == id_a  # 复用原 id，桥 ack 能对上账
        # B 有订阅者、C 未超龄：都不注入。
        assert hub_b.last_seq == 0
        assert get_session_event_hub(entry_c.session_key).last_seq == 0
        # A 已进入记录级退避：紧接着再跑一轮不会注入第二份。
        assert await web_shell._reconcile_external_outbox_once() == (0, 0)
        await asyncio.sleep(0.05)
        assert hub_a.last_seq == 1
    finally:
        hub_b.unsubscribe(queue_b)
        await _stop(task)


@pytest.mark.asyncio
async def test_reconcile_expires_stale_and_loop_compacts_ledger(ext_registry, monkeypatch) -> None:
    """过期清理不再依赖重启：对账循环每轮跑 expire；活动轮立即压实账本。"""
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:old")
    oid = external_outbox.record_outbound_message(
        session_key=entry.session_key, external_key="qq:dm:old", text="25小时前的提醒"
    )
    _age_records({oid: 25 * 3600.0})
    monkeypatch.setattr(web_shell, "_outbox_republish_backoff", {})
    monkeypatch.setattr(web_shell, "OUTBOX_RECONCILE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(web_shell, "_global_bus", MessageBus())
    monkeypatch.setattr(web_shell, "_global_outbound_drain_task", None)

    async def fake_sync() -> None:
        return None

    monkeypatch.setattr(web_shell, "_sync_qq_official_service", fake_sync)
    task = asyncio.create_task(web_shell._outbox_reconcile_loop(), name="test-reconcile")
    try:
        await _wait_until(lambda: external_outbox.load_pending_outbound() == [])
        # expired>0 的活动轮立即 compact：msg 与 expired tombstone 一起清掉。
        await _wait_until(
            lambda: external_outbox._outbox_path().read_text(encoding="utf-8").strip() == ""
        )
        assert not task.done()
    finally:
        await _stop(task)
        drain_task = web_shell._global_outbound_drain_task
        if drain_task is not None:
            await _stop(drain_task)


@pytest.mark.asyncio
async def test_reconcile_backoff_suppresses_repeat_republish(ext_registry, monkeypatch) -> None:
    """永久无消费者的记录（如 openai-compat 会话）按指数退避压制重复注入，
    而不是每轮一次直到 24h 过期；退避状态清空（≈进程重启）后可再注入。"""
    entry, _ = ext_registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:bo")
    oid = external_outbox.record_outbound_message(
        session_key=entry.session_key, external_key="qq:dm:bo", text="无消费者的提醒"
    )
    monkeypatch.setattr(web_shell, "OUTBOX_REPUBLISH_MIN_AGE_SECONDS", 0.0)
    monkeypatch.setattr(web_shell, "OUTBOX_REPUBLISH_INITIAL_BACKOFF_SECONDS", 3600.0)
    monkeypatch.setattr(web_shell, "_outbox_republish_backoff", {})
    bus = MessageBus()
    monkeypatch.setattr(web_shell, "_global_bus", bus)
    task = _start_outbound_drain(bus)
    hub = get_session_event_hub(entry.session_key)
    try:
        assert await web_shell._reconcile_external_outbox_once() == (1, 0)
        await _wait_until(lambda: hub.last_seq >= 1)
        assert await web_shell._reconcile_external_outbox_once() == (0, 0)
        assert await web_shell._reconcile_external_outbox_once() == (0, 0)
        await asyncio.sleep(0.05)
        assert hub.last_seq == 1  # 只有第一份副本
        web_shell._outbox_republish_backoff.clear()
        assert await web_shell._reconcile_external_outbox_once() == (1, 0)
        await _wait_until(lambda: hub.last_seq >= 2)
        assert hub.replay(0)[1]["outbox_id"] == oid
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_outbox_reconcile_loop_survives_errors_and_periodic_bridge_sync(monkeypatch) -> None:
    """对账循环单轮异常绝不终结（drain 同款守护）；每 N 轮同步一次
    qq-official 服务（崩溃桥自愈的触发点）。"""
    monkeypatch.setattr(web_shell, "OUTBOX_RECONCILE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(web_shell, "OUTBOX_BRIDGE_SYNC_EVERY_N_CYCLES", 2)
    monkeypatch.setattr(web_shell, "OUTBOX_COMPACT_EVERY_N_CYCLES", 10_000)
    monkeypatch.setattr(web_shell, "_global_bus", None)  # drain ensure 早退，不起真 drain
    passes: list[int] = []
    syncs: list[int] = []

    async def fake_once() -> tuple[int, int]:
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError("boom")
        return (0, 0)

    async def fake_sync() -> None:
        syncs.append(1)

    monkeypatch.setattr(web_shell, "_reconcile_external_outbox_once", fake_once)
    monkeypatch.setattr(web_shell, "_sync_qq_official_service", fake_sync)
    task = asyncio.create_task(web_shell._outbox_reconcile_loop(), name="test-reconcile-loop")
    try:
        await _wait_until(lambda: len(passes) >= 3 and len(syncs) >= 1)
        assert not task.done()  # 首轮异常没有杀死循环
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_ensure_outbox_reconcile_running_is_idempotent(monkeypatch) -> None:
    """启动器幂等：bus 缺失早退（ensure_web_runtime_services 的既有测试都不设
    bus，不得泄漏任务）；重复调用复用同一 task。"""
    monkeypatch.setattr(web_shell, "OUTBOX_RECONCILE_INTERVAL_SECONDS", 3600.0)
    monkeypatch.setattr(web_shell, "_global_outbox_reconcile_task", None)
    monkeypatch.setattr(web_shell, "_global_bus", None)
    web_shell._ensure_outbox_reconcile_running()
    assert web_shell._global_outbox_reconcile_task is None

    monkeypatch.setattr(web_shell, "_global_bus", MessageBus())
    web_shell._ensure_outbox_reconcile_running()
    first = web_shell._global_outbox_reconcile_task
    assert first is not None
    web_shell._ensure_outbox_reconcile_running()
    assert web_shell._global_outbox_reconcile_task is first
    await _stop(first)
