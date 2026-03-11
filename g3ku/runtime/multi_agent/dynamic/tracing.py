from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class TraceContext:
    parent_session_id: str | None = None
    current_session_id: str | None = None
    task_id: str | None = None
    category: str | None = None
    run_mode: str | None = None
    lifecycle_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "parent_session_id": self.parent_session_id or "",
            "current_session_id": self.current_session_id or "",
            "task_id": self.task_id or "",
            "category": self.category or "",
            "run_mode": self.run_mode or "",
            "lifecycle_status": self.lifecycle_status or "",
        }
        return {key: value for key, value in data.items() if value != ""}


def trace_payload(context: TraceContext | None = None, **extra: Any) -> dict[str, Any]:
    payload = context.as_dict() if context is not None else {}
    for key, value in extra.items():
        if value is not None:
            payload[key] = value
    return payload
