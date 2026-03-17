from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class TaskEventRegistry:
    def __init__(self) -> None:
        self._ceo_subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._task_subscribers: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._ceo_global_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._global_task_subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._ceo_seq: dict[str, int] = defaultdict(int)
        self._task_seq: dict[tuple[str, str], int] = defaultdict(int)
        self._global_task_seq: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def subscribe_ceo(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._ceo_subscribers[str(session_id or 'web:shared')].add(queue)
        return queue

    async def unsubscribe_ceo(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            key = str(session_id or 'web:shared')
            queues = self._ceo_subscribers.get(key)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    self._ceo_subscribers.pop(key, None)

    async def subscribe_global_ceo(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._ceo_global_subscribers.add(queue)
        return queue

    async def unsubscribe_global_ceo(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._ceo_global_subscribers.discard(queue)

    async def subscribe_task(self, session_id: str, task_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._task_subscribers[(str(session_id or 'web:shared'), str(task_id or ''))].add(queue)
        return queue

    async def unsubscribe_task(self, session_id: str, task_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            key = (str(session_id or 'web:shared'), str(task_id or ''))
            queues = self._task_subscribers.get(key)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    self._task_subscribers.pop(key, None)

    async def subscribe_global_task(self, task_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._global_task_subscribers[str(task_id or '')].add(queue)
        return queue

    async def unsubscribe_global_task(self, task_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            key = str(task_id or '')
            queues = self._global_task_subscribers.get(key)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    self._global_task_subscribers.pop(key, None)

    def next_ceo_seq(self, session_id: str) -> int:
        key = str(session_id or 'web:shared')
        self._ceo_seq[key] += 1
        return self._ceo_seq[key]

    def next_task_seq(self, session_id: str, task_id: str) -> int:
        key = (str(session_id or 'web:shared'), str(task_id or ''))
        self._task_seq[key] += 1
        return self._task_seq[key]

    def next_global_task_seq(self, task_id: str) -> int:
        key = str(task_id or '')
        self._global_task_seq[key] += 1
        return self._global_task_seq[key]

    def publish_ceo(self, session_id: str, payload: dict[str, Any]) -> None:
        key = str(session_id or 'web:shared')
        for queue in list(self._ceo_subscribers.get(key, set())):
            queue.put_nowait(dict(payload))

    def publish_global_ceo(self, payload: dict[str, Any]) -> None:
        for queue in list(self._ceo_global_subscribers):
            queue.put_nowait(dict(payload))

    def publish_task(self, session_id: str, task_id: str, payload: dict[str, Any]) -> None:
        key = (str(session_id or 'web:shared'), str(task_id or ''))
        for queue in list(self._task_subscribers.get(key, set())):
            queue.put_nowait(dict(payload))

    def publish_global_task(self, task_id: str, payload: dict[str, Any]) -> None:
        key = str(task_id or '')
        for queue in list(self._global_task_subscribers.get(key, set())):
            queue.put_nowait(dict(payload))

    async def close(self) -> None:
        async with self._lock:
            self._ceo_subscribers.clear()
            self._task_subscribers.clear()
            self._ceo_global_subscribers.clear()
            self._global_task_subscribers.clear()
            self._ceo_seq.clear()
            self._task_seq.clear()
            self._global_task_seq.clear()
