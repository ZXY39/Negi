from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


WakeHandler = Callable[[str], Awaitable[float | None]]


class SessionHeartbeatWakeQueue:
    def __init__(self, *, handler: WakeHandler) -> None:
        self._handler = handler
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: dict[str, float] = {}
        self._closed = False

    def request(self, session_id: str, *, delay_s: float = 0.25) -> bool:
        key = str(session_id or '').strip()
        if self._closed or not key:
            return False
        delay = max(0.0, float(delay_s or 0.0))
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            current = self._pending.get(key)
            if current is None or delay < current:
                self._pending[key] = delay
            return False
        try:
            task = asyncio.create_task(self._run(key, initial_delay=delay))
        except RuntimeError:
            return False
        self._tasks[key] = task

        def _cleanup(done_task: asyncio.Task[None]) -> None:
            current = self._tasks.get(key)
            if current is done_task:
                self._tasks.pop(key, None)
            self._pending.pop(key, None)

        task.add_done_callback(_cleanup)
        return True

    async def _run(self, session_id: str, *, initial_delay: float) -> None:
        delay = initial_delay
        while not self._closed:
            if delay > 0:
                await asyncio.sleep(delay)
            next_delay = await self._handler(session_id)
            pending_delay = self._pending.pop(session_id, None)
            if pending_delay is not None:
                if next_delay is None:
                    delay = pending_delay
                    continue
                next_delay = min(float(next_delay or 0.0), pending_delay)
            if next_delay is None:
                return
            delay = max(0.0, float(next_delay or 0.0))

    def clear_session(self, session_id: str) -> None:
        key = str(session_id or '').strip()
        self._pending.pop(key, None)
        task = self._tasks.pop(key, None)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._pending.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
