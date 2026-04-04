from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any


class GlobalScheduler:
    def __init__(
        self,
        *,
        runner: Any,
        max_concurrent_tasks: int | None = None,
        per_task_limit: int = 1,
    ) -> None:
        self._runner = runner
        self._max_concurrent_tasks = None if max_concurrent_tasks is None else max(1, int(max_concurrent_tasks or 1))
        self._per_task_limit = max(1, int(per_task_limit or 1))
        self._running: dict[str, asyncio.Task[None]] = {}
        self._queued: deque[str] = deque()
        self._queued_set: set[str] = set()
        self._waiters: dict[str, list[asyncio.Future[None]]] = {}
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False

    def snapshot(self) -> dict[str, Any]:
        return {
            'max_concurrent_tasks': None if self._max_concurrent_tasks is None else int(self._max_concurrent_tasks),
            'per_task_limit': int(self._per_task_limit),
            'running_task_ids': sorted(self._running.keys()),
            'queued_task_ids': list(self._queued),
        }

    def active_task_count(self) -> int:
        return len(self._running)

    def queued_task_count(self) -> int:
        return len(self._queued_set)

    def is_active(self, task_id: str) -> bool:
        return str(task_id or '').strip() in self._running

    def is_queued(self, task_id: str) -> bool:
        return str(task_id or '').strip() in self._queued_set

    async def enqueue_task(self, task_id: str) -> None:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id or self._closed:
            return
        if normalized_task_id in self._running or normalized_task_id in self._queued_set:
            return
        self._queued.append(normalized_task_id)
        self._queued_set.add(normalized_task_id)
        self._ensure_pump()
        await asyncio.sleep(0)

    async def cancel_task(self, task_id: str) -> None:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return
        if normalized_task_id in self._queued_set:
            self._queued = deque(item for item in self._queued if item != normalized_task_id)
            self._queued_set.discard(normalized_task_id)
            self._resolve_waiters(normalized_task_id)
        current = self._running.get(normalized_task_id)
        if current is not None and not current.done():
            current.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.shield(current)

    async def wait(self, task_id: str) -> None:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return
        current = self._running.get(normalized_task_id)
        if current is not None:
            try:
                await asyncio.shield(current)
            except asyncio.CancelledError:
                if current.done():
                    return
                raise
            return
        if normalized_task_id not in self._queued_set:
            return
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        self._waiters.setdefault(normalized_task_id, []).append(waiter)
        try:
            await waiter
        finally:
            if waiter.cancelled():
                waiters = [item for item in self._waiters.get(normalized_task_id, []) if item is not waiter]
                if waiters:
                    self._waiters[normalized_task_id] = waiters
                else:
                    self._waiters.pop(normalized_task_id, None)

    async def close(self) -> None:
        self._closed = True
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
            await asyncio.gather(self._pump_task, return_exceptions=True)
        self._pump_task = None
        running = list(self._running.values())
        self._running.clear()
        self._queued.clear()
        self._queued_set.clear()
        for task in running:
            if not task.done():
                task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        for task_id in list(self._waiters.keys()):
            self._resolve_waiters(task_id)

    def _ensure_pump(self) -> None:
        if self._closed:
            return
        if self._pump_task is not None and not self._pump_task.done():
            return
        loop = asyncio.get_running_loop()
        self._pump_task = loop.create_task(self._pump(), name='task-global-scheduler')

    async def _pump(self) -> None:
        try:
            while not self._closed:
                started = False
                while ((self._max_concurrent_tasks is None) or (len(self._running) < self._max_concurrent_tasks)) and self._queued:
                    task_id = self._queued.popleft()
                    self._queued_set.discard(task_id)
                    if task_id in self._running:
                        continue
                    worker = asyncio.create_task(self._runner.run_task(task_id), name=f'task-actor:{task_id}')
                    self._running[task_id] = worker
                    worker.add_done_callback(
                        lambda done_task, stored_task_id=task_id: self._on_task_done(stored_task_id, done_task)
                    )
                    started = True
                if not started:
                    break
                await asyncio.sleep(0)
        finally:
            self._pump_task = None

    def _on_task_done(self, task_id: str, done_task: asyncio.Task[None]) -> None:
        current = self._running.get(task_id)
        if current is done_task:
            self._running.pop(task_id, None)
        self._resolve_waiters(task_id)
        if self._closed:
            return
        try:
            asyncio.get_running_loop().call_soon(self._ensure_pump)
        except RuntimeError:
            return

    def _resolve_waiters(self, task_id: str) -> None:
        for waiter in self._waiters.pop(task_id, []):
            if not waiter.done():
                waiter.set_result(None)
