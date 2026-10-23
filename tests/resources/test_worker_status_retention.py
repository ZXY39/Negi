from __future__ import annotations

from datetime import datetime, timedelta

from main.storage.sqlite_store import WORKER_STATUS_KEEP_PER_ROLE, SQLiteTaskStore


def test_upsert_prunes_stale_worker_rows_per_role(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        base = datetime(2026, 1, 1, 0, 0, 0)
        total = WORKER_STATUS_KEEP_PER_ROLE + 7
        for index in range(total):
            store.upsert_worker_status(
                worker_id=f'worker:{index:04d}',
                role='task_worker',
                status='running',
                updated_at=(base + timedelta(minutes=index)).isoformat(),
                payload={'active_task_count': 0},
            )

        rows = store.list_worker_status(role='task_worker')
        assert len(rows) == WORKER_STATUS_KEEP_PER_ROLE
        kept = {str(row.get('worker_id') or '') for row in rows}
        assert f'worker:{total - 1:04d}' in kept
        assert 'worker:0000' not in kept

        store.upsert_worker_status(
            worker_id='heartbeat:1',
            role='heartbeat',
            status='running',
            updated_at=base.isoformat(),
            payload={},
        )
        assert [row.get('worker_id') for row in store.list_worker_status(role='heartbeat')] == ['heartbeat:1']
    finally:
        store.close()
