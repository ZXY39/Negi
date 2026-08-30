from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class NodeErrorScanner:
    """Poll persisted error-paused nodes and enqueue each error once."""

    def __init__(self, *, main_task_service: Any, heartbeat: Any, interval_seconds: float = 60.0) -> None:
        self._main_task_service = main_task_service
        self._heartbeat = heartbeat
        self._interval_seconds = max(1.0, float(interval_seconds or 60.0))
        self._task: asyncio.Task[Any] | None = None
        self._stopped = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self.run_forever(), name='heartbeat-node-error-scanner')

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run_forever(self) -> None:
        # Scan immediately so restart recovery does not wait for the first interval.
        await self.scan_once()
        while not self._stopped:
            await asyncio.sleep(self._interval_seconds)
            await self.scan_once()

    async def scan_once(self) -> int:
        store = getattr(self._main_task_service, 'store', None)
        list_pending = getattr(store, 'list_new_error_pauses', None)
        enqueue = getattr(self._heartbeat, 'enqueue_task_node_error_payload', None)
        origin_getter = getattr(self._main_task_service, '_task_origin_session_id', None)
        if not callable(list_pending) or not callable(enqueue):
            return 0
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for raw in list(list_pending() or []):
            item = raw.model_dump(mode='json') if hasattr(raw, 'model_dump') else dict(raw or {})
            task_id = str(item.get('task_id') or '').strip()
            node_id = str(item.get('node_id') or '').strip()
            if not task_id or not node_id:
                continue
            task = self._main_task_service.get_task(task_id) if hasattr(self._main_task_service, 'get_task') else None
            session_id = str(origin_getter(task) if callable(origin_getter) else getattr(task, 'session_id', 'web:shared') or 'web:shared').strip() or 'web:shared'
            pause_id = int(item.get('id') or 0)
            groups[session_id].append({
                'task_id': task_id,
                'task_title': str(getattr(task, 'title', '') or task_id),
                'node_id': node_id,
                'node_title': str(item.get('node_title') or node_id),
                'pause_reason': str(item.get('pause_reason') or 'error'),
                'remark': str(item.get('remark') or ''),
                'error_text': str(item.get('error_text') or ''),
                'pause_row_id': pause_id,
                'dedupe_key': f'node-error:{task_id}:{node_id}:{pause_id}',
            })
        delivered = 0
        for session_id, items in groups.items():
            try:
                accepted = bool(enqueue(session_id, items))
            except Exception:
                accepted = False
            if not accepted:
                continue
            for item in items:
                pause_id = int(item.get('pause_row_id') or 0)
                if pause_id <= 0:
                    continue
                try:
                    store.mark_task_node_pause_delivered(pause_id)
                    delivered += 1
                except Exception:
                    continue
        return delivered
