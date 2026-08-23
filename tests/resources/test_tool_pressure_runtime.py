from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from main.runtime.adaptive_tool_budget import AdaptiveToolBudgetController
from main.runtime.tool_pressure_monitor import WorkerPressureMonitor
from main.service.runtime_service import MainRuntimeService


class _FakeStore:
    def __init__(self) -> None:
        self.depth = 0

    def writer_queue_depth(self) -> int:
        return int(self.depth)


@pytest.mark.asyncio
async def test_adaptive_tool_budget_controller_releases_waiters_in_fifo_order() -> None:
    controller = AdaptiveToolBudgetController(normal_limit=1, safe_limit=1, step_up=1)
    first = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:one',
        tool_name='filesystem',
        tool_call_id='call:1',
    )
    second_task = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:one',
            tool_name='filesystem',
            tool_call_id='call:2',
        )
    )
    third_task = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:one',
            tool_name='filesystem',
            tool_call_id='call:3',
        )
    )
    await asyncio.sleep(0)
    assert controller.snapshot()['tool_pressure_waiting_count'] == 2

    controller.release_tool_slot(first)
    second = await asyncio.wait_for(second_task, timeout=1.0)
    assert second.tool_call_id == 'call:2'
    assert controller.snapshot()['tool_pressure_waiting_count'] == 1

    controller.release_tool_slot(second)
    third = await asyncio.wait_for(third_task, timeout=1.0)
    assert third.tool_call_id == 'call:3'
    controller.release_tool_slot(third)
    assert controller.snapshot()['tool_pressure_running_count'] == 0


@pytest.mark.asyncio
async def test_adaptive_tool_budget_controller_does_not_preempt_running_tools_when_throttled() -> None:
    controller = AdaptiveToolBudgetController(normal_limit=2, safe_limit=1, step_up=1)
    first = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:a',
        tool_name='filesystem',
        tool_call_id='call:a',
    )
    controller.set_budget_state('normal', at='2026-03-30T00:00:00+08:00', target_limit=2)
    second = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:b',
        tool_name='filesystem',
        tool_call_id='call:b',
    )
    controller.throttle(at='2026-03-30T00:00:00+08:00')
    queued = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:c',
            tool_name='filesystem',
            tool_call_id='call:c',
        )
    )
    await asyncio.sleep(0)
    snapshot = controller.snapshot()
    assert snapshot['tool_pressure_state'] == 'throttled'
    assert snapshot['tool_pressure_target_limit'] == 2
    assert snapshot['tool_pressure_running_count'] == 2
    assert snapshot['tool_pressure_waiting_count'] == 1

    controller.release_tool_slot(first)
    acquired = await asyncio.wait_for(queued, timeout=1.0)
    assert acquired.tool_call_id == 'call:c'
    controller.release_tool_slot(second)
    controller.release_tool_slot(acquired)
    assert controller.snapshot()['tool_pressure_state'] == 'normal'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1


@pytest.mark.asyncio
async def test_worker_pressure_monitor_eases_backlog_without_fixed_ceiling() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=2, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        sample_seconds=1.0,
        recover_window_seconds=1.0,
        warn_consecutive_samples=3,
        safe_consecutive_samples=3,
        pressure_snapshot_stale_after_seconds=3.0,
        event_loop_warn_ms=250.0,
        event_loop_safe_ms=100.0,
        event_loop_critical_ms=1500.0,
        writer_queue_warn=50,
        writer_queue_safe=10,
        writer_queue_critical=100,
        sqlite_write_wait_warn_ms=200.0,
        sqlite_write_wait_safe_ms=50.0,
        sqlite_write_wait_critical_ms=250.0,
        sqlite_query_warn_ms=150.0,
        sqlite_query_safe_ms=30.0,
        sqlite_query_critical_ms=250.0,
        machine_cpu_warn_percent=85.0,
        machine_cpu_safe_percent=55.0,
        machine_cpu_critical_percent=95.0,
        machine_memory_warn_percent=88.0,
        machine_memory_safe_percent=75.0,
        machine_memory_critical_percent=94.0,
        machine_disk_busy_warn_percent=70.0,
        machine_disk_busy_safe_percent=35.0,
        machine_disk_busy_critical_percent=90.0,
        process_cpu_warn_ratio=0.85,
        process_cpu_safe_ratio=0.50,
    )
    first = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:a',
        tool_name='filesystem',
        tool_call_id='call:a',
    )
    second_task = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:b',
            tool_name='filesystem',
            tool_call_id='call:b',
        )
    )
    third_task = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:c',
            tool_name='filesystem',
            tool_call_id='call:c',
        )
    )
    await asyncio.sleep(0)
    assert controller.snapshot()['tool_pressure_waiting_count'] == 2

    for index in range(3):
        monitor.observe_sample(
            machine_cpu_percent=91.0,
            machine_memory_percent=40.0,
            machine_disk_busy_percent=20.0,
            machine_available=True,
            event_loop_lag_ms=300.0,
            writer_queue_depth=0,
            sqlite_write_wait_ms=0.0,
            sqlite_query_latency_ms=0.0,
            process_cpu_ratio=0.10,
            now_mono=float(index),
            now_iso=f'2026-03-30T00:00:0{index}+08:00',
        )
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1

    for index in range(3, 6):
        monitor.observe_sample(
            machine_cpu_percent=20.0,
            machine_memory_percent=30.0,
            machine_disk_busy_percent=10.0,
            machine_available=True,
            event_loop_lag_ms=10.0,
            writer_queue_depth=0,
            sqlite_write_wait_ms=0.0,
            sqlite_query_latency_ms=0.0,
            process_cpu_ratio=0.10,
            now_mono=float(index),
            now_iso=f'2026-03-30T00:00:0{index}+08:00',
        )
    assert controller.snapshot()['tool_pressure_state'] == 'easing'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1

    monitor.observe_sample(
        machine_cpu_percent=20.0,
        machine_memory_percent=30.0,
        machine_disk_busy_percent=10.0,
        machine_available=True,
        event_loop_lag_ms=10.0,
        writer_queue_depth=0,
        sqlite_write_wait_ms=0.0,
        sqlite_query_latency_ms=0.0,
        process_cpu_ratio=0.10,
        now_mono=6.0,
        now_iso='2026-03-30T00:00:06+08:00',
    )
    second = await asyncio.wait_for(second_task, timeout=1.0)
    assert second.tool_call_id == 'call:b'
    assert controller.snapshot()['tool_pressure_target_limit'] == 2
    assert controller.snapshot()['tool_pressure_state'] == 'easing'

    monitor.observe_sample(
        machine_cpu_percent=20.0,
        machine_memory_percent=30.0,
        machine_disk_busy_percent=10.0,
        machine_available=True,
        event_loop_lag_ms=10.0,
        writer_queue_depth=0,
        sqlite_write_wait_ms=0.0,
        sqlite_query_latency_ms=0.0,
        process_cpu_ratio=0.10,
        now_mono=7.0,
        now_iso='2026-03-30T00:00:07+08:00',
    )
    third = await asyncio.wait_for(third_task, timeout=1.0)
    assert third.tool_call_id == 'call:c'
    assert controller.snapshot()['tool_pressure_state'] == 'easing'
    assert controller.snapshot()['tool_pressure_target_limit'] == 3

    controller.release_tool_slot(first)
    controller.release_tool_slot(second)
    controller.release_tool_slot(third)
    assert controller.snapshot()['tool_pressure_state'] == 'normal'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1


def test_worker_pressure_monitor_enters_critical_immediately_on_single_machine_critical_sample() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        sample_seconds=1.0,
        recover_window_seconds=1.0,
        warn_consecutive_samples=3,
        safe_consecutive_samples=3,
        pressure_snapshot_stale_after_seconds=3.0,
        event_loop_warn_ms=250.0,
        event_loop_safe_ms=100.0,
        event_loop_critical_ms=1500.0,
        writer_queue_warn=50,
        writer_queue_safe=10,
        writer_queue_critical=100,
        sqlite_write_wait_warn_ms=200.0,
        sqlite_write_wait_safe_ms=50.0,
        sqlite_write_wait_critical_ms=250.0,
        sqlite_query_warn_ms=150.0,
        sqlite_query_safe_ms=30.0,
        sqlite_query_critical_ms=250.0,
        machine_cpu_warn_percent=85.0,
        machine_cpu_safe_percent=55.0,
        machine_cpu_critical_percent=95.0,
        machine_memory_warn_percent=88.0,
        machine_memory_safe_percent=75.0,
        machine_memory_critical_percent=94.0,
        machine_disk_busy_warn_percent=70.0,
        machine_disk_busy_safe_percent=35.0,
        machine_disk_busy_critical_percent=90.0,
        process_cpu_warn_ratio=0.85,
        process_cpu_safe_ratio=0.50,
    )

    monitor.observe_sample(
        machine_cpu_percent=97.0,
        machine_memory_percent=30.0,
        machine_disk_busy_percent=10.0,
        machine_available=True,
        event_loop_lag_ms=10.0,
        writer_queue_depth=0,
        sqlite_write_wait_ms=0.0,
        sqlite_query_latency_ms=0.0,
        process_cpu_ratio=0.10,
        now_mono=0.0,
        now_iso='2026-03-30T00:01:00+08:00',
    )

    assert controller.snapshot()['tool_pressure_state'] == 'critical'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1


def test_worker_pressure_monitor_enters_critical_immediately_on_single_local_critical_sample() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        sample_seconds=1.0,
        recover_window_seconds=1.0,
        warn_consecutive_samples=3,
        safe_consecutive_samples=3,
        pressure_snapshot_stale_after_seconds=3.0,
        event_loop_warn_ms=250.0,
        event_loop_safe_ms=100.0,
        event_loop_critical_ms=1500.0,
        writer_queue_warn=50,
        writer_queue_safe=10,
        writer_queue_critical=100,
        sqlite_write_wait_warn_ms=200.0,
        sqlite_write_wait_safe_ms=50.0,
        sqlite_write_wait_critical_ms=250.0,
        sqlite_query_warn_ms=150.0,
        sqlite_query_safe_ms=30.0,
        sqlite_query_critical_ms=250.0,
        machine_cpu_warn_percent=85.0,
        machine_cpu_safe_percent=55.0,
        machine_cpu_critical_percent=95.0,
        machine_memory_warn_percent=88.0,
        machine_memory_safe_percent=75.0,
        machine_memory_critical_percent=94.0,
        machine_disk_busy_warn_percent=70.0,
        machine_disk_busy_safe_percent=35.0,
        machine_disk_busy_critical_percent=90.0,
        process_cpu_warn_ratio=0.85,
        process_cpu_safe_ratio=0.50,
    )

    monitor.observe_sample(
        machine_cpu_percent=20.0,
        machine_memory_percent=30.0,
        machine_disk_busy_percent=10.0,
        machine_available=True,
        event_loop_lag_ms=10.0,
        writer_queue_depth=101,
        sqlite_write_wait_ms=0.0,
        sqlite_query_latency_ms=0.0,
        process_cpu_ratio=0.10,
        now_mono=0.0,
        now_iso='2026-03-30T00:01:01+08:00',
    )

    assert controller.snapshot()['tool_pressure_state'] == 'critical'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1


def test_worker_pressure_monitor_marks_snapshot_unfresh_when_machine_metrics_are_missing() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(controller=controller, store=store)

    for index in range(3):
        monitor.observe_sample(
            machine_cpu_percent=0.0,
            machine_memory_percent=0.0,
            machine_disk_busy_percent=0.0,
            machine_available=False,
            disk_busy_available=False,
            event_loop_lag_ms=0.0,
            writer_queue_depth=0,
            sqlite_write_wait_ms=0.0,
            sqlite_query_latency_ms=0.0,
            process_cpu_ratio=0.0,
            now_mono=float(index),
            now_iso=f'2026-03-30T00:01:0{index}+08:00',
        )

    snapshot = monitor.snapshot()
    assert controller.snapshot()['tool_pressure_state'] == 'normal'
    assert snapshot['pressure_snapshot_fresh'] is False
    assert snapshot['machine_pressure_available'] is False


def test_worker_pressure_monitor_does_not_throttle_on_lag_alone_when_machine_is_healthy() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(controller=controller, store=store)

    for index in range(3):
        monitor.observe_sample(
            machine_cpu_percent=18.0,
            machine_memory_percent=42.0,
            machine_disk_busy_percent=5.0,
            machine_available=True,
            event_loop_lag_ms=900.0,
            writer_queue_depth=0,
            sqlite_write_wait_ms=0.0,
            sqlite_query_latency_ms=0.0,
            process_cpu_ratio=0.1,
            now_mono=float(index),
            now_iso=f'2026-03-30T00:02:0{index}+08:00',
        )

    snapshot = monitor.snapshot()
    assert snapshot['local_pressure_state'] == 'degraded'
    assert controller.snapshot()['tool_pressure_state'] == 'normal'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1


def test_worker_pressure_monitor_falls_back_to_read_write_times_for_disk_busy(monkeypatch) -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=4, safe_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(controller=controller, store=store)
    samples = [
        SimpleNamespace(read_bytes=1_000, write_bytes=2_000, read_time=100, write_time=50),
        SimpleNamespace(read_bytes=4_000, write_bytes=5_000, read_time=140, write_time=90),
    ]

    class _FakePsutil:
        @staticmethod
        def cpu_percent(interval=None):
            return 12.0

        @staticmethod
        def virtual_memory():
            return SimpleNamespace(percent=34.0)

        @staticmethod
        def disk_io_counters():
            return samples.pop(0)

    monkeypatch.setattr("main.runtime.tool_pressure_monitor.psutil", _FakePsutil)

    first = monitor._sample_machine_metrics(1.0)
    second = monitor._sample_machine_metrics(2.0)

    assert first["disk_busy_available"] is False
    assert second["disk_busy_available"] is True
    assert second["disk_busy_percent"] == pytest.approx(4.0)
    assert second["disk_read_bytes_per_sec"] == pytest.approx(3_000.0)
    assert second["disk_write_bytes_per_sec"] == pytest.approx(3_000.0)


# ---------------------------------------------------------------------------
# Self-healing (A: dwell timeout, B: starvation, C: local-driven recovery)
# and turn-gate (D: staleness fail-open, E: close only on local critical).
# ---------------------------------------------------------------------------


def _standard_monitor_kwargs(**overrides):
    kwargs = dict(
        sample_seconds=1.0,
        recover_window_seconds=1.0,
        warn_consecutive_samples=3,
        safe_consecutive_samples=3,
        pressure_snapshot_stale_after_seconds=3.0,
        event_loop_warn_ms=250.0,
        event_loop_safe_ms=100.0,
        event_loop_critical_ms=1500.0,
        writer_queue_warn=50,
        writer_queue_safe=10,
        writer_queue_critical=100,
        sqlite_write_wait_warn_ms=200.0,
        sqlite_write_wait_safe_ms=50.0,
        sqlite_write_wait_critical_ms=250.0,
        sqlite_query_warn_ms=150.0,
        sqlite_query_safe_ms=30.0,
        sqlite_query_critical_ms=250.0,
        machine_cpu_warn_percent=85.0,
        machine_cpu_safe_percent=55.0,
        machine_cpu_critical_percent=95.0,
        machine_memory_warn_percent=88.0,
        machine_memory_safe_percent=75.0,
        machine_memory_critical_percent=94.0,
        machine_disk_busy_warn_percent=70.0,
        machine_disk_busy_safe_percent=35.0,
        machine_disk_busy_critical_percent=90.0,
        process_cpu_warn_ratio=0.85,
        process_cpu_safe_ratio=0.50,
    )
    kwargs.update(overrides)
    return kwargs


def _observe(monitor, *, index, machine_cpu_percent=60.0, machine_available=True, event_loop_lag_ms=10.0, writer_queue_depth=0, process_cpu_ratio=0.10, **overrides):
    """Inject one sample with local metrics in the healthy range by default.

    ``machine_cpu_percent=60.0`` is intentionally between the safe (55) and
    warn (85) thresholds, so the machine state reads 'unknown': neither
    machine_recovery nor machine_warn can fire on it.
    """
    return monitor.observe_sample(
        machine_cpu_percent=machine_cpu_percent,
        machine_memory_percent=40.0,
        machine_disk_busy_percent=20.0,
        machine_available=machine_available,
        event_loop_lag_ms=event_loop_lag_ms,
        writer_queue_depth=writer_queue_depth,
        sqlite_write_wait_ms=0.0,
        sqlite_query_latency_ms=0.0,
        process_cpu_ratio=process_cpu_ratio,
        now_mono=float(index),
        now_iso=f'2026-03-30T00:10:{int(index) % 60:02d}+08:00',
        **overrides,
    )


@pytest.mark.asyncio
async def test_dwell_timeout_forces_easing_when_machine_never_becomes_safe() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        **_standard_monitor_kwargs(
            max_pressure_dwell_seconds=60.0,
            max_tool_wait_ms=0.0,  # isolate mechanism A from B
            local_recovery_enabled=False,  # isolate mechanism A from C
        ),
    )
    first = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:a',
        tool_name='filesystem',
        tool_call_id='call:a',
    )
    queued = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:b',
            tool_name='filesystem',
            tool_call_id='call:b',
        )
    )
    await asyncio.sleep(0)
    assert controller.snapshot()['tool_pressure_waiting_count'] == 1

    for index in range(3):
        _observe(monitor, index=index, machine_cpu_percent=91.0)
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1

    # Neutral CPU keeps machine_state 'unknown' forever; without the dwell
    # valve this throttled state would never exit.
    _observe(monitor, index=3)
    _observe(monitor, index=61)
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'

    # Dwell reaches 60s (restricted since the throttle at mono 2.0).
    snapshot = _observe(monitor, index=62)
    second = await asyncio.wait_for(queued, timeout=1.0)
    assert second.tool_call_id == 'call:b'
    assert controller.snapshot()['tool_pressure_state'] == 'easing'
    assert controller.snapshot()['tool_pressure_target_limit'] == 2
    assert snapshot['tool_pressure_self_heal_reason'] == 'dwell_timeout'

    controller.release_tool_slot(first)
    controller.release_tool_slot(second)
    assert controller.snapshot()['tool_pressure_state'] == 'normal'


@pytest.mark.asyncio
async def test_dwell_timeout_disabled_keeps_throttled_lockout() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        **_standard_monitor_kwargs(
            max_pressure_dwell_seconds=0.0,
            max_tool_wait_ms=0.0,
            local_recovery_enabled=False,
        ),
    )
    first = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:a',
        tool_name='filesystem',
        tool_call_id='call:a',
    )
    queued = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:b',
            tool_name='filesystem',
            tool_call_id='call:b',
        )
    )
    await asyncio.sleep(0)

    for index in range(3):
        _observe(monitor, index=index, machine_cpu_percent=91.0)
    _observe(monitor, index=4)
    snapshot = _observe(monitor, index=5000)

    assert controller.snapshot()['tool_pressure_state'] == 'throttled'
    assert controller.snapshot()['tool_pressure_waiting_count'] == 1
    assert snapshot['tool_pressure_self_heal_reason'] == ''

    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    controller.release_tool_slot(first)


@pytest.mark.asyncio
async def test_starvation_forces_easing_when_oldest_waiter_exceeds_max_wait() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        **_standard_monitor_kwargs(
            max_pressure_dwell_seconds=0.0,  # isolate mechanism B from A
            max_tool_wait_ms=5.0,
            local_recovery_enabled=False,  # isolate mechanism B from C
        ),
    )
    first = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:a',
        tool_name='filesystem',
        tool_call_id='call:a',
    )
    queued = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:b',
            tool_name='filesystem',
            tool_call_id='call:b',
        )
    )
    await asyncio.sleep(0)

    for index in range(3):
        _observe(monitor, index=index, machine_cpu_percent=91.0)
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'

    # Age the oldest waiter past the 5ms threshold (oldest_wait_ms is driven
    # by the real clock), then feed one neutral sample. Machine stays
    # non-safe, so only the starvation valve can release the waiter.
    await asyncio.sleep(0.02)
    snapshot = _observe(monitor, index=3)
    second = await asyncio.wait_for(queued, timeout=1.0)
    assert second.tool_call_id == 'call:b'
    assert controller.snapshot()['tool_pressure_state'] == 'easing'
    assert controller.snapshot()['tool_pressure_target_limit'] == 2
    assert snapshot['tool_pressure_self_heal_reason'] == 'starvation'

    controller.release_tool_slot(first)
    controller.release_tool_slot(second)
    assert controller.snapshot()['tool_pressure_state'] == 'normal'


@pytest.mark.asyncio
async def test_forced_easing_never_overrides_active_critical_sample() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        **_standard_monitor_kwargs(
            max_pressure_dwell_seconds=0.001,  # expires almost immediately
            max_tool_wait_ms=5.0,
            local_recovery_enabled=True,
        ),
    )
    first = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:a',
        tool_name='filesystem',
        tool_call_id='call:a',
    )
    queued = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:b',
            tool_name='filesystem',
            tool_call_id='call:b',
        )
    )
    await asyncio.sleep(0)

    _observe(monitor, index=1, machine_cpu_percent=97.0)
    assert controller.snapshot()['tool_pressure_state'] == 'critical'

    # Both valves are armed (dwell long expired, waiter starved), but the
    # sample itself is still critical: critical must preempt forced easing.
    await asyncio.sleep(0.02)
    snapshot = _observe(monitor, index=10, machine_cpu_percent=97.0)
    assert controller.snapshot()['tool_pressure_state'] == 'critical'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1
    assert controller.snapshot()['tool_pressure_waiting_count'] == 1
    assert snapshot['tool_pressure_self_heal_reason'] == ''

    queued.cancel()
    await asyncio.gather(queued, return_exceptions=True)
    controller.release_tool_slot(first)


@pytest.mark.asyncio
async def test_local_recovery_heals_critical_when_machine_metrics_unavailable() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        **_standard_monitor_kwargs(
            max_pressure_dwell_seconds=0.0,  # isolate mechanism C from A
            max_tool_wait_ms=0.0,  # isolate mechanism C from B
            local_recovery_enabled=True,
        ),
    )

    # Machine metrics unavailable throughout: machine_safe is structurally
    # False, so the legacy machine-driven recovery path can never fire.
    _observe(monitor, index=1, machine_available=False, writer_queue_depth=101)
    assert controller.snapshot()['tool_pressure_state'] == 'critical'

    for index in (2, 3):
        _observe(monitor, index=index, machine_available=False)
    assert controller.snapshot()['tool_pressure_state'] == 'critical'

    snapshot = _observe(monitor, index=4, machine_available=False)
    assert controller.snapshot()['tool_pressure_state'] == 'normal'
    assert snapshot['tool_pressure_self_heal_reason'] == 'local_recovery'


def test_legacy_lockout_reproduced_when_all_self_heal_knobs_disabled() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        **_standard_monitor_kwargs(
            max_pressure_dwell_seconds=0.0,
            max_tool_wait_ms=0.0,
            local_recovery_enabled=False,
        ),
    )

    _observe(monitor, index=1, machine_available=False, writer_queue_depth=101)
    assert controller.snapshot()['tool_pressure_state'] == 'critical'

    # Local pressure clears immediately, but with every self-heal knob off the
    # monitor reproduces the legacy one-way trap: critical forever.
    snapshot = {}
    for index in range(2, 12):
        snapshot = _observe(monitor, index=index, machine_available=False)
    assert controller.snapshot()['tool_pressure_state'] == 'critical'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1
    assert snapshot['tool_pressure_self_heal_reason'] == ''


@pytest.mark.asyncio
async def test_easing_step_respects_configured_recover_window() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        **_standard_monitor_kwargs(
            recover_window_seconds=5.0,
            max_pressure_dwell_seconds=0.0,
            max_tool_wait_ms=0.0,
            local_recovery_enabled=False,  # recovery driven by machine_safe only
        ),
    )
    first = await controller.acquire_tool_slot(
        task_id='task:one',
        node_id='node:a',
        tool_name='filesystem',
        tool_call_id='call:a',
    )
    queued_b = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:b',
            tool_name='filesystem',
            tool_call_id='call:b',
        )
    )
    queued_c = asyncio.create_task(
        controller.acquire_tool_slot(
            task_id='task:one',
            node_id='node:c',
            tool_name='filesystem',
            tool_call_id='call:c',
        )
    )
    await asyncio.sleep(0)
    assert controller.snapshot()['tool_pressure_waiting_count'] == 2

    for index in range(3):
        _observe(monitor, index=index, machine_cpu_percent=91.0)
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'

    # Three consecutive safe samples start easing but do not step yet.
    for index in (3, 4, 5):
        _observe(monitor, index=index, machine_cpu_percent=20.0)
    assert controller.snapshot()['tool_pressure_state'] == 'easing'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1

    # Window is 5s: samples 6..9 must not raise the limit.
    for index in (6, 7, 8, 9):
        _observe(monitor, index=index, machine_cpu_percent=20.0)
    assert controller.snapshot()['tool_pressure_target_limit'] == 1
    assert controller.snapshot()['tool_pressure_waiting_count'] == 2

    # At mono 10 the 5s window since begin_easing (mono 5) has elapsed.
    _observe(monitor, index=10, machine_cpu_percent=20.0)
    second = await asyncio.wait_for(queued_b, timeout=1.0)
    assert second.tool_call_id == 'call:b'
    assert controller.snapshot()['tool_pressure_target_limit'] == 2
    assert controller.snapshot()['tool_pressure_waiting_count'] == 1

    controller.release_tool_slot(first)
    third = await asyncio.wait_for(queued_c, timeout=1.0)
    assert third.tool_call_id == 'call:c'
    controller.release_tool_slot(second)
    controller.release_tool_slot(third)
    assert controller.snapshot()['tool_pressure_state'] == 'normal'


def _gate_stub(*, age_ms, local_state='normal', machine_state='unknown', budget_state='normal', stale_after_seconds=10.0, close_on_machine_critical=False):
    snapshot = {
        'pressure_sample_age_ms': age_ms,
        'local_pressure_state': local_state,
        'machine_pressure_state': machine_state,
        'budget_state': budget_state,
    }
    return SimpleNamespace(
        tool_pressure_monitor=SimpleNamespace(snapshot=lambda: dict(snapshot)),
        _pressure_gate_stale_after_seconds=stale_after_seconds,
        _pressure_gate_close_on_machine_critical=close_on_machine_critical,
    )


def test_node_turn_gate_fails_open_when_pressure_sample_is_stale() -> None:
    stub = _gate_stub(age_ms=60_000.0, local_state='critical')
    assert MainRuntimeService._node_turn_gate_allowed(stub) is True


def test_node_turn_gate_fails_open_when_sample_age_missing_or_unparseable() -> None:
    assert MainRuntimeService._node_turn_gate_allowed(_gate_stub(age_ms=None, local_state='critical')) is True
    assert MainRuntimeService._node_turn_gate_allowed(_gate_stub(age_ms='garbage', local_state='critical')) is True


def test_node_turn_gate_closes_on_fresh_local_critical() -> None:
    stub = _gate_stub(age_ms=1_000.0, local_state='critical')
    assert MainRuntimeService._node_turn_gate_allowed(stub) is False


def test_node_turn_gate_ignores_machine_critical_by_default() -> None:
    stub = _gate_stub(age_ms=1_000.0, machine_state='critical', budget_state='critical')
    assert MainRuntimeService._node_turn_gate_allowed(stub) is True


def test_node_turn_gate_legacy_toggle_closes_on_machine_critical() -> None:
    stub = _gate_stub(
        age_ms=1_000.0,
        machine_state='critical',
        budget_state='critical',
        close_on_machine_critical=True,
    )
    assert MainRuntimeService._node_turn_gate_allowed(stub) is False


def test_node_turn_gate_staleness_check_disabled_keeps_local_critical_closed() -> None:
    stub = _gate_stub(age_ms=60_000.0, local_state='critical', stale_after_seconds=0.0)
    assert MainRuntimeService._node_turn_gate_allowed(stub) is False


def test_node_turn_gate_allows_when_monitor_missing() -> None:
    stub = SimpleNamespace(
        _pressure_gate_stale_after_seconds=10.0,
        _pressure_gate_close_on_machine_critical=False,
    )
    assert MainRuntimeService._node_turn_gate_allowed(stub) is True
