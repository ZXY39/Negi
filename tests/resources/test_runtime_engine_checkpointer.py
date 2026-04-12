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
