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

import pytest

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
