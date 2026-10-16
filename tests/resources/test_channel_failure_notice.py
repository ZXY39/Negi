"""Terminal turn failures must reach the channel, not only the web transcript.

Regression for the silent-QQ lane: a turn that dies with an unhandled exception
composed a user-readable failure line, persisted it into the transcript and
showed it on the web, but never put it on the channel outbound bus. The QQ user
saw nothing at all (2026-10-16 live: three MemoryError turns over 8 minutes,
zero ``qq-official delivered`` lines in between).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import SessionManager


class _CancelToken:
    def cancel(self, *, reason: str = "") -> None:
        _ = reason


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish_outbound(self, msg) -> None:
        self.published.append(msg)


class _RaisingRunner:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run_turn(self, *, user_input, session, on_progress):
        _ = user_input, session, on_progress
        raise self._exc


class _EmptyRunner:
    async def run_turn(self, *, user_input, session, on_progress):
        _ = user_input, session, on_progress
        return ""


def _make_loop(tmp_path: Path, runner, bus) -> SimpleNamespace:
    return SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=runner,
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        bus=bus,
        create_session_cancellation_token=lambda _session_key: _CancelToken(),
        release_session_cancellation_token=lambda _session_key, _token: None,
        cancel_session_tasks=asyncio.sleep,
        _use_rag_memory=lambda: False,
    )


@pytest.fixture(autouse=True)
def _no_runtime_refresh(monkeypatch):
    async def _refresh_web_agent_runtime(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason
        return None

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _refresh_web_agent_runtime)


def _outbound_bodies(bus: _FakeBus) -> list[tuple[str, str]]:
    return [(str(m.channel), str(m.content)) for m in bus.published]


@pytest.mark.asyncio
async def test_channel_turn_failure_publishes_the_failure_line(tmp_path: Path) -> None:
    bus = _FakeBus()
    session_key = "ext:test-bridge:fail-1"
    session = RuntimeAgentSession(
        _make_loop(tmp_path, _RaisingRunner(RuntimeError("boom")), bus),
        session_key=session_key,
        channel="ext",
        chat_id="qq:c2c:OPENID",
    )

    with pytest.raises(RuntimeError):
        await session.prompt("先告诉我你的发现")

    assert _outbound_bodies(bus) == [("ext", "这一轮处理失败：boom")]


@pytest.mark.asyncio
async def test_channel_memory_error_publishes_the_memory_line(tmp_path: Path) -> None:
    bus = _FakeBus()
    session = RuntimeAgentSession(
        _make_loop(tmp_path, _RaisingRunner(MemoryError()), bus),
        session_key="ext:test-bridge:fail-2",
        channel="ext",
        chat_id="qq:c2c:OPENID",
    )

    with pytest.raises(MemoryError):
        await session.prompt("继续")

    assert _outbound_bodies(bus) == [("ext", "运行时内存不足，未能完成当前轮次")]


@pytest.mark.asyncio
async def test_channel_internal_turn_failure_also_publishes(tmp_path: Path) -> None:
    """cron/heartbeat lanes share the terminal branch, so they are covered too."""
    bus = _FakeBus()
    session = RuntimeAgentSession(
        _make_loop(tmp_path, _RaisingRunner(RuntimeError("quota exhausted")), bus),
        session_key="ext:test-bridge:fail-3",
        channel="ext",
        chat_id="qq:c2c:OPENID",
    )

    with pytest.raises(RuntimeError):
        await session.prompt(
            UserInputMessage(
                content="内部提醒",
                metadata={"heartbeat_internal": True, "heartbeat_reason": "wake"},
            )
        )

    assert _outbound_bodies(bus) == [("ext", "这一轮处理失败：quota exhausted")]


@pytest.mark.asyncio
async def test_web_session_failure_publishes_nothing(tmp_path: Path) -> None:
    bus = _FakeBus()
    session = RuntimeAgentSession(
        _make_loop(tmp_path, _RaisingRunner(RuntimeError("boom")), bus),
        session_key="web:ceo-fail-4",
        channel="web",
        chat_id="ceo-fail-4",
    )

    with pytest.raises(RuntimeError):
        await session.prompt("hi")

    assert bus.published == []


@pytest.mark.asyncio
async def test_published_reply_lane_publishes_no_failure_notice(tmp_path: Path) -> None:
    bus = _FakeBus()
    session = RuntimeAgentSession(
        _make_loop(tmp_path, _EmptyRunner(), bus),
        session_key="ext:test-bridge:ok-5",
        channel="ext",
        chat_id="qq:c2c:OPENID",
    )

    result = await session.prompt("hi")

    assert result.output == ""
    assert bus.published == []
