from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from main.protocol import now_iso


@dataclass(slots=True)
class SessionHeartbeatEvent:
    event_id: str
    session_id: str
    source: str
    reason: str
    created_at: str
    dedupe_key: str
    payload: dict[str, Any]
    ready_at_monotonic: float


class SessionHeartbeatEventQueue:
    def __init__(self) -> None:
        self._events: dict[str, list[SessionHeartbeatEvent]] = {}
        self._dedupe: dict[str, set[str]] = {}
        # 已经注入上下文的事件 key。pop 之后仍要拒绝同一 key 再入队：源表重放、
        # 扫描器重扫、worker outbox 重投都不允许把同一心跳再送进上下文一次。
        self._consumed: dict[str, set[str]] = {}

    def enqueue(
        self,
        *,
        session_id: str,
        source: str,
        reason: str,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
        delay_seconds: float = 0.0,
    ) -> SessionHeartbeatEvent | None:
        key = str(session_id or '').strip()
        dedupe = str(dedupe_key or '').strip()
        if not key or not dedupe:
            return None
        session_dedupe = self._dedupe.setdefault(key, set())
        if dedupe in session_dedupe or dedupe in self._consumed.get(key, set()):
            return None
        event = SessionHeartbeatEvent(
            event_id=f'hb:{uuid4().hex}',
            session_id=key,
            source=str(source or 'runtime').strip() or 'runtime',
            reason=str(reason or 'task_terminal').strip() or 'task_terminal',
            created_at=now_iso(),
            dedupe_key=dedupe,
            payload=dict(payload or {}),
            ready_at_monotonic=time.monotonic() + max(0.0, float(delay_seconds or 0.0)),
        )
        self._events.setdefault(key, []).append(event)
        session_dedupe.add(dedupe)
        return event

    def peek(self, session_id: str) -> list[SessionHeartbeatEvent]:
        key = str(session_id or '').strip()
        return list(self._events.get(key, []))

    def pop_many(self, session_id: str, *, event_ids: set[str]) -> list[SessionHeartbeatEvent]:
        key = str(session_id or '').strip()
        if not key or not event_ids:
            return []
        removed: list[SessionHeartbeatEvent] = []
        retained: list[SessionHeartbeatEvent] = []
        for event in self._events.get(key, []):
            if event.event_id in event_ids:
                removed.append(event)
            else:
                retained.append(event)
        if retained:
            self._events[key] = retained
        else:
            self._events.pop(key, None)
        dedupe = self._dedupe.get(key)
        if dedupe is not None:
            for event in removed:
                dedupe.discard(event.dedupe_key)
            if not dedupe:
                self._dedupe.pop(key, None)
        return removed

    def consume_many(self, session_id: str, *, event_ids: set[str]) -> list[SessionHeartbeatEvent]:
        """弹出事件并记住 dedupe key：这些事件已经进入上下文，不再允许入队。"""
        key = str(session_id or '').strip()
        removed = self.pop_many(session_id, event_ids=event_ids)
        if not key or not removed:
            return removed
        consumed = self._consumed.setdefault(key, set())
        for event in removed:
            dedupe = str(event.dedupe_key or '').strip()
            if dedupe:
                consumed.add(dedupe)
        return removed

    def remove_where(self, session_id: str, *, predicate) -> list[SessionHeartbeatEvent]:
        key = str(session_id or "").strip()
        if not key or not callable(predicate):
            return []
        removed: list[SessionHeartbeatEvent] = []
        retained: list[SessionHeartbeatEvent] = []
        for event in self._events.get(key, []):
            if predicate(event):
                removed.append(event)
            else:
                retained.append(event)
        if retained:
            self._events[key] = retained
        else:
            self._events.pop(key, None)
        dedupe = self._dedupe.get(key)
        if dedupe is not None:
            for event in removed:
                dedupe.discard(event.dedupe_key)
            if not dedupe:
                self._dedupe.pop(key, None)
        return removed

    def clear_session(self, session_id: str) -> None:
        key = str(session_id or '').strip()
        if not key:
            return
        self._events.pop(key, None)
        self._dedupe.pop(key, None)
        self._consumed.pop(key, None)

    def clear_all(self) -> None:
        self._events.clear()
        self._dedupe.clear()
        self._consumed.clear()

    def has_events(self, session_id: str) -> bool:
        key = str(session_id or '').strip()
        return bool(self._events.get(key))

    def peek_ready(self, session_id: str, *, now_monotonic: float | None = None) -> list[SessionHeartbeatEvent]:
        key = str(session_id or '').strip()
        if not key:
            return []
        now_value = time.monotonic() if now_monotonic is None else float(now_monotonic)
        return [event for event in self._events.get(key, []) if float(event.ready_at_monotonic) <= now_value]

    def next_delay(self, session_id: str, *, now_monotonic: float | None = None) -> float | None:
        key = str(session_id or '').strip()
        if not key:
            return None
        now_value = time.monotonic() if now_monotonic is None else float(now_monotonic)
        delays = [
            max(0.0, float(event.ready_at_monotonic) - now_value)
            for event in self._events.get(key, [])
            if float(event.ready_at_monotonic) > now_value
        ]
        if not delays:
            return None
        return min(delays)

    def session_ids(self) -> list[str]:
        return sorted(key for key, events in self._events.items() if events)
