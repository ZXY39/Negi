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
    # notice_kind 决定会话侧读的是哪一套文案：failed（任务暂停）还是 skipped（分发已完成、
    # 只有若干支被降级）。缺省按 failed，保持旧 payload 的行为。
    # 键名避开 kind：那是 prompt lane 解析事件 reason 的中间优先级键
    # （`g3ku/heartbeat/prompt_lane.py`，顺序 event_reason → kind → reason）。真实投递路径
    # 总会补上 event_reason（`g3ku/heartbeat/session_service.py` 的事件富化），所以带 kind
    # 不会顶掉 reason；但载荷与事件同键存放时，离线/手工构造的字典会把档位误读成事件类型。
    notice_kind = str(source.get("notice_kind") or source.get("kind") or "").strip().lower() or "failed"
    root_message = str(source.get("root_message") or source.get("rootMessage") or "").strip()
    skipped_items: list[dict[str, str]] = []
    for raw in list(source.get("skipped") or []):
        if not isinstance(raw, dict):
            continue
        node_id = str(raw.get("node_id") or "").strip()
        if not node_id:
            continue
        skipped_items.append(
            {
                "node_id": node_id,
                "reason": str(raw.get("reason") or "").strip()[:200] or "distribution turn failed",
            }
        )
    # The dedupe key is always recomputed server-side so caller-supplied variants
    # cannot bypass exact-key dedupe; one notification per failed epoch at most.
    dedupe_key = build_task_distribution_error_dedupe_key(task_id=task_id, epoch_id=epoch_id)
    normalized = {
        "dedupe_key": dedupe_key,
        "task_id": task_id,
        "session_id": session_id,
        "title": title,
        "epoch_id": epoch_id,
        "notice_kind": notice_kind,
        "error_text": error_text[:2000],
    }
    if notice_kind == "skipped":
        normalized["root_message"] = root_message[:4000]
        normalized["skipped"] = skipped_items[:40]
        normalized["skipped_count"] = len(skipped_items)
    return normalized