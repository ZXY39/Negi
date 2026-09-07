"""End-to-end outbound routing tests: heartbeat notifier -> bus -> drain -> event hub.

Covers the proactive push path for external bridge sessions (heartbeat/cron/
task-terminal replies) plus the ext branch of heartbeat session-meta
derivation.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from g3ku.bus.events import OutboundMessage
from g3ku.bus.queue import MessageBus
from g3ku.heartbeat.session_service import _derive_session_channel_chat
from g3ku.runtime.external_events import get_session_event_hub, reset_session_event_hubs
from g3ku.runtime.external_sessions import (
    ExternalSessionRegistry,
    reset_external_session_registry,
)
from g3ku.shells import web as web_shell
from g3ku.shells.web import _notify_heartbeat_channel_reply, _start_outbound_drain


@pytest.fixture
def env(monkeypatch, tmp_path):
    reset_external_session_registry()
    reset_session_event_hubs()
    registry = ExternalSessionRegistry(tmp_path)
    monkeypatch.setattr(web_shell, "get_external_session_registry", lambda: registry)
    bus = MessageBus()
    monkeypatch.setattr(web_shell, "_global_bus", bus)
    yield registry, bus
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


@pytest.mark.asyncio
async def test_heartbeat_ext_reply_flows_to_event_hub(env):
    registry, bus = env
    entry, _ = registry.resolve_or_create(bridge_id="qq", external_key="qq:group:42")
    task = _start_outbound_drain(bus)
    try:
        await _notify_heartbeat_channel_reply(entry.session_key, "定时提醒到了")
        hub = get_session_event_hub(entry.session_key)
        await _wait_until(lambda: hub.last_seq >= 1)
        events = hub.replay(0)
        assert events[0]["type"] == "outbound.created"
        assert events[0]["text"] == "定时提醒到了"
        assert events[0]["external_key"] == "qq:group:42"
        assert events[0]["session_key"] == entry.session_key
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_heartbeat_ext_reply_ignores_empty_text(env):
    registry, bus = env
    entry, _ = registry.resolve_or_create(bridge_id="qq", external_key="qq:dm:1")
    await _notify_heartbeat_channel_reply(entry.session_key, "   ")
    assert bus.outbound.empty()


@pytest.mark.asyncio
async def test_heartbeat_china_keys_are_skipped_after_subsystem_removal(env, monkeypatch):
    """Legacy china: keys keep their transcripts but lost their delivery path
    when the China channel subsystem was removed: the notifier must skip
    silently instead of publishing outbound or touching the ext hub."""
    registry, bus = env
    published: list[OutboundMessage] = []

    async def capture(msg: OutboundMessage) -> None:
        published.append(msg)

    monkeypatch.setattr(bus, "publish_outbound", capture)
    await _notify_heartbeat_channel_reply("china:qqbot:default:group:9", "渠道回复")
    assert published == []
    assert bus.outbound.empty()


def test_derive_session_channel_chat_ext_branch():
    channel, chat_id = _derive_session_channel_chat("ext:qq:abc123")
    assert channel == "ext"
    assert chat_id == "ext:qq:abc123"  # full key, resolvable by the registry


def test_derive_session_channel_chat_china_branch_unchanged():
    channel, chat_id = _derive_session_channel_chat("china:qqbot:acct1:group:9")
    assert channel == "qqbot"
    assert chat_id == "acct1:group:9"
