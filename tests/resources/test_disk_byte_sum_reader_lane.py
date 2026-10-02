"""磁盘字节汇总必须走自己的读口，并在长块榜上留名。

实盘证据：`task:1d9cddf9858e` 的 DB 份额 **526.79 MB**（tool_results 351.15 MB / 62,498 行、
model_calls 97.68 MB / 39,146 行、details 74.20 MB / 1,011 行），冷页时
`SUM(LENGTH(payload_json))` 单句实测 **20,999 ms**（task_model_calls）与 **36,859 ms**
（task_node_tool_results）。那时共享读锁把无关的 `fetchone:tasks[from=_require_task]`
拖到 36,680 ms、`log_service.update_frame` 拖到 36,792 ms。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from main.protocol import now_iso
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:bytes'
BIG = 'x' * 5_000
_TABLES = list(SQLiteTaskStore._DETAIL_PRUNE_TABLES)  # noqa: SLF001


class _Recorder:
    def __init__(self) -> None:
        self.sections: list[str] = []

    def record(self, *, section: str, elapsed_ms: float, started_at: str) -> None:  # noqa: ARG002
        self.sections.append(str(section))


def _seed(store: SQLiteTaskStore) -> None:
    stamp = now_iso()
    payload = json.dumps({'payload': {'goal': BIG}})
    store._execute_write(  # noqa: SLF001
        'INSERT INTO task_node_details (node_id, task_id, updated_at, payload_json) VALUES (?, ?, ?, ?)',
        ('node:a', TASK_ID, stamp, payload),
    )
    store._execute_write(  # noqa: SLF001
        'INSERT INTO task_node_tool_results (task_id, node_id, tool_call_id, order_index, payload_json) VALUES (?, ?, ?, ?, ?)',
        (TASK_ID, 'node:a', 'call:a', 0, json.dumps({'arguments_text': BIG})),
    )
    store._execute_write(  # noqa: SLF001
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        (TASK_ID, 'node:a', stamp, json.dumps({'prompt': BIG})),
    )
    # 另一个任务的分量不许混进来
    store._execute_write(  # noqa: SLF001
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        ('task:other', 'node:z', stamp, json.dumps({'prompt': 'y' * 9_000})),
    )


def test_byte_sum_matches_direct_sql_per_task(tmp_path: Path):
    path = tmp_path / 'runtime.sqlite3'
    store = SQLiteTaskStore(path)
    try:
        _seed(store)
        totals = store.sum_task_detail_bytes([TASK_ID, 'task:other'])
        with sqlite3.connect(path) as conn:
            expected = 0
            other = 0
            for table in _TABLES:
                expected += int(
                    conn.execute(
                        f'SELECT COALESCE(SUM(LENGTH(payload_json)), 0) FROM {table} WHERE task_id = ?',
                        (TASK_ID,),
                    ).fetchone()[0]
                )
                other += int(
                    conn.execute(
                        f'SELECT COALESCE(SUM(LENGTH(payload_json)), 0) FROM {table} WHERE task_id = ?',
                        ('task:other',),
                    ).fetchone()[0]
                )
        assert totals[TASK_ID] == expected > 0
        assert totals['task:other'] == other
        assert totals[TASK_ID] != totals['task:other']
        assert store.sum_task_detail_bytes([]) == {}
        assert store.sum_task_detail_bytes(['task:absent'])['task:absent'] == 0
    finally:
        store.close()


def test_byte_sum_does_not_take_the_shared_reader(tmp_path: Path):
    """这条读数不许占共享读口：占住它，控制流读会跟着等几十秒。"""
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        _seed(store)

        def _boom(*args, **kwargs):  # noqa: ARG001
            raise AssertionError('bulk byte sum must not read through the shared connection')

        store._fetchall = _boom  # type: ignore[method-assign]  # noqa: SLF001
        store._fetchone = _boom  # type: ignore[method-assign]  # noqa: SLF001
        assert store.sum_task_detail_bytes([TASK_ID])[TASK_ID] > 0
    finally:
        store.close()


def test_byte_sum_keeps_the_table_name_on_the_board(tmp_path: Path):
    recorder = _Recorder()
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3', debug_recorder=recorder)
    try:
        _seed(store)
        store.sum_task_detail_bytes([TASK_ID])
    finally:
        store.close()

    for table in _TABLES:
        assert f'sqlite.query.fetchall:{table}' in recorder.sections, (table, recorder.sections)


def test_byte_sum_is_query_only(tmp_path: Path):
    """它开的是只读连接：这条读数不许写任何东西。"""
    path = tmp_path / 'runtime.sqlite3'
    store = SQLiteTaskStore(path)
    try:
        _seed(store)
        before = store._conn.execute('SELECT COUNT(*) FROM task_model_calls').fetchone()[0]  # noqa: SLF001
        store.sum_task_detail_bytes([TASK_ID])
        try:
            store._open_read_conn().execute('INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?,?,?,?)', ('task:x', 'node:x', now_iso(), '{}')).execute('INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?,?,?,?)', ('task:x', 'node:x', now_iso(), '{}'))
            raised = False
        except sqlite3.OperationalError:
            raised = True
        assert raised, 'read connections must be query_only'
        assert store._conn.execute('SELECT COUNT(*) FROM task_model_calls').fetchone()[0] == before  # noqa: SLF001
    finally:
        store.close()
