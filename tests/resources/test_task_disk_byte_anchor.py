"""磁盘字节锚点：小时级只读水位之后的新行，不整任务重扫。

实盘形状（`task:1d9cddf9858e`）：整任务重扫 tool_results 367.24 MB / 页热 782 ms /
页冷 **55,602 ms**；`rowid > 水位` 只读最近 2,000 行 = **13.45 MB / 35.7 ms**，且
EXPLAIN QUERY PLAN 是 `SEARCH ... USING INDEX idx_task_node_tool_results_task_node_order (task_id=?)`
——索引条目自带 rowid，水位之外的行根本不取 payload 页。

盲区只有一条并有用例钉住：同一行被 ON CONFLICT 原地改写时 rowid 与行数都不变，
水位增量看不见它，最长 24 h 后由过期重锚归零。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from main.protocol import now_iso
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:anchor'
OTHER = 'task:other'
BIG = 'a' * 4_000


def _insert_result(store: SQLiteTaskStore, node_id: str, call_id: str, *, body: str = BIG) -> None:
    store._execute_write(  # noqa: SLF001
        'INSERT INTO task_node_tool_results (task_id, node_id, tool_call_id, order_index, payload_json) '
        'VALUES (?, ?, ?, ?, ?)',
        (TASK_ID, node_id, call_id, 0, json.dumps({'arguments_text': body})),
    )


def _insert_call(store: SQLiteTaskStore, node_id: str, body: str = BIG) -> None:
    store._execute_write(  # noqa: SLF001
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        (TASK_ID, node_id, now_iso(), json.dumps({'prompt': body})),
    )


def _seed(store: SQLiteTaskStore) -> None:
    for index in range(3):
        _insert_result(store, 'node:a', f'call-{index}')
        _insert_call(store, 'node:a')
    # 别的任务的分量不许混进来
    store._execute_write(  # noqa: SLF001
        'INSERT INTO task_node_tool_results (task_id, node_id, tool_call_id, order_index, payload_json) '
        'VALUES (?, ?, ?, ?, ?)',
        (OTHER, 'node:z', 'call-z', 0, json.dumps({'arguments_text': 'z' * 9_000})),
    )


def test_first_pass_is_exact_and_anchors(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        _seed(store)
        db_bytes, anchor, mode = store.reconcile_task_detail_bytes(TASK_ID, {})
        assert mode == 'exact'
        assert db_bytes == store.sum_task_detail_bytes([TASK_ID])[TASK_ID] > 0
        marks = dict(anchor.get('marks') or {})
        assert set(marks) == set(SQLiteTaskStore._BYTE_ANCHOR_TABLES)  # noqa: SLF001
        assert int(anchor['big_bytes']) > 0
        assert anchor.get('anchored_at')
    finally:
        store.close()


def test_appended_rows_are_counted_without_rescanning(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        _seed(store)
        _db, anchor, _mode = store.reconcile_task_detail_bytes(TASK_ID, {})
        _insert_result(store, 'node:a', 'call-new')
        _insert_call(store, 'node:a')
        db_bytes, new_anchor, mode = store.reconcile_task_detail_bytes(TASK_ID, anchor)
        assert mode == 'delta'
        assert int(new_anchor['last']['scanned_rows']) == 2
        # 增量结果必须与"现在做一次精确重扫"逐字节相等
        assert db_bytes == store.sum_task_detail_bytes([TASK_ID])[TASK_ID]
    finally:
        store.close()


def test_shrunk_row_count_forces_reanchor(tmp_path: Path):
    """裁剪/删除会让水位之上的行消失；少行就必须重锚，否则总量一直虚高。"""
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        _seed(store)
        _db, anchor, _mode = store.reconcile_task_detail_bytes(TASK_ID, {})
        store._execute_write('DELETE FROM task_node_tool_results WHERE tool_call_id = ?', ('call-0',))  # noqa: SLF001
        _db2, anchor2, mode = store.reconcile_task_detail_bytes(TASK_ID, anchor)
        assert mode == 'exact'
        assert _db2 == store.sum_task_detail_bytes([TASK_ID])[TASK_ID]
        assert int(anchor2['rows']['task_node_tool_results']) == 2
    finally:
        store.close()


def test_expired_anchor_forces_reanchor(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        _seed(store)
        _db, anchor, _mode = store.reconcile_task_detail_bytes(TASK_ID, {})
        stale = dict(anchor)
        stale['anchored_at'] = (datetime.now().astimezone() - timedelta(seconds=store._BYTE_ANCHOR_MAX_AGE_SECONDS + 60)).isoformat(timespec='seconds')  # noqa: SLF001
        _insert_result(store, 'node:a', 'call-late')
        _db2, _anchor2, mode = store.reconcile_task_detail_bytes(TASK_ID, stale)
        assert mode == 'exact'
    finally:
        store.close()


def test_in_place_rewrite_is_blind_until_the_next_reanchor(tmp_path: Path):
    """这条把"盲区"钉成合同，而不是假装增量全能。

    `upsert_task_node_tool_result` 走 ON CONFLICT DO UPDATE：同一 (task,node,tool_call)
    被改写时 rowid 与行数都不动，水位增量看不见字节变化，只能等 24 h 过期重锚归零。
    """
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        _seed(store)
        _db, anchor, _mode = store.reconcile_task_detail_bytes(TASK_ID, {})
        store._execute_write(  # noqa: SLF001
            'UPDATE task_node_tool_results SET payload_json = ? WHERE tool_call_id = ?',
            (json.dumps({'arguments_text': 'b' * 60_000}), 'call-1'),
        )
        blind_bytes, _anchor2, mode = store.reconcile_task_detail_bytes(TASK_ID, anchor)
        truth = store.sum_task_detail_bytes([TASK_ID])[TASK_ID]
        assert mode == 'delta'
        assert blind_bytes < truth
        recovered, _anchor3, mode3 = store.reconcile_task_detail_bytes(TASK_ID, {})
        assert mode3 == 'exact'
        assert recovered == truth
    finally:
        store.close()


def test_anchor_round_trips_through_the_ledger_row(tmp_path: Path):
    """目录 bump 那条车道不带锚点，不许把已存的锚点擦掉。"""
    path = tmp_path / 'runtime.sqlite3'
    store = SQLiteTaskStore(path)
    try:
        _seed(store)
        db_bytes, anchor, _mode = store.reconcile_task_detail_bytes(TASK_ID, {})
        store.upsert_task_disk_usage(TASK_ID, 1_000_000, anchor=anchor)
        assert store.read_task_disk_anchor(TASK_ID)['marks'] == anchor['marks']
        store.upsert_task_disk_usage(TASK_ID, 2_000_000)  # 只 bump 目录字节
        assert store.read_task_disk_anchor(TASK_ID)['marks'] == anchor['marks']
        row = store._fetchone('SELECT total_bytes FROM task_disk_usage WHERE task_id=?', (TASK_ID,))  # noqa: SLF001
        assert int(row['total_bytes']) == 2_000_000
        assert db_bytes > 0
    finally:
        store.close()
