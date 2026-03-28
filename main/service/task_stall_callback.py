from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_PATH,
    resolve_task_terminal_callback_token,
    resolve_task_terminal_callback_url,
)


TASK_STALL_CALLBACK_PATH = "/api/internal/task-stall"
TASK_STALL_REASON_SUSPECTED_STALL = "suspected_stall"
TASK_STALL_REASON_USER_PAUSED = "user_paused"
TASK_STALL_REASON_WORKER_UNAVAILABLE = "worker_unavailable"
TASK_STALL_REASON_CANCEL_REQUESTED = "cancel_requested"
TASK_STALL_REASON_NOT_IN_PROGRESS = "not_in_progress"
TASK_STALL_REASON_MISSING_TASK = "missing_task"
_TASK_STALL_REASONS = {
    TASK_STALL_REASON_SUSPECTED_STALL,
    TASK_STALL_REASON_USER_PAUSED,
    TASK_STALL_REASON_WORKER_UNAVAILABLE,
    TASK_STALL_REASON_CANCEL_REQUESTED,
    TASK_STALL_REASON_NOT_IN_PROGRESS,
    TASK_STALL_REASON_MISSING_TASK,
}


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


def resolve_task_stall_callback_url(*, workspace: Path | str | None = None) -> str:
    terminal_url = resolve_task_terminal_callback_url(workspace=workspace)
    return _replace_callback_path(
        terminal_url,
        expected_path=TASK_TERMINAL_CALLBACK_PATH,
        target_path=TASK_STALL_CALLBACK_PATH,
    )


def resolve_task_stall_callback_token(*, workspace: Path | str | None = None) -> str:
    return resolve_task_terminal_callback_token(workspace=workspace)


def build_task_stall_dedupe_key(*, task_id: str, bucket_minutes: int, last_visible_output_at: str) -> str:
    return (
        f"task-stall:{str(task_id or '').strip()}:"
        f"{max(0, int(bucket_minutes or 0))}:{str(last_visible_output_at or '').strip()}"
    )


def _normalize_task_id(task_id: Any) -> str:
    text = str(task_id or "").strip()
    if text and not text.startswith("task:") and ":" not in text:
        return f"task:{text}"
    return text


def _coerce_positive_int(value: Any, *, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default or 0))


def _normalize_iso(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def normalize_task_stall_reason(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in _TASK_STALL_REASONS else TASK_STALL_REASON_SUSPECTED_STALL


def normalize_task_stall_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    task_id = _normalize_task_id(source.get("task_id") or source.get("taskId"))
    session_id = str(source.get("session_id") or source.get("sessionId") or "").strip() or "web:shared"
    title = str(source.get("title") or task_id).strip() or task_id
    last_visible_output_at = _normalize_iso(
        source.get("last_visible_output_at") or source.get("lastVisibleOutputAt")
    )
    bucket_minutes = _coerce_positive_int(
        source.get("bucket_minutes") or source.get("bucketMinutes"),
        default=0,
    )
    stalled_minutes = _coerce_positive_int(
        source.get("stalled_minutes") or source.get("stalledMinutes"),
        default=bucket_minutes,
    )
    if not task_id or not last_visible_output_at or bucket_minutes <= 0:
        return {}
    dedupe_key = str(source.get("dedupe_key") or source.get("dedupeKey") or "").strip()
    if not dedupe_key:
        dedupe_key = build_task_stall_dedupe_key(
            task_id=task_id,
            bucket_minutes=bucket_minutes,
            last_visible_output_at=last_visible_output_at,
        )
    return {
        "dedupe_key": dedupe_key,
        "task_id": task_id,
        "session_id": session_id,
        "title": title,
        "stalled_minutes": stalled_minutes,
        "bucket_minutes": bucket_minutes,
        "last_visible_output_at": last_visible_output_at,
        "reason": normalize_task_stall_reason(source.get("reason")),
        "brief_text": str(source.get("brief_text") or source.get("briefText") or "").strip(),
        "latest_node_summary": str(
            source.get("latest_node_summary") or source.get("latestNodeSummary") or ""
        ).strip(),
        "runtime_summary_excerpt": str(
            source.get("runtime_summary_excerpt") or source.get("runtimeSummaryExcerpt") or ""
        ).strip(),
    }
