from __future__ import annotations

import copy
from typing import Any, Callable

RAW_REPRESENTATION = "raw"
COMPACT_REPRESENTATION = "compact"
EXTERNALIZED_REPRESENTATION = "externalized"
DEFAULT_RETAIN_RAW_COMPLETED_STAGES = 3
DEFAULT_EXTERNALIZE_THRESHOLD = 20
DEFAULT_EXTERNALIZE_BATCH_SIZE = 10


def default_frontdoor_canonical_context() -> dict[str, Any]:
    return {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [],
    }


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _normalize_key_refs(values: Any) -> list[dict[str, Any]]:
    return [copy.deepcopy(item) for item in list(values or []) if isinstance(item, dict)]


def _normalize_tool(tool: Any) -> dict[str, Any]:
    item = _as_dict(tool)
    arguments = item.get("arguments")
    return {
        "tool_call_id": _as_str(item.get("tool_call_id")),
        "tool_name": _as_str(item.get("tool_name")),
        "status": _as_str(item.get("status")),
        "arguments": dict(arguments) if isinstance(arguments, dict) else {},
        "arguments_text": _as_str(item.get("arguments_text")),
        "output_text": str(item.get("output_text") or ""),
        "output_preview_text": _as_str(item.get("output_preview_text")),
        "output_ref": _as_str(item.get("output_ref")),
        "started_at": _as_str(item.get("started_at")),
        "finished_at": _as_str(item.get("finished_at")),
        "timestamp": _as_str(item.get("timestamp")),
        "kind": _as_str(item.get("kind")),
        "source": _as_str(item.get("source")),
        **(
            {"elapsed_seconds": float(item.get("elapsed_seconds"))}
            if isinstance(item.get("elapsed_seconds"), (int, float))
            else {}
        ),
    }


def _normalize_round(round_item: Any) -> dict[str, Any]:
    current = _as_dict(round_item)
    return {
        "round_id": _as_str(current.get("round_id")),
        "round_index": _as_int(current.get("round_index")),
        "created_at": _as_str(current.get("created_at")),
        "budget_counted": bool(current.get("budget_counted")),
        "tool_names": [
            _as_str(item)
            for item in list(current.get("tool_names") or [])
            if _as_str(item)
        ],
        "tool_call_ids": [
            _as_str(item)
            for item in list(current.get("tool_call_ids") or [])
            if _as_str(item)
        ],
        "tools": [
            _normalize_tool(tool)
            for tool in list(current.get("tools") or [])
            if isinstance(tool, dict)
        ],
    }


def _normalized_representation(stage_kind: str, raw_representation: Any) -> str:
    if stage_kind == "compression":
        return EXTERNALIZED_REPRESENTATION
    normalized = _as_str(raw_representation).lower()
    if normalized in {RAW_REPRESENTATION, COMPACT_REPRESENTATION, EXTERNALIZED_REPRESENTATION}:
        return normalized
    return RAW_REPRESENTATION


def _normalize_stage(stage: Any, *, fallback_index: int) -> dict[str, Any]:
    current = _as_dict(stage)
    stage_kind = _as_str(current.get("stage_kind") or "normal") or "normal"
    representation = _normalized_representation(stage_kind, current.get("representation"))
    rounds = [
        _normalize_round(round_item)
        for round_item in list(current.get("rounds") or [])
        if isinstance(round_item, dict)
    ]
    rounds.sort(key=lambda item: int(item.get("round_index") or 0))
    if representation != RAW_REPRESENTATION:
        rounds = []
    return {
        "stage_id": _as_str(current.get("stage_id") or f"frontdoor-stage-{fallback_index}"),
        "stage_index": _as_int(current.get("stage_index"), fallback_index),
        "stage_goal": _as_str(current.get("stage_goal")),
        "representation": representation,
        "status": _as_str(current.get("status") or "completed") or "completed",
        "stage_kind": stage_kind,
        "mode": _as_str(current.get("mode") or "自主执行") or "自主执行",
        "system_generated": bool(current.get("system_generated")),
        "completed_stage_summary": _as_str(current.get("completed_stage_summary")),
        "final_stage": bool(current.get("final_stage")),
        "key_refs": _normalize_key_refs(current.get("key_refs")),
        "tool_round_budget": max(0, _as_int(current.get("tool_round_budget"))),
        "tool_rounds_used": max(0, _as_int(current.get("tool_rounds_used"))),
        "archive_ref": _as_str(current.get("archive_ref")),
        "archive_stage_index_start": max(0, _as_int(current.get("archive_stage_index_start"))),
        "archive_stage_index_end": max(0, _as_int(current.get("archive_stage_index_end"))),
        "created_at": _as_str(current.get("created_at")),
        "finished_at": _as_str(current.get("finished_at")),
        "rounds": rounds,
    }


def normalize_frontdoor_canonical_context(raw: Any) -> dict[str, Any]:
    source = _as_dict(raw)
    active_stage_id = _as_str(source.get("active_stage_id"))
    stages = [
        _normalize_stage(stage, fallback_index=index)
        for index, stage in enumerate(list(source.get("stages") or []), start=1)
        if isinstance(stage, dict)
    ]
    stages.sort(key=lambda item: int(item.get("stage_index") or 0))
    if active_stage_id and not any(
        _as_str(stage.get("stage_id")) == active_stage_id
        and _as_str(stage.get("status")).lower() == "active"
        for stage in stages
    ):
        active_stage_id = ""
    transition_required = bool(source.get("transition_required")) if active_stage_id else False
    return {
        "active_stage_id": active_stage_id,
        "transition_required": transition_required,
        "stages": stages,
    }


def _rebased_turn_stage_id(stage_kind: str, stage_index: int) -> str:
    if stage_kind == "compression":
        return f"frontdoor-compression-{stage_index}"
    return f"frontdoor-stage-{stage_index}"


def rebase_turn_stage_state_against_context(
    turn_stage_state: Any,
    canonical_context: Any,
) -> dict[str, Any]:
    turn_state = normalize_frontdoor_canonical_context(turn_stage_state)
    if not list(turn_state.get("stages") or []):
        return default_frontdoor_canonical_context()
    base_index = max(
        (int(stage.get("stage_index") or 0) for stage in list(normalize_frontdoor_canonical_context(canonical_context).get("stages") or [])),
        default=0,
    )
    id_map: dict[str, str] = {}
    rebased_stages: list[dict[str, Any]] = []
    for stage in list(turn_state.get("stages") or []):
        local_stage = copy.deepcopy(stage)
        new_stage_index = base_index + max(1, int(local_stage.get("stage_index") or 0))
        new_stage_id = _rebased_turn_stage_id(str(local_stage.get("stage_kind") or "normal"), new_stage_index)
        previous_stage_id = _as_str(local_stage.get("stage_id"))
        id_map[previous_stage_id] = new_stage_id
        local_stage["stage_index"] = new_stage_index
        local_stage["stage_id"] = new_stage_id
        local_stage["representation"] = RAW_REPRESENTATION
        for round_item in list(local_stage.get("rounds") or []):
            if not isinstance(round_item, dict):
                continue
            round_index = max(1, _as_int(round_item.get("round_index"), 1))
            round_item["round_id"] = f"{new_stage_id}:round-{round_index}"
        rebased_stages.append(local_stage)
    active_stage_id = id_map.get(_as_str(turn_state.get("active_stage_id")), "")
    return {
        "active_stage_id": active_stage_id,
        "transition_required": bool(turn_state.get("transition_required")) if active_stage_id else False,
        "stages": rebased_stages,
    }


def combine_canonical_context(
    canonical_context: Any,
    turn_stage_state: Any,
) -> dict[str, Any]:
    durable = normalize_frontdoor_canonical_context(canonical_context)
    rebased_turn_state = rebase_turn_stage_state_against_context(turn_stage_state, durable)
    if not list(rebased_turn_state.get("stages") or []):
        return durable
    return normalize_frontdoor_canonical_context(
        {
            "active_stage_id": _as_str(rebased_turn_state.get("active_stage_id")),
            "transition_required": bool(rebased_turn_state.get("transition_required")),
            "stages": [*list(durable.get("stages") or []), *list(rebased_turn_state.get("stages") or [])],
        }
    )


def _completed_normal_stage_positions(context: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    return [
        (index, stage)
        for index, stage in enumerate(list(context.get("stages") or []))
        if _as_str(stage.get("stage_kind") or "normal") == "normal"
        and _as_str(stage.get("status")).lower() != "active"
    ]


def _compact_stage(stage: dict[str, Any]) -> dict[str, Any]:
    current = copy.deepcopy(stage)
    current["representation"] = COMPACT_REPRESENTATION
    current["rounds"] = []
    return current


def _apply_completed_stage_representations(
    context: dict[str, Any],
    *,
    keep_latest_raw: int,
) -> dict[str, Any]:
    normalized = normalize_frontdoor_canonical_context(context)
    completed_positions = _completed_normal_stage_positions(normalized)
    retained_positions = {
        index
        for index, _stage in completed_positions[-max(0, int(keep_latest_raw or 0)) :]
    }
    stages: list[dict[str, Any]] = []
    for index, stage in enumerate(list(normalized.get("stages") or [])):
        current = copy.deepcopy(stage)
        if _as_str(current.get("stage_kind")) == "compression":
            current["representation"] = EXTERNALIZED_REPRESENTATION
            current["rounds"] = []
            stages.append(current)
            continue
        if _as_str(current.get("status")).lower() == "active" or index in retained_positions:
            current["representation"] = RAW_REPRESENTATION
            stages.append(current)
            continue
        stages.append(_compact_stage(current))
    normalized["stages"] = stages
    return normalize_frontdoor_canonical_context(normalized)


def _externalize_completed_batches(
    context: dict[str, Any],
    *,
    externalize_batch: Callable[[list[dict[str, Any]], int, int], tuple[str, str]] | None,
    completed_threshold: int,
    batch_size: int,
) -> dict[str, Any]:
    normalized = normalize_frontdoor_canonical_context(context)
    if not callable(externalize_batch):
        return normalized
    stages = [copy.deepcopy(stage) for stage in list(normalized.get("stages") or [])]
    while True:
        completed_positions = [
            (index, stage)
            for index, stage in enumerate(stages)
            if _as_str(stage.get("stage_kind") or "normal") == "normal"
            and _as_str(stage.get("status")).lower() != "active"
        ]
        if len(completed_positions) <= max(0, int(completed_threshold or DEFAULT_EXTERNALIZE_THRESHOLD)):
            break
        batch = completed_positions[: max(1, int(batch_size or DEFAULT_EXTERNALIZE_BATCH_SIZE))]
        if not batch:
            break
        archive_stages = [copy.deepcopy(stage) for _index, stage in batch]
        stage_index_start = int(batch[0][1].get("stage_index") or 0)
        stage_index_end = int(batch[-1][1].get("stage_index") or 0)
        archive_summary, archive_ref = externalize_batch(archive_stages, stage_index_start, stage_index_end)
        if not _as_str(archive_ref):
            break
        compression_stage = {
            "stage_id": f"frontdoor-compression-{stage_index_start}-{stage_index_end}",
            "stage_index": stage_index_end,
            "stage_kind": "compression",
            "representation": EXTERNALIZED_REPRESENTATION,
            "system_generated": True,
            "mode": "自主执行",
            "status": "completed",
            "stage_goal": f"Archive completed stage history {stage_index_start}-{stage_index_end}",
            "completed_stage_summary": _as_str(archive_summary)
            or f"Archived completed stages {stage_index_start}-{stage_index_end}.",
            "key_refs": [],
            "archive_ref": _as_str(archive_ref),
            "archive_stage_index_start": stage_index_start,
            "archive_stage_index_end": stage_index_end,
            "tool_round_budget": 0,
            "tool_rounds_used": 0,
            "created_at": _as_str(batch[-1][1].get("finished_at") or batch[-1][1].get("created_at")),
            "finished_at": _as_str(batch[-1][1].get("finished_at") or batch[-1][1].get("created_at")),
            "rounds": [],
        }
        batch_indexes = {index for index, _stage in batch}
        insert_at = min(batch_indexes)
        next_stages: list[dict[str, Any]] = []
        for index, stage in enumerate(stages):
            if index == insert_at:
                next_stages.append(compression_stage)
            if index in batch_indexes:
                continue
            next_stages.append(copy.deepcopy(stage))
        stages = next_stages
    normalized["stages"] = stages
    return normalize_frontdoor_canonical_context(normalized)


def merge_turn_stage_state_into_canonical_context(
    canonical_context: Any,
    turn_stage_state: Any,
    *,
    externalize_batch: Callable[[list[dict[str, Any]], int, int], tuple[str, str]] | None = None,
    keep_latest_raw_completed_stages: int = DEFAULT_RETAIN_RAW_COMPLETED_STAGES,
    completed_threshold: int = DEFAULT_EXTERNALIZE_THRESHOLD,
    externalize_batch_size: int = DEFAULT_EXTERNALIZE_BATCH_SIZE,
) -> dict[str, Any]:
    combined = combine_canonical_context(canonical_context, turn_stage_state)
    externalized = _externalize_completed_batches(
        combined,
        externalize_batch=externalize_batch,
        completed_threshold=completed_threshold,
        batch_size=externalize_batch_size,
    )
    return _apply_completed_stage_representations(
        externalized,
        keep_latest_raw=keep_latest_raw_completed_stages,
    )


def canonical_context_tool_items(canonical_context: Any) -> list[dict[str, Any]]:
    normalized = normalize_frontdoor_canonical_context(canonical_context)
    tools: list[dict[str, Any]] = []
    for stage in list(normalized.get("stages") or []):
        for round_item in list(stage.get("rounds") or []):
            for tool in list(round_item.get("tools") or []):
                if isinstance(tool, dict):
                    tools.append(copy.deepcopy(tool))
    return tools


__all__ = [
    "COMPACT_REPRESENTATION",
    "EXTERNALIZED_REPRESENTATION",
    "RAW_REPRESENTATION",
    "canonical_context_tool_items",
    "combine_canonical_context",
    "default_frontdoor_canonical_context",
    "merge_turn_stage_state_into_canonical_context",
    "normalize_frontdoor_canonical_context",
    "rebase_turn_stage_state_against_context",
]
