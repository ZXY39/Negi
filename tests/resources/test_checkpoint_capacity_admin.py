from __future__ import annotations

import sqlite3 as sqlite3_sync
import sys
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fastapi import HTTPException

from main.api import admin_rest


@pytest.mark.asyncio
async def test_maintain_checkpoints_returns_reclaim_report(monkeypatch):
    async def _reclaim(*, force=False):
        assert force is True
        return {"path": "memory/checkpoints.sqlite3", "vacuumed": True, "file_size_after": 123}

    monkeypatch.setattr(
        admin_rest, "_checkpoint_agent", lambda: SimpleNamespace(reclaim_checkpointer_space=_reclaim)
    )
    result = await admin_rest.maintain_checkpoints()
    assert result["ok"] is True
    assert result["report"]["vacuumed"] is True
    assert result["report"]["file_size_after"] == 123


@pytest.mark.asyncio
async def test_maintain_checkpoints_503_when_engine_lacks_reclaim(monkeypatch):
    monkeypatch.setattr(admin_rest, "_checkpoint_agent", lambda: SimpleNamespace())
    with pytest.raises(HTTPException) as excinfo:
        await admin_rest.maintain_checkpoints()
    assert excinfo.value.status_code == 503


@pytest.mark.asyncio
async def test_checkpoints_status_reports_governance_knobs(monkeypatch):
    agent = SimpleNamespace(
        _checkpointer_enabled=True,
        _checkpointer_backend="sqlite",
        _checkpointer_path=None,
        _checkpointer=None,
        _checkpointer_max_checkpoints_per_thread=200,
        _checkpointer_trim_interval_seconds=300.0,
        _checkpointer_vacuum_min_file_size_bytes=536870912,
        _checkpointer_vacuum_interval_seconds=21600.0,
    )
    monkeypatch.setattr(admin_rest, "_checkpoint_agent", lambda: agent)
    result = await admin_rest.get_checkpoints_status()
    item = result["item"]
    assert item["enabled"] is True
    assert item["backend"] == "sqlite"
    assert item["max_checkpoints_per_thread"] == 200
    assert item["vacuum_min_file_size_bytes"] == 536870912
    assert item["vacuum_interval_seconds"] == 21600.0


@pytest.mark.asyncio
async def test_checkpoints_status_reads_sqlite_stats(tmp_path, monkeypatch):
    db_path = tmp_path / "checkpoints.sqlite3"
    conn = sqlite3_sync.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT DEFAULT '', checkpoint_id TEXT, checkpoint BLOB)"
        )
        conn.execute(
            "CREATE TABLE writes (thread_id TEXT, checkpoint_ns TEXT DEFAULT '', checkpoint_id TEXT, task_id TEXT, idx INTEGER, value BLOB)"
        )
        conn.execute("INSERT INTO checkpoints(thread_id, checkpoint_id) VALUES ('web:shared', 'cp-1')")
        conn.commit()
    finally:
        conn.close()

    aconn = await aiosqlite.connect(db_path)
    try:
        agent = SimpleNamespace(
            _checkpointer_enabled=True,
            _checkpointer_backend="sqlite",
            _checkpointer_path=str(db_path),
            _checkpointer=SimpleNamespace(conn=aconn),
            _checkpointer_lock=None,
            _checkpointer_max_checkpoints_per_thread=200,
            _checkpointer_trim_interval_seconds=300.0,
            _checkpointer_vacuum_min_file_size_bytes=536870912,
            _checkpointer_vacuum_interval_seconds=21600.0,
        )
        monkeypatch.setattr(admin_rest, "_checkpoint_agent", lambda: agent)
        result = await admin_rest.get_checkpoints_status()
        item = result["item"]
        assert item["file_size_bytes"] > 0
        assert item["checkpoint_rows"] == 1
        assert item["page_size"] > 0
        assert item["page_count"] > 0
        assert item["reclaimable_estimate_bytes"] >= 0
    finally:
        await aconn.close()
