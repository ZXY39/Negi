from __future__ import annotations

import asyncio

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.cancellation import ToolCancellationToken
from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport
from g3ku.runtime.frontdoor.turns import build_frontdoor_turn_tools
from g3ku.runtime.tool_jobs import ToolJobRunner
from main.storage.sqlite_store import SQLiteTaskStore


class _FakeHeartbeat:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def enqueue_tool_job(self, payload: dict) -> bool:
        self.events.append(dict(payload))
        return True


class _FakeTools:
    def push_runtime_context(self, _context):
        return object()

    def pop_runtime_context(self, _token):
        return None


class _FakeLoop:
    def __init__(self) -> None:
        self.tools = _FakeTools()


class _FakeRuntimeSession:
    def __init__(self, store, heartbeat) -> None:
        self.notified = 0
        self.runner = ToolJobRunner(
            session_key="web:test",
            store_getter=lambda: store,
            heartbeat_getter=lambda: heartbeat,
        )

    def start_tool_job(self, **kwargs):
        return self.runner.start_job(**kwargs)

    async def wait_tool_job(self, job, timeout_seconds):
        return await self.runner.wait_first(job, timeout_seconds)

    def list_tool_jobs(self):
        return self.runner.list_jobs("web:test")

    def notify_background_items_changed(self) -> None:
        self.notified += 1


class _Harness(CeoFrontDoorSupport):
    def __init__(self, loop) -> None:
        self._loop = loop


class _FastTool(Tool):
    @property
    def name(self) -> str:
        return "fast_tool"

    @property
    def description(self) -> str:
        return "fast"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        return "fast-result"


class _SlowTool(Tool):
    def __init__(self, seconds: float = 0.2) -> None:
        self._seconds = seconds

    @property
    def name(self) -> str:
        return "slow_tool"

    @property
    def description(self) -> str:
        return "slow"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        await asyncio.sleep(self._seconds)
        return "slow-result"


class _CaptureRuntimeTool(Tool):
    def __init__(self) -> None:
        self.runtime: dict | None = None

    @property
    def name(self) -> str:
        return "capture_runtime"

    @property
    def description(self) -> str:
        return "capture"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs):
        self.runtime = dict(kwargs.get("__g3ku_runtime") or {})
        notify = self.runtime.get("_notify_background_items_changed")
        if callable(notify):
            notify()
        return "captured"


@pytest.fixture()
def store(tmp_path) -> SQLiteTaskStore:
    return SQLiteTaskStore(tmp_path / "runtime.sqlite3")


def _context(session, *, auto_background_seconds: float, cancel_token=None) -> dict:
    return {
        "session_key": "web:test",
        "actor_role": "ceo",
        "_runtime_agent_session": session,
        "cancel_token": cancel_token,
        "fs_read_roots": (),
        "tool_watchdog": {
            "default_timeout_seconds": 600,
            "poll_interval_seconds": 0.01,
            "auto_background_seconds": auto_background_seconds,
        },
    }


async def _run_tool(harness: _Harness, tool: Tool, context: dict):
    return await harness._execute_tool_call_with_raw_result(
        tool=tool,
        tool_name=tool.name,
        arguments={},
        runtime_context=context,
        on_progress=None,
        tool_call_id="call-1",
    )


@pytest.mark.asyncio
async def test_fast_tool_path_is_unchanged_and_does_not_persist(store):
    heartbeat = _FakeHeartbeat()
    session = _FakeRuntimeSession(store, heartbeat)
    harness = _Harness(_FakeLoop())

    raw, rendered, status, started_at, finished_at, elapsed = await _run_tool(
        harness,
        _FastTool(),
        _context(session, auto_background_seconds=0.2),
    )

    assert raw == "fast-result"
    assert rendered == "fast-result"
    assert status == "success"
    assert started_at and finished_at
    assert store.list_tool_jobs(limit=10) == []
    assert heartbeat.events == []


@pytest.mark.asyncio
async def test_tool_gets_narrow_background_change_callback_not_the_session(store):
    heartbeat = _FakeHeartbeat()
    session = _FakeRuntimeSession(store, heartbeat)
    harness = _Harness(_FakeLoop())
    tool = _CaptureRuntimeTool()

    await _run_tool(
        harness,
        tool,
        _context(session, auto_background_seconds=0.2),
    )

    assert tool.runtime is not None
    # 会话对象不进工具视图（`tool_facing_runtime_context` 会剥掉它），
    # 否则 exec 后台进程的启动帧永远发不出去。
    assert "_runtime_agent_session" not in tool.runtime
    assert callable(tool.runtime.get("_notify_background_items_changed"))
    assert session.notified == 1


@pytest.mark.asyncio
async def test_slow_tool_returns_synthetic_result_and_finishes_in_background(store):
    heartbeat = _FakeHeartbeat()
    session = _FakeRuntimeSession(store, heartbeat)
    harness = _Harness(_FakeLoop())

    raw, rendered, status, started_at, _finished_at, _elapsed = await _run_tool(
        harness,
        _SlowTool(0.25),
        _context(session, auto_background_seconds=0.02),
    )

    assert status == "background"
    assert rendered.startswith("[BACKGROUND]")
    assert "job_id=tooljob:" in rendered
    assert "Do not call it again" in rendered
    assert started_at
    rows = store.list_tool_jobs(limit=10)
    assert len(rows) == 1 and rows[0]["status"] == "running"

    ledger = build_frontdoor_turn_tools(
        [{"id": "call-1", "name": "slow_tool", "arguments": {}}],
        [{"status": status, "result_text": rendered, "started_at": started_at}],
    )
    assert ledger[0]["status"] == "background"

    await asyncio.sleep(0.35)
    row = store.list_tool_jobs(limit=10)[0]
    assert row["status"] == "success"
    assert len(heartbeat.events) == 1
    assert heartbeat.events[0]["status"] == "success"
    assert heartbeat.events[0]["result_preview"] == "slow-result"


@pytest.mark.asyncio
async def test_background_handoff_detaches_the_call_from_session_cancellation(store):
    heartbeat = _FakeHeartbeat()
    session = _FakeRuntimeSession(store, heartbeat)
    harness = _Harness(_FakeLoop())
    session_token = ToolCancellationToken(session_key="web:test")

    _raw, _rendered, status, _started_at, _finished_at, _elapsed = await _run_tool(
        harness,
        _SlowTool(0.2),
        _context(session, auto_background_seconds=0.02, cancel_token=session_token),
    )

    assert status == "background"
    # 交棒后 job 不再挂在会话取消令牌下：pause/stop 不会级联杀掉后台作业。
    assert session_token._children == set()
    await asyncio.sleep(0.3)
    assert store.list_tool_jobs(limit=10)[0]["status"] == "success"
    assert heartbeat.events[-1]["status"] == "success"
