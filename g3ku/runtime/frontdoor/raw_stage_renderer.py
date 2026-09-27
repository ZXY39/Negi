from __future__ import annotations

import copy
import json
from typing import Any

from g3ku.runtime.stage_prompt_compaction import (
    STAGE_RAW_PREFIX,
    retained_completed_stage_ids,
)


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
        "created_at": str(current.get("created_at") or "").strip(),
        "text": str(current.get("text") or "").strip(),
        "budget_counted": bool(current.get("budget_counted")),
        "overflow": bool(current.get("overflow")),
        "orphan": bool(current.get("orphan")),
        "orphan_grafted": bool(current.get("orphan_grafted")),
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
        "preamble_text": str(current.get("preamble_text") or "").strip(),
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


def retained_raw_stage_messages(
    stage_state: Any,
) -> tuple[list[dict[str, Any]], set[str]]:
    """仍该逐帧重渲染的阶段：共享判据 `retained_completed_stage_ids` + 活动阶段。

    判据只有一份（`g3ku/runtime/stage_prompt_compaction.py`），此处不再镜像——
    两份窗口各写一遍时，改一处就会让 raw 块与 compact 块同时漏掉或同时渲染同一条阶段。
    """
    stages_by_id = {
        str(stage.get("stage_id") or "").strip(): stage
        for stage in _stage_list(stage_state)
        if str(stage.get("stage_id") or "").strip()
    }
    retained_completed_ids = retained_completed_stage_ids(stage_state)
    completed: list[dict[str, Any]] = [
        stage
        for stage in stages_by_id.values()
        if str(stage.get("stage_id") or "").strip() in retained_completed_ids
    ]
    ordered = list(completed)
    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    if active_stage_id:
        active_stage = stages_by_id.get(active_stage_id)
        if isinstance(active_stage, dict):
            ordered.append(active_stage)

    def _stage_id(stage: Any) -> str:
        return str(stage.get("stage_id") or "").strip()

    # 返回给调用方当 skip_stage_ids 用的是**真正渲染出来的**那批完成阶段 id：判据说
    # "该留 raw"但本模块渲不出它（账本里混进非 dict 条目）时，把它留在 skip 集合里就等于
    # 这条阶段既无 raw 块也无 compact 块——彻底隐身。少 skip 一格顶多多一个块。
    rendered_completed_ids = {_stage_id(stage) for stage in completed if _stage_id(stage)}
    ordered.sort(key=lambda item: int(item.get("stage_index") or 0))
    messages = [
        {
            # system 角色：raw 阶段块与 compact/externalized 块同属运行时标注的
            # 阶段上下文（压缩元数据、非对话内容），assistant 角色会诱导模型在
            # 续写位置仿造/回显整块 JSON。角色合同见 stage_prompt_compaction.py
            # completed_stage_blocks 与 context-and-cache-troubleshooting.md
            # 「压缩块的格式与字段语义」。
            "role": "system",
            "content": f"{STAGE_RAW_PREFIX}\n{json.dumps(_normalize_stage(stage), ensure_ascii=False, sort_keys=True)}",
        }
        for stage in ordered
    ]
    return messages, rendered_completed_ids


__all__ = [
    "retained_raw_stage_messages",
]
