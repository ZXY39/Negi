"""磁盘治理改造单测：自动删除移除后的契约。

覆盖：终态清理删「确定不再使用的数据」（event-history 快照）但绝不删任务；
对账口径含 DB 明细字节；模型工具按记账表取大小并支持按大小排序；
清理线/全删渐进相关 API 已从服务与策略上整体移除。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from main.models import TaskRecord
from main.monitoring.log_service import TaskLogService
from main.service.runtime_service import MainRuntimeService
from main.storage.disk_guard import DiskPolicies


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


def _task(task_id: str, *, status: str = 'in_progress', created_at: str = '2026-09-16T10:00:00+08:00') -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="web:demo",
        title=f'测试任务 {task_id}',
        user_request="请帮我做一件事",
        status=status,
        root_node_id="node-root",
        created_at=created_at,
        updated_at=created_at,
        finished_at=created_at if status in {'success', 'failed'} else '',
    )


# --- 需求1：清理线/全删渐进整体移除（回归守卫） ---


def test_progressive_wipe_apis_removed():
    for name in ('_wipe_progressive_sweep', '_query_wipe_candidates', '_disk_cleanup_sweep_loop', '_disk_cleanup_due', '_disk_cleanup_signal'):
        assert not hasattr(MainRuntimeService, name), name
    assert not hasattr(MainRuntimeService, '_FULL_DELETE_GRACE_HOURS')
    policies = DiskPolicies()
    assert not hasattr(policies, 'purge_enabled')
    assert not hasattr(policies, 'cleanup_min_bytes')
    assert not hasattr(policies, 'cleanup_min_ratio')
    # P3 明细裁剪默认停用（0=关闭），配置 >0 可恢复
    assert policies.detail_retention_days == 0
    # 手动删除核心仍在
    assert hasattr(MainRuntimeService, '_wipe_task_data')


def test_terminal_cleanup_does_not_delete_task(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:terminal-keep'
    service.store.upsert_task(_task(task_id))
    service._run_terminal_intermediate_cleanup(task_id)
    # 任务行仍在：终态清理只清数据、不删任务（全删仅用户/模型工具手动触发）
    assert service.store.get_task(task_id) is not None
    assert service.store.list_task_delete_ledger_rows() == []


# --- 需求1：终态清理删除「确定不再使用的数据」 ---


def test_terminal_cleanup_removes_event_history_keeps_final_artifacts(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:terminal-clean'
    service.store.upsert_task(_task(task_id, status='success'))
    # live.patch 单份快照（终态后无读者）
    assert service.store.write_task_live_snapshot(task_id, '{"frame": {"x": 1}}')
    history_dir = service.store._event_history_dir / task_id.replace(':', '_')
    assert history_dir.exists()
    # 保留清单产物 + 中间产物
    kept = service.artifact_store.create_text_artifact(
        task_id=task_id, node_id='node-root', kind='final_output', title='最终报告', content='result',
    )
    dropped = service.artifact_store.create_text_artifact(
        task_id=task_id, node_id='node-root', kind='node_output', title='中间输出', content='x' * 64,
    )
    assert kept is not None and dropped is not None

    service._run_terminal_intermediate_cleanup(task_id)

    assert not history_dir.exists()  # event-history 目录终态即删
    remaining_ids = {item.artifact_id for item in service.list_artifacts(task_id)}
    assert kept.artifact_id in remaining_ids
    assert dropped.artifact_id not in remaining_ids
    # 对账已执行（终态值 = 清理后目录实测 + DB 明细字节）
    usage = service.store.get_task_disk_usages([task_id])
    assert task_id in usage and usage[task_id] >= 0


def test_terminal_temp_dir_default_kept(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:terminal-temp'
    service.store.upsert_task(_task(task_id, status='failed'))
    temp_dir = service._task_temp_dir(task_id, create=True)
    (temp_dir / 'scratch.txt').write_text('x', encoding='utf-8')
    service._run_terminal_intermediate_cleanup(task_id)
    # 默认保留任务临时目录（防误写产物丢失），仅显式开关才硬删
    assert temp_dir.exists()


# --- 需求2：对账口径 = 目录实测 + DB 明细字节 ---


def test_reconcile_includes_detail_bytes(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:reconcile-db'
    service.store.upsert_task(_task(task_id))
    payload = '{"big": "%s"}' % ('z' * 5000)
    service.store._execute_write(
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        (task_id, 'node-root', '2026-09-16T10:00:00+08:00', payload),
    )
    service._reconcile_task_disk_usage(task_id)
    expected_db = service.store.sum_task_detail_bytes([task_id])[task_id]
    assert expected_db >= 5000
    usage = service.store.get_task_disk_usages([task_id])[task_id]
    # 目录为空 → 记账 == DB 明细字节（含 payload）
    assert usage == expected_db


def test_reconcile_falls_back_when_sum_fails(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:reconcile-fallback'
    service.store.upsert_task(_task(task_id))

    def _boom(_task_ids):
        raise RuntimeError('simulated')

    service.store.sum_task_detail_bytes = _boom  # type: ignore[method-assign]
    # DB 字节不可得时回落纯目录值，绝不抛
    service._reconcile_task_disk_usage(task_id)
    assert task_id in service.store.get_task_disk_usages(None)


def test_store_point_query_disk_usage(tmp_path) -> None:
    from main.storage.sqlite_store import SQLiteTaskStore

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        assert store.get_task_disk_usage('task:missing') == 0
        store.upsert_task_disk_usage('task:t1', 777)
        assert store.get_task_disk_usage('task:t1') == 777
        assert store.get_task_disk_usage('') == 0
    finally:
        store.close()


# --- 需求2：WS summary payload 携带大小字段 ---


def test_summary_payload_optional_disk_usage() -> None:
    task = _task('task:payload', status='success')
    payload_without = TaskLogService._task_summary_payload(task)
    assert 'disk_usage_bytes' not in payload_without  # 取不到时省略（不清空前端旧值）
    payload_with = TaskLogService._task_summary_payload(task, disk_usage_bytes=1234)
    assert payload_with['disk_usage_bytes'] == 1234


# --- 需求2/3：模型工具读记账表 + 按大小排序 ---


def test_task_stats_list_sort_by_size(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    # 三个任务：新小 / 旧大 / 旧小
    service.store.upsert_task(_task('task:new-small', created_at='2026-09-16T12:00:00+08:00'))
    service.store.upsert_task(_task('task:old-big', created_at='2026-09-10T09:00:00+08:00'))
    service.store.upsert_task(_task('task:old-small', created_at='2026-09-09T09:00:00+08:00'))
    service.store.upsert_task_disk_usage('task:new-small', 100)
    service.store.upsert_task_disk_usage('task:old-big', 999_999)
    service.store.upsert_task_disk_usage('task:old-small', 500)

    by_size = service.task_stats(mode='list', date_from='2026/9/1', date_to='2026/9/30', sort='size')
    assert by_size['sort'] == 'size'
    ids = [item['task_id'] for item in by_size['items']]
    assert ids == ['task:old-big', 'task:old-small', 'task:new-small']
    assert by_size['items'][0]['disk_usage_bytes'] == 999_999

    by_time = service.task_stats(mode='list', date_from='2026/9/1', date_to='2026/9/30')
    assert by_time['sort'] == 'time'
    assert [item['task_id'] for item in by_time['items']] == ['task:new-small', 'task:old-big', 'task:old-small']


def test_task_stats_fallback_measure_when_no_usage_row(tmp_path) -> None:
    service = _make_web_service(tmp_path)
    task_id = 'task:no-usage-row'
    service.store.upsert_task(_task(task_id))
    # temp 目录放点数据但无记账行 → 工具兜底一次目录实测
    temp_dir = service._task_temp_dir(task_id, create=True)
    (temp_dir / 'blob.bin').write_bytes(b'x' * 2048)
    result = service.task_stats(mode='list', date_from='2026/9/1', date_to='2026/9/30', sort='size')
    items = {item['task_id']: item for item in result['items']}
    assert items[task_id]['disk_usage_bytes'] >= 2048
