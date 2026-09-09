"""P1 磁盘治理单测：水位监控、紧急硬闸、排队中止（防死锁）、增量记账。"""

from __future__ import annotations

import asyncio

import pytest

from main.errors import TaskPausedError
from main.runtime.adaptive_tool_budget import AdaptiveToolBudgetController
from main.runtime.tool_pressure_monitor import WorkerPressureMonitor
from main.storage import disk_guard
from main.storage.artifact_store import TaskArtifactStore
from main.storage.disk_guard import (
    DiskPolicies,
    cleanup_threshold_bytes,
    configure_disk_policies,
    disk_waterline_snapshot,
    emergency_threshold_bytes,
)
from main.storage.sqlite_store import SQLiteTaskStore

GB = 1024 ** 3


@pytest.fixture()
def policies_guard():
    previous = disk_guard.disk_policies()
    yield
    configure_disk_policies(previous)
    disk_guard.invalidate_disk_usage_cache()


def _make_monitor(controller, **overrides) -> WorkerPressureMonitor:
    store = type('DummyStore', (), {
        'runtime_metrics_snapshot': staticmethod(lambda: {}),
        'writer_queue_depth': staticmethod(lambda: 0),
    })()
    kwargs = dict(controller=controller, store=store, sample_seconds=1.0)
    kwargs.update(overrides)
    return WorkerPressureMonitor(**kwargs)


def _observe(monitor, *, free: int | None, total: int | None, **overrides):
    kwargs = dict(
        machine_cpu_percent=10.0,
        machine_memory_percent=20.0,
        machine_disk_busy_percent=5.0,
        machine_available=True,
        disk_busy_available=True,
        event_loop_lag_ms=1.0,
        writer_queue_depth=0,
        sqlite_write_wait_ms=1.0,
        sqlite_query_latency_ms=1.0,
        process_cpu_ratio=0.05,
        machine_disk_free_bytes=free,
        machine_disk_total_bytes=total,
    )
    kwargs.update(overrides)
    return monitor.observe_sample(**kwargs)


# --- disk_guard 纯函数 ---


def test_threshold_pure_functions(policies_guard):
    configure_disk_policies(DiskPolicies())
    # 100GB 盘：紧急线 = max(300MB, 1GB) = 1GB；清理线 = max(1GB, 5GB) = 5GB
    assert emergency_threshold_bytes(100 * GB) == 1 * GB
    assert cleanup_threshold_bytes(100 * GB) == 5 * GB
    # 10GB 小盘：紧急线 = max(300MB, 100MB) = 300MB；清理线 = max(1GB, 500MB) = 1GB
    assert emergency_threshold_bytes(10 * GB) == 300 * 1024 * 1024
    assert cleanup_threshold_bytes(10 * GB) == 1 * GB


def test_disk_waterline_snapshot_cwd():
    snapshot = disk_waterline_snapshot(('.',))
    assert snapshot is not None
    free, total = snapshot
    assert total > 0 and 0 <= free <= total
    assert disk_waterline_snapshot(()) is None


# --- controller 紧急硬闸 ---


async def test_controller_emergency_queues_and_survives_release():
    controller = AdaptiveToolBudgetController(normal_limit=2)
    controller.set_disk_emergency(True)
    snap = controller.snapshot()
    assert snap['disk_emergency_active'] is True
    assert snap['tool_pressure_target_limit'] == 0

    # 紧急态下 acquire 排队不 resolve
    acquire_task = asyncio.create_task(controller.acquire_tool_slot(
        task_id='task:a', node_id='node:1', tool_name='shell', tool_call_id='c1',
    ))
    await asyncio.sleep(0.05)
    assert not acquire_task.done()

    # critical()/set_budget_state 不得抬起 limit（防御性钳制）
    controller.critical()
    assert controller.snapshot()['tool_pressure_target_limit'] == 0
    controller.set_budget_state('normal')
    assert controller.snapshot()['tool_pressure_target_limit'] == 0

    # 解除：按压力态恢复并 drain 等待者
    controller.set_disk_emergency(False)
    lease = await asyncio.wait_for(acquire_task, timeout=2.0)
    assert lease.task_id == 'task:a'
    assert controller.snapshot()['disk_emergency_active'] is False
    # 清理：release 后 idle reset 正常（无紧急态时）
    controller.release_tool_slot(lease)
    assert controller.snapshot()['tool_pressure_target_limit'] >= 1


async def test_controller_release_does_not_reset_during_emergency():
    controller = AdaptiveToolBudgetController(normal_limit=3)
    lease = await controller.acquire_tool_slot(
        task_id='task:a', node_id='node:1', tool_name='t', tool_call_id='c',
    )
    controller.set_disk_emergency(True)
    controller.release_tool_slot(lease)  # running=0 且队列空 → 不得冲掉紧急态
    snap = controller.snapshot()
    assert snap['disk_emergency_active'] is True
    assert snap['tool_pressure_target_limit'] == 0


async def test_abort_task_waiters_only_target_task():
    controller = AdaptiveToolBudgetController(normal_limit=1)
    controller.set_disk_emergency(True)
    waiter_a = asyncio.create_task(controller.acquire_tool_slot(
        task_id='task:a', node_id='n1', tool_name='t', tool_call_id='ca',
    ))
    waiter_b = asyncio.create_task(controller.acquire_tool_slot(
        task_id='task:b', node_id='n2', tool_name='t', tool_call_id='cb',
    ))
    await asyncio.sleep(0.05)
    aborted = controller.abort_task_waiters('task:a', TaskPausedError('task:a'))
    assert aborted == 1
    with pytest.raises(TaskPausedError):
        await asyncio.wait_for(waiter_a, timeout=2.0)
    assert not waiter_b.done()  # 其它任务的排队不受影响
    # 解除紧急态后 b 正常拿到槽
    controller.set_disk_emergency(False)
    lease_b = await asyncio.wait_for(waiter_b, timeout=2.0)
    assert lease_b.task_id == 'task:b'
    controller.release_tool_slot(lease_b)


# --- monitor 水位决策 ---


async def test_monitor_emergency_enter_exit_with_hooks(policies_guard):
    configure_disk_policies(DiskPolicies(
        emergency_min_bytes=2 * GB, emergency_min_ratio=0.0,
        cleanup_min_bytes=4 * GB, cleanup_min_ratio=0.0,
        emergency_streak_samples=3, emergency_recovery_samples=5,
    ))
    controller = AdaptiveToolBudgetController(normal_limit=2)
    events: list[str] = []
    monitor = _make_monitor(controller)
    monitor.set_disk_emergency_hooks(
        enter=lambda: events.append('enter'),
        exit_=lambda: events.append('exit'),
        cleanup=lambda: events.append('cleanup'),
    )
    total = 100 * GB
    # 清理线之下、紧急线之上：第 1 拍触发 cleanup 边沿 + throttle
    _observe(monitor, free=3 * GB, total=total)
    assert events == ['cleanup']
    assert monitor.snapshot()['disk_cleanup_active'] is True
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'
    # 跌破紧急线：连续 3 拍进入
    _observe(monitor, free=1 * GB, total=total)
    _observe(monitor, free=1 * GB, total=total)
    assert 'enter' not in events
    _observe(monitor, free=1 * GB, total=total)
    assert events.count('enter') == 1
    assert monitor.snapshot()['disk_emergency_active'] is True
    assert controller.snapshot()['disk_emergency_active'] is True
    assert controller.snapshot()['tool_pressure_target_limit'] == 0
    # 紧急态期间机器 critical 样本不得抬起 limit（决策链冻结）
    _observe(monitor, free=1 * GB, total=total, machine_cpu_percent=99.0)
    assert controller.snapshot()['tool_pressure_target_limit'] == 0
    # 恢复：连续 5 拍高于紧急线才解除（第 4 拍仍紧急）
    for _ in range(4):
        _observe(monitor, free=3 * GB, total=total)
    assert monitor.snapshot()['disk_emergency_active'] is True
    _observe(monitor, free=3 * GB, total=total)
    assert events.count('exit') == 1
    assert monitor.snapshot()['disk_emergency_active'] is False
    assert controller.snapshot()['disk_emergency_active'] is False
    # 防抖：单拍抖动不触发（streak 清零）
    _observe(monitor, free=1 * GB, total=total)
    _observe(monitor, free=3 * GB, total=total)
    assert events.count('enter') == 1


async def test_monitor_starvation_valve_cannot_break_emergency(policies_guard):
    configure_disk_policies(DiskPolicies(
        emergency_min_bytes=2 * GB, emergency_min_ratio=0.0,
        cleanup_min_bytes=4 * GB, cleanup_min_ratio=0.0,
        emergency_streak_samples=1, emergency_recovery_samples=1,
    ))
    controller = AdaptiveToolBudgetController(normal_limit=2)
    monitor = _make_monitor(controller, max_tool_wait_ms=1.0, max_pressure_dwell_seconds=0.001)
    total = 100 * GB
    # 先落到清理线：pressure_state=throttled（restricted），逃逸阀条件就位
    _observe(monitor, free=3 * GB, total=total)
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'
    # 跌破紧急线：streak=1 立即进入硬闸（target 0）
    _observe(monitor, free=1 * GB, total=total)
    assert controller.snapshot()['disk_emergency_active'] is True
    assert controller.snapshot()['tool_pressure_target_limit'] == 0
    waiter = asyncio.create_task(controller.acquire_tool_slot(
        task_id='task:a', node_id='n', tool_name='t', tool_call_id='c',
    ))
    await asyncio.sleep(0.05)
    # 排队等待已超时（oldest_wait_ms > max_tool_wait_ms）且 dwell 超时——
    # starvation/dwell 逃逸阀在紧急态必须被决策链冻结短路：limit 恒 0，waiter 不放行
    for _ in range(5):
        _observe(monitor, free=1 * GB, total=total)
        await asyncio.sleep(0.01)
    assert controller.snapshot()['tool_pressure_target_limit'] == 0
    assert not waiter.done()
    controller.abort_task_waiters('task:a', TaskPausedError('task:a'))
    with pytest.raises(TaskPausedError):
        await waiter


def test_monitor_waterline_fields_empty_paths():
    controller = AdaptiveToolBudgetController()
    monitor = _make_monitor(controller)
    fields = monitor._disk_waterline_fields()
    assert fields == {'disk_free_bytes': None, 'disk_total_bytes': None}
    monitor.set_disk_watermark_paths(['.'])
    fields = monitor._disk_waterline_fields()
    assert isinstance(fields.get('disk_free_bytes'), int) and fields['disk_free_bytes'] >= 0


# --- 增量记账 ---


def test_task_disk_usage_bump_upsert_get(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.bump_task_disk_usage('task:t1', 500)
        store.bump_task_disk_usage('task:t1', 700)
        assert store.get_task_disk_usages(['task:t1']) == {'task:t1': 1200}
        # 负 delta 下限 0
        store.bump_task_disk_usage('task:t1', -5000)
        assert store.get_task_disk_usages(['task:t1']) == {'task:t1': 0}
        # 对账：绝对值覆盖
        store.upsert_task_disk_usage('task:t1', 4242)
        assert store.get_task_disk_usages() == {'task:t1': 4242}
        # 空 task_id / 0 delta 幂等无害
        store.bump_task_disk_usage('', 100)
        store.bump_task_disk_usage('task:t1', 0)
        assert store.get_task_disk_usages()['task:t1'] == 4242
    finally:
        store.close()


def test_artifact_creation_bumps_task_disk_usage(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies(artifact_gzip_threshold_bytes=10_000))
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        record = artifacts.create_text_artifact(
            task_id='task:t1', node_id=None, kind='node_output', title='a', content='x' * 100,
        )
        usage = store.get_task_disk_usages(['task:t1'])
        assert usage['task:t1'] == record.size_bytes == 100
        # singleton 覆盖写按差值记账
        artifacts.create_or_replace_singleton_text_artifact(
            task_id='task:t1', node_id='n', kind='task_runtime_messages', title='f', content='y' * 30,
        )
        assert store.get_task_disk_usages(['task:t1'])['task:t1'] == 130
        artifacts.create_or_replace_singleton_text_artifact(
            task_id='task:t1', node_id='n', kind='task_runtime_messages', title='f', content='y' * 10,
        )
        assert store.get_task_disk_usages(['task:t1'])['task:t1'] == 110
    finally:
        store.close()


def test_event_archive_bumps_task_disk_usage(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3', event_history_enabled=True)
    try:
        seq = store.append_task_event(
            task_id='task:t1', session_id='web:shared', event_type='task.live.patch',
            created_at='2026-09-09T00:00:00+08:00', payload={'big': 'z' * 5000},
        )
        assert seq > 0
        usage = store.get_task_disk_usages(['task:t1'])
        assert usage.get('task:t1', 0) > 0  # gz 归档字节已入账
    finally:
        store.close()


# --- runtime_service 轻量单元（不实例化完整服务）---


def test_abort_queued_waits_helper_degrades_gracefully():
    from types import MethodType, SimpleNamespace

    from main.service.runtime_service import MainRuntimeService

    calls: list[tuple] = []

    class _Controller:
        def abort_task_waiters(self, task_id, exc):
            calls.append((task_id, type(exc).__name__))
            return 1

    harness = SimpleNamespace(adaptive_tool_budget_controller=_Controller())
    abort = MethodType(MainRuntimeService._abort_queued_waits, harness)
    abort('task:a')
    assert calls == [('task:a', 'TaskPausedError')]
    # controller 缺失/无方法时静默
    harness2 = SimpleNamespace(adaptive_tool_budget_controller=None)
    MethodType(MainRuntimeService._abort_queued_waits, harness2)('task:a')
