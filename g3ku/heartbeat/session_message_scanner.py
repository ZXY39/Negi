from __future__ import annotations

import asyncio
from typing import Any


class SessionMessageScanner:
    """Poll the peer-session inbox and wake recipients for pending rows.

    与 FrontdoorMessageScanner 同形：消费点不在 enqueue，heartbeat 在事件束
    成功注入上下文后才 mark_frontdoor_session_message_consumed；这里保持
    pending，注入失败下一轮重试，队列 in-flight dedupe 防重复入队。

    两道闸只在本车道：
    - 暂停闸（A5=A 同档）：目标会话暂停（驻留状态或持久暂停侧车）时不入队，
      行留 pending，恢复后下一拍入队。不改心跳对暂停会话的全局行为。
    - hop 闸：wakeable=False（续链到顶）的行入队但不请求唤醒，随下一次
      别的唤醒并入上下文。
    """

    def __init__(
        self,
        *,
        main_task_service: Any,
        heartbeat: Any,
        runtime_manager: Any,
        session_manager: Any,
        interval_seconds: float = 15.0,
    ) -> None:
        self._main_task_service = main_task_service
        self._heartbeat = heartbeat
        self._runtime_manager = runtime_manager
        self._session_manager = session_manager
        self._interval_seconds = max(1.0, float(interval_seconds or 15.0))
        self._task: asyncio.Task[Any] | None = None
        self._stopped = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._task = asyncio.create_task(self.run_forever(), name='heartbeat-session-message-scanner')

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

    def _session_is_paused(self, key: str) -> bool:
        getter = getattr(self._runtime_manager, 'get', None)
        session = getter(key) if callable(getter) else None
        if session is not None:
            state = getattr(session, 'state', None)
            if bool(getattr(state, 'paused', False)):
                return True
            if str(getattr(state, 'status', '') or '').strip().lower() == 'paused':
                return True
        try:
            from g3ku.runtime.web_ceo_sessions import read_paused_execution_context

            return read_paused_execution_context(key) is not None
        except Exception:
            return False

    def _source_title(self, key: str) -> str:
        try:
            session = self._session_manager.get_or_create(key)
            metadata = getattr(session, 'metadata', None) or {}
            title = str(metadata.get('title') or '').strip()
            if title:
                return title
        except Exception:
            pass
        if key.startswith('ext:'):
            try:
                from g3ku.runtime.external_sessions import get_external_session_registry

                entry = get_external_session_registry(self._session_manager.workspace).get_by_session_key(key)
                if entry is not None:
                    return str(getattr(entry, 'title', '') or '').strip()
            except Exception:
                pass
        return ''

    async def scan_once(self) -> int:
        store = getattr(self._main_task_service, 'store', None)
        list_pending = getattr(store, 'list_pending_frontdoor_session_messages', None)
        enqueue = getattr(self._heartbeat, 'enqueue_session_message_payload', None)
        if not callable(list_pending) or not callable(enqueue):
            return 0
        rows = list(list_pending(limit=500) or [])
        delivered = 0
        for row in rows:
            message_id = str(getattr(row, 'message_id', '') or '').strip()
            session_id = str(getattr(row, 'session_id', '') or '').strip()
            source_key = str(getattr(row, 'source_session_key', '') or '').strip()
            if not message_id or not session_id:
                continue
            if self._session_is_paused(session_id):
                continue
            payload_extra = dict(getattr(row, 'payload', None) or {})
            source_alias = str(payload_extra.get('source_alias') or '').strip()
            if not source_alias:
                try:
                    source_alias = str(store.get_session_alias(source_key) or '').strip()
                except Exception:
                    source_alias = ''
            try:
                from g3ku.runtime.session_trust import session_trust

                trust = session_trust(source_key)
            except Exception:
                trust = ''
            payload = {
                'message_id': message_id,
                'source_session_key': source_key,
                'source_alias': source_alias,
                'source_title': self._source_title(source_key),
                'source_trust': trust,
                'content': str(getattr(row, 'content', '') or ''),
                'ref': str(getattr(row, 'ref', '') or ''),
                'chain_id': str(payload_extra.get('chain_id') or ''),
                'hop': int(payload_extra.get('hop') or 0),
                'reply_to': str(payload_extra.get('reply_to') or ''),
                'wakeable': bool(payload_extra.get('wakeable', True)),
            }
            try:
                accepted = bool(enqueue(session_id, payload))
            except Exception:
                continue
            if not accepted:
                continue
            delivered += 1
        return delivered
