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
