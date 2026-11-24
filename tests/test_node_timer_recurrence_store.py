"""Node timer recurrence: schema migration and two-kind store contract.

The migration rebuilds the legacy one-shot table (PK task_id,node_id) into the
two-kind shape (PK task_id,node_id,kind) without losing existing rows; once and
recurring wakes coexist per node and never overwrite each other.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from main.storage.sqlite_store import SQLiteTaskStore

TASK = 'task:rec'
NODE = 'node:root'


def _seed_legacy_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        '''
        CREATE TABLE task_node_timers (
            task_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            wake_at_ms INTEGER NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            armed_at TEXT NOT NULL,
            claimed_by TEXT NOT NULL DEFAULT '',
            claimed_at_ms INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, node_id)
        )
        '''
    )
    conn.execute(
        'INSERT INTO task_node_timers (task_id, node_id, wake_at_ms, note, armed_at) VALUES (?, ?, ?, ?, ?)',
        (TASK, NODE, 1_700_000_000_000, 'legacy note', '2026-11-24T08:00:00+08:00'),
    )
    conn.commit()
    conn.close()


def _timer_columns(store: SQLiteTaskStore) -> dict[str, int]:
    rows = store._conn.execute('PRAGMA table_info(task_node_timers)').fetchall()
    return {str(row[1]): int(row[5] or 0) for row in rows}


def test_migration_adds_kind_and_preserves_legacy_rows(tmp_path):
    path = tmp_path / 'runtime.sqlite3'
    _seed_legacy_db(path)
    store = SQLiteTaskStore(path)
    try:
        columns = _timer_columns(store)
        assert 'kind' in columns and 'schedule_kind' in columns and 'cron_expr' in columns
        # PK is now (task_id, node_id, kind): pk positions 1, 2, 3.
        assert columns['task_id'] == 1 and columns['node_id'] == 2 and columns['kind'] == 3
        record = store.get_task_node_timer(TASK, NODE)
        assert record is not None
        assert record.kind == 'once'
        assert record.wake_at_ms == 1_700_000_000_000
        assert record.note == 'legacy note'
    finally:
        store.close()


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / 'runtime.sqlite3'
    _seed_legacy_db(path)
    first = SQLiteTaskStore(path)
    first.close()
    second = SQLiteTaskStore(path)
    try:
        rows = second.list_task_node_timers(TASK)
        assert len(rows) == 1 and rows[0].kind == 'once'
    finally:
        second.close()


def test_once_and_recurring_coexist(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_node_timer(
            task_id=TASK, node_id=NODE, wake_at_ms=1000, note='once', armed_at='t',
        )
        store.upsert_task_node_timer(
            task_id=TASK, node_id=NODE, wake_at_ms=2000, note='recurring', armed_at='t',
            kind='recurring', schedule_kind='every', every_ms=60_000,
        )
        rows = store.list_task_node_timers(TASK)
        assert len(rows) == 2
        once = store.get_task_node_timer(TASK, NODE, kind='once')
        rec = store.get_task_node_timer(TASK, NODE, kind='recurring')
        assert once is not None and once.wake_at_ms == 1000
        assert rec is not None and rec.wake_at_ms == 2000 and rec.every_ms == 60_000
        # Re-arming recurring replaces only recurring, leaves once untouched.
        store.upsert_task_node_timer(
            task_id=TASK, node_id=NODE, wake_at_ms=3000, note='recurring2', armed_at='t',
            kind='recurring', schedule_kind='every', every_ms=120_000,
        )
        assert store.get_task_node_timer(TASK, NODE, kind='once').wake_at_ms == 1000
        assert store.get_task_node_timer(TASK, NODE, kind='recurring').wake_at_ms == 3000
        assert len(store.list_task_node_timers(TASK)) == 2
    finally:
        store.close()


def test_rearm_updates_moment_clears_claim_keeps_schedule(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_node_timer(
            task_id=TASK, node_id=NODE, wake_at_ms=1000, note='rec', armed_at='t',
            kind='recurring', schedule_kind='cron', cron_expr='0 6 * * *', tz='UTC',
        )
        claimed = store.claim_due_task_node_timers(now_ms=5000, claimed_by='w1')
        assert len(claimed) == 1 and claimed[0].claimed_by == 'w1'
        rearmed = store.rearm_task_node_timer(
            task_id=TASK, node_id=NODE, kind='recurring', wake_at_ms=9000,
        )
        assert rearmed is not None
        assert rearmed.wake_at_ms == 9000
        assert rearmed.claimed_by == '' and rearmed.claimed_at_ms == 0
        assert rearmed.schedule_kind == 'cron' and rearmed.cron_expr == '0 6 * * *' and rearmed.tz == 'UTC'
    finally:
        store.close()


def test_delete_is_kind_scoped(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_node_timer(task_id=TASK, node_id=NODE, wake_at_ms=1000, note='o', armed_at='t')
        store.upsert_task_node_timer(
            task_id=TASK, node_id=NODE, wake_at_ms=2000, note='r', armed_at='t', kind='recurring',
        )
        store.delete_task_node_timer(task_id=TASK, node_id=NODE, kind='once')
        rows = store.list_task_node_timers(TASK)
        assert len(rows) == 1 and rows[0].kind == 'recurring'
    finally:
        store.close()
