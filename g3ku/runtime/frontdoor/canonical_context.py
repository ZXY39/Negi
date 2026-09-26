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


def _is_context_visible(value: Any) -> bool:
    """收口标记的读法：只有显式 False 才隐藏，字段缺失/历史数据一律可见。

    存量 durable 基线、continuity sidecar 与转录投影里的阶段记录都没有这个字段，
    按 True 处理才能保证旧账本不被静默抹掉。"""
    return value is not False


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
    normalized_stage = {
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
    # 收口标记只在隐藏时落字段：可见是常态，逐条带一个布尔键会让存量大会话的
    # 转录投影与 continuity sidecar 白涨体积（每轮投影 382 条 × ~24 字符）。
    if not _is_context_visible(current.get("context_visible")):
        normalized_stage["context_visible"] = False
    # 裁撤标记同样必须穿过这份白名单，且只在成立时落字段（缺失即未裁撤）。
    if current.get("context_evicted") is True:
        normalized_stage["context_evicted"] = True
    return normalized_stage


def _dedupe_canonical_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse re-appended copies of the same stage, keeping the newest copy.

    Turn finalization historically appended the whole carried stage workset to
    the durable canonical chain. Copies share the same ``stage_id`` and the
    newest copy is the one rendered from the current turn state, so keeping the
    last occurrence preserves content while preventing unbounded chain growth.
    收口标记跟着逻辑阶段走：被丢弃的旧副本带标记时，存活副本也必须带上，否则
    "较新的副本没标"会把已收口阶段的块重新渲染出来。裁撤标记同一口径。
    """
    latest_index: dict[str, int] = {}
    hidden_ids: set[str] = set()
    evicted_ids: set[str] = set()
    for index, stage in enumerate(stages):
        stage_id = _as_str(stage.get("stage_id"))
        if stage_id:
            latest_index[stage_id] = index
            if stage.get("context_visible") is False:
                hidden_ids.add(stage_id)
            if stage.get("context_evicted") is True:
                evicted_ids.add(stage_id)
    result: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        stage_id = _as_str(stage.get("stage_id"))
        if stage_id and latest_index.get(stage_id) != index:
            continue
        needs_visible_mark = bool(stage_id) and stage_id in hidden_ids and stage.get("context_visible") is not False
        needs_evicted_mark = bool(stage_id) and stage_id in evicted_ids and stage.get("context_evicted") is not True
        if needs_visible_mark or needs_evicted_mark:
            stage = dict(stage)
            if needs_visible_mark:
                stage["context_visible"] = False
            if needs_evicted_mark:
                stage["context_evicted"] = True
        result.append(stage)
    return result


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
    hidden_identities: set[str] = set()
    for index, stage in enumerate(stages):
        identity = _completed_stage_content_identity(stage)
        if identity:
            latest_index[identity] = index
            if stage.get("context_visible") is False:
                hidden_identities.add(identity)
    kept: list[dict[str, Any]] = []
    for index, stage in enumerate(stages):
        identity = _completed_stage_content_identity(stage)
        if identity and latest_index.get(identity) != index:
            continue
        if identity and identity in hidden_identities and stage.get("context_visible") is not False:
            # 同一逻辑阶段的任一副本被收口，存活副本同样不再进 provider 上下文。
            stage = {**stage, "context_visible": False}
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
    # 收口标记不得进入重叠签名：否则被隐藏的 durable 阶段与本轮携带的可见副本
    # 判不成同一条阶段，rebase 会把同一阶段当新阶段追加（stage_index 虚增 +
    # 已收口阶段的块重新长回来）。裁撤标记同理：它只改变肉身进不进上下文，
    # 不改变"这是哪一条阶段"。
    current.pop("context_visible", None)
    current.pop("context_evicted", None)
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
    return ui_canonical_context_delta_from_views(previous_view, current_view, current_context)


def ui_canonical_context_delta_from_views(
    previous_view: Any,
    current_view: Any,
    current_source: Any,
) -> dict[str, Any]:
    """UI delta over transcript-projected views the caller already holds.

    The snapshot loop replays transcript rows in order and keeps the previous
    row's transcript projection, so re-projecting both sides per row would make
    the whole build quadratic. Both views are treated as read-only: the sticky
    baseline adjustments are applied to per-stage copies, and the returned delta
    is byte-identical to what `ui_canonical_context_delta` produces from raw
    inputs. `current_source` supplies the unprojected round bodies.
    """
    previous_by_identity = {
        canonical_stage_identity(stage, index): stage
        for index, stage in enumerate(list((previous_view or {}).get("stages") or []))
        if isinstance(stage, dict)
    }
    adjusted_stages: list[dict[str, Any]] = []
    for index, stage in enumerate(list((current_view or {}).get("stages") or [])):
        if not isinstance(stage, dict):
            continue
        current = dict(stage)
        adjusted_stages.append(current)
        previous_stage = previous_by_identity.get(canonical_stage_identity(stage, index))
        if previous_stage is None:
            continue
        previous_representation = _as_str(previous_stage.get("representation"))
        if previous_representation != RAW_REPRESENTATION:
            # A stage the baseline renders as compact must not re-expand just
            # because the latest-stage window moved after a new turn.
            current["representation"] = previous_representation
            current["rounds"] = []
        elif _as_str(current.get("representation")) != RAW_REPRESENTATION:
            # The window also moves in the other direction: a stage that the
            # baseline kept raw would otherwise be stripped from this view and
            # reappear as a delta. Restore the established raw baseline.
            current["representation"] = RAW_REPRESENTATION
            current["rounds"] = copy.deepcopy(list(previous_stage.get("rounds") or []))
    current_view_for_delta = (
        {**current_view, "stages": adjusted_stages}
        if isinstance(current_view, dict)
        else {"stages": adjusted_stages}
    )
    delta = canonical_context_delta(previous_view, current_view_for_delta)
    if not list(delta.get("stages") or []):
        return {}
    scoped_bodies, unscoped_bodies = _round_bodies_by_identity(current_source)
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


TRANSCRIPT_DELTA_PROJECTION_MODE = "delta_window"
TRANSCRIPT_CC_UPSERT_FIELD = "cc_upsert"
# checkpoint 密度：真实渠道会话实测相邻视图 100% 可 upsert 编码（均值 2 个
# stage/行），每 K 行一个全量锚点把重放长度与体积同时钉死在上界。
TRANSCRIPT_CC_CHECKPOINT_INTERVAL_ROWS = 40
TRANSCRIPT_CC_CHECKPOINT_CHAIN_BYTES = 96_000
_MISSING_HEADER = object()


def _transcript_rail_fields(row: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(row, dict) or str(row.get("role") or "").strip().lower() != "assistant":
        return None, None
    stored = row.get("canonical_context")
    stored = stored if isinstance(stored, dict) and stored else None
    upsert = row.get(TRANSCRIPT_CC_UPSERT_FIELD)
    upsert = upsert if isinstance(upsert, dict) else None
    return stored, upsert


def plan_transcript_cc_row(messages: Any, view: dict[str, Any]) -> dict[str, Any]:
    """Fields for the assistant rail row about to be appended at the tail.

    Emits a `cc_upsert` row when the tail chain stays short/light and the
    candidate replays exactly; otherwise falls back to a full checkpoint row.
    Every fallback keeps the file readable by the same row-level dispatch the
    snapshot loop uses, so a chain never has to be "fixed" retroactively here.
    """
    checkpoint_fields = {
        "canonical_context": copy.deepcopy(view),
        "canonical_context_projection": TRANSCRIPT_PROJECTION_MODE,
    }
    rows = list(messages or [])
    prev_index = -1
    chain_rows = 0
    chain_bytes = 0
    for index in range(len(rows) - 1, -1, -1):
        stored, upsert = _transcript_rail_fields(rows[index])
        if stored is not None:
            prev_index = index
            break
        if upsert is not None:
            chain_rows += 1
            chain_bytes += len(json.dumps(upsert, ensure_ascii=False))
            continue
    if prev_index < 0:
        return checkpoint_fields
    if (
        chain_rows >= TRANSCRIPT_CC_CHECKPOINT_INTERVAL_ROWS
        or chain_bytes >= TRANSCRIPT_CC_CHECKPOINT_CHAIN_BYTES
    ):
        return checkpoint_fields
    prev_view = materialize_transcript_view(rows, prev_index)
    if not prev_view:
        return checkpoint_fields
    payload = encode_cc_upsert(prev_view, view)
    if payload is None:
        return checkpoint_fields
    return {
        TRANSCRIPT_CC_UPSERT_FIELD: payload,
        "canonical_context_projection": TRANSCRIPT_DELTA_PROJECTION_MODE,
    }


def _transcript_stored_view(row: Any) -> dict[str, Any]:
    """View of a row that stores a full `canonical_context` (checkpoint or legacy raw)."""
    stored, _ = _transcript_rail_fields(row)
    if stored is None:
        return {}
    if str(row.get("canonical_context_projection") or "").strip() == TRANSCRIPT_PROJECTION_MODE:
        return stored
    return project_canonical_context_for_transcript(stored)


def repair_transcript_cc_chain(messages: Any, index: int, replaced_view: Any) -> int:
    """Re-encode delta rows that followed an in-place-mutated rail row.

    A rail row replaced mid-file (paused archive upsert) invalidates every
    `cc_upsert` row encoded against its old view. The caller must pass the
    replaced row's pre-mutation view: downstream originals are only
    recoverable by replaying the old chain forward from it. Each upsert row is
    then re-encoded against the fresh anchor chain, checkpointing whatever
    will not re-encode. Returns the number of rows rewritten.
    """
    rows = messages if isinstance(messages, list) else list(messages or [])
    if not isinstance(index, int) or not (0 <= index < len(rows)):
        return 0
    originals: list[tuple[int, dict[str, Any]]] = []
    walk = replaced_view if isinstance(replaced_view, dict) else {}
    for cursor in range(index + 1, len(rows)):
        stored, upsert = _transcript_rail_fields(rows[cursor])
        if stored is not None:
            walk = _transcript_stored_view(rows[cursor])
            continue
        if upsert is None:
            continue
        original = apply_cc_upsert(walk, upsert)
        originals.append((cursor, original))
        walk = original
    rewritten = 0
    prev_view = _transcript_stored_view(rows[index]) or materialize_transcript_view(rows, index)
    for cursor, original_view in originals:
        if not original_view:
            continue
        row = dict(rows[cursor])
        row.pop(TRANSCRIPT_CC_UPSERT_FIELD, None)
        row.pop("canonical_context", None)
        payload = encode_cc_upsert(prev_view, original_view) if prev_view else None
        if payload is None:
            row["canonical_context"] = copy.deepcopy(original_view)
            row["canonical_context_projection"] = TRANSCRIPT_PROJECTION_MODE
        else:
            row[TRANSCRIPT_CC_UPSERT_FIELD] = payload
            row["canonical_context_projection"] = TRANSCRIPT_DELTA_PROJECTION_MODE
        rows[cursor] = row
        rewritten += 1
        prev_view = original_view
    return rewritten


def apply_cc_upsert(previous_view: Any, upsert: Any) -> dict[str, Any]:
    """Replay a transcript stage-view upsert onto the previous stored view.

    Storage-side delta: each entry is a complete stage object in transcript
    projection shape. An existing `stage_id` is replaced in place (covers the
    active->completed finalization and one-way raw->compact window moves), an
    unknown one is appended (covers new stages). Header overrides ride along
    only when the writer saw a change. Pure function; inputs are not mutated.
    """
    payload = upsert if isinstance(upsert, dict) else {}
    # Copy-on-write: unchanged stages stay shared with the previous view (the
    # whole pipeline treats views as read-only after C2-a removed the input
    # deepcopy); only replaced/appended stages get their own copies. Per-row
    # full deep copies put the snapshot build back on a quadratic curve.
    previous_stages = [
        stage
        for stage in list((previous_view or {}).get("stages") or [])
        if isinstance(stage, dict)
    ]
    positions = {
        _as_str(stage.get("stage_id")): index
        for index, stage in enumerate(previous_stages)
        if _as_str(stage.get("stage_id"))
    }
    stages = list(previous_stages)
    for stage in list(payload.get("upsert") or []):
        if not isinstance(stage, dict):
            continue
        current = copy.deepcopy(stage)
        stage_id = _as_str(current.get("stage_id"))
        if stage_id and stage_id in positions:
            stages[positions[stage_id]] = current
        else:
            if stage_id:
                positions[stage_id] = len(stages)
            stages.append(current)
    view: dict[str, Any] = {}
    if isinstance(previous_view, dict):
        for key, value in previous_view.items():
            if key != "stages":
                view[key] = value
    view["stages"] = stages
    headers = payload.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if key != "stages":
                view[key] = value
    return view


def encode_cc_upsert(previous_view: Any, current_view: Any) -> dict[str, Any] | None:
    """Reduce two transcript views to a replayable upsert payload, or None.

    The candidate is always verified by replay: if applying it to
    `previous_view` does not reproduce `current_view` exactly (stage order or
    content drift beyond replace/append semantics), the writer must store a
    full checkpoint row instead. That self-check is what keeps a delta chain
    from silently diverging.
    """
    if not isinstance(current_view, dict):
        return None
    if not list(current_view.get("stages") or []):
        return None
    previous = (previous_view or {}) if isinstance(previous_view, dict) else {}
    previous_stages = [stage for stage in list(previous.get("stages") or []) if isinstance(stage, dict)]
    previous_by_id = {
        _as_str(stage.get("stage_id")): stage
        for stage in previous_stages
        if _as_str(stage.get("stage_id"))
    }
    entries: list[dict[str, Any]] = []
    for stage in list(current_view.get("stages") or []):
        if not isinstance(stage, dict):
            return None
        stage_id = _as_str(stage.get("stage_id"))
        stored = previous_by_id.get(stage_id)
        if stored is None or stored != stage:
            entries.append(copy.deepcopy(stage))
    payload: dict[str, Any] = {"upsert": entries}
    headers = {
        key: copy.deepcopy(value)
        for key, value in current_view.items()
        if key != "stages" and previous.get(key, _MISSING_HEADER) != value
    }
    if headers:
        payload["headers"] = headers
    # Always verify by replay: an empty upsert must still prove the two views
    # are identical (a pure reorder encodes to nothing but replays wrong).
    if apply_cc_upsert(previous_view, payload) != current_view:
        return None
    return payload


def materialize_transcript_view(messages: Any, index: int) -> dict[str, Any]:
    """Replay a transcript row's stored view from its nearest full anchor.

    cc_upsert rows only carry the stage-level delta relative to the previous
    rail row (hidden runtime rows included), so readers that need one row's
    cumulative view backtrack to the nearest stored `canonical_context` and
    re-apply the chain forward. Rows with a stored full `canonical_context`
    return it as-is (legacy raw rows keep their unprojected bodies exactly as
    persisted); returns {} for non-rail rows or broken chains.
    """
    rows = list(messages or [])
    if not isinstance(index, int) or not (0 <= index < len(rows)):
        return {}
    target = rows[index]
    if not isinstance(target, dict) or not isinstance(target.get("cc_upsert"), dict):
        # Only upsert rows need a replay; full rows and non-rail rows answer directly.
        stored = target.get("canonical_context") if isinstance(target, dict) else None
        if isinstance(stored, dict) and stored:
            return stored
        return {}
    chain: list[dict[str, Any]] = [target[TRANSCRIPT_CC_UPSERT_FIELD]]
    anchor: dict[str, Any] | None = None
    cursor = index - 1
    while cursor >= 0:
        row = rows[cursor]
        cursor -= 1
        if not isinstance(row, dict) or str(row.get("role") or "").strip().lower() != "assistant":
            continue
        stored = row.get("canonical_context")
        if isinstance(stored, dict) and stored:
            if str(row.get("canonical_context_projection") or "").strip() == TRANSCRIPT_PROJECTION_MODE:
                anchor = stored
            else:
                anchor = project_canonical_context_for_transcript(stored)
            break
        upsert = row.get(TRANSCRIPT_CC_UPSERT_FIELD)
        if isinstance(upsert, dict):
            chain.append(upsert)
    if anchor is None:
        return {}
    view = anchor
    for payload in reversed(chain):
        view = apply_cc_upsert(view, payload)
    return view


def migrate_transcript_rows_to_delta(messages: Any) -> int:
    """Rewrite legacy full-rail rows into the checkpoint/delta forms in place.

    Runs at load time (next to `_migrate_oversized_records`) so a persisted
    session converges to the storage contract the writer produces: every
    `stage_window` row becomes a `cc_upsert` row while its replay is verified
    per step (``encode_cc_upsert`` refuses anything it cannot reproduce, and
    refusal just keeps the row as the next anchor), legacy unprojected rows
    first collapse to checkpoint form, and rows already in delta form only
    advance the cursor — which makes the pass idempotent and crash-reentrant.
    Returns the number of rows rewritten; the caller's next full save
    (structural edit already flagged by the mutation) persists them.
    """
    rows = messages if isinstance(messages, list) else list(messages or [])
    rewritten = 0
    prev_view: dict[str, Any] | None = None
    chain_rows = 0
    chain_bytes = 0
    for index, row in enumerate(rows):
        stored, upsert = _transcript_rail_fields(row)
        if upsert is not None and stored is None:
            if prev_view is None:
                # Delta without a preceding anchor: leave the row untouched and
                # let the next full row restart the chain.
                chain_rows = 0
                chain_bytes = 0
                continue
            # 滚动推进，绝不做按行回溯物化：那会把整趟扫描拖成平方级。
            prev_view = apply_cc_upsert(prev_view, upsert)
            chain_rows += 1
            chain_bytes += len(json.dumps(upsert, ensure_ascii=False))
            continue
        if stored is None:
            continue
        marker = str(row.get("canonical_context_projection") or "").strip()
        view = stored if marker == TRANSCRIPT_PROJECTION_MODE else project_canonical_context_for_transcript(stored)
        if not list(view.get("stages") or []):
            continue
        reducible = (
            prev_view is not None
            and chain_rows < TRANSCRIPT_CC_CHECKPOINT_INTERVAL_ROWS
            and chain_bytes < TRANSCRIPT_CC_CHECKPOINT_CHAIN_BYTES
        )
        payload = encode_cc_upsert(prev_view, view) if reducible else None
        if payload is None:
            if marker != TRANSCRIPT_PROJECTION_MODE:
                normalized = dict(row)
                normalized["canonical_context"] = copy.deepcopy(view)
                normalized["canonical_context_projection"] = TRANSCRIPT_PROJECTION_MODE
                rows[index] = normalized
                rewritten += 1
            prev_view = view
            chain_rows = 0
            chain_bytes = 0
            continue
        delta_row = dict(row)
        delta_row.pop("canonical_context", None)
        delta_row[TRANSCRIPT_CC_UPSERT_FIELD] = payload
        delta_row["canonical_context_projection"] = TRANSCRIPT_DELTA_PROJECTION_MODE
        rows[index] = delta_row
        rewritten += 1
        prev_view = view
        chain_rows += 1
        chain_bytes += len(json.dumps(payload, ensure_ascii=False))
    return rewritten


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


# 收口标记只决定阶段正文进不进模型上下文，不决定轨道怎么画，前端也完全不读它。
# 把它算进 UI delta 的代价实测很贵：一次 token 压缩会把压缩区间内几百个历史阶段一次性
# 标记收口，全部落进同一行转录，那一行的 delta 变成 429 条阶段 / 302KB，
# 对应气泡看起来像把整部历史堆在自己身上（QQ 渠道会话）。裁撤标记同一性质。
UI_INERT_STAGE_FIELDS = frozenset({"context_visible", "context_evicted"})


def _ui_comparable_stage(stage: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in stage.items() if key not in UI_INERT_STAGE_FIELDS}


def canonical_context_delta(previous_context: Any, current_context: Any) -> dict[str, Any]:
    """Structural diff of two canonical contexts (stage -> round -> tool).

    Both inputs are treated as read-only; only changed objects are copied into
    the returned delta. Unchanged prefixes dominate transcript replays, so each
    level first tries a plain dict equality before the JSON fingerprint — on
    normalized (JSON-parsed) views the two checks agree. UI-inert stage fields
    (``UI_INERT_STAGE_FIELDS``) are excluded from both the comparison and the
    emitted payload: this function feeds the rendered rail only, and the storage
    upsert codec keeps those fields independently.
    """
    previous = previous_context if isinstance(previous_context, dict) else {}
    current = current_context if isinstance(current_context, dict) else {}
    current_stages = [item for item in list(current.get("stages") or []) if isinstance(item, dict)]
    if not current_stages:
        return {}
    previous_stages = {
        canonical_stage_identity(stage, index): stage
        for index, stage in enumerate(list(previous.get("stages") or []))
        if isinstance(stage, dict)
    }
    delta_stages: list[dict[str, Any]] = []
    for stage_index, stage in enumerate(current_stages):
        stage_identity = canonical_stage_identity(stage, stage_index)
        previous_stage = previous_stages.get(stage_identity)
        if previous_stage is None:
            delta_stages.append(_ui_comparable_stage(copy.deepcopy(stage)))
            continue
        if stage == previous_stage:
            continue
        stage_for_ui = _ui_comparable_stage(stage)
        previous_stage_for_ui = _ui_comparable_stage(previous_stage)
        if stage_for_ui == previous_stage_for_ui:
            # 只有簿记位变了：这条阶段在轨道上没有任何可视差异，不重画。
            continue
        stage_header = {key: value for key, value in stage_for_ui.items() if key != "rounds"}
        previous_stage_header = {
            key: value for key, value in previous_stage_for_ui.items() if key != "rounds"
        }
        stage_header_changed = (
            canonical_value_fingerprint(previous_stage_header)
            != canonical_value_fingerprint(stage_header)
        )
        previous_rounds = {
            canonical_round_identity(round_payload, round_index): round_payload
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
            if round_payload == previous_round:
                continue
            previous_tools = {
                canonical_tool_identity(tool_payload, tool_index): tool_payload
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
                if tool_payload == previous_tool:
                    continue
                if canonical_value_fingerprint(previous_tool) != canonical_value_fingerprint(tool_payload):
                    delta_tools.append(copy.deepcopy(tool_payload))
            if delta_tools:
                delta_round = copy.deepcopy(round_payload)
                delta_round["tools"] = delta_tools
                delta_rounds.append(delta_round)
        if stage_header_changed or delta_rounds:
            delta_stage = copy.deepcopy(stage_for_ui)
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
    "UI_INERT_STAGE_FIELDS",
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
    "TRANSCRIPT_DELTA_PROJECTION_MODE",
    "TRANSCRIPT_CC_UPSERT_FIELD",
    "TRANSCRIPT_CC_CHECKPOINT_INTERVAL_ROWS",
    "TRANSCRIPT_CC_CHECKPOINT_CHAIN_BYTES",
    "apply_cc_upsert",
    "encode_cc_upsert",
    "materialize_transcript_view",
    "migrate_transcript_rows_to_delta",
    "plan_transcript_cc_row",
    "repair_transcript_cc_chain",
    "ui_canonical_context_delta",
    "ui_canonical_context_delta_from_views",
]
