from __future__ import annotations

import asyncio


class HybridScheduler:
    def __init__(self, max_parallel: int):
        max_parallel = int(max_parallel)
        self._semaphore = None if max_parallel < 0 else asyncio.Semaphore(max(1, max_parallel))

    async def run_parallel(self, coroutines, *, return_exceptions: bool = False):
        if self._semaphore is None:
            return await asyncio.gather(*coroutines, return_exceptions=return_exceptions)

        async def _guard(coro):
            async with self._semaphore:
                return await coro

        return await asyncio.gather(*[_guard(coro) for coro in coroutines], return_exceptions=return_exceptions)
