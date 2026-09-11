"""event-history 保留期清理回归测试（P3+）。

修复缺陷：任务事件外置归档 event-history/task_<id>/*.json.gz 此前除用户
delete_task 外永不清理（设计上"永久保留"），重度任务持续累积（实例：两个
success 任务 ~4.7 GB），是磁盘写满事故的累积源头之一。现在按
event_history_retention_days（默认 14 天，0=关闭）在小时级维护循环中清理
超期终态任务的外置归档：删除归档文件并置空 DB 行引用，task_events 行与
slim 预览保留（list_task_events 自动降级）；pinned / archived_at 任务豁免；
顺带清扫 tasks 表已无对应行且超过宽限期的孤儿目录。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from main.models import TaskRecord
from main.storage import disk_guard
from main.storage.disk_guard import DiskPolicies, configure_disk_policies
from main.storage.sqlite_store import SQLiteTaskStore


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds')


def _days_ago(days: float) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(days=days))


def _make_store(tmp_path) -> SQLiteTaskStore:
    return SQLiteTaskStore(
        tmp_path / 'runtime.sqlite3',
        event_history_dir=tmp_path / 'event-history',
    )


def _task(
    task_id: str,
    *,
    status: str = 'success',
    days_old: float = 20,
    pinned: bool = False,
    archived: bool = False,
) -> TaskRecord:
    finished = _days_ago(days_old)
    metadata: dict = {}
    if pinned:
        metadata['pinned'] = True
    if archived:
        metadata['archived_at'] = finished
    return TaskRecord(
        task_id=task_id,
        session_id='web:demo',
        title='retention 测试',
        user_request='x',
        status=status,
        root_node_id='node-root',
        created_at=finished,
        updated_at=finished,
        finished_at=finished if status in ('success', 'failed') else None,
        metadata=metadata,
    )


def _append_live_patch(store: SQLiteTaskStore, task_id: str, marker: int) -> None:
    """写入一条会被外置归档的 task.live.patch 事件（payload 含 marker 防去重）。"""
    store.append_task_event(
        task_id=task_id,
        session_id='web:demo',
        event_type='task.live.patch',
        created_at=_iso(datetime.now(timezone.utc)),
        payload={
            'marker': marker,
            'task_id': task_id,
            'runtime_summary': {'active_node_ids': ['node-1'], 'runnable_node_ids': [], 'waiting_node_ids': []},
            'frame': {
                'node_id': 'node-1',
                'phase': 'running',
                'stage_goal': 'do work',
                'tool_calls': [{'name': 'exec'}, {'name': 'read'}],
                'child_pipelines': [],
            },
        },
    )


def _cutoff(days: float = 14) -> str:
    return _days_ago(days)


def test_expired_terminal_task_pruned_with_slim_fallback(tmp_path) -> None:
    store = _make_store(tmp_path)
    try:
        store.upsert_task(_task('task:old', days_old=20))
        _append_live_patch(store, 'task:old', 1)
        archive_dir = tmp_path / 'event-history' / 'task_old'
        assert any(archive_dir.glob('*.json*')), '外置归档文件应已写入'

        before = store.list_task_events(task_id='task:old', hydrate_external=True)
        assert 'frame' in before[0]['payload'], '清理前可水合全量 payload'

        result = store.prune_event_history_archives(_cutoff())

        assert result['task_count'] == 1
        assert result['deleted_dirs'] == 1
        assert result['deleted_bytes'] > 0
        assert result['cleared_refs'] == 1
        assert not archive_dir.exists(), '归档目录必须被删除'

        after = store.list_task_events(task_id='task:old', hydrate_external=True)
        assert len(after) == 1, 'task_events 行保留（审计条目不丢）'
        payload = after[0]['payload']
        assert 'frame' not in payload, '全量 payload 已不可水合'
        assert payload['frame_preview']['node_id'] == 'node-1', 'slim 预览降级可查'
        assert payload['frame_preview']['tool_call_count'] == 2
    finally:
        store.close()


def test_pinned_task_exempt(tmp_path) -> None:
    store = _make_store(tmp_path)
    try:
        store.upsert_task(_task('task:pinned', days_old=30, pinned=True))
        _append_live_patch(store, 'task:pinned', 1)
        archive_dir = tmp_path / 'event-history' / 'task_pinned'

        result = store.prune_event_history_archives(_cutoff())

        assert result['task_count'] == 0
        assert archive_dir.exists(), 'pinned 任务归档必须豁免'
        events = store.list_task_events(task_id='task:pinned', hydrate_external=True)
        assert 'frame' in events[0]['payload']
    finally:
        store.close()


def test_archived_task_exempt(tmp_path) -> None:
    store = _make_store(tmp_path)
    try:
        store.upsert_task(_task('task:archived', days_old=30, archived=True))
        _append_live_patch(store, 'task:archived', 1)
        archive_dir = tmp_path / 'event-history' / 'task_archived'

        result = store.prune_event_history_archives(_cutoff())

        assert result['task_count'] == 0
        assert archive_dir.exists(), '压缩归档任务解压回看需要水合，必须豁免'
    finally:
        store.close()


def test_recent_and_in_progress_tasks_untouched(tmp_path) -> None:
    store = _make_store(tmp_path)
    try:
        store.upsert_task(_task('task:recent', status='failed', days_old=5))
        store.upsert_task(_task('task:running', status='in_progress', days_old=30))
        _append_live_patch(store, 'task:recent', 1)
        _append_live_patch(store, 'task:running', 2)

        result = store.prune_event_history_archives(_cutoff())

        assert result['task_count'] == 0
        assert (tmp_path / 'event-history' / 'task_recent').exists(), '未超期不清'
        assert (tmp_path / 'event-history' / 'task_running').exists(), '进行中任务绝不清'
    finally:
        store.close()


def test_orphan_dirs_swept_with_grace_window(tmp_path) -> None:
    store = _make_store(tmp_path)
    try:
        store.upsert_task(_task('task:alive', days_old=1))
        history_root = tmp_path / 'event-history'

        stale_orphan = history_root / 'task_ghost'
        stale_orphan.mkdir(parents=True, exist_ok=True)
        (stale_orphan / '1.json.gz').write_bytes(b'x')
        eight_days_ago = time.time() - 8 * 24 * 3600
        os.utime(stale_orphan, (eight_days_ago, eight_days_ago))

        fresh_orphan = history_root / 'task_justborn'
        fresh_orphan.mkdir(parents=True, exist_ok=True)
        (fresh_orphan / '1.json.gz').write_bytes(b'x')

        global_dir = history_root / 'global'
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / '1.json.gz').write_bytes(b'x')

        result = store.prune_event_history_archives(_cutoff())

        assert result['orphan_dirs'] == 1
        assert not stale_orphan.exists(), 'tasks 表已无行且超宽限期的孤儿目录必须清扫'
        assert fresh_orphan.exists(), '宽限期内目录不扫（保护未落库新任务）'
        assert global_dir.exists(), 'global 桶永不按孤儿清扫'
    finally:
        store.close()


# ----------------------------------------------------------------------
# runtime 层接线：_run_event_history_retention_if_due 走策略开关、紧急水位
# 跳过与 23h 跨进程卡权（claim_maintenance_run）。
# ----------------------------------------------------------------------


@pytest.fixture()
def policies_guard():
    previous = disk_guard.disk_policies()
    yield
    configure_disk_policies(previous)
    disk_guard.invalidate_disk_usage_cache()


class _DummyChatBackend:
    async def chat(self, **kwargs):
        return SimpleNamespace(content='', tool_calls=[], finish_reason='stop', usage={})


def _make_web_service(tmp_path):
    from main.service.runtime_service import MainRuntimeService

    return MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )


async def test_runtime_retention_wiring_and_claim_gate(tmp_path, policies_guard) -> None:
    service = _make_web_service(tmp_path)
    # 必须在服务构造后注入：MainRuntimeService.__init__ 会用 config 默认值
    # 覆盖进程级策略单例（configure_disk_policies）。
    configure_disk_policies(DiskPolicies(
        event_history_retention_days=14,
        emergency_min_bytes=1,
        emergency_min_ratio=0.0,
    ))
    store = service.store
    store.upsert_task(_task('task:wire', days_old=20))
    _append_live_patch(store, 'task:wire', 1)
    archive_dir = service.store._event_history_dir / 'task_wire'
    assert archive_dir.exists()

    await service._run_event_history_retention_if_due()
    assert not archive_dir.exists(), '接线后端到端清理生效'

    # 23h 卡权：claim 已记账，紧随其后的第二次调用不得再清理
    _append_live_patch(store, 'task:wire', 2)
    assert archive_dir.exists()
    await service._run_event_history_retention_if_due()
    assert archive_dir.exists(), '卡权窗口内第二次调用必须跳过'


async def test_runtime_retention_disabled_by_zero(tmp_path, policies_guard) -> None:
    service = _make_web_service(tmp_path)
    configure_disk_policies(DiskPolicies(
        event_history_retention_days=0,
        emergency_min_bytes=1,
        emergency_min_ratio=0.0,
    ))
    store = service.store
    store.upsert_task(_task('task:off', days_old=20))
    _append_live_patch(store, 'task:off', 1)
    archive_dir = service.store._event_history_dir / 'task_off'

    await service._run_event_history_retention_if_due()
    assert archive_dir.exists(), 'retention_days=0 必须完全关闭清理'
