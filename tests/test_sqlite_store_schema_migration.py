from __future__ import annotations

import sqlite3

import pytest

from main.storage.sqlite_store import SQLiteTaskStore

# 4fc53500（后台进程气泡）之前的存量 tool_jobs 形状：表存在、没有
# owner_kind / task_id / node_id 三列。实测取自旧库的 PRAGMA table_info。
_LEGACY_TOOL_JOBS_DDL = """
    CREATE TABLE tool_jobs (
        job_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        turn_id TEXT NOT NULL,
        tool_call_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        args_hash TEXT NOT NULL DEFAULT '',
        hint TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        timeout_seconds REAL NOT NULL DEFAULT 0,
        owner_pid INTEGER NOT NULL,
        recovery_policy TEXT NOT NULL DEFAULT 'model_decide',
        stop_requested_by_user INTEGER NOT NULL DEFAULT 0,
        result_preview TEXT NOT NULL DEFAULT '',
        result_ref TEXT NOT NULL DEFAULT '',
        log_path TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
"""


def test_store_open_backfills_tool_jobs_owner_columns_before_indexes(tmp_path) -> None:
    """补列必须先于建索引：存量库缺列时，引用新列的索引先建会让整个 _setup
    抛 `no such column: owner_kind`、ensure 永远跑不到——实盘重启即主运行时
    初始化失败（`main runtime init failed: no such column: owner_kind`），
    worker-status 503、任务大厅加载不出来。"""
    legacy_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.execute(_LEGACY_TOOL_JOBS_DDL)
    conn.commit()
    conn.close()

    store = SQLiteTaskStore(str(legacy_path))
    try:
        columns = {
            str(row["name"])
            for row in store._conn.execute("PRAGMA table_info(tool_jobs)").fetchall()
        }
        assert {"owner_kind", "task_id", "node_id"} <= columns
        indexes = {
            str(row["name"])
            for row in store._conn.execute("PRAGMA index_list(tool_jobs)").fetchall()
        }
        assert "idx_tool_jobs_owner_task_status" in indexes
        # 补完列之后引用新列的查询要真的能跑。
        store._conn.execute(
            "SELECT job_id, owner_kind, task_id, node_id FROM tool_jobs LIMIT 1"
        ).fetchall()
    finally:
        store.close()


@pytest.mark.parametrize("reopen", [True, False])
def test_store_open_is_idempotent_on_migrated_schema(tmp_path, reopen: bool) -> None:
    """已迁移的库再开一次（重启/重开连接）不得因索引或补列再次报错。"""
    legacy_path = tmp_path / "legacy-reopen.db"
    conn = sqlite3.connect(str(legacy_path))
    conn.execute(_LEGACY_TOOL_JOBS_DDL)
    conn.commit()
    conn.close()

    first = SQLiteTaskStore(str(legacy_path))
    first.close()

    if not reopen:
        return
    second = SQLiteTaskStore(str(legacy_path))
    try:
        columns = {
            str(row["name"])
            for row in second._conn.execute("PRAGMA table_info(tool_jobs)").fetchall()
        }
        assert "owner_kind" in columns
    finally:
        second.close()
