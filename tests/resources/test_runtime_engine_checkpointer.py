from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from g3ku.runtime.engine import AgentRuntimeEngine


@pytest.mark.asyncio
async def test_ensure_checkpointer_ready_rebuilds_inactive_sqlite_checkpointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import langgraph.checkpoint.sqlite.aio as sqlite_aio

    cleanup_calls: list[str] = []
    factory_calls: list[str] = []

    class _DeadConnection:
        @property
        def _conn(self):
            raise ValueError("no active connection")

    class _AliveConnection:
        @property
        def _conn(self):
            return object()

    class _OldCheckpointer:
        conn = _DeadConnection()

        async def close(self) -> None:
            cleanup_calls.append("old_checkpointer_close")

    class _OldContextManager:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb
            cleanup_calls.append("old_context_exit")

    class _NewCheckpointer:
        conn = _AliveConnection()

        async def setup(self) -> None:
            cleanup_calls.append("new_checkpointer_setup")

    class _NewContextManager:
        async def __aenter__(self):
            cleanup_calls.append("new_context_enter")
            return _NewCheckpointer()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb
            cleanup_calls.append("new_context_exit")

    class _FakeAsyncSqliteSaver:
        @staticmethod
        def from_conn_string(value: str):
            factory_calls.append(value)
            return _NewContextManager()

    monkeypatch.setattr(sqlite_aio, "AsyncSqliteSaver", _FakeAsyncSqliteSaver)

    engine = AgentRuntimeEngine.__new__(AgentRuntimeEngine)
    engine._checkpointer_enabled = True
    engine._checkpointer_backend = "sqlite"
    engine._checkpointer_path = str(tmp_path / "checkpoints.sqlite3")
    engine._checkpointer = _OldCheckpointer()
    engine._checkpointer_cm = _OldContextManager()
    engine._checkpointer_lock = asyncio.Lock()

    await AgentRuntimeEngine._ensure_checkpointer_ready(engine)

    assert factory_calls == [str(tmp_path / "checkpoints.sqlite3")]
    assert cleanup_calls[:3] == [
        "old_checkpointer_close",
        "old_context_exit",
        "new_context_enter",
    ]
    assert "new_checkpointer_setup" in cleanup_calls
    assert engine._checkpointer is not None
    assert engine._checkpointer_cm is not None


@pytest.mark.asyncio
async def test_reset_checkpointer_handles_logs_checkpointer_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[tuple[str, str]] = []

    def _record(level: str):
        def _inner(template, *args, **kwargs):
            _ = kwargs
            try:
                rendered = str(template).format(*args)
            except Exception:
                rendered = str(template)
            logs.append((level, rendered))
        return _inner

    monkeypatch.setattr(
        "g3ku.runtime.engine.logger",
        type(
            "_Logger",
            (),
            {
                "info": staticmethod(_record("info")),
                "warning": staticmethod(_record("warning")),
                "debug": staticmethod(_record("debug")),
            },
        )(),
    )

    cleanup_calls: list[str] = []

    class _Checkpointer:
        async def close(self) -> None:
            cleanup_calls.append("checkpointer_close")

    class _ContextManager:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb
            cleanup_calls.append("context_exit")

    engine = AgentRuntimeEngine.__new__(AgentRuntimeEngine)
    engine._checkpointer = _Checkpointer()
    engine._checkpointer_cm = _ContextManager()

    await AgentRuntimeEngine._reset_checkpointer_handles(engine)

    info_logs = [message for level, message in logs if level == "info"]
    assert any("Closing stale SQLite checkpointer handles" in message for message in info_logs)
    assert cleanup_calls == ["checkpointer_close", "context_exit"]


@pytest.mark.asyncio
async def test_close_mcp_logs_active_sessions_before_checkpointer_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[tuple[str, str]] = []

    def _record(level: str):
        def _inner(template, *args, **kwargs):
            _ = kwargs
            try:
                rendered = str(template).format(*args)
            except Exception:
                rendered = str(template)
            logs.append((level, rendered))
        return _inner

    monkeypatch.setattr(
        "g3ku.runtime.engine.logger",
        type(
            "_Logger",
            (),
            {
                "info": staticmethod(_record("info")),
                "warning": staticmethod(_record("warning")),
                "debug": staticmethod(_record("debug")),
            },
        )(),
    )

    cleanup_calls: list[str] = []

    class _Checkpointer:
        async def close(self) -> None:
            cleanup_calls.append("checkpointer_close")

    class _ContextManager:
        async def __aexit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb
            cleanup_calls.append("context_exit")

    engine = AgentRuntimeEngine.__new__(AgentRuntimeEngine)
    engine._runtime_closed = False
    engine._consolidation_tasks = set()
    engine._commit_tasks = set()
    engine.background_pool = None
    engine.main_task_service = None
    engine.memory_manager = None
    engine._checkpointer = _Checkpointer()
    engine._checkpointer_cm = _ContextManager()
    engine._active_tasks = {"web:ceo-a": {object()}, "web:ceo-b": {object()}}

    await AgentRuntimeEngine.close_mcp(engine)

    warning_logs = [message for level, message in logs if level == "warning"]
    assert any("Closing runtime while active sessions still exist" in message for message in warning_logs)
    assert any("active_task_sessions=web:ceo-a,web:ceo-b" in message for message in warning_logs)
    assert cleanup_calls == ["checkpointer_close", "context_exit"]
