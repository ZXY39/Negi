from __future__ import annotations

import copy
from typing import Any

from main.protocol import now_iso
from main.runtime.stage_budget import response_tool_calls_count_against_stage_budget


CEO_STAGE_MODE_SELF = "self_execute"
CEO_STAGE_STATUS_ACTIVE = "active"
CEO_STAGE_STATUS_COMPLETED = "completed"
CEO_STAGE_STATUS_FAILED = "failed"


def _normalize_elapsed_seconds(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return round(max(0.0, float(value)), 1)
    except (TypeError, ValueError):
        return None


def _normalize_tool_step(step: Any) -> dict[str, Any]:
    payload = dict(step or {}) if isinstance(step, dict) else {}
    return {
        "tool_call_id": str(payload.get("tool_call_id") or "").strip(),
        "tool_name": str(payload.get("tool_name") or "tool").strip() or "tool",
        "arguments_text": str(payload.get("arguments_text") or ""),
        "output_text": str(payload.get("output_text") or ""),
        "output_ref": str(payload.get("output_ref") or ""),
        "status": str(payload.get("status") or "info").strip() or "info",
        "started_at": str(payload.get("started_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
        "elapsed_seconds": _normalize_elapsed_seconds(payload.get("elapsed_seconds")),
    }


def _normalize_round(round_item: Any, *, index: int) -> dict[str, Any]:
    payload = dict(round_item or {}) if isinstance(round_item, dict) else {}
    return {
        "round_id": str(payload.get("round_id") or "").strip(),
        "round_index": max(1, int(payload.get("round_index") or index)),
        "created_at": str(payload.get("created_at") or ""),
        "budget_counted": bool(payload.get("budget_counted")),
        "tools": [
            _normalize_tool_step(step)
            for step in list(payload.get("tools") or [])
            if isinstance(step, dict)
        ],
    }


def _normalize_stage(stage: Any, *, index: int) -> dict[str, Any]:
    payload = dict(stage or {}) if isinstance(stage, dict) else {}
    return {
        "stage_id": str(payload.get("stage_id") or "").strip(),
        "stage_index": max(1, int(payload.get("stage_index") or index)),
        "mode": str(payload.get("mode") or CEO_STAGE_MODE_SELF).strip() or CEO_STAGE_MODE_SELF,
        "status": str(payload.get("status") or CEO_STAGE_STATUS_ACTIVE).strip() or CEO_STAGE_STATUS_ACTIVE,
        "stage_goal": str(payload.get("stage_goal") or "").strip(),
        "tool_round_budget": max(0, int(payload.get("tool_round_budget") or 0)),
        "tool_rounds_used": max(0, int(payload.get("tool_rounds_used") or 0)),
        "created_at": str(payload.get("created_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
        "rounds": [
            _normalize_round(round_item, index=round_index)
            for round_index, round_item in enumerate(list(payload.get("rounds") or []), start=1)
            if isinstance(round_item, dict)
        ],
    }


def normalize_interaction_trace(value: Any) -> dict[str, Any]:
    payload = dict(value or {}) if isinstance(value, dict) else {}
    return {
        "stages": [
            _normalize_stage(stage, index=index)
            for index, stage in enumerate(list(payload.get("stages") or []), start=1)
            if isinstance(stage, dict)
        ],
        "final_output": str(payload.get("final_output") or ""),
    }


def active_stage(trace: dict[str, Any] | None) -> dict[str, Any] | None:
    normalized = normalize_interaction_trace(trace)
    stages = list(normalized.get("stages") or [])
    for stage in reversed(stages):
        if str(stage.get("status") or "").strip() == CEO_STAGE_STATUS_ACTIVE:
            return stage
    return None


def stage_summary(trace: dict[str, Any] | None, *, transition_required: bool = False) -> dict[str, Any] | None:
    active = active_stage(trace)
    if active is None:
        return None
    return {
        "stage_id": str(active.get("stage_id") or ""),
        "stage_index": int(active.get("stage_index") or 0),
        "mode": str(active.get("mode") or CEO_STAGE_MODE_SELF),
        "status": str(active.get("status") or CEO_STAGE_STATUS_ACTIVE),
        "stage_goal": str(active.get("stage_goal") or ""),
        "tool_round_budget": int(active.get("tool_round_budget") or 0),
        "tool_rounds_used": int(active.get("tool_rounds_used") or 0),
        "created_at": str(active.get("created_at") or ""),
        "finished_at": str(active.get("finished_at") or ""),
        "transition_required": bool(transition_required),
    }


def is_transition_required(trace: dict[str, Any] | None) -> bool:
    active = active_stage(trace)
    if active is None:
        return False
    budget = int(active.get("tool_round_budget") or 0)
    used = int(active.get("tool_rounds_used") or 0)
    return bool(budget > 0 and used >= budget)


def new_interaction_trace() -> dict[str, Any]:
    return {"stages": [], "final_output": ""}


def submit_next_stage(
    trace: dict[str, Any] | None,
    *,
    stage_goal: str,
    tool_round_budget: int,
    mode: str = CEO_STAGE_MODE_SELF,
    created_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = normalize_interaction_trace(trace)
    now = str(created_at or now_iso())
    stages: list[dict[str, Any]] = []
    for stage in list(normalized.get("stages") or []):
        current = dict(stage)
        if str(current.get("status") or "").strip() == CEO_STAGE_STATUS_ACTIVE:
            current["status"] = CEO_STAGE_STATUS_COMPLETED
            current["finished_at"] = now
        stages.append(current)
    next_stage = {
        "stage_id": f"ceo-stage-{len(stages) + 1}",
        "stage_index": len(stages) + 1,
        "mode": str(mode or CEO_STAGE_MODE_SELF).strip() or CEO_STAGE_MODE_SELF,
        "status": CEO_STAGE_STATUS_ACTIVE,
        "stage_goal": str(stage_goal or "").strip(),
        "tool_round_budget": max(0, int(tool_round_budget or 0)),
        "tool_rounds_used": 0,
        "created_at": now,
        "finished_at": "",
        "rounds": [],
    }
    stages.append(next_stage)
    normalized["stages"] = stages
    return normalized, copy.deepcopy(next_stage)


def record_stage_round(
    trace: dict[str, Any] | None,
    *,
    tool_calls: list[dict[str, Any]],
    created_at: str | None = None,
    extra_non_budget_tools: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    normalized = normalize_interaction_trace(trace)
    stages = list(normalized.get("stages") or [])
    if not stages:
        return normalized, None
    active_index = next(
        (
            index
            for index in range(len(stages) - 1, -1, -1)
            if str(stages[index].get("status") or "").strip() == CEO_STAGE_STATUS_ACTIVE
        ),
        -1,
    )
    if active_index < 0:
        return normalized, None
    visible_calls = [
        dict(item)
        for item in list(tool_calls or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    if not visible_calls:
        return normalized, None
    active = dict(stages[active_index])
    rounds = list(active.get("rounds") or [])
    round_payload = {
        "round_id": f"{str(active.get('stage_id') or 'ceo-stage')}:round:{len(rounds) + 1}",
        "round_index": len(rounds) + 1,
        "created_at": str(created_at or now_iso()),
        "budget_counted": response_tool_calls_count_against_stage_budget(
            visible_calls,
            extra_non_budget_tools=extra_non_budget_tools,
        ),
        "tools": [
            {
                "tool_call_id": str(item.get("id") or "").strip(),
                "tool_name": str(item.get("name") or "tool").strip() or "tool",
                "arguments_text": str(item.get("arguments_text") or ""),
                "output_text": "",
                "output_ref": "",
                "status": "running",
                "started_at": str(created_at or now_iso()),
                "finished_at": "",
                "elapsed_seconds": None,
            }
            for item in visible_calls
        ],
    }
    rounds.append(round_payload)
    active["rounds"] = rounds
    if round_payload["budget_counted"]:
        next_used = int(active.get("tool_rounds_used") or 0) + 1
        budget = int(active.get("tool_round_budget") or 0)
        active["tool_rounds_used"] = min(next_used, budget) if budget > 0 else next_used
    stages[active_index] = active
    normalized["stages"] = stages
    return normalized, copy.deepcopy(round_payload)


def update_round_tool(
    trace: dict[str, Any] | None,
    *,
    stage_id: str,
    round_id: str,
    tool_call_id: str,
    output_text: str,
    output_ref: str = "",
    status: str = "success",
    finished_at: str | None = None,
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    normalized = normalize_interaction_trace(trace)
    for stage in list(normalized.get("stages") or []):
        if str(stage.get("stage_id") or "") != str(stage_id or ""):
            continue
        for round_item in list(stage.get("rounds") or []):
            if str(round_item.get("round_id") or "") != str(round_id or ""):
                continue
            for tool in list(round_item.get("tools") or []):
                if str(tool.get("tool_call_id") or "") != str(tool_call_id or ""):
                    continue
                tool["output_text"] = str(output_text or "")
                tool["output_ref"] = str(output_ref or "")
                tool["status"] = str(status or "success").strip() or "success"
                tool["finished_at"] = str(finished_at or "")
                tool["elapsed_seconds"] = _normalize_elapsed_seconds(elapsed_seconds)
                return normalized
    return normalized


def finalize_active_stage(trace: dict[str, Any] | None, *, status: str, finished_at: str | None = None) -> dict[str, Any]:
    normalized = normalize_interaction_trace(trace)
    final_status = str(status or "").strip() or CEO_STAGE_STATUS_COMPLETED
    now = str(finished_at or now_iso())
    for stage in reversed(list(normalized.get("stages") or [])):
        if str(stage.get("status") or "").strip() != CEO_STAGE_STATUS_ACTIVE:
            continue
        stage["status"] = final_status
        stage["finished_at"] = now
        break
    return normalized
