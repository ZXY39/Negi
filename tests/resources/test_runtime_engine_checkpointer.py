from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import aiosqlite
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


@pytest.mark.asyncio
async def test_purge_checkpointer_thread_deletes_thread_rows_and_checkpoints_wal(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite3"
    sqlite3.connect(db_path).close()

    class _Checkpointer:
        def __init__(self) -> None:
            self.deleted_threads: list[str] = []
            self.conn = None

        async def adelete_thread(self, thread_id: str) -> None:
            self.deleted_threads.append(thread_id)

    class _Conn:
        def __init__(self) -> None:
            self.executed: list[str] = []

        async def execute(self, sql: str) -> None:
            self.executed.append(str(sql))

    engine = AgentRuntimeEngine.__new__(AgentRuntimeEngine)
    engine._checkpointer_enabled = True
    engine._checkpointer_backend = "sqlite"
    engine._checkpointer_path = str(db_path)
    engine._checkpointer = _Checkpointer()
    engine._checkpointer.conn = _Conn()
    engine._checkpointer_cm = None
    engine._checkpointer_lock = asyncio.Lock()

    await AgentRuntimeEngine._purge_checkpointer_thread(engine, "web:shared")

    assert engine._checkpointer.deleted_threads == ["web:shared"]
    assert engine._checkpointer.conn.executed == ["PRAGMA wal_checkpoint(TRUNCATE)"]


@pytest.mark.asyncio
async def test_trim_checkpointer_history_keeps_latest_rows_per_thread(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints.sqlite3"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BLOB,
                PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )
            """
        )
        for checkpoint_id in ("cp-1", "cp-2", "cp-3"):
            conn.execute(
                "INSERT INTO checkpoints(thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata) VALUES (?, '', ?, 'msgpack', X'01', X'02')",
                ("web:shared", checkpoint_id),
            )
            conn.execute(
                "INSERT INTO writes(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) VALUES (?, '', ?, 'task:demo', 0, 'messages', 'msgpack', X'03')",
                ("web:shared", checkpoint_id),
            )
        conn.commit()
    finally:
        conn.close()

    aconn = await aiosqlite.connect(db_path)
    try:
        class _Checkpointer:
            def __init__(self, conn) -> None:
                self.conn = conn

        engine = AgentRuntimeEngine.__new__(AgentRuntimeEngine)
        engine._checkpointer_enabled = True
        engine._checkpointer_backend = "sqlite"
        engine._checkpointer_path = str(db_path)
        engine._checkpointer = _Checkpointer(aconn)
        engine._checkpointer_cm = None
        engine._checkpointer_lock = asyncio.Lock()
        engine._checkpointer_max_checkpoints_per_thread = 2

        await AgentRuntimeEngine._trim_checkpointer_history(engine)

        remaining_checkpoints = [
            row[0]
            async for row in (await aconn.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? ORDER BY rowid",
                ("web:shared",),
            ))
        ]
        remaining_writes = [
            row[0]
            async for row in (await aconn.execute(
                "SELECT checkpoint_id FROM writes WHERE thread_id = ? ORDER BY checkpoint_id",
                ("web:shared",),
            ))
        ]
    finally:
        await aconn.close()

    assert remaining_checkpoints == ["cp-2", "cp-3"]
    assert remaining_writes == ["cp-2", "cp-3"]
