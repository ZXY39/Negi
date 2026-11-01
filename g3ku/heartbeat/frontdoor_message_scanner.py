from __future__ import annotations

import asyncio
from typing import Any


class FrontdoorMessageScanner:
    """Poll the durable frontdoor inbox and wake the session for pending rows."""

    def __init__(self, *, main_task_service: Any, heartbeat: Any, interval_seconds: float = 15.0) -> None:
        self._main_task_service = main_task_service
        self._heartbeat = heartbeat
        self._interval_seconds = max(1.0, float(interval_seconds or 15.0))
        self._task: asyncio.Task[Any] | None = None
        self._stopped = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self.run_forever(), name='heartbeat-frontdoor-message-scanner')

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run_forever(self) -> None:
        await self.scan_once()
        while not self._stopped:
            await asyncio.sleep(self._interval_seconds)
            await self.scan_once()

    async def scan_once(self) -> int:
        store = getattr(self._main_task_service, 'store', None)
        list_pending = getattr(store, 'list_pending_frontdoor_node_messages', None)
        enqueue = getattr(self._heartbeat, 'enqueue_frontdoor_node_message_payload', None)
        if not callable(list_pending) or not callable(enqueue):
            return 0
        rows = list(list_pending(limit=500) or [])
        delivered = 0
        for row in rows:
            message_id = str(getattr(row, 'message_id', '') or '').strip()
            session_id = str(getattr(row, 'session_id', '') or '').strip()
            task_id = str(getattr(row, 'task_id', '') or '').strip()
            source_node_id = str(getattr(row, 'source_node_id', '') or '').strip()
            if not message_id or not session_id:
                continue
            task = None
            node = None
            try:
                task = self._main_task_service.get_task(task_id)
            except Exception:
                task = None
            try:
                node = self._main_task_service.get_node(source_node_id)
            except Exception:
                node = None
            payload = {
                'message_id': message_id,
                'task_id': task_id,
                'task_title': str(getattr(task, 'title', '') or task_id),
                'source_node_id': source_node_id,
                'source_goal': str(getattr(node, 'goal', '') or '') if node is not None else '',
                'content': str(getattr(row, 'content', '') or ''),
                'ref': str(getattr(row, 'ref', '') or ''),
            }
            try:
                accepted = bool(enqueue(session_id, payload))
            except Exception:
                continue
            if not accepted:
                continue
            # 消费点不在 enqueue：heartbeat 在事件束成功注入上下文后才
            # mark_frontdoor_node_message_consumed。这里保持 pending，注入失败下一轮重试；
            # 队列的 in-flight dedupe 防止同一 message 在两次扫描间重复入队。
            delivered += 1
        return delivered
