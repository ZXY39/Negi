from __future__ import annotations

import copy
import json
from typing import Any

from g3ku.runtime.stage_prompt_compaction import STAGE_RAW_PREFIX


def _stage_get(stage: Any, key: str, default: Any = None) -> Any:
    if isinstance(stage, dict):
        return stage.get(key, default)
    return getattr(stage, key, default)


def _stage_list(stage_state: Any) -> list[dict[str, Any]]:
    return [
        dict(stage)
        for stage in list(_stage_get(stage_state, "stages", []) or [])
        if isinstance(stage, dict)
    ]


def _normalize_key_refs(values: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in list(values or []):
        if isinstance(item, dict):
            normalized.append(copy.deepcopy(item))
    return normalized


def _normalize_tool(tool: Any) -> dict[str, Any]:
    item = dict(tool) if isinstance(tool, dict) else {}
    arguments = item.get("arguments")
    normalized_arguments = dict(arguments) if isinstance(arguments, dict) else {}
    return {
        "tool_call_id": str(item.get("tool_call_id") or "").strip(),
        "tool_name": str(item.get("tool_name") or "").strip(),
        "status": str(item.get("status") or "").strip(),
        "arguments": normalized_arguments,
        "arguments_text": str(item.get("arguments_text") or "").strip(),
        "output_text": str(item.get("output_text") or ""),
        "output_preview_text": str(item.get("output_preview_text") or "").strip(),
        "output_ref": str(item.get("output_ref") or "").strip(),
        "started_at": str(item.get("started_at") or "").strip(),
        "finished_at": str(item.get("finished_at") or "").strip(),
        "timestamp": str(item.get("timestamp") or "").strip(),
        "elapsed_seconds": float(item.get("elapsed_seconds") or 0.0)
        if isinstance(item.get("elapsed_seconds"), (int, float))
        else None,
        "kind": str(item.get("kind") or "").strip(),
        "source": str(item.get("source") or "").strip(),
    }


def _normalize_round(round_item: Any) -> dict[str, Any]:
    current = dict(round_item) if isinstance(round_item, dict) else {}
    return {
        "round_id": str(current.get("round_id") or "").strip(),
        "round_index": int(current.get("round_index") or 0),
        "budget_counted": bool(current.get("budget_counted")),
        "tool_names": [
            str(item or "").strip()
            for item in list(current.get("tool_names") or [])
            if str(item or "").strip()
        ],
        "tool_call_ids": [
            str(item or "").strip()
            for item in list(current.get("tool_call_ids") or [])
            if str(item or "").strip()
        ],
        "tools": [
            _normalize_tool(tool)
            for tool in list(current.get("tools") or [])
            if isinstance(tool, dict)
        ],
    }


def _normalize_stage(stage: Any) -> dict[str, Any]:
    current = dict(stage) if isinstance(stage, dict) else {}
    rounds = [
        _normalize_round(round_item)
        for round_item in list(current.get("rounds") or [])
        if isinstance(round_item, dict)
    ]
    rounds.sort(key=lambda item: int(item.get("round_index") or 0))
    return {
        "stage_index": int(current.get("stage_index") or 0),
        "stage_id": str(current.get("stage_id") or "").strip(),
        "stage_goal": str(current.get("stage_goal") or "").strip(),
        "status": str(current.get("status") or "").strip(),
        "stage_kind": str(current.get("stage_kind") or "normal").strip() or "normal",
        "mode": str(current.get("mode") or "").strip(),
        "system_generated": bool(current.get("system_generated")),
        "tool_round_budget": int(current.get("tool_round_budget") or 0),
        "tool_rounds_used": int(current.get("tool_rounds_used") or 0),
        "completed_stage_summary": str(current.get("completed_stage_summary") or "").strip(),
        "created_at": str(current.get("created_at") or "").strip(),
        "finished_at": str(current.get("finished_at") or "").strip(),
        "key_refs": _normalize_key_refs(current.get("key_refs")),
        "rounds": rounds,
    }


def retained_completed_raw_stage_ids(stage_state: Any, *, keep_latest: int) -> set[str]:
    if keep_latest <= 0:
        return set()
    completed: list[tuple[int, str]] = []
    for stage in _stage_list(stage_state):
        if str(stage.get("stage_kind") or "normal").strip().lower() != "normal":
            continue
        stage_id = str(stage.get("stage_id") or "").strip()
        if not stage_id:
            continue
        if str(stage.get("status") or "").strip().lower() == "active":
            continue
        completed.append((int(stage.get("stage_index") or 0), stage_id))
    completed.sort()
    return {stage_id for _stage_index, stage_id in completed[-max(0, int(keep_latest or 0)) :]}


def retained_raw_stage_messages(
    stage_state: Any,
    *,
    keep_latest_completed_stages: int = 3,
) -> tuple[list[dict[str, Any]], set[str]]:
    stages_by_id = {
        str(stage.get("stage_id") or "").strip(): stage
        for stage in _stage_list(stage_state)
        if str(stage.get("stage_id") or "").strip()
    }
    retained_completed_ids = retained_completed_raw_stage_ids(
        stage_state,
        keep_latest=keep_latest_completed_stages,
    )
    ordered: list[dict[str, Any]] = [
        stage
        for stage in stages_by_id.values()
        if str(stage.get("stage_id") or "").strip() in retained_completed_ids
    ]
    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    if active_stage_id:
        active_stage = stages_by_id.get(active_stage_id)
        if isinstance(active_stage, dict):
            ordered.append(active_stage)
    ordered.sort(key=lambda item: int(item.get("stage_index") or 0))
    messages = [
        {
            "role": "assistant",
            "content": f"{STAGE_RAW_PREFIX}\n{json.dumps(_normalize_stage(stage), ensure_ascii=False, sort_keys=True)}",
        }
        for stage in ordered
    ]
    return messages, retained_completed_ids


__all__ = [
    "retained_completed_raw_stage_ids",
    "retained_raw_stage_messages",
]
