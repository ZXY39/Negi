from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_PATH,
    resolve_task_terminal_callback_token,
    resolve_task_terminal_callback_url,
)


TASK_EVENT_CALLBACK_PATH = "/api/internal/task-event"
_ALLOWED_TASK_EVENT_TYPES = {"task.snapshot", "task.list.patch", "task.worker.status"}


def _replace_callback_path(url: str, *, expected_path: str, target_path: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    path = str(parsed.path or "").strip()
    if path.endswith(expected_path):
        next_path = f"{path[: -len(expected_path)]}{target_path}"
    elif not path or path == "/":
        next_path = target_path
    else:
        return ""
    return urlunparse(parsed._replace(path=next_path))


def resolve_task_event_callback_url(*, workspace: Path | str | None = None) -> str:
    terminal_url = resolve_task_terminal_callback_url(workspace=workspace)
    return _replace_callback_path(
        terminal_url,
        expected_path=TASK_TERMINAL_CALLBACK_PATH,
        target_path=TASK_EVENT_CALLBACK_PATH,
    )


def resolve_task_event_callback_token(*, workspace: Path | str | None = None) -> str:
    return resolve_task_terminal_callback_token(workspace=workspace)


def _normalize_task_id(value: Any) -> str:
    text = str(value or "").strip()
    if text and not text.startswith("task:") and ":" not in text:
        return f"task:{text}"
    return text


def normalize_task_event_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    event_type = str(
        source.get("event_type")
        or source.get("eventType")
        or source.get("type")
        or ""
    ).strip()
    if event_type not in _ALLOWED_TASK_EVENT_TYPES:
        return {}
    raw_data = source.get("data")
    data = dict(raw_data) if isinstance(raw_data, dict) else {}
    session_id = str(source.get("session_id") or source.get("sessionId") or "").strip()
    task_id = _normalize_task_id(source.get("task_id") or source.get("taskId") or "")

    if event_type == "task.snapshot":
        if not task_id:
            return {}
        if not session_id:
            session_id = str((data.get("task") or {}).get("session_id") or "web:shared").strip() or "web:shared"
        return {
            "event_type": event_type,
            "session_id": session_id,
            "task_id": task_id,
            "data": data,
        }

    if event_type == "task.list.patch":
        task_payload = dict(data.get("task") or {}) if isinstance(data.get("task"), dict) else {}
        task_id = _normalize_task_id(task_payload.get("task_id") or task_payload.get("taskId") or task_id)
        if not task_id:
            return {}
        if not session_id:
            session_id = str(task_payload.get("session_id") or task_payload.get("sessionId") or "web:shared").strip() or "web:shared"
        return {
            "event_type": event_type,
            "session_id": session_id,
            "task_id": task_id,
            "data": {"task": task_payload},
        }

    worker_payload = dict(data.get("worker") or {}) if isinstance(data.get("worker"), dict) else None
    normalized: dict[str, Any] = {
        "event_type": event_type,
        "session_id": session_id or "all",
        "task_id": "",
        "data": {
            "worker_online": data.get("worker_online") is not False,
            "worker_stale_after_seconds": data.get("worker_stale_after_seconds"),
        },
    }
    if worker_payload is not None:
        normalized["data"]["worker"] = worker_payload
    return normalized
