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


@pytest.mark.asyncio
async def test_entry_gate_bounds_concurrent_node_turns() -> None:
    """节点回合闸：一次放 5 个执行器，同时在跑的不超过天花板。"""
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': 2})

    inside = 0
    peak = 0
    started = asyncio.Event()

    async def turn(index: int) -> None:
        nonlocal inside, peak
        await controller.acquire_entry_slot(role='execution')
        try:
            inside += 1
            peak = max(peak, inside)
            if inside == 2:
                started.set()
            await asyncio.sleep(0.02)
        finally:
            inside -= 1
            controller.release_entry_slot(role='execution')

    tasks = [asyncio.create_task(turn(index)) for index in range(5)]
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)

    assert peak == 2
    assert controller.entry_snapshot()['execution']['running'] == 0
    assert controller.entry_snapshot()['execution']['queued'] == 0


@pytest.mark.asyncio
async def test_entry_gate_ignores_tool_pressure_state_and_yields_only_to_disk_emergency() -> None:
    """回合闸不再被工具压力状态踩到地板以下。

    实盘（09-30 17:06:15）：一次 141 秒的 SQLite 读让 budget 落 critical，双闸被踩成 1/1
    约 9 分钟，而那时内存与上游都还空着——工具槽量的是"一次工具调用"的积压，不该决定
    同时在物化上下文的执行器数。现在只有磁盘紧急还能把回合闸直接踩到 0。
    """
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': 3})

    await controller.acquire_entry_slot(role='execution')
    await controller.acquire_entry_slot(role='execution')
    await controller.acquire_entry_slot(role='execution')
    assert controller.entry_snapshot()['execution']['limit'] == 3

    blocked = asyncio.Event()

    async def waiter() -> None:
        await controller.acquire_entry_slot(role='execution')
        blocked.set()
        controller.release_entry_slot(role='execution')

    pending = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not blocked.is_set()

    controller.critical()
    assert controller.entry_snapshot()['execution']['limit'] == 3

    controller.throttle()
    assert controller.entry_snapshot()['execution']['limit'] == 3

    controller.set_disk_emergency(True)
    assert controller.entry_snapshot()['execution']['limit'] == 0

    controller.set_disk_emergency(False)
    controller.set_budget_state('normal')
    assert controller.entry_snapshot()['execution']['limit'] == 3

    controller.release_entry_slot(role='execution')
    controller.release_entry_slot(role='execution')
    controller.release_entry_slot(role='execution')
    await asyncio.wait_for(pending, timeout=2)
    assert blocked.is_set()
    controller.release_entry_slot(role='execution')


@pytest.mark.asyncio
async def test_entry_gate_follows_published_target_with_slew() -> None:
    """目标是资源判据，跳幅才是控制：上行每拍最多 +2，下行每拍 -1。

    一次模型调用实测 p50 37 s / p90 84 s，反馈远慢于 1 s 采样；不限跳幅就会在上一拍的
    后果可见之前继续放人（凌晨那次 90 秒爬到 27 格、Private 2.4 GB 的形态）。
    """
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': 2})
    await controller.acquire_entry_slot(role='execution')
    await controller.acquire_entry_slot(role='execution')

    admitted = asyncio.Event()

    async def waiter() -> None:
        await controller.acquire_entry_slot(role='execution')
        admitted.set()
        controller.release_entry_slot(role='execution')

    pending = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not admitted.is_set()

    assert controller.set_entry_targets({'execution': 6}) == {'execution': 4}
    await asyncio.wait_for(pending, timeout=2)
    assert admitted.is_set()
    # 只动回合闸，不带动工具槽
    assert controller.snapshot()['tool_pressure_target_limit'] == 1

    assert controller.set_entry_targets({'execution': 6}) == {'execution': 6}
    assert controller.set_entry_targets({'execution': 1}) == {'execution': 5}
    assert controller.set_entry_targets({'execution': 1}) == {'execution': 4}


@pytest.mark.asyncio
async def test_entry_gate_resets_to_floor_when_queue_drains() -> None:
    """排空后回到地板：下一次风暴不继承上次的高水位一次放出去（21:31 无闸事故的形态）。"""
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': 2})

    await controller.acquire_entry_slot(role='execution')
    await controller.acquire_entry_slot(role='execution')
    assert controller.set_entry_targets({'execution': 9}) == {'execution': 4}
    snapshot = controller.entry_snapshot()['execution']
    assert snapshot['limit'] == 4
    assert snapshot['running'] == 2

    await controller.acquire_entry_slot(role='execution')
    await controller.acquire_entry_slot(role='execution')
    assert controller.entry_snapshot()['execution']['running'] == 4

    for _ in range(4):
        controller.release_entry_slot(role='execution')
    snapshot = controller.entry_snapshot()['execution']
    assert snapshot['running'] == 0
    assert snapshot['queued'] == 0
    assert snapshot['limit'] == 2


def test_configure_entry_ceilings_clamps_high_water_only_when_floor_changed() -> None:
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': 8})
    controller.set_entry_targets({'execution': 20})
    controller.set_entry_targets({'execution': 20})
    assert controller.entry_snapshot()['execution']['limit'] == 12

    # 同一份配置的刷新：保留已经爬到的位置（operator 没改意图）
    controller.configure_entry_ceilings({'execution': 8})
    assert controller.entry_snapshot()['execution']['limit'] == 12

    # 地板本身变了：当场钳到新地板（operator 改数就是改意图）
    controller.configure_entry_ceilings({'execution': 5})
    assert controller.entry_snapshot()['execution']['limit'] == 5


def _entry_target_monitor(
    controller: AdaptiveToolBudgetController,
    store,
    *,
    rate_observer=None,
) -> WorkerPressureMonitor:
    """阈值全部取线上默认值（lag warn 250ms、写入队列 warn 50、SQLite 写 warn 200ms /
    读 warn 150ms、机器内存 warn 88%）；回合闸的余量目标就用这些线算，不另起一套数字。"""
    return WorkerPressureMonitor(
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
        rate_limit_observer=rate_observer,
    )


# 8 GiB 总内存、剩 4 GiB 可用：按 warn(88%) 保留 0.96 GiB 后还能容 ~28 格（兜底成本 110 MB/格），
# 所以内存轴在这些用例里不构成约束。
_ROOMY_MEMORY = {
    'memory_total_bytes': 8 * 1024 ** 3,
    'memory_available_bytes': 4 * 1024 ** 3,
}


def _observe_pressure(
    monitor: WorkerPressureMonitor,
    mono: float,
    *,
    lag_ms: float = 0.0,
    writer_depth: int = 0,
    sqlite_write_ms: float = 0.0,
    sqlite_query_ms: float = 0.0,
    process_cpu_ratio: float = 0.10,
    memory_total_bytes: int | None = None,
    memory_available_bytes: int | None = None,
    worker_memory_bytes: int | None = None,
    rate_pressure: dict[str, float] | None = None,
) -> dict:
    return monitor.observe_sample(
        machine_cpu_percent=20.0,
        machine_memory_percent=30.0,
        machine_disk_busy_percent=10.0,
        machine_available=True,
        event_loop_lag_ms=lag_ms,
        writer_queue_depth=writer_depth,
        sqlite_write_wait_ms=sqlite_write_ms,
        sqlite_query_latency_ms=sqlite_query_ms,
        process_cpu_ratio=process_cpu_ratio,
        now_mono=mono,
        now_iso=f'2026-09-30T00:00:{int(mono) % 60:02d}+08:00',
        machine_memory_total_bytes=memory_total_bytes,
        machine_memory_available_bytes=memory_available_bytes,
        worker_memory_bytes=worker_memory_bytes,
        rate_pressure=rate_pressure,
    )


@pytest.mark.asyncio
async def test_monitor_grows_entry_gate_toward_headroom_and_records_the_verdict() -> None:
    """闸被需求顶住、四条积压轴都安静时目标翻倍，闸位每拍 +2；目标与落到的闸位都进快照，
    否则操作员只能看到"节点数没变"，看不到是哪条轴拦的。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    monitor = _entry_target_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 8})

    for _ in range(8):
        await controller.acquire_entry_slot(role='execution')
    # 4 个执行器排在闸口：这才是"闸卡住需求"的证据，也是继续抬闸的唯一理由
    waiting = [asyncio.create_task(controller.acquire_entry_slot(role='execution')) for _ in range(4)]
    await asyncio.sleep(0.05)

    snapshot = _observe_pressure(monitor, 100.0, **_ROOMY_MEMORY)
    assert snapshot['entry_gate_targets'] == {'execution': 16}
    assert snapshot['entry_gate_limits'] == {'execution': 10}

    snapshot = _observe_pressure(monitor, 101.0, **_ROOMY_MEMORY)
    assert snapshot['entry_gate_targets'] == {'execution': 20}
    assert snapshot['entry_gate_limits'] == {'execution': 12}

    for task in waiting:
        await asyncio.wait_for(task, timeout=5)
    for _ in range(12):
        controller.release_entry_slot(role='execution')


@pytest.mark.asyncio
async def test_monitor_does_not_raise_the_entry_gate_without_demand() -> None:
    """闸口没人在等、在飞的也没占满时不抬闸位：那只会攒出一把"随时可以一次放出几百份
    上下文物化"的空权限（21:31 无闸事故的形态），而不是并发。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    monitor = _entry_target_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 8})

    for index in range(6):
        snapshot = _observe_pressure(monitor, 100.0 + index, **_ROOMY_MEMORY)
    assert snapshot['entry_gate_targets'] == {'execution': 8}
    assert snapshot['entry_gate_limits'] == {'execution': 8}

    # 需求一到（排队或占满）才开始抬
    held = [await controller.acquire_entry_slot(role='execution') for _ in range(8)]
    snapshot = _observe_pressure(monitor, 110.0, **_ROOMY_MEMORY)
    assert snapshot['entry_gate_limits'] == {'execution': 10}
    for _ in held:
        controller.release_entry_slot(role='execution')


@pytest.mark.asyncio
async def test_monitor_keeps_entry_gate_at_the_floor_when_memory_headroom_is_unreadable() -> None:
    """读不到内存读数时不许越过地板：v1 那版正是在"内存读数不可用/滞后"的状态下
    90 秒爬到 27 格、把 worker Private 推到 2.4 GB 的（01:24 实盘）。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    monitor = _entry_target_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 8})

    held = [await controller.acquire_entry_slot(role='execution') for _ in range(8)]
    waiting = [asyncio.create_task(controller.acquire_entry_slot(role='execution')) for _ in range(2)]
    await asyncio.sleep(0.05)
    for index in range(4):
        snapshot = _observe_pressure(monitor, 100.0 + index)
    assert snapshot['entry_gate_targets'] == {'execution': 8}
    assert snapshot['entry_gate_limits'] == {'execution': 8}
    for task in waiting:
        task.cancel()
    for _ in held:
        controller.release_entry_slot(role='execution')


@pytest.mark.asyncio
async def test_monitor_holds_entry_gate_once_an_axis_reaches_its_warn_line() -> None:
    """lag 越过 warn(250ms) 但没到 critical(1500ms) ⇒ 只"保持"当前节点数。

    用例里需求是顶着的（8 格占满 + 2 个排队），否则"没长"这件事不需要这条轴来解释。
    没有这层夹住，warn 级落后（300ms 对 250ms 线）会被一路削到 1 并钉死——292 个节点
    排队时"循环落后 0.3 秒"是常态，不是紧急，削到 1 是塌方而不是控制。
    """
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    monitor = _entry_target_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 8})

    for _ in range(8):
        await controller.acquire_entry_slot(role='execution')
    waiting = [asyncio.create_task(controller.acquire_entry_slot(role='execution')) for _ in range(2)]
    await asyncio.sleep(0.05)

    for index in range(4):
        snapshot = _observe_pressure(monitor, 100.0 + index, lag_ms=300.0, **_ROOMY_MEMORY)
    assert snapshot['entry_gate_targets'] == {'execution': 8}
    assert snapshot['entry_gate_limits'] == {'execution': 8}

    for task in waiting:
        task.cancel()
    for _ in range(8):
        controller.release_entry_slot(role='execution')


def test_monitor_walks_entry_gate_down_and_below_the_floor_when_the_loop_backs_up() -> None:
    """事件循环积压是本进程自己造成的，允许一路缓减到地板以下（最低 1），每拍一格。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    monitor = _entry_target_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 8})

    limits = []
    for index in range(5):
        snapshot = _observe_pressure(monitor, 100.0 + index, lag_ms=12_000.0, **_ROOMY_MEMORY)
        limits.append(snapshot['entry_gate_limits']['execution'])
    assert limits == [7, 6, 5, 4, 3]


@pytest.mark.asyncio
async def test_upstream_rate_limit_penalty_holds_then_shrinks_the_entry_gate() -> None:
    """429 惩罚按实测分布分档：p50=0 / p90=0.18 / p99=4.16 / max=13.57（6255 条路由观测）。
    越过 1.0 只保持，越过 4.0 才开始缓减；其余四轴此刻仍然安静。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    monitor = _entry_target_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 8})

    held = [await controller.acquire_entry_slot(role='execution') for _ in range(8)]
    waiting = [asyncio.create_task(controller.acquire_entry_slot(role='execution')) for _ in range(2)]
    await asyncio.sleep(0.05)

    for index in range(3):
        snapshot = _observe_pressure(
            monitor, 100.0 + index, rate_pressure={'penalty_429_max': 1.5}, **_ROOMY_MEMORY
        )
    assert snapshot['entry_gate_limits'] == {'execution': 8}

    shrunk = _observe_pressure(monitor, 110.0, rate_pressure={'penalty_429_max': 5.0}, **_ROOMY_MEMORY)
    assert shrunk['entry_gate_limits'] == {'execution': 7}

    for task in waiting:
        task.cancel()
    for _ in held:
        controller.release_entry_slot(role='execution')


@pytest.mark.asyncio
async def test_memory_headroom_caps_entry_growth_but_never_pushes_below_the_floor() -> None:
    """内存余量只限制增长：机器内存吃紧多半是外部进程造成的，那该走磁盘/内存紧急道，
    不该把操作员配的地板压掉。保留量直接取机器内存 warn 线之上那一段，不新设阈值。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    monitor = _entry_target_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 8})
    total = 8 * 1024 ** 3

    for _ in range(8):
        await controller.acquire_entry_slot(role='execution')
    waiting = [asyncio.create_task(controller.acquire_entry_slot(role='execution')) for _ in range(2)]
    await asyncio.sleep(0.05)

    # available 恰好等于 warn(88%) 以下的保留量 ⇒ 还能容 0 格，闸位停在地板而不是被压下去
    pinned = _observe_pressure(
        monitor,
        100.0,
        memory_total_bytes=total,
        memory_available_bytes=int(total * 0.12),
    )
    assert pinned['entry_gate_limits'] == {'execution': 8}

    roomy = _observe_pressure(
        monitor,
        101.0,
        memory_total_bytes=total,
        memory_available_bytes=4 * 1024 ** 3,
    )
    assert roomy['entry_gate_limits'] == {'execution': 10}

    for task in waiting:
        await asyncio.wait_for(task, timeout=5)
    for _ in range(10):
        controller.release_entry_slot(role='execution')


@pytest.mark.asyncio
async def test_entry_slot_memory_cost_is_estimated_from_running_slots_and_rss() -> None:
    """一格内存成本用滑动窗口里 (在飞格数, RSS) 的跨度回归：实测 9 格 RSS 1.04 GB、
    凌晨 27 格 Private 2.4 GB（≈89–115 MB/格）。样本跨度不足或斜率出格时保留上一值。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    monitor = _entry_target_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 12})

    for _ in range(2):
        await controller.acquire_entry_slot(role='execution')
    _observe_pressure(monitor, 100.0, worker_memory_bytes=400 * 1024 ** 2)

    for _ in range(8):
        await controller.acquire_entry_slot(role='execution')
    snapshot = _observe_pressure(monitor, 101.0, worker_memory_bytes=1200 * 1024 ** 2)

    assert snapshot['entry_gate_running_total'] == 10
    assert snapshot['entry_slot_memory_bytes'] == 100 * 1024 ** 2
    assert snapshot['worker_memory_bytes'] == 1200 * 1024 ** 2


def test_perf_history_records_why_the_entry_gate_moved() -> None:
    """目标、落到的闸位、一格成本、进程 RSS、上游限流惩罚与本进程占核数都要能在历史里读到。"""
    from main.service.worker_heartbeat_service_v2 import _PERF_HISTORY_FIELDS

    for key in (
        'entry_gate_targets',
        'entry_gate_limits',
        'entry_gate_running_total',
        'entry_slot_memory_bytes',
        'worker_memory_bytes',
        'machine_memory_available_bytes',
        'model_rate_penalty_429_max',
        'model_rolling_rpm_60s_max',
        'tool_pressure_process_cpu_ratio',
    ):
        assert key in _PERF_HISTORY_FIELDS, f'{key} 没进性能历史，闸为什么动读不出来'

def test_dispatcher_reports_live_entry_gate_limit_not_the_config_floor() -> None:
    """界面读的"限"必须是闸现在真开到第几格：余量目标每拍推它，只报地板的话
    "闸在动"这件事在 UI 上完全读不出来。磁盘紧急时它要如实显示 0。"""
    from main.runtime.task_actor_service import TaskNodeDispatcher

    def stub_parts():
        return SimpleNamespace(), SimpleNamespace(update_task_runtime_meta=lambda *_a, **_k: None), SimpleNamespace()

    store, log_service, node_runner = stub_parts()
    budget = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    dispatcher = TaskNodeDispatcher(
        task_id='task:live-limit',
        store=store,
        log_service=log_service,
        node_runner=node_runner,
        execution_limit=3,
        inspection_limit=1,
        entry_budget=budget,
    )
    assert dispatcher.snapshot()['dispatch_limits'] == {'execution': 3, 'inspection': 1}

    budget.set_entry_targets({'execution': 5})
    assert dispatcher.snapshot()['dispatch_limits']['execution'] == 5

    budget.set_disk_emergency(True)
    assert dispatcher.snapshot()['dispatch_limits']['execution'] == 0


def test_dispatcher_uses_adaptive_gate_only_when_budget_wired() -> None:
    from main.runtime.adaptive_tool_budget import AdaptiveToolBudgetController
    from main.runtime.task_actor_service import TaskNodeDispatcher, _AdaptiveRoleGate

    def stub_dispatcher_parts():
        return SimpleNamespace(), SimpleNamespace(update_task_runtime_meta=lambda *_a, **_k: None), SimpleNamespace()

    budget = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    store, log_service, node_runner = stub_dispatcher_parts()
    dispatcher = TaskNodeDispatcher(
        task_id='task:gate',
        store=store,
        log_service=log_service,
        node_runner=node_runner,
        execution_limit=3,
        inspection_limit=1,
        entry_budget=budget,
    )
    assert isinstance(dispatcher._semaphores['execution'], _AdaptiveRoleGate)
    assert budget.entry_snapshot()['execution']['ceiling'] == 3
    assert budget.entry_snapshot()['inspection']['ceiling'] == 1

    plain = TaskNodeDispatcher(
        task_id='task:gate2',
        store=store,
        log_service=log_service,
        node_runner=node_runner,
        execution_limit=3,
        inspection_limit=1,
    )
    assert isinstance(plain._semaphores['execution'], asyncio.Semaphore)

    off = TaskNodeDispatcher(
        task_id='task:gate3',
        store=store,
        log_service=log_service,
        node_runner=node_runner,
        execution_limit=None,
        inspection_limit=None,
        entry_budget=budget,
    )
    assert off._semaphores['execution'] is None
