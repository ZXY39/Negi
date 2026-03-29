from __future__ import annotations

from typing import Any

from main.protocol import now_iso


class TaskEventWriter:
    def __init__(self, *, store) -> None:
        self._store = store

    def append_task_event(self, *, task_id: str | None, session_id: str, event_type: str, data: dict[str, Any]) -> int:
        return self._store.append_task_event(
            task_id=task_id,
            session_id=session_id,
            event_type=event_type,
            created_at=now_iso(),
            payload=data,
        )

    def append_task_model_call(
        self,
        *,
        task_id: str,
        node_id: str,
        created_at: str,
        payload: dict[str, Any],
    ) -> int:
        return self._store.append_task_model_call(
            task_id=task_id,
            node_id=node_id,
            created_at=created_at,
            payload=payload,
        )
