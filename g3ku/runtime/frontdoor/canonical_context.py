from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

RAW_REPRESENTATION = "raw"
COMPACT_REPRESENTATION = "compact"
EXTERNALIZED_REPRESENTATION = "externalized"
DEFAULT_RETAIN_RAW_COMPLETED_STAGES = 3
TRANSCRIPT_PROJECTION_MODE = "stage_window"
DEFAULT_TRANSCRIPT_MAX_OUTPUT_TEXT_CHARS = 2000
DEFAULT_TRANSCRIPT_MAX_ARGUMENTS_CHARS = 2000
DEFAULT_TRANSCRIPT_MAX_ARGUMENTS_TEXT_CHARS = 4000
DEFAULT_TRANSCRIPT_MAX_ROUND_TEXT_CHARS = 4000


def default_frontdoor_canonical_context() -> dict[str, Any]:
    return {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [],
        "pending_orphan_rounds": [],
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
        "text": _as_str(current.get("text")),
        "budget_counted": bool(current.get("budget_counted")),
        "overflow": bool(current.get("overflow")),
        "orphan": bool(current.get("orphan")),
        "orphan_grafted": bool(current.get("orphan_grafted")),
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
        "preamble_text": _as_str(current.get("preamble_text")),
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


def _dedupe_canonical_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse re-appended copies of the same stage, keeping the newest copy.

    Turn finalization historically appended the whole carried stage workset to
    the durable canonical chain. Copies share the same ``stage_id`` and the
    newest copy is the one rendered from the current turn state, so keeping the
    last occurrence preserves content while preventing unbounded chain growth.
    """
    latest_index: dict[str, int] = {}
    for index, stage in enumerate(stages):
        stage_id = _as_str(stage.get("stage_id"))
        if stage_id:
            latest_index[stage_id] = index
    return [
        stage
        for index, stage in enumerate(stages)
        if not _as_str(stage.get("stage_id")) or latest_index[_as_str(stage.get("stage_id"))] == index
    ]


def _completed_stage_content_identity(stage: dict[str, Any]) -> str:
    """Identity of a completed stage independent of its current stage_id.

    Turn finalization re-appends the carried workset with rebased ids. Created
    and finished timestamps plus the goal/summary identify the same logical
    stage across rebases, so re-appended copies collapse to the newest one.
    """
    if _as_str(stage.get("status")).lower() == "active":
        return ""
    created_at = _as_str(stage.get("created_at"))
    if not created_at:
        return ""
    return "|".join(
        (
            _as_str(stage.get("stage_kind") or "normal"),
            created_at,
            _as_str(stage.get("finished_at")),
            _as_str(stage.get("stage_goal")),
            _as_str(stage.get("completed_stage_summary")),
            "1" if bool(stage.get("system_generated")) else "0",
            "1" if bool(stage.get("final_stage")) else "0",
        )
    )


def _dedupe_completed_stage_content(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_index: dict[str, int] = {}
    for index, stage in enumerate(stages):
        identity = _completed_stage_content_identity(stage)
        if identity:
            latest_index[identity] = index
    kept: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        identity = _completed_stage_content_identity(stage)
        if identity and latest_index.get(identity) != index:
            continue
        kept.append(stage)
    return kept


def normalize_frontdoor_canonical_context(raw: Any) -> dict[str, Any]:
    source = _as_dict(raw)
    active_stage_id = _as_str(source.get("active_stage_id"))
    stages = [
        _normalize_stage(stage, fallback_index=index)
        for index, stage in enumerate(list(source.get("stages") or []), start=1)
        if isinstance(stage, dict)
    ]
    stages.sort(key=lambda item: int(item.get("stage_index") or 0))
    stages = _dedupe_canonical_stages(stages)
    stages = _dedupe_completed_stage_content(stages)
    if active_stage_id and not any(
        _as_str(stage.get("stage_id")) == active_stage_id
        and _as_str(stage.get("status")).lower() == "active"
        for stage in stages
    ):
        active_stage_id = ""
    transition_required = bool(source.get("transition_required")) if active_stage_id else False
    pending_orphan_rounds = [
        _normalize_round(item)
        for item in list(source.get("pending_orphan_rounds") or [])
        if isinstance(item, dict)
    ]
    return {
        "active_stage_id": active_stage_id,
        "transition_required": transition_required,
        "stages": stages,
        "pending_orphan_rounds": pending_orphan_rounds,
    }


def _rebased_turn_stage_id(stage_kind: str, stage_index: int) -> str:
    if stage_kind == "compression":
        return f"frontdoor-compression-{stage_index}"
    return f"frontdoor-stage-{stage_index}"


def _completed_stage_overlap_signature(stage: Any) -> str:
    current = _normalize_stage(stage, fallback_index=0)
    if _as_str(current.get("status")).lower() == "active":
        return ""
    current.pop("stage_id", None)
    current.pop("stage_index", None)
    current.pop("representation", None)
    return json.dumps(current, ensure_ascii=False, sort_keys=True)


def rebase_turn_stage_state_against_context(
    turn_stage_state: Any,
    canonical_context: Any,
) -> dict[str, Any]:
    turn_state = normalize_frontdoor_canonical_context(turn_stage_state)
    if not list(turn_state.get("stages") or []):
        return default_frontdoor_canonical_context()
    durable_context = normalize_frontdoor_canonical_context(canonical_context)
    base_index = max(
        (int(stage.get("stage_index") or 0) for stage in list(durable_context.get("stages") or [])),
        default=0,
    )
    durable_overlaps = {
        _completed_stage_overlap_signature(stage): dict(stage)
        for stage in list(durable_context.get("stages") or [])
        if _completed_stage_overlap_signature(stage)
    }
    overlapping_stage_ids: dict[str, str] = {}
    overlapping_stage_indexes: list[int] = []
    for stage in list(turn_state.get("stages") or []):
        signature = _completed_stage_overlap_signature(stage)
        durable_stage = durable_overlaps.get(signature)
        if not signature or not isinstance(durable_stage, dict):
            continue
        overlapping_stage_ids[_as_str(stage.get("stage_id"))] = _as_str(durable_stage.get("stage_id"))
        overlapping_stage_indexes.append(max(0, int(stage.get("stage_index") or 0)))
    stage_index_offset = max(0, base_index - max(overlapping_stage_indexes, default=0))
    id_map: dict[str, str] = {}
    rebased_stages: list[dict[str, Any]] = []
    for stage in list(turn_state.get("stages") or []):
        local_stage = copy.deepcopy(stage)
        previous_stage_id = _as_str(local_stage.get("stage_id"))
        overlapped_stage_id = overlapping_stage_ids.get(previous_stage_id)
        if overlapped_stage_id:
            id_map[previous_stage_id] = overlapped_stage_id
            continue
        new_stage_index = stage_index_offset + max(1, int(local_stage.get("stage_index") or 0))
        new_stage_id = _rebased_turn_stage_id(str(local_stage.get("stage_kind") or "normal"), new_stage_index)
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


def _truncate_text(value: Any, max_chars: int) -> str:
    text = _as_str(value)
    if max_chars <= 0:
        return "" if text else ""
    return text[:max_chars]


def _cap_tool_payload(
    tool: dict[str, Any],
    *,
    max_output_text_chars: int,
    max_arguments_chars: int,
    max_arguments_text_chars: int,
) -> dict[str, Any]:
    current = dict(tool)
    output_text = _as_str(current.get("output_text"))
    if len(output_text) > max_output_text_chars:
        current["output_text"] = ""
    arguments = current.get("arguments")
    arguments_oversized = False
    if isinstance(arguments, dict) and arguments:
        try:
            arguments_oversized = (
                len(json.dumps(arguments, ensure_ascii=False, default=str)) > max_arguments_chars
            )
        except Exception:
            arguments_oversized = True
    elif arguments not in (None, ""):
        arguments_oversized = True
    if arguments_oversized:
        current["arguments"] = {}
        current["arguments_text"] = _truncate_text(
            current.get("arguments_text"),
            max_arguments_text_chars,
        )
    else:
        arguments_text = _as_str(current.get("arguments_text"))
        if len(arguments_text) > max_arguments_text_chars:
            current["arguments_text"] = _truncate_text(arguments_text, max_arguments_text_chars)
    return current


def _cap_round_payload(
    round_payload: dict[str, Any],
    *,
    max_round_text_chars: int,
    max_output_text_chars: int,
    max_arguments_chars: int,
    max_arguments_text_chars: int,
) -> dict[str, Any]:
    current = dict(round_payload)
    round_text = _as_str(current.get("text"))
    if len(round_text) > max_round_text_chars:
        current["text"] = _truncate_text(round_text, max_round_text_chars)
    current["tools"] = [
        _cap_tool_payload(
            tool,
            max_output_text_chars=max_output_text_chars,
            max_arguments_chars=max_arguments_chars,
            max_arguments_text_chars=max_arguments_text_chars,
        )
        for tool in list(current.get("tools") or [])
        if isinstance(tool, dict)
    ]
    return current


def project_canonical_context_for_transcript(
    raw: Any,
    *,
    keep_latest_raw_completed_stages: int = DEFAULT_RETAIN_RAW_COMPLETED_STAGES,
    max_output_text_chars: int = DEFAULT_TRANSCRIPT_MAX_OUTPUT_TEXT_CHARS,
    max_arguments_chars: int = DEFAULT_TRANSCRIPT_MAX_ARGUMENTS_CHARS,
    max_arguments_text_chars: int = DEFAULT_TRANSCRIPT_MAX_ARGUMENTS_TEXT_CHARS,
    max_round_text_chars: int = DEFAULT_TRANSCRIPT_MAX_ROUND_TEXT_CHARS,
) -> dict[str, Any]:
    """Project canonical context for durable transcript storage.

    The provider prompt still uses the full stage workset via the durable
    canonical chain and current stage state. Transcript records only need the
    same representation the model sees: the latest raw stages plus compact
    summaries for older completed stages, with oversized tool bodies capped.
    """
    normalized = normalize_frontdoor_canonical_context(raw)
    if not list(normalized.get("stages") or []):
        return {}
    projected = _apply_completed_stage_representations(
        normalized,
        keep_latest_raw=keep_latest_raw_completed_stages,
    )
    for stage in list(projected.get("stages") or []):
        if _as_str(stage.get("representation")) != RAW_REPRESENTATION:
            continue
        stage["rounds"] = [
            _cap_round_payload(
                round_payload,
                max_round_text_chars=max_round_text_chars,
                max_output_text_chars=max_output_text_chars,
                max_arguments_chars=max_arguments_chars,
                max_arguments_text_chars=max_arguments_text_chars,
            )
            for round_payload in list(stage.get("rounds") or [])
            if isinstance(round_payload, dict)
        ]
    return projected


def _source_stage_by_identity(canonical_context: Any) -> dict[str, dict[str, Any]]:
    return {
        canonical_stage_identity(stage, index): stage
        for index, stage in enumerate(list((canonical_context or {}).get("stages") or []))
        if isinstance(stage, dict)
    }


def _round_bodies_by_identity(
    canonical_context: Any,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, dict[str, Any]]]:
    scoped: dict[tuple[str, str], dict[str, Any]] = {}
    unscoped: dict[str, dict[str, Any]] = {}
    for index, stage in enumerate(list((canonical_context or {}).get("stages") or [])):
        if not isinstance(stage, dict):
            continue
        stage_id = canonical_stage_identity(stage, index)
        for round_index, round_payload in enumerate(list(stage.get("rounds") or [])):
            if not isinstance(round_payload, dict):
                continue
            identity = canonical_round_identity(round_payload, round_index)
            scoped[(stage_id, identity)] = round_payload
            unscoped.setdefault(identity, round_payload)
    return scoped, unscoped


def project_canonical_context_for_ui_payload(canonical_context: Any) -> dict[str, Any]:
    """Project a canonical workset for Web UI payloads without losing live bodies.

    The transcript projection strips rounds outside the raw stage window and caps
    oversized tool bodies. UI payloads keep the same stage view so delta baselines
    are comparable, but the retained raw stages keep their unprojected rounds so
    tool cards still show the full narration, arguments, and output bodies live.
    """
    projected = project_canonical_context_for_transcript(canonical_context)
    if not list(projected.get("stages") or []):
        return {}
    source_by_identity = _source_stage_by_identity(canonical_context)
    for stage in list(projected.get("stages") or []):
        if _as_str(stage.get("representation")) != RAW_REPRESENTATION:
            continue
        source = source_by_identity.get(
            canonical_stage_identity(stage, int(stage.get("stage_index") or 0))
        )
        if source is not None:
            stage["rounds"] = copy.deepcopy(list(source.get("rounds") or []))
    return projected


def ui_canonical_context_delta(previous_context: Any, current_context: Any) -> dict[str, Any]:
    """UI delta over two canonical contexts that may use different projections.

    Persisted transcript records are projected, while live frontdoor stage state
    is not. Comparing them directly would treat `compact -> raw` and `rounds=[] ->
    rounds=[...]` as changes for every historical stage. Compare both sides in the
    transcript projection first, then reattach the unprojected round bodies of the
    stages the delta keeps so the rendered rail is complete for this turn only.
    """
    previous_view = project_canonical_context_for_transcript(previous_context)
    current_view = project_canonical_context_for_transcript(current_context)
    previous_by_identity = {
        canonical_stage_identity(stage, index): stage
        for index, stage in enumerate(list((previous_view or {}).get("stages") or []))
        if isinstance(stage, dict)
    }
    for index, stage in enumerate(list((current_view or {}).get("stages") or [])):
        if not isinstance(stage, dict):
            continue
        previous_stage = previous_by_identity.get(canonical_stage_identity(stage, index))
        if previous_stage is None:
            continue
        previous_representation = _as_str(previous_stage.get("representation"))
        if previous_representation != RAW_REPRESENTATION:
            # A stage the baseline renders as compact must not re-expand just
            # because the latest-stage window moved after a new turn.
            stage["representation"] = previous_representation
            stage["rounds"] = []
        elif _as_str(stage.get("representation")) != RAW_REPRESENTATION:
            # The window also moves in the other direction: a stage that the
            # baseline kept raw would otherwise be stripped from this view and
            # reappear as a delta. Restore the established raw baseline.
            stage["representation"] = RAW_REPRESENTATION
            stage["rounds"] = copy.deepcopy(list(previous_stage.get("rounds") or []))
    delta = canonical_context_delta(previous_view, current_view)
    if not list(delta.get("stages") or []):
        return {}
    scoped_bodies, unscoped_bodies = _round_bodies_by_identity(current_context)
    for stage in list(delta.get("stages") or []):
        stage_id = canonical_stage_identity(stage, int(stage.get("stage_index") or 0))
        rebuilt: list[dict[str, Any]] = []
        for round_index, round_payload in enumerate(list(stage.get("rounds") or [])):
            if not isinstance(round_payload, dict):
                rebuilt.append(round_payload)
                continue
            identity = canonical_round_identity(round_payload, round_index)
            live_round = scoped_bodies.get((stage_id, identity)) or unscoped_bodies.get(identity)
            rebuilt.append(copy.deepcopy(live_round) if live_round is not None else round_payload)
        stage["rounds"] = rebuilt
    return delta


def merge_turn_stage_state_into_canonical_context(
    canonical_context: Any,
    turn_stage_state: Any,
    *,
    keep_latest_raw_completed_stages: int = DEFAULT_RETAIN_RAW_COMPLETED_STAGES,
) -> dict[str, Any]:
    combined = combine_canonical_context(canonical_context, turn_stage_state)
    return _apply_completed_stage_representations(
        combined,
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


def canonical_value_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_stage_identity(stage: dict[str, Any], index: int) -> str:
    return str(stage.get("stage_id") or stage.get("stage_index") or f"stage:{index}").strip()


def canonical_round_identity(round_payload: dict[str, Any], index: int) -> str:
    return str(round_payload.get("round_id") or round_payload.get("round_index") or f"round:{index}").strip()


def canonical_tool_identity(tool_payload: dict[str, Any], index: int) -> str:
    tool_call_id = str(tool_payload.get("tool_call_id") or "").strip()
    if tool_call_id:
        return tool_call_id
    tool_name = str(tool_payload.get("tool_name") or "tool").strip() or "tool"
    return f"{tool_name}:{index}"


def canonical_context_delta(previous_context: Any, current_context: Any) -> dict[str, Any]:
    previous = copy.deepcopy(previous_context) if isinstance(previous_context, dict) else {}
    current = copy.deepcopy(current_context) if isinstance(current_context, dict) else {}
    current_stages = [dict(item) for item in list(current.get("stages") or []) if isinstance(item, dict)]
    if not current_stages:
        return {}
    previous_stages = {
        canonical_stage_identity(stage, index): dict(stage)
        for index, stage in enumerate(list(previous.get("stages") or []))
        if isinstance(stage, dict)
    }
    delta_stages: list[dict[str, Any]] = []
    for stage_index, stage in enumerate(current_stages):
        stage_identity = canonical_stage_identity(stage, stage_index)
        previous_stage = previous_stages.get(stage_identity)
        if previous_stage is None:
            delta_stages.append(copy.deepcopy(stage))
            continue
        stage_header = {key: copy.deepcopy(value) for key, value in stage.items() if key != "rounds"}
        previous_stage_header = {
            key: copy.deepcopy(value) for key, value in previous_stage.items() if key != "rounds"
        }
        stage_header_changed = canonical_value_fingerprint(previous_stage_header) != canonical_value_fingerprint(stage_header)
        previous_rounds = {
            canonical_round_identity(round_payload, round_index): dict(round_payload)
            for round_index, round_payload in enumerate(list(previous_stage.get("rounds") or []))
            if isinstance(round_payload, dict)
        }
        delta_rounds: list[dict[str, Any]] = []
        for round_index, round_payload in enumerate(list(stage.get("rounds") or [])):
            if not isinstance(round_payload, dict):
                continue
            round_identity = canonical_round_identity(round_payload, round_index)
            previous_round = previous_rounds.get(round_identity)
            if previous_round is None:
                delta_rounds.append(copy.deepcopy(round_payload))
                continue
            previous_tools = {
                canonical_tool_identity(tool_payload, tool_index): dict(tool_payload)
                for tool_index, tool_payload in enumerate(list(previous_round.get("tools") or []))
                if isinstance(tool_payload, dict)
            }
            delta_tools: list[dict[str, Any]] = []
            for tool_index, tool_payload in enumerate(list(round_payload.get("tools") or [])):
                if not isinstance(tool_payload, dict):
                    continue
                tool_identity = canonical_tool_identity(tool_payload, tool_index)
                previous_tool = previous_tools.get(tool_identity)
                if previous_tool is None:
                    delta_tools.append(copy.deepcopy(tool_payload))
                    continue
                if canonical_value_fingerprint(previous_tool) != canonical_value_fingerprint(tool_payload):
                    delta_tools.append(copy.deepcopy(tool_payload))
            if delta_tools:
                delta_round = copy.deepcopy(round_payload)
                delta_round["tools"] = delta_tools
                delta_rounds.append(delta_round)
        if stage_header_changed or delta_rounds:
            delta_stage = copy.deepcopy(stage)
            delta_stage["rounds"] = delta_rounds
            delta_stages.append(delta_stage)
    if not delta_stages:
        return {}
    delta: dict[str, Any] = {"stages": delta_stages}
    active_stage_id = str(current.get("active_stage_id") or "").strip()
    if active_stage_id and any(str(stage.get("stage_id") or "").strip() == active_stage_id for stage in delta_stages):
        delta["active_stage_id"] = active_stage_id
    if current.get("transition_required") is True:
        delta["transition_required"] = True
    return delta


__all__ = [
    "COMPACT_REPRESENTATION",
    "EXTERNALIZED_REPRESENTATION",
    "RAW_REPRESENTATION",
    "canonical_context_delta",
    "canonical_context_tool_items",
    "canonical_round_identity",
    "canonical_stage_identity",
    "canonical_tool_identity",
    "canonical_value_fingerprint",
    "combine_canonical_context",
    "default_frontdoor_canonical_context",
    "merge_turn_stage_state_into_canonical_context",
    "normalize_frontdoor_canonical_context",
    "project_canonical_context_for_ui_payload",
    "project_canonical_context_for_transcript",
    "rebase_turn_stage_state_against_context",
    "TRANSCRIPT_PROJECTION_MODE",
    "ui_canonical_context_delta",
]
