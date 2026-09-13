"""删除台账（task_delete_ledger）单测：守卫、补偿、过期、遗留墓碑清扫、孤儿目录。"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from main.models import TaskRecord
from main.service.runtime_service import MainRuntimeService


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _make_web_service(tmp_path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )


def _terminal_task(task_id: str, metadata: dict | None = None, days_ago: float = 3.0) -> TaskRecord:
    iso = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec='seconds')
    return TaskRecord(
        task_id=task_id,
        session_id="web:demo",
        title="ledger 测试",
        user_request="x",
        status="success",
        root_node_id="node-root",
        created_at=iso,
        updated_at=iso,
        finished_at=iso,
        metadata=dict(metadata or {}),
    )


def test_ledger_record_marks_and_queries(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    store = service.store
    assert not store.is_task_deleted('task:t1')
    store.record_task_delete('task:t1', reason='user_delete', deliverables_exported=2)
    assert store.is_task_deleted('task:t1')
    rows = store.list_task_delete_ledger_rows(wiped=0)
    assert rows[0]['task_id'] == 'task:t1' and rows[0]['deliverables_exported'] == 2
    store.mark_task_delete_wiped('task:t1')
    assert store.list_task_delete_ledger_rows(wiped=0) == []
    assert len(store.list_task_delete_ledger_rows(wiped=1)) == 1
    # 重复 record 保留原 deleted_at（过期时钟不被补偿重放重置）
    store._execute_write(
        "UPDATE task_delete_ledger SET deleted_at = '2026-01-01T00:00:00+00:00', wiped = 0 WHERE task_id = 'task:t1'"
    )
    store.record_task_delete('task:t1', reason='compensate', deliverables_exported=0)
    rows = store.list_task_delete_ledger_rows()
    assert rows[0]['deleted_at'] == '2026-01-01T00:00:00+00:00'
    store.delete_task_delete_ledger_row('task:t1')
    assert not store.is_task_deleted('task:t1')


def test_compensate_wipe_replays_idempotently(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:compensate'
    service.store.upsert_task(_terminal_task(task_id))
    service.store.record_task_delete(task_id, reason='user_delete')
    # 模拟中断残留：event-history 目录 + temp 目录还在
    eh_dir = service.store._event_history_dir / task_id.replace(':', '_')
    eh_dir.mkdir(parents=True, exist_ok=True)
    (eh_dir / 'latest.json.gz').write_bytes(b'x')
    temp_dir = tmp_path / 'temp' / 'tasks' / task_id.replace(':', '_')
    temp_dir.mkdir(parents=True, exist_ok=True)
    (temp_dir / 'leftover.txt').write_text('y', encoding='utf-8')

    assert service._compensate_wipe(task_id) is True
    assert service.store.get_task(task_id) is None
    assert not eh_dir.exists()
    assert not temp_dir.exists()
    rows = service.store.list_task_delete_ledger_rows(wiped=1)
    assert [r['task_id'] for r in rows] == [task_id]
    # 幂等：再补一次不报错
    assert service._compensate_wipe(task_id) is True


async def test_sweep_expires_ledger_and_cleans_legacy_tombstones(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    store = service.store
    # 1) 过期台账行：deleted_at 8 天前 + wiped=1 + 一条复活的 task_events 行
    store.record_task_delete('task:expired', reason='user_delete')
    store.mark_task_delete_wiped('task:expired')
    store._execute_write(
        "UPDATE task_delete_ledger SET deleted_at = ? WHERE task_id = 'task:expired'",
        ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec='seconds'),),
    )
    store._execute_write(
        "INSERT INTO task_events (task_id, session_id, event_type, created_at, payload_json) "
        "VALUES ('task:expired', 'web:demo', 'task.node.patch', '2026-09-01T00:00:00+08:00', '{}')"
    )
    # 2) 遗留 purged 墓碑任务（zip 已不存在的旧删除渐进产物）
    store.upsert_task(_terminal_task('task:legacy', metadata={'purged_at': '2026-09-01T00:00:00+08:00', 'purge_reason': 'disk_cleanup'}))
    # 3) 孤儿 event-history 目录（tasks 表无行 + mtime 超宽限）
    orphan_dir = store._event_history_dir / 'task_gone'
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / 'latest.json.gz').write_bytes(b'x')
    old = time.time() - 10 * 24 * 3600
    os.utime(orphan_dir, (old, old))

    await service._run_delete_ledger_sweep_if_due()

    # task:expired 台账行过期删除（最终补偿顺带清掉复活的 task_events 行）；
    # task:legacy 的台账行是本轮 sweep 新记的（未过期），仍在守卫窗口内
    remaining_ids = {row['task_id'] for row in store.list_task_delete_ledger_rows()}
    assert remaining_ids == {'task:legacy'}
    assert not store.is_task_deleted('task:expired')
    row = store._fetchone("SELECT COUNT(*) AS n FROM task_events WHERE task_id = 'task:expired'", ())
    assert int(row['n']) == 0
    # 遗留墓碑任务被全删
    assert store.get_task('task:legacy') is None
    assert store.is_task_deleted('task:legacy')
    # 孤儿目录被清扫
    assert not orphan_dir.exists()


def test_sum_task_detail_bytes(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    store = service.store
    store._execute_write(
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        ('task:a', 'node:1', '2026-09-11T10:00:00+08:00', 'x' * 100),
    )
    store._execute_write(
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        ('task:b', 'node:1', '2026-09-11T10:00:00+08:00', 'y' * 50),
    )
    totals = store.sum_task_detail_bytes(['task:a', 'task:b', 'task:missing'])
    assert totals['task:a'] == 100
    assert totals['task:b'] == 50
    assert totals['task:missing'] == 0
    assert store.sum_task_detail_bytes([]) == {}
