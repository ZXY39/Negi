from __future__ import annotations

import json
from typing import Any

from g3ku.runtime.tool_history import extract_call_id

STAGE_COMPACT_PREFIX = "[G3KU_STAGE_COMPACT_V1]"
STAGE_EXTERNALIZED_PREFIX = "[G3KU_STAGE_EXTERNALIZED_V1]"
STAGE_RAW_PREFIX = "[G3KU_STAGE_RAW_V1]"


def _stage_get(stage: Any, key: str, default: Any = None) -> Any:
    if isinstance(stage, dict):
        return stage.get(key, default)
    return getattr(stage, key, default)


def _message_role(message: dict[str, Any]) -> str:
    return str((message or {}).get("role") or "").strip().lower()


def _normalize_key_ref(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    if isinstance(item, dict):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    try:
        return dict(item)
    except Exception:
        return None


def is_stage_context_message(message: dict[str, Any]) -> bool:
    if _message_role(message) != "assistant":
        return False
    content = str((message or {}).get("content") or "")
    return (
        content.startswith(STAGE_COMPACT_PREFIX)
        or content.startswith(STAGE_EXTERNALIZED_PREFIX)
        or content.startswith(STAGE_RAW_PREFIX)
    )


def stage_prompt_prefix(
    messages: list[dict[str, Any]],
    *,
    preserve_leading_system: bool = True,
    preserve_leading_user: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cleaned = [
        dict(item)
        for item in list(messages or [])
        if isinstance(item, dict) and not is_stage_context_message(item)
    ]
    prefix: list[dict[str, Any]] = []
    remainder = list(cleaned)
    if preserve_leading_system and remainder and _message_role(remainder[0]) == "system":
        prefix.append(remainder.pop(0))
    if preserve_leading_user and remainder and _message_role(remainder[0]) == "user":
        prefix.append(remainder.pop(0))
    return prefix, remainder


def retained_completed_stage_ids(stage_state: Any, *, keep_latest: int) -> set[str]:
    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    if not active_stage_id or keep_latest <= 0:
        return set()
    completed: list[tuple[int, str]] = []
    for stage in list(_stage_get(stage_state, "stages", []) or []):
        stage_id = str(_stage_get(stage, "stage_id", "") or "").strip()
        if not stage_id or stage_id == active_stage_id:
            continue
        completed.append((int(_stage_get(stage, "stage_index", 0) or 0), stage_id))
    completed.sort()
    return {stage_id for _stage_index, stage_id in completed[-max(0, int(keep_latest or 0)) :]}


def completed_stage_blocks(stage_state: Any, *, skip_stage_ids: set[str] | None = None) -> list[dict[str, Any]]:
    externalized: list[dict[str, Any]] = []
    compacted: list[dict[str, Any]] = []
    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    skipped = {
        str(item or "").strip()
        for item in list(skip_stage_ids or set())
        if str(item or "").strip()
    }
    for stage in list(_stage_get(stage_state, "stages", []) or []):
        stage_id = str(_stage_get(stage, "stage_id", "") or "").strip()
        if stage_id == active_stage_id or stage_id in skipped:
            continue
        if str(_stage_get(stage, "stage_kind", "normal") or "normal").strip() == "compression":
            payload = {
                "stage_index": int(_stage_get(stage, "stage_index", 0) or 0),
                "stage_kind": "compression",
                "system_generated": bool(_stage_get(stage, "system_generated", False)),
                "stage_goal": str(_stage_get(stage, "stage_goal", "") or ""),
                "completed_stage_summary": str(_stage_get(stage, "completed_stage_summary", "") or ""),
                "archive_ref": str(_stage_get(stage, "archive_ref", "") or ""),
                "archive_stage_index_start": int(_stage_get(stage, "archive_stage_index_start", 0) or 0),
                "archive_stage_index_end": int(_stage_get(stage, "archive_stage_index_end", 0) or 0),
            }
            externalized.append(
                {
                    "role": "assistant",
                    "content": f"{STAGE_EXTERNALIZED_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
                }
            )
            continue
        payload = {
            "stage_index": int(_stage_get(stage, "stage_index", 0) or 0),
            "stage_kind": "normal",
            "system_generated": bool(_stage_get(stage, "system_generated", False)),
            "mode": str(_stage_get(stage, "mode", "") or ""),
            "status": str(_stage_get(stage, "status", "") or ""),
            "stage_goal": str(_stage_get(stage, "stage_goal", "") or ""),
            "completed_stage_summary": str(_stage_get(stage, "completed_stage_summary", "") or ""),
            "key_refs": [
                normalized
                for normalized in (
                    _normalize_key_ref(item)
                    for item in list(_stage_get(stage, "key_refs", []) or [])
                )
                if normalized is not None
            ],
            "tool_round_budget": int(_stage_get(stage, "tool_round_budget", 0) or 0),
            "tool_rounds_used": int(_stage_get(stage, "tool_rounds_used", 0) or 0),
        }
        compacted.append(
            {
                "role": "assistant",
                "content": f"{STAGE_COMPACT_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
            }
        )
    return [*externalized, *compacted]


def repair_split_stage_tool_boundaries(
    messages: list[dict[str, Any]],
    *,
    stage_tool_name: str = "submit_next_stage",
) -> list[dict[str, Any]]:
    normalized_stage_tool_name = str(stage_tool_name or "").strip()
    if not normalized_stage_tool_name:
        return [dict(item) for item in list(messages or []) if isinstance(item, dict)]

    declared_stage_call_ids: set[str] = set()
    for message in list(messages or []):
        if _message_role(message) != "assistant":
            continue
        for tool_call in list(message.get("tool_calls") or []):
            call_id = extract_call_id((tool_call or {}).get("id"))
            function = (tool_call or {}).get("function") or {}
            tool_name = str(function.get("name") or (tool_call or {}).get("name") or "").strip()
            if call_id and tool_name == normalized_stage_tool_name:
                declared_stage_call_ids.add(call_id)

    repaired: list[dict[str, Any]] = []
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        role = _message_role(message)
        if role == "tool":
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            call_id = extract_call_id(tool_call_id)
            tool_name = str(message.get("name") or "").strip()
            if call_id and tool_name == normalized_stage_tool_name and call_id not in declared_stage_call_ids:
                repaired.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": tool_call_id or call_id,
                                "type": "function",
                                "function": {
                                    "name": normalized_stage_tool_name,
                                    # Preserve tool-call pairing even when the original
                                    # assistant half was lost during history rewriting.
                                    "arguments": "{}",
                                },
                            }
                        ],
                    }
                )
                declared_stage_call_ids.add(call_id)
        repaired.append(dict(message))
    return repaired


def current_stage_active_window(
    messages: list[dict[str, Any]],
    *,
    keep_completed_stages: int = 0,
    stage_tool_name: str = "submit_next_stage",
) -> list[dict[str, Any]]:
    message_list = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
    successful_stage_boundaries: list[int] = []
    pending_stage_call_ids: dict[str, int] = {}
    for index, message in enumerate(message_list):
        role = _message_role(message)
        if role == "assistant":
            for tool_call in list(message.get("tool_calls") or []):
                call_id = extract_call_id((tool_call or {}).get("id"))
                function = (tool_call or {}).get("function") or {}
                tool_name = str(function.get("name") or (tool_call or {}).get("name") or "").strip()
                if call_id and tool_name == stage_tool_name:
                    pending_stage_call_ids[call_id] = index
            continue
        if role != "tool":
            continue
        tool_call_id = extract_call_id(message.get("tool_call_id"))
        if (
            tool_call_id
            and tool_call_id in pending_stage_call_ids
            and str(message.get("name") or "").strip() == stage_tool_name
            and not str(message.get("content") or "").strip().startswith("Error:")
        ):
            successful_stage_boundaries.append(pending_stage_call_ids[tool_call_id])
            pending_stage_call_ids.clear()
    if not successful_stage_boundaries:
        return message_list
    keep_completed = max(0, int(keep_completed_stages or 0))
    boundary_index = max(0, len(successful_stage_boundaries) - 1 - keep_completed)
    stage_boundary = successful_stage_boundaries[boundary_index]
    return [dict(item) for item in message_list[stage_boundary:]]


def prepare_stage_prompt_messages(
    messages: list[dict[str, Any]],
    *,
    stage_state: Any,
    keep_latest_completed_stages: int = 3,
    stage_tool_name: str = "submit_next_stage",
    preserve_leading_system: bool = True,
    preserve_leading_user: bool = True,
) -> list[dict[str, Any]]:
    parts = decompose_stage_prompt_messages(
        messages,
        stage_state=stage_state,
        keep_latest_completed_stages=keep_latest_completed_stages,
        stage_tool_name=stage_tool_name,
        preserve_leading_system=preserve_leading_system,
        preserve_leading_user=preserve_leading_user,
    )
    return [
        *list(parts["prefix"]),
        *list(parts["completed_blocks"]),
        *list(parts["active_window"]),
    ]


def decompose_stage_prompt_messages(
    messages: list[dict[str, Any]],
    *,
    stage_state: Any,
    keep_latest_completed_stages: int = 3,
    stage_tool_name: str = "submit_next_stage",
    preserve_leading_system: bool = True,
    preserve_leading_user: bool = True,
) -> dict[str, Any]:
    prefix, remainder = stage_prompt_prefix(
        messages,
        preserve_leading_system=preserve_leading_system,
        preserve_leading_user=preserve_leading_user,
    )
    remainder = repair_split_stage_tool_boundaries(remainder, stage_tool_name=stage_tool_name)
    if not list(_stage_get(stage_state, "stages", []) or []):
        return {
            "prefix": prefix,
            "remainder": remainder,
            "retained_completed_stage_ids": set(),
            "completed_blocks": [],
            "active_window": list(remainder),
            "global_zone_source": [],
        }
    retained_ids = retained_completed_stage_ids(stage_state, keep_latest=keep_latest_completed_stages)
    completed_blocks = completed_stage_blocks(stage_state, skip_stage_ids=retained_ids)
    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    active_window = (
        current_stage_active_window(
            remainder,
            keep_completed_stages=len(retained_ids),
            stage_tool_name=stage_tool_name,
        )
        if active_stage_id
        else list(remainder)
    )
    global_zone_length = max(0, len(remainder) - len(active_window))
    return {
        "prefix": prefix,
        "remainder": remainder,
        "retained_completed_stage_ids": retained_ids,
        "completed_blocks": completed_blocks,
        "active_window": active_window,
        "global_zone_source": [dict(item) for item in remainder[:global_zone_length]],
    }


__all__ = [
    "STAGE_COMPACT_PREFIX",
    "STAGE_EXTERNALIZED_PREFIX",
    "STAGE_RAW_PREFIX",
    "completed_stage_blocks",
    "current_stage_active_window",
    "decompose_stage_prompt_messages",
    "is_stage_context_message",
    "prepare_stage_prompt_messages",
    "repair_split_stage_tool_boundaries",
    "retained_completed_stage_ids",
    "stage_prompt_prefix",
]
