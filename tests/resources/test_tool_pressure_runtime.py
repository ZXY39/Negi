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
async def test_entry_gate_follows_pressure_state_and_disk_emergency() -> None:
    """critical 收到 1、磁盘紧急收到 0、恢复按步抬回天花板。"""
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
    assert controller.entry_snapshot()['execution']['limit'] == 1

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
async def test_entry_gate_climbs_above_configured_floor_while_demand_exists() -> None:
    """配置值是地板不是上限：闸口还有排队时，easing 每格 +1 要能越过地板。"""
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
    assert controller.entry_snapshot()['execution']['limit'] == 2

    assert controller.step_entry_easing() is True
    assert controller.entry_snapshot()['execution']['limit'] == 3
    await asyncio.wait_for(pending, timeout=2)
    assert admitted.is_set()
    # 只抬回合闸，不带动工具槽
    assert controller.snapshot()['tool_pressure_target_limit'] == 1


@pytest.mark.asyncio
async def test_entry_gate_resets_to_floor_when_queue_drains() -> None:
    """排空后必须回到地板：下一次风暴从地板按格爬，不继承上次的高水位一次放出（21:31 形态）。"""
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': 2})

    await controller.acquire_entry_slot(role='execution')
    await controller.acquire_entry_slot(role='execution')
    controller.step_entry_easing()
    controller.step_entry_easing()
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


@pytest.mark.asyncio
async def test_entry_gate_high_water_freezes_on_throttle_and_collapses_on_critical() -> None:
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': 1})

    await controller.acquire_entry_slot(role='execution')
    controller.step_entry_easing()
    await controller.acquire_entry_slot(role='execution')
    controller.step_entry_easing()
    await controller.acquire_entry_slot(role='execution')
    assert controller.entry_snapshot()['execution']['limit'] == 3
    assert controller.entry_snapshot()['execution']['running'] == 3

    controller.throttle()
    assert controller.entry_snapshot()['execution']['limit'] == 3

    controller.critical()
    assert controller.entry_snapshot()['execution']['limit'] == 1

    controller.set_budget_state('normal')
    assert controller.entry_snapshot()['execution']['limit'] == 1


def test_configure_entry_ceilings_clamps_high_water_only_when_floor_changed() -> None:
    controller = AdaptiveToolBudgetController(normal_limit=6, step_up=1)
    controller.configure_entry_ceilings({'execution': 8})
    for _ in range(4):
        controller.step_entry_easing()
    assert controller.entry_snapshot()['execution']['limit'] == 12

    controller.configure_entry_ceilings({'execution': 8})
    assert controller.entry_snapshot()['execution']['limit'] == 12

    controller.configure_entry_ceilings({'execution': 4})
    assert controller.entry_snapshot()['execution']['limit'] == 4


@pytest.mark.asyncio
async def test_monitor_steps_entry_gate_from_gate_queue_without_tool_waiters() -> None:
    """回合闸的向上车道必须看自己的排队数：工具队列常年为空时，只看 tool waiting 会锁死这条道。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=2, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = _entry_climb_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 1})
    await controller.acquire_entry_slot(role='execution')

    admitted = asyncio.Event()

    async def waiter() -> None:
        await controller.acquire_entry_slot(role='execution')
        admitted.set()

    pending = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    assert not admitted.is_set()

    def sample(mono: float) -> None:
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
            now_mono=mono,
            now_iso=f'2026-03-30T00:00:{int(mono):02d}+08:00',
        )

    for index in range(3):
        sample(float(index))
    # 工具队列没人等 ⇒ 状态回 normal（回合闸的排队不参与状态迁移，否则一次 critical 之后
    # 会卡在低 limit 上按分钟爬）
    assert controller.snapshot()['tool_pressure_state'] == 'normal'
    assert controller.entry_snapshot()['execution']['limit'] == 1

    sample(3.0)
    await asyncio.wait_for(pending, timeout=2)
    assert admitted.is_set()
    assert controller.entry_snapshot()['execution']['limit'] == 2
    assert controller.snapshot()['tool_pressure_target_limit'] == 1


@pytest.mark.asyncio
async def test_entry_gate_restores_floor_on_first_normal_tick_after_critical() -> None:
    """critical 之后必须一拍就把闸位恢复到地板，不能靠放大那条道按格爬回来。

    实盘回归（01:44:14）：一次 critical 把 limit 打到 1，122 个节点在闸口排队，
    而"有排队就不回 normal"的写法让 limit 以 ~1 格/分钟的速度爬，回合数被压在 5。
    """
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=2, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = _entry_climb_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 3})

    await controller.acquire_entry_slot(role='execution')
    controller.critical()
    assert controller.entry_snapshot()['execution']['limit'] == 1

    admitted = asyncio.Event()

    async def queued() -> None:
        await controller.acquire_entry_slot(role='execution')
        admitted.set()

    pending = asyncio.create_task(queued())
    await asyncio.sleep(0.05)
    assert controller.entry_snapshot()['execution']['queued'] == 1

    for index in range(3):
        monitor.observe_sample(
            machine_cpu_percent=20.0,
            machine_memory_percent=30.0,
            machine_disk_busy_percent=10.0,
            machine_available=True,
            event_loop_lag_ms=5.0,
            writer_queue_depth=0,
            sqlite_write_wait_ms=0.0,
            sqlite_query_latency_ms=0.0,
            process_cpu_ratio=0.10,
            now_mono=float(index),
            now_iso=f'2026-03-30T00:00:0{index}+08:00',
        )
    assert controller.snapshot()['tool_pressure_state'] == 'normal'
    assert controller.entry_snapshot()['execution']['limit'] == 3
    await asyncio.wait_for(pending, timeout=2)
    assert admitted.is_set()


def _entry_climb_monitor(controller: AdaptiveToolBudgetController, store) -> WorkerPressureMonitor:
    """阈值取线上默认值（warn 3 拍、safe 3 拍、recover 窗口 1s、lag warn 250ms/critical 1500ms）。"""
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
    )


@pytest.mark.asyncio
async def test_entry_gate_does_not_climb_while_event_loop_is_backing_up() -> None:
    """机器水位安全但事件循环在积压时不许放大：01:24 实测 limit=11 那拍 lag 已 8.0s、
    机器内存才 73.7%——只有循环积压能预测"再多放一份上下文构建"的代价。"""
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=2, throttled_limit=2, critical_limit=1, step_up=1)
    monitor = _entry_climb_monitor(controller, store)
    controller.configure_entry_ceilings({'execution': 1})
    await controller.acquire_entry_slot(role='execution')

    blocked = asyncio.Event()

    async def waiter() -> None:
        await controller.acquire_entry_slot(role='execution')
        blocked.set()

    pending = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)

    def sample(mono: float, lag_ms: float) -> None:
        monitor.observe_sample(
            machine_cpu_percent=20.0,
            machine_memory_percent=30.0,
            machine_disk_busy_percent=10.0,
            machine_available=True,
            event_loop_lag_ms=lag_ms,
            writer_queue_depth=0,
            sqlite_write_wait_ms=0.0,
            sqlite_query_latency_ms=0.0,
            process_cpu_ratio=0.10,
            now_mono=mono,
            now_iso=f'2026-03-30T00:00:{int(mono):02d}+08:00',
        )

    # 30 拍全部 machine-safe（cpu 20%、mem 30%）但 lag=1500ms（≥warn 250ms，未到 critical 1500ms 之上那条线）
    for index in range(30):
        sample(float(index) + 100.0, 1500.0)
    assert not blocked.is_set()
    assert controller.entry_snapshot()['execution']['limit'] == 1

    # 循环转安静后，按 `safe_consecutive_samples` 的节奏一格一格抬
    for index in range(8):
        sample(float(index) + 200.0, 5.0)
    assert blocked.is_set() or controller.entry_snapshot()['execution']['limit'] > 1
    await asyncio.wait_for(pending, timeout=2)
    assert controller.entry_snapshot()['execution']['limit'] >= 2


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
