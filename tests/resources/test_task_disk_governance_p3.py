"""P3 磁盘治理单测：终态大行裁剪、维护窗口卡权、删除渐进+墓碑、auto_vacuum、维护脚本。"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from main.models import TaskRecord
from main.storage import disk_guard
from main.storage.disk_guard import DiskPolicies, configure_disk_policies
from main.storage.sqlite_store import SQLiteTaskStore
from main.storage.task_archive import TaskArchiver

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / 'scripts' / 'compact_task_database.py'


@pytest.fixture()
def policies_guard():
    previous = disk_guard.disk_policies()
    yield
    configure_disk_policies(previous)
    disk_guard.invalidate_disk_usage_cache()


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec='seconds')


def _task(task_id: str, status: str = 'success', days_ago: float = 20.0, metadata: dict | None = None) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        title='t',
        user_request='r',
        root_node_id='node:root',
        status=status,
        created_at=_iso(days_ago + 1),
        updated_at=_iso(days_ago),
        finished_at=_iso(days_ago),
        metadata=dict(metadata or {}),
    )


def _insert_model_call(store: SQLiteTaskStore, task_id: str, node_id: str = 'node:root') -> None:
    store._execute_write(
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        (task_id, node_id, _iso(20), '{}'),
    )


# --- auto_vacuum 新库即 INCREMENTAL ---


def test_new_db_auto_vacuum_incremental(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        rows = store._fetchall('PRAGMA auto_vacuum')
        assert int(rows[0][0]) == 2  # INCREMENTAL
    finally:
        store.close()


# --- 14 天裁剪 ---


def test_prune_task_detail_rows(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task(_task('task:old', days_ago=20))
        store.upsert_task(_task('task:recent', days_ago=3))
        store.upsert_task(_task('task:running', status='in_progress', days_ago=30))
        _insert_model_call(store, 'task:old')
        _insert_model_call(store, 'task:recent')
        _insert_model_call(store, 'task:running')
        cutoff = _iso(14)
        counts = store.count_prunable_rows(cutoff)
        assert counts['task_model_calls'] == 1  # 只算终态且过期的 task:old
        result = store.prune_task_detail_rows(cutoff)
        assert result['task_count'] == 1
        assert result['deleted']['task_model_calls'] == 1
        remaining = store._fetchall('SELECT task_id FROM task_model_calls ORDER BY task_id')
        assert [str(row['task_id']) for row in remaining] == ['task:recent', 'task:running']
        # error_logs / tasks 结构不受影响
        assert store.get_task('task:old') is not None
        # 幂等：再裁一次 0 行
        again = store.prune_task_detail_rows(cutoff)
        assert again['task_count'] == 1 and again['deleted']['task_model_calls'] == 0
    finally:
        store.close()


def test_claim_maintenance_run_interval(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        assert store.claim_maintenance_run('detail_prune', min_interval_seconds=3600) is True
        assert store.claim_maintenance_run('detail_prune', min_interval_seconds=3600) is False
        assert store.claim_maintenance_run('detail_prune', min_interval_seconds=0) is True
        assert store.claim_maintenance_run('', min_interval_seconds=0) is False
    finally:
        store.close()


# --- 删除渐进 + 墓碑 ---


def _bind_purge_harness(store, archiver, dirs_by_task):
    import main.service.runtime_service as runtime_service_module

    harness = SimpleNamespace()
    harness.store = store
    harness.task_archiver = archiver
    harness.log_service = SimpleNamespace(append_task_event=lambda **_kw: 0)
    harness.get_task = lambda task_id: store.get_task(task_id)
    harness.normalize_task_id = lambda task_id: str(task_id or '').strip()
    harness._task_archive_dirs = lambda task_id: dirs_by_task.get(task_id, {})
    harness._publish_task_archive_state_changed = MethodType(
        runtime_service_module.MainRuntimeService._publish_task_archive_state_changed, harness,
    )
    harness._purge_one_archive = MethodType(
        runtime_service_module.MainRuntimeService._purge_one_archive, harness,
    )
    harness._decompress_one_task = MethodType(
        runtime_service_module.MainRuntimeService._decompress_one_task, harness,
    )
    harness._reconcile_task_disk_usage = lambda task_id: None
    harness._decompress_inflight = set()
    return harness


def _make_dirs(root: Path, task_dir_name: str) -> dict[str, Path]:
    artifacts = root / 'artifacts' / task_dir_name
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / 'a.md').write_text('content' * 200, encoding='utf-8')
    return {'artifacts': artifacts}


async def test_purge_one_archive_tombstone(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task(_task('task:t1', metadata={'archived_at': _iso(2), 'archived_bytes': 1400}))
        store.upsert_task(_task('task:pinned', metadata={'archived_at': _iso(3), 'pinned': True}))
        store.upsert_task_disk_usage('task:t1', 300)
        archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
        # 伪造归档 zip（直接压一个源目录）
        dirs = _make_dirs(tmp_path, 'task_t1')
        result = archiver.archive('task:t1', dirs)
        assert result is not None
        pinned_dirs = _make_dirs(tmp_path / 'pinned_src', 'task_pinned')
        archiver.archive('task:pinned', pinned_dirs)

        harness = _bind_purge_harness(store, archiver, {'task:t1': dirs})
        # list_archived_tasks 过滤与排序
        entries = store.list_archived_tasks()
        assert {entry['task_id'] for entry in entries} == {'task:t1', 'task:pinned'}
        candidates = sorted(
            (e for e in entries if not e['pinned'] and not e['purged_at']),
            key=lambda e: e['archived_at'],
        )
        assert [e['task_id'] for e in candidates] == ['task:t1']
        # purge：zip 删除、墓碑落 metadata、占用归零
        assert await harness._purge_one_archive('task:t1') is True
        assert not archiver.archive_path_for('task:t1').exists()
        task = store.get_task('task:t1')
        assert task.is_purged()
        assert task.metadata.get('purge_reason') == 'disk_cleanup'
        assert not task.metadata.get('archived_at')
        # tasks 行仍在（墓碑可查）
        assert store.get_task('task:t1') is not None
        assert store.get_task_disk_usages(['task:t1'])['task:t1'] == 0
        # 二次 purge False；pinned 拒绝
        assert await harness._purge_one_archive('task:t1') is False
        assert await harness._purge_one_archive('task:pinned') is False
        assert archiver.archive_path_for('task:pinned').exists()
        # purged 任务解压返回 purged
        dec = await harness._decompress_one_task('task:t1')
        assert dec['result'] == 'purged'
    finally:
        store.close()


def test_is_purged_helper():
    task = _task('task:t1')
    assert task.is_purged() is False
    purged = _task('task:t1', metadata={'purged_at': _iso(1)})
    assert purged.is_purged() is True


# --- 维护脚本 ---


def test_compact_script_dry_run_and_apply(tmp_path):
    db_path = tmp_path / 'runtime.sqlite3'
    store = SQLiteTaskStore(db_path)
    store.upsert_task(_task('task:old', days_ago=20))
    store.upsert_task(_task('task:recent', days_ago=1))
    _insert_model_call(store, 'task:old')
    _insert_model_call(store, 'task:recent')
    store.close()
    py = str(REPO_ROOT / '.venv' / 'Scripts' / 'python.exe')
    # dry-run：不写库
    before = db_path.stat().st_mtime_ns
    proc = subprocess.run(
        [py, str(SCRIPT), '--runtime-db', str(db_path), '--retention-days', '14'],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0
    assert 'dry-run' in proc.stdout
    assert 'task_model_calls: 1 行' in proc.stdout
    # apply：裁掉旧任务行
    proc = subprocess.run(
        [py, str(SCRIPT), '--runtime-db', str(db_path), '--retention-days', '14', '--apply', '--backup'],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '裁剪完成' in proc.stdout
    assert list(tmp_path.glob('runtime.sqlite3.bak-*')), '--backup 应生成镜像'
    store = SQLiteTaskStore(db_path)
    try:
        remaining = store._fetchall('SELECT task_id FROM task_model_calls')
        assert [str(row['task_id']) for row in remaining] == ['task:recent']
        assert store.get_task('task:old') is not None  # 墓碑结构保留
    finally:
        store.close()
    _ = before
