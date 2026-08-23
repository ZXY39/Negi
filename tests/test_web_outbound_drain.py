"""Tests for the web shell outbound drain (bus -> China host bridge).

Regression coverage for the silent-drain-death failure mode: the drain used
to catch only TimeoutError/CancelledError, so any other exception (e.g. the
control WebSocket not being connected yet) killed the task silently and all
later outbound messages (cron reminders, heartbeat replies) were stranded in
the bus queue forever.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.shells.web import _start_china_outbound_drain


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
async def test_drain_delivers_china_channel_message() -> None:
    bus = MessageBus()
    transport = _FakeTransport()
    task = _start_china_outbound_drain(bus, transport)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="hello")
        )
        await _wait_until(lambda: len(transport.sent) == 1)
        assert transport.sent[0].content == "hello"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_skips_non_china_channel_message() -> None:
    bus = MessageBus()
    transport = _FakeTransport()
    task = _start_china_outbound_drain(bus, transport)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="web", chat_id="direct", content="not for china bridge")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="after skip")
        )
        await _wait_until(lambda: len(transport.sent) == 1)
        assert transport.sent[0].content == "after skip"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_survives_non_china_channel_message() -> None:
    # Regression: poisoned session meta used to publish channel="china"
    # messages, which the drain skipped. The skip must not kill the drain and
    # subsequent china messages must still be delivered.
    bus = MessageBus()
    transport = _FakeTransport()
    task = _start_china_outbound_drain(bus, transport)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="china", chat_id="qqbot:default:dm", content="poisoned meta")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="still delivered")
        )
        await _wait_until(lambda: len(transport.sent) == 1)
        assert transport.sent[0].chat_id == "default:dm:user-1"
        assert transport.sent[0].content == "still delivered"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_survives_poison_message_and_keeps_draining() -> None:
    bus = MessageBus()
    transport = _FakeTransport()
    transport.fail_next["bad"] = ValueError("boom")
    task = _start_china_outbound_drain(bus, transport)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="bad", content="poison")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="good", content="still delivered")
        )
        await _wait_until(lambda: len(transport.sent) == 1)
        assert transport.sent[0].chat_id == "good"
        assert transport.sent[0].content == "still delivered"
        assert not task.done()
    finally:
        await _stop(task)


@pytest.mark.asyncio
async def test_drain_retries_message_after_transient_not_connected_error() -> None:
    bus = MessageBus()
    transport = _FakeTransport()
    transport.fail_next["default:dm:user-1"] = RuntimeError("china bridge not connected")
    task = _start_china_outbound_drain(bus, transport)
    try:
        await bus.publish_outbound(
            OutboundMessage(channel="qqbot", chat_id="default:dm:user-1", content="reminder")
        )
        # First attempt raises; the drain must keep the message and retry
        # (1s backoff) instead of dying.
        await _wait_until(lambda: len(transport.sent) == 1, timeout=10.0)
        assert transport.sent[0].content == "reminder"
        assert not task.done()
    finally:
        await _stop(task)
