from __future__ import annotations

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


class SessionHeartbeatEventQueue:
    def __init__(self) -> None:
        self._events: dict[str, list[SessionHeartbeatEvent]] = {}
        self._dedupe: dict[str, set[str]] = {}

    def enqueue(
        self,
        *,
        session_id: str,
        source: str,
        reason: str,
        dedupe_key: str,
        payload: dict[str, Any] | None = None,
    ) -> SessionHeartbeatEvent | None:
        key = str(session_id or '').strip()
        dedupe = str(dedupe_key or '').strip()
        if not key or not dedupe:
            return None
        session_dedupe = self._dedupe.setdefault(key, set())
        if dedupe in session_dedupe:
            return None
        event = SessionHeartbeatEvent(
            event_id=f'hb:{uuid4().hex}',
            session_id=key,
            source=str(source or 'runtime').strip() or 'runtime',
            reason=str(reason or 'task_terminal').strip() or 'task_terminal',
            created_at=now_iso(),
            dedupe_key=dedupe,
            payload=dict(payload or {}),
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

    def clear_session(self, session_id: str) -> None:
        key = str(session_id or '').strip()
        if not key:
            return
        self._events.pop(key, None)
        self._dedupe.pop(key, None)

    def clear_all(self) -> None:
        self._events.clear()
        self._dedupe.clear()

    def has_events(self, session_id: str) -> bool:
        key = str(session_id or '').strip()
        return bool(self._events.get(key))

    def session_ids(self) -> list[str]:
        return sorted(key for key, events in self._events.items() if events)
