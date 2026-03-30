from __future__ import annotations

import asyncio

import pytest

from main.runtime.adaptive_tool_budget import AdaptiveToolBudgetController
from main.runtime.tool_pressure_monitor import WorkerPressureMonitor


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
    assert snapshot['tool_pressure_target_limit'] == 1
    assert snapshot['tool_pressure_running_count'] == 2
    assert snapshot['tool_pressure_waiting_count'] == 1

    controller.release_tool_slot(first)
    await asyncio.sleep(0.05)
    assert queued.done() is False

    controller.release_tool_slot(second)
    acquired = await asyncio.wait_for(queued, timeout=1.0)
    assert acquired.tool_call_id == 'call:c'
    controller.release_tool_slot(acquired)


def test_worker_pressure_monitor_throttles_and_recovers_stepwise() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=4, safe_limit=1, step_up=1)
    monitor = WorkerPressureMonitor(
        controller=controller,
        store=store,
        sample_seconds=1.0,
        recover_window_seconds=10.0,
        warn_consecutive_samples=3,
        safe_consecutive_samples=5,
        pressure_snapshot_stale_after_seconds=3.0,
        event_loop_warn_ms=250.0,
        event_loop_safe_ms=100.0,
        writer_queue_warn=50,
        writer_queue_safe=10,
        sqlite_write_wait_warn_ms=200.0,
        sqlite_write_wait_safe_ms=50.0,
        sqlite_query_warn_ms=150.0,
        sqlite_query_safe_ms=30.0,
        machine_cpu_warn_percent=85.0,
        machine_cpu_safe_percent=55.0,
        machine_memory_warn_percent=88.0,
        machine_memory_safe_percent=75.0,
        machine_disk_busy_warn_percent=70.0,
        machine_disk_busy_safe_percent=35.0,
        process_cpu_warn_ratio=0.85,
        process_cpu_safe_ratio=0.50,
    )

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

    for index in range(3, 8):
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
            now_iso=f'2026-03-30T00:00:1{index - 3}+08:00',
        )
    assert controller.snapshot()['tool_pressure_state'] == 'recovering'
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
        now_mono=18.0,
        now_iso='2026-03-30T00:00:18+08:00',
    )
    assert controller.snapshot()['tool_pressure_target_limit'] == 2
    assert controller.snapshot()['tool_pressure_state'] == 'recovering'

    monitor.observe_sample(
        machine_cpu_percent=92.0,
        machine_memory_percent=30.0,
        machine_disk_busy_percent=10.0,
        machine_available=True,
        event_loop_lag_ms=400.0,
        writer_queue_depth=0,
        sqlite_write_wait_ms=0.0,
        sqlite_query_latency_ms=0.0,
        process_cpu_ratio=0.10,
        now_mono=19.0,
        now_iso='2026-03-30T00:00:19+08:00',
    )
    monitor.observe_sample(
        machine_cpu_percent=92.0,
        machine_memory_percent=30.0,
        machine_disk_busy_percent=10.0,
        machine_available=True,
        event_loop_lag_ms=400.0,
        writer_queue_depth=0,
        sqlite_write_wait_ms=0.0,
        sqlite_query_latency_ms=0.0,
        process_cpu_ratio=0.10,
        now_mono=20.0,
        now_iso='2026-03-30T00:00:20+08:00',
    )
    monitor.observe_sample(
        machine_cpu_percent=92.0,
        machine_memory_percent=30.0,
        machine_disk_busy_percent=10.0,
        machine_available=True,
        event_loop_lag_ms=400.0,
        writer_queue_depth=0,
        sqlite_write_wait_ms=0.0,
        sqlite_query_latency_ms=0.0,
        process_cpu_ratio=0.10,
        now_mono=21.0,
        now_iso='2026-03-30T00:00:21+08:00',
    )
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'
    assert controller.snapshot()['tool_pressure_target_limit'] == 1


def test_worker_pressure_monitor_marks_snapshot_unfresh_when_machine_metrics_are_missing() -> None:
    store = _FakeStore()
    controller = AdaptiveToolBudgetController(normal_limit=4, safe_limit=1, step_up=1)
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
    assert controller.snapshot()['tool_pressure_state'] == 'throttled'
    assert snapshot['pressure_snapshot_fresh'] is False
    assert snapshot['machine_pressure_available'] is False
