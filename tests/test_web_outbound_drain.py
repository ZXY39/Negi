"""Tests for the web shell outbound drain (bus -> China transport / external event hubs).

Regression coverage for the silent-drain-death failure mode: the drain used
to catch only TimeoutError/CancelledError, so any other exception (e.g. the
control WebSocket not being connected yet) killed the task silently and all
later outbound messages (cron reminders, heartbeat replies) were stranded in
the bus queue forever.

The drain now routes two channel families:
- ``channel in CHINA_CHANNELS`` -> legacy China transport (unchanged contract)
- ``channel == "ext"`` -> external session registry lookup -> per-session
  event hub ``outbound.created`` event
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.runtime.external_events import get_session_event_hub, reset_session_event_hubs
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)
from g3ku.shells import web as web_shell
from g3ku.shells.web import _start_outbound_drain


class _FakeTransport:
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        # chat_id -> exception to raise on the next send for that chat_id
        self.fail_next: dict[str, BaseException | None] = {}

    async def send_outbound(self, msg: OutboundMessage) -> None:
        exc = self.fail_next.get(msg.chat_id)
        if exc is not None:
            self.fail_next[msg.chat_id] = None
            raise exc
        self.sent.append(msg)


@pytest.fixture
def fake_transport(monkeypatch):
    transport = _FakeTransport()
    monkeypatch.setattr(web_shell, "_global_china_transport", transport)
    return transport


@pytest.fixture
def ext_registry(monkeypatch, tmp_path):
    reset_external_session_registry()
    reset_session_event_hubs()
    registry = ExternalSessionRegistry(tmp_path)
    monkeypatch.setattr(web_shell, "get_external_session_registry", lambda: registry)
    yield registry
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
async def test_drain_delivers_china_channel_message(fake_transport) -> None:
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="hello")
        )
        await _wait_until(lambda: len(fake_transport.sent) == 1)
        assert fake_transport.sent[0].content == "hello"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_skips_non_china_channel_message(fake_transport) -> None:
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="web", chat_id="direct", content="not for china bridge")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="after skip")
        )
        await _wait_until(lambda: len(fake_transport.sent) == 1)
        assert fake_transport.sent[0].content == "after skip"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_survives_non_china_channel_message(fake_transport) -> None:
    # Regression: poisoned session meta used to publish channel="china"
    # messages, which the drain skipped. The skip must not kill the drain and
    # subsequent china messages must still be delivered.
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="china", chat_id="qqbot:default:dm", content="poisoned meta")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="still delivered")
        )
        await _wait_until(lambda: len(fake_transport.sent) == 1)
        assert fake_transport.sent[0].chat_id == "default:dm:user-1"
        assert fake_transport.sent[0].content == "still delivered"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_survives_poison_message_and_keeps_draining(fake_transport) -> None:
    bus = MessageBus()
    fake_transport.fail_next["bad"] = ValueError("boom")
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="bad", content="poison")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="good", content="still delivered")
        )
        await _wait_until(lambda: len(fake_transport.sent) == 1)
        assert fake_transport.sent[0].chat_id == "good"
        assert fake_transport.sent[0].content == "still delivered"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_retries_message_after_transient_not_connected_error(fake_transport) -> None:
    bus = MessageBus()
    fake_transport.fail_next["default:dm:user-1"] = RuntimeError("china bridge not connected")
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="reminder")
        )
        # First attempt raises; the drain must keep the message and retry
        # (1s backoff) instead of dying.
        await _wait_until(lambda: len(fake_transport.sent) == 1, timeout=10.0)
        assert fake_transport.sent[0].content == "reminder"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_drops_china_message_when_transport_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(web_shell, "_global_china_transport", None)
    bus = MessageBus()
    task = _start_outbound_drain(bus)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="orphan")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-2", content="next")
        )
        # Both messages must be consumed (dropped), not retried forever: the
        # queue drains back to empty.
        await _wait_until(lambda: bus.outbound.empty())
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
