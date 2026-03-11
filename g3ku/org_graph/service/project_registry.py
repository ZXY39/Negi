from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ProjectControl:
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.pause_event.set()


class ProjectRegistry:
    def __init__(self):
        self._ceo_subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._project_subscribers: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = {}
        self._project_tasks: dict[str, asyncio.Task[Any]] = {}
        self._controls: dict[str, ProjectControl] = {}
        self._lock = asyncio.Lock()

    async def subscribe_ceo(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._ceo_subscribers.setdefault(session_id, set()).add(queue)
        return queue

    async def unsubscribe_ceo(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            queues = self._ceo_subscribers.get(session_id)
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    self._ceo_subscribers.pop(session_id, None)

    async def subscribe_project(self, session_id: str, project_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        async with self._lock:
            self._project_subscribers.setdefault((session_id, project_id), set()).add(queue)
        return queue

    async def unsubscribe_project(self, session_id: str, project_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            queues = self._project_subscribers.get((session_id, project_id))
            if queues is not None:
                queues.discard(queue)
                if not queues:
                    self._project_subscribers.pop((session_id, project_id), None)

    async def publish_ceo(self, session_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._ceo_subscribers.get(session_id, set()))
        for queue in targets:
            await queue.put(dict(payload))

    async def publish_project(self, session_id: str, project_id: str, payload: dict[str, Any]) -> None:
        async with self._lock:
            targets = list(self._project_subscribers.get((session_id, project_id), set()))
        for queue in targets:
            await queue.put(dict(payload))

    async def register_task(self, project_id: str, task: asyncio.Task[Any]) -> None:
        async with self._lock:
            self._project_tasks[project_id] = task
            self._controls.setdefault(project_id, ProjectControl())

    async def clear_task(self, project_id: str) -> None:
        async with self._lock:
            self._project_tasks.pop(project_id, None)

    async def task_for(self, project_id: str) -> asyncio.Task[Any] | None:
        async with self._lock:
            return self._project_tasks.get(project_id)

    async def control_for(self, project_id: str) -> ProjectControl:
        async with self._lock:
            return self._controls.setdefault(project_id, ProjectControl())

    async def pause(self, project_id: str) -> None:
        control = await self.control_for(project_id)
        control.pause_event.clear()

    async def resume(self, project_id: str) -> None:
        control = await self.control_for(project_id)
        control.pause_event.set()

    async def cancel(self, project_id: str) -> None:
        control = await self.control_for(project_id)
        control.cancel_event.set()
        control.pause_event.set()
        task = await self.task_for(project_id)
        if task is not None:
            task.cancel()

    async def wait_until_resumed(self, project_id: str) -> None:
        control = await self.control_for(project_id)
        await control.pause_event.wait()
        if control.cancel_event.is_set():
            raise asyncio.CancelledError(project_id)

    async def is_canceled(self, project_id: str) -> bool:
        control = await self.control_for(project_id)
        return control.cancel_event.is_set()

    async def purge_project(self, project_id: str, payload: dict[str, Any] | None = None) -> None:
        async with self._lock:
            task = self._project_tasks.pop(project_id, None)
            self._controls.pop(project_id, None)
            subscriber_items = [
                (key, queues)
                for key, queues in self._project_subscribers.items()
                if key[1] == project_id
            ]
            for key, _ in subscriber_items:
                self._project_subscribers.pop(key, None)
        if payload is not None:
            for _, queues in subscriber_items:
                for queue in list(queues):
                    await queue.put(dict(payload))
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._project_tasks.values())
            self._project_tasks.clear()
            self._ceo_subscribers.clear()
            self._project_subscribers.clear()
            self._controls.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
