"""全删渐进（wipe progressive sweep）单测：宽限窗口、size×age 排序、闸门、开关。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from main.models import TaskRecord
from main.service.runtime_service import MainRuntimeService
from main.storage import disk_guard
from main.storage.disk_guard import DiskPolicies, configure_disk_policies

import pytest


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


def _task(task_id: str, *, status: str = 'success', days_ago: float = 3.0) -> TaskRecord:
    iso = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec='seconds')
    return TaskRecord(
        task_id=task_id,
        session_id='web:demo',
        title='sweep 测试',
        user_request='x',
        status=status,
        root_node_id='node-root',
        created_at=iso,
        updated_at=iso,
        finished_at=iso,
    )


def _seed_detail_bytes(service: MainRuntimeService, task_id: str, size: int) -> None:
    service.store._execute_write(
        'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
        (task_id, 'node:root', '2026-09-01T00:00:00+08:00', 'z' * size),
    )


@pytest.fixture()
def policies_guard():
    previous = disk_guard.disk_policies()
    yield
    configure_disk_policies(previous)
    disk_guard.invalidate_disk_usage_cache()


async def test_sweep_respects_grace_window_and_wipes_oldest(tmp_path, policies_guard) -> None:
    service = _make_web_service(tmp_path)
    configure_disk_policies(DiskPolicies())
    service.store.upsert_task(_task('task:old', days_ago=3))
    service.store.upsert_task(_task('task:fresh', days_ago=0.01))  # 终态仅 ~14 分钟：宽限内
    service.store.upsert_task(_task('task:running', status='in_progress', days_ago=5))
    _seed_detail_bytes(service, 'task:old', 1000)

    service._disk_cleanup_requested = True  # 触发清理线条件
    wiped = await service._wipe_progressive_sweep()

    assert wiped == 1
    assert service.store.get_task('task:old') is None
    assert service.store.is_task_deleted('task:old')
    assert service.store.get_task('task:fresh') is not None      # 24h 宽限保护
    assert service.store.get_task('task:running') is not None    # 非终态不进候选


async def test_sweep_orders_by_size_times_age(tmp_path, policies_guard) -> None:
    service = _make_web_service(tmp_path)
    configure_disk_policies(DiskPolicies())
    # small 更老，但 big 的 DB 明细远大：size×age 加权应优先 big
    service.store.upsert_task(_task('task:small', days_ago=10))
    service.store.upsert_task(_task('task:big', days_ago=4))
    _seed_detail_bytes(service, 'task:small', 100)
    _seed_detail_bytes(service, 'task:big', 50_000_000)

    service._disk_cleanup_requested = True
    # 排序断言：size×age 加权下 big 优先（尽管 small 更老）
    assert service._query_wipe_candidates(batch=1) == ['task:big']
    wiped = await service._wipe_progressive_sweep(batch=1)

    assert wiped >= 1
    assert service.store.get_task('task:big') is None


async def test_sweep_disabled_by_purge_flag(tmp_path, policies_guard) -> None:
    service = _make_web_service(tmp_path)
    configure_disk_policies(DiskPolicies(purge_enabled=False))
    service.store.upsert_task(_task('task:old', days_ago=3))
    service._disk_cleanup_requested = True
    wiped = await service._wipe_progressive_sweep()
    assert wiped == 0
    assert service.store.get_task('task:old') is not None


async def test_sweep_skips_ledger_tasks(tmp_path, policies_guard) -> None:
    service = _make_web_service(tmp_path)
    configure_disk_policies(DiskPolicies())
    service.store.upsert_task(_task('task:old', days_ago=3))
    service.store.record_task_delete('task:old', reason='user_delete')  # 台账已有（守卫中）
    service._disk_cleanup_requested = True
    candidates = service._query_wipe_candidates(batch=5)
    assert 'task:old' not in candidates
