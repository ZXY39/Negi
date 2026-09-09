from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger


WakeHandler = Callable[[str], Awaitable[float | None]]

# handler 泄漏未捕获异常时的重试间隔。wake 循环不能被单次异常杀死：任务死亡后
# 该会话排队的事件全部无人再处理，且只留一条延迟到 GC 才出现的
# "Task exception was never retrieved"，排障时几乎不可见。
_HANDLER_FAILURE_RETRY_DELAY_SECONDS = 60.0


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
            try:
                next_delay = await self._handler(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("heartbeat wake handler failed for {}", session_id)
                next_delay = _HANDLER_FAILURE_RETRY_DELAY_SECONDS
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
