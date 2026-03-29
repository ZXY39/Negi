from __future__ import annotations

import asyncio

import pytest

from main.runtime.global_scheduler import GlobalScheduler


class _FakeRunner:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        self._events: dict[str, asyncio.Event] = {}

    def gate(self, task_id: str) -> asyncio.Event:
        event = self._events.get(task_id)
        if event is None:
            event = asyncio.Event()
            self._events[task_id] = event
        return event

    async def run_task(self, task_id: str) -> None:
        normalized_task_id = str(task_id or '').strip()
        self.started.append(normalized_task_id)
        await self.gate(normalized_task_id).wait()
        self.finished.append(normalized_task_id)


@pytest.mark.asyncio
async def test_global_scheduler_limits_concurrency_and_releases_queued_tasks_in_order() -> None:
    runner = _FakeRunner()
    scheduler = GlobalScheduler(runner=runner, max_concurrent_tasks=1, per_task_limit=1)
    try:
        await scheduler.enqueue_task('task:one')
        await scheduler.enqueue_task('task:two')

        for _ in range(100):
            if runner.started == ['task:one']:
                break
            await asyncio.sleep(0.01)

        assert runner.started == ['task:one']
        assert scheduler.is_active('task:one') is True
        assert scheduler.is_queued('task:two') is True

        runner.gate('task:one').set()

        for _ in range(100):
            if runner.started == ['task:one', 'task:two']:
                break
            await asyncio.sleep(0.01)

        assert runner.started == ['task:one', 'task:two']
        assert scheduler.is_active('task:two') is True
        assert scheduler.is_queued('task:two') is False
    finally:
        runner.gate('task:two').set()
        await scheduler.close()


@pytest.mark.asyncio
async def test_global_scheduler_cancel_removes_queued_task_before_it_runs() -> None:
    runner = _FakeRunner()
    scheduler = GlobalScheduler(runner=runner, max_concurrent_tasks=1, per_task_limit=1)
    try:
        await scheduler.enqueue_task('task:one')
        await scheduler.enqueue_task('task:two')

        for _ in range(100):
            if runner.started == ['task:one']:
                break
            await asyncio.sleep(0.01)

        await scheduler.cancel_task('task:two')
        runner.gate('task:one').set()
        await scheduler.wait('task:one')
        await asyncio.sleep(0.05)

        assert runner.started == ['task:one']
        assert scheduler.is_queued('task:two') is False
        assert scheduler.is_active('task:two') is False
    finally:
        runner.gate('task:one').set()
        runner.gate('task:two').set()
        await scheduler.close()
