from __future__ import annotations

from pathlib import Path
from typing import Any

from main.service.task_stall_callback import _replace_callback_path
from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_PATH,
    resolve_task_terminal_callback_token,
    resolve_task_terminal_callback_url,
)


TASK_DISTRIBUTION_ERROR_CALLBACK_PATH = "/api/internal/task-distribution-error"


def resolve_task_distribution_error_callback_url(*, workspace: Path | str | None = None) -> str:
    terminal_url = resolve_task_terminal_callback_url(workspace=workspace)
    return _replace_callback_path(
        terminal_url,
        expected_path=TASK_TERMINAL_CALLBACK_PATH,
        target_path=TASK_DISTRIBUTION_ERROR_CALLBACK_PATH,
    )


def resolve_task_distribution_error_callback_token(*, workspace: Path | str | None = None) -> str:
    return resolve_task_terminal_callback_token(workspace=workspace)


def build_task_distribution_error_dedupe_key(*, task_id: str, epoch_id: str) -> str:
    return f"task-distribution-error:{str(task_id or '').strip()}:{str(epoch_id or '').strip()}"


def _normalize_task_id(task_id: Any) -> str:
    text = str(task_id or "").strip()
    if text and not text.startswith("task:") and ":" not in text:
        return f"task:{text}"
    return text


def normalize_task_distribution_error_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    task_id = _normalize_task_id(source.get("task_id") or source.get("taskId"))
    epoch_id = str(source.get("epoch_id") or source.get("epochId") or "").strip()
    if not task_id:
        return {}
    session_id = str(source.get("session_id") or source.get("sessionId") or "").strip() or "web:shared"
    title = str(source.get("title") or task_id).strip() or task_id
    error_text = str(source.get("error_text") or source.get("errorText") or "").strip()
    # The dedupe key is always recomputed server-side so caller-supplied variants
    # cannot bypass exact-key dedupe; one notification per failed epoch at most.
    dedupe_key = build_task_distribution_error_dedupe_key(task_id=task_id, epoch_id=epoch_id)
    return {
        "dedupe_key": dedupe_key,
        "task_id": task_id,
        "session_id": session_id,
        "title": title,
        "epoch_id": epoch_id,
        "error_text": error_text[:2000],
    }