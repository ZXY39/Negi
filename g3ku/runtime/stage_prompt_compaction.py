from __future__ import annotations

import json
from typing import Any

from g3ku.runtime.tool_history import extract_call_id

STAGE_COMPACT_PREFIX = "[G3KU_STAGE_COMPACT_V1]"
STAGE_EXTERNALIZED_PREFIX = "[G3KU_STAGE_EXTERNALIZED_V1]"
STAGE_RAW_PREFIX = "[G3KU_STAGE_RAW_V1]"

# 内部事件束（心跳/定时）按是否承载因果拆成两类处理：
# - 事件体（因果载荷）保留：后续回合需要它才能追溯"这一轮为什么做这些动作"，
#   因此压缩时不再删除——否则心跳/定时通过开阶段处理问题后，下一轮模型会失去
#   触发上下文，把孤立的工具流水误读成"无事发生"或"被拦截"。被保留的事件体标记：
#   "## EVENT BUNDLE"（心跳事件束）、"[CRON INTERNAL EVENT]" 与
#   "你接收到了之前你定时的任务"（定时任务种子）。
# - 稳定规则文本（重复框架规则）随过期内容一并移除：每次心跳都注入相同的规则，
#   压缩时顺路清理，避免在历史中无限堆积。
DEFAULT_INTERNAL_RULE_MARKERS: tuple[str, ...] = (
    "This is a background heartbeat.",
    "# Heartbeat Rules",
)


def _stage_get(stage: Any, key: str, default: Any = None) -> Any:
    if isinstance(stage, dict):
        return stage.get(key, default)
    return getattr(stage, key, default)


def _message_role(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
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
    # 保留最近 keep_latest 个完成的普通阶段为 raw。与是否存在活动阶段无关：
    # 无活动阶段（纯对话回合）同样适用该契约，否则过期阶段无人压缩、
    # 保留阶段又全部丢失工具上下文。
    if keep_latest <= 0:
        return set()
    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    completed: list[tuple[int, str]] = []
    for stage in list(_stage_get(stage_state, "stages", []) or []):
        if str(_stage_get(stage, "stage_kind", "normal") or "normal").strip().lower() != "normal":
            continue
        stage_id = str(_stage_get(stage, "stage_id", "") or "").strip()
        if not stage_id or stage_id == active_stage_id:
            continue
        if str(_stage_get(stage, "status", "") or "").strip().lower() == "active":
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
        if not isinstance(message, dict) or _message_role(message) != "assistant":
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


def _stage_block_stage_index(message: dict[str, Any]) -> int | None:
    content = str((message or {}).get("content") or "")
    if not (
        content.startswith(STAGE_COMPACT_PREFIX)
        or content.startswith(STAGE_EXTERNALIZED_PREFIX)
        or content.startswith(STAGE_RAW_PREFIX)
    ):
        return None
    _marker, _separator, payload_text = content.partition("\n")
    try:
        payload = json.loads(payload_text)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        stage_index = int(payload.get("stage_index") or 0)
    except (TypeError, ValueError):
        return None
    return stage_index if stage_index > 0 else None


def _parse_stage_submit_response(content: Any) -> tuple[int, str] | None:
    """解析成功的 submit_next_stage 响应，返回其创建阶段的 (stage_index, stage_id)。"""
    text = str(content or "").strip()
    if not text or text.startswith("Error:"):
        return None
    try:
        payload = json.loads(text)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    stage_id = str(payload.get("stage_id") or "").strip()
    try:
        stage_index = int(payload.get("stage_index") or 0)
    except (TypeError, ValueError):
        stage_index = 0
    if stage_index <= 0 and not stage_id:
        return None
    return stage_index, stage_id


def compact_stage_prompt_messages_in_place(
    messages: list[dict[str, Any]],
    *,
    stage_state: Any,
    keep_latest_completed_stages: int = 3,
    stage_tool_name: str = "submit_next_stage",
    preserve_leading_system: bool = True,
    preserve_leading_user: bool = True,
    internal_rule_markers: tuple[str, ...] = DEFAULT_INTERNAL_RULE_MARKERS,
) -> dict[str, Any]:
    """按阶段归属原位压缩：只移除过期阶段的工具肉身，对话与保留阶段原位不动。

    与"块置顶 + 盲切窗口"不同：压缩块回插在被压缩阶段原本的位置，
    阶段之外的对话（用户消息/纯文本回复）逐条保留，provider 前缀缓存
    只从最早的压缩点起失效，而不是从第 3 条消息起整体失效。
    对同一输入重复执行结果收敛（块剥离后按记忆位置回插）。
    """
    normalized_stage_tool = str(stage_tool_name or "").strip() or "submit_next_stage"
    markers = tuple(str(item or "") for item in list(internal_rule_markers or ()) if str(item or ""))

    # 0) 剥离既有阶段块并记忆其位置与内容（清洗流坐标），随后换算到 remainder 坐标。
    block_positions: dict[int, int] = {}
    block_contents: dict[int, str] = {}
    cleaned: list[dict[str, Any]] = []
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        if is_stage_context_message(message):
            payload_index = _stage_block_stage_index(message)
            if payload_index is not None and payload_index not in block_positions:
                block_positions[payload_index] = len(cleaned)
                block_contents[payload_index] = str(message.get("content") or "")
            continue
        cleaned.append(dict(message))
    prefix: list[dict[str, Any]] = []
    remainder = list(cleaned)
    if preserve_leading_system and remainder and _message_role(remainder[0]) == "system":
        prefix.append(remainder.pop(0))
    if preserve_leading_user and remainder and _message_role(remainder[0]) == "user":
        prefix.append(remainder.pop(0))
    prefix_length = len(prefix)
    remembered_positions: dict[int, int] = {
        stage_index: max(0, position - prefix_length) for stage_index, position in block_positions.items()
    }

    remainder = repair_split_stage_tool_boundaries(remainder, stage_tool_name=normalized_stage_tool)

    stages = list(_stage_get(stage_state, "stages", []) or [])
    if not stages:
        return {
            "prefix": prefix,
            "rewritten": remainder,
            "retained_completed_stage_ids": set(),
            "compacted_stage_ids": set(),
            "removed_message_count": 0,
            "stage_compaction_applied": False,
        }

    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    retained_ids = retained_completed_stage_ids(stage_state, keep_latest=keep_latest_completed_stages)

    # 1) 阶段划分：过期阶段 = 完成普通阶段 − 保留集；收集其 rounds 的 tool_call_ids。
    expired_ids: set[str] = set()
    compacted_ids: set[str] = set()
    expired_call_ids: set[str] = set()
    call_id_to_expired_index: dict[str, int] = {}
    normal_all: list[tuple[int, str]] = []
    for stage in stages:
        stage_kind = str(_stage_get(stage, "stage_kind", "normal") or "normal").strip().lower()
        if stage_kind != "normal":
            continue
        stage_id = str(_stage_get(stage, "stage_id", "") or "").strip()
        if not stage_id:
            continue
        stage_index = int(_stage_get(stage, "stage_index", 0) or 0)
        normal_all.append((stage_index, stage_id))
        status = str(_stage_get(stage, "status", "") or "").strip().lower()
        if status == "active" or stage_id == active_stage_id:
            continue
        if stage_id in retained_ids:
            continue
        compacted_ids.add(stage_id)
        expired_ids.add(stage_id)
        for round_item in list(_stage_get(stage, "rounds", []) or []):
            if not isinstance(round_item, dict):
                continue
            for call_id in list(round_item.get("tool_call_ids") or []):
                normalized_call_id = extract_call_id(call_id)
                if normalized_call_id:
                    expired_call_ids.add(normalized_call_id)
                    call_id_to_expired_index.setdefault(normalized_call_id, stage_index)
            for tool in list(round_item.get("tools") or []):
                normalized_call_id = extract_call_id((tool or {}).get("tool_call_id")) if isinstance(tool, dict) else ""
                if normalized_call_id:
                    expired_call_ids.add(normalized_call_id)
                    call_id_to_expired_index.setdefault(normalized_call_id, stage_index)
    normal_all.sort()
    stage_id_by_index = {stage_index: stage_id for stage_index, stage_id in normal_all}

    # 2) submit 归属：成功响应解析创建阶段；双邻规则（关闭侧与创建侧都过期才移除）。
    submit_assistant_indexes: list[int] = []
    submit_response_indexes: dict[int, int] = {}
    submit_created: dict[int, tuple[int, str]] = {}
    pending_submit_call_ids: dict[str, int] = {}
    for index, message in enumerate(remainder):
        role = _message_role(message)
        if role == "assistant":
            for tool_call in list(message.get("tool_calls") or []):
                call_id = extract_call_id((tool_call or {}).get("id"))
                function = (tool_call or {}).get("function") or {}
                tool_name = str(function.get("name") or (tool_call or {}).get("name") or "").strip()
                if call_id and tool_name == normalized_stage_tool:
                    pending_submit_call_ids[call_id] = index
            continue
        if role != "tool":
            continue
        call_id = extract_call_id(message.get("tool_call_id"))
        if (
            call_id
            and call_id in pending_submit_call_ids
            and str(message.get("name") or "").strip() == normalized_stage_tool
            and not str(message.get("content") or "").strip().startswith("Error:")
        ):
            assistant_index = pending_submit_call_ids.pop(call_id)
            submit_assistant_indexes.append(assistant_index)
            submit_response_indexes[assistant_index] = index
            parsed = _parse_stage_submit_response(message.get("content"))
            if parsed is not None:
                submit_created[assistant_index] = parsed

    fallback_eligible = bool(normal_all) and len(submit_assistant_indexes) == len(normal_all)
    submit_remove: dict[int, bool] = {}
    submit_created_stage_index: dict[int, int | None] = {}
    for order, assistant_index in enumerate(submit_assistant_indexes):
        created_id = ""
        created_stage_index: int | None = None
        closed_expired = True  # 首个 submit 无关闭阶段，视同过期侧
        if assistant_index in submit_created:
            parsed_index, parsed_id = submit_created[assistant_index]
            if parsed_id and parsed_id in {stage_id for _index, stage_id in normal_all}:
                created_id = parsed_id
                created_stage_index = parsed_index
            elif parsed_index in stage_id_by_index:
                created_id = stage_id_by_index[parsed_index]
                created_stage_index = parsed_index
        elif fallback_eligible:
            created_stage_index, created_id = normal_all[order]
            if order > 0:
                closed_id = normal_all[order - 1][1]
                closed_expired = closed_id in expired_ids
        if created_id:
            submit_remove[assistant_index] = closed_expired and created_id in expired_ids
        else:
            # 无法判定创建阶段归属：保留（宁可多留，不可误删）。
            submit_remove[assistant_index] = False
        submit_created_stage_index[assistant_index] = created_stage_index

    # 3) 消息级移除决策。
    remove_flags = [False] * len(remainder)
    first_removed_by_stage: dict[int, int] = {}

    def _note_removed_stage_anchor(stage_index: int | None, index: int) -> None:
        if stage_index is None:
            return
        if stage_index not in first_removed_by_stage or index < first_removed_by_stage[stage_index]:
            first_removed_by_stage[stage_index] = index

    for index, message in enumerate(remainder):
        role = _message_role(message)
        if role == "assistant":
            tool_calls = list(message.get("tool_calls") or [])
            if not tool_calls:
                continue
            removable_all = True
            expired_any = False
            anchor_stage_index: int | None = None
            for tool_call in tool_calls:
                call_id = extract_call_id((tool_call or {}).get("id"))
                function = (tool_call or {}).get("function") or {}
                tool_name = str(function.get("name") or (tool_call or {}).get("name") or "").strip()
                if tool_name == normalized_stage_tool:
                    if submit_remove.get(index):
                        expired_any = True
                        created_index = submit_created_stage_index.get(index)
                        if created_index is not None and anchor_stage_index is None:
                            anchor_stage_index = created_index
                    else:
                        removable_all = False
                elif call_id and call_id in expired_call_ids:
                    expired_any = True
                    if anchor_stage_index is None:
                        anchor_stage_index = call_id_to_expired_index.get(call_id)
                else:
                    removable_all = False
            if removable_all and expired_any:
                remove_flags[index] = True
                _note_removed_stage_anchor(anchor_stage_index, index)
        elif role == "tool":
            # tool 消息的移除与其声明方 assistant 严格成对（见下方成对清理），
            # 这里不单独按归属判定，避免打破既有配对。
            continue
        elif role in ("user", "system"):
            # 内部事件束的移除在结构变化点确定后按缓存中性规则统一判定。
            continue

    # 被移除 assistant 声明的 tool 消息成对移除；submit 成功响应同理。
    removed_declared_call_ids: set[str] = set()
    for index, message in enumerate(remainder):
        if not remove_flags[index] or _message_role(message) != "assistant":
            continue
        for tool_call in list(message.get("tool_calls") or []):
            call_id = extract_call_id((tool_call or {}).get("id"))
            if call_id:
                removed_declared_call_ids.add(call_id)
    for index, message in enumerate(remainder):
        if remove_flags[index] or _message_role(message) != "tool":
            continue
        call_id = extract_call_id(message.get("tool_call_id"))
        if call_id and call_id in removed_declared_call_ids:
            remove_flags[index] = True
            anchor_stage_index = call_id_to_expired_index.get(call_id)
            if anchor_stage_index is None:
                assistant_index = next(
                    (
                        submit_index
                        for submit_index, response_index in submit_response_indexes.items()
                        if response_index == index
                    ),
                    None,
                )
                if assistant_index is not None:
                    anchor_stage_index = submit_created_stage_index.get(assistant_index)
            _note_removed_stage_anchor(anchor_stage_index, index)

    # 4) 块回插：按当前 stage_state 重新渲染，位置 = 记忆位置 → 首次移除位置 → 区域开头。
    blocks = completed_stage_blocks(stage_state, skip_stage_ids=retained_ids)
    anchors: list[tuple[int, int, dict[str, Any]]] = []
    structural_change_points: list[int] = []
    rendered_block_stage_indexes: set[int] = set()
    for block in blocks:
        stage_index = _stage_block_stage_index(block) or 0
        rendered_block_stage_indexes.add(stage_index)
        if stage_index in remembered_positions:
            anchor = remembered_positions[stage_index]
            if str(block.get("content") or "") != block_contents.get(stage_index, ""):
                structural_change_points.append(anchor)
        elif stage_index in first_removed_by_stage:
            anchor = first_removed_by_stage[stage_index]
            structural_change_points.append(anchor)
        else:
            anchor = 0
            structural_change_points.append(anchor)
        anchors.append((anchor, stage_index, dict(block)))
    anchors.sort(key=lambda item: (item[0], item[1]))

    # 内部事件束（心跳/定时种子）按缓存中性规则移除：只清理不早于本次压缩
    # 既有最早结构变化点的条目；本次没有任何结构变化时一律保留，避免为清理
    # 历史内部事件额外打断 provider 前缀缓存。
    structural_change_points.extend(index for index, flagged in enumerate(remove_flags) if flagged)
    structural_change_points.extend(
        position
        for stage_index, position in remembered_positions.items()
        if stage_index not in rendered_block_stage_indexes
    )
    if markers and structural_change_points:
        earliest_structural_change = min(structural_change_points)
        for index, message in enumerate(remainder):
            if index < earliest_structural_change:
                continue
            if _message_role(message) not in ("user", "system"):
                continue
            content = str(message.get("content") or "")
            if any(marker in content for marker in markers):
                remove_flags[index] = True

    rewritten: list[dict[str, Any]] = []
    anchor_pointer = 0
    for index, message in enumerate(remainder):
        while anchor_pointer < len(anchors) and anchors[anchor_pointer][0] <= index:
            rewritten.append(dict(anchors[anchor_pointer][2]))
            anchor_pointer += 1
        if not remove_flags[index]:
            rewritten.append(dict(message))
    while anchor_pointer < len(anchors):
        rewritten.append(dict(anchors[anchor_pointer][2]))
        anchor_pointer += 1

    removed_message_count = sum(1 for flagged in remove_flags if flagged)
    return {
        "prefix": prefix,
        "rewritten": rewritten,
        "retained_completed_stage_ids": set(retained_ids),
        "compacted_stage_ids": compacted_ids,
        "removed_message_count": removed_message_count,
        "stage_compaction_applied": removed_message_count > 0,
    }


def prepare_stage_prompt_messages(
    messages: list[dict[str, Any]],
    *,
    stage_state: Any,
    keep_latest_completed_stages: int = 3,
    stage_tool_name: str = "submit_next_stage",
    preserve_leading_system: bool = True,
    preserve_leading_user: bool = True,
) -> list[dict[str, Any]]:
    parts = compact_stage_prompt_messages_in_place(
        messages,
        stage_state=stage_state,
        keep_latest_completed_stages=keep_latest_completed_stages,
        stage_tool_name=stage_tool_name,
        preserve_leading_system=preserve_leading_system,
        preserve_leading_user=preserve_leading_user,
    )
    return [
        *list(parts["prefix"]),
        *list(parts["rewritten"]),
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
    if active_stage_id:
        # 边界含活动阶段的 submit：保留最近 len(retained) 个完成阶段 + 活动阶段。
        active_window = current_stage_active_window(
            remainder,
            keep_completed_stages=len(retained_ids),
            stage_tool_name=stage_tool_name,
        )
    else:
        # 无活动阶段时边界全属完成阶段：保留最近 len(retained) 段需 keep-1。
        active_window = current_stage_active_window(
            remainder,
            keep_completed_stages=max(0, len(retained_ids) - 1),
            stage_tool_name=stage_tool_name,
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
    "DEFAULT_INTERNAL_RULE_MARKERS",
    "STAGE_COMPACT_PREFIX",
    "STAGE_EXTERNALIZED_PREFIX",
    "STAGE_RAW_PREFIX",
    "compact_stage_prompt_messages_in_place",
    "completed_stage_blocks",
    "current_stage_active_window",
    "decompose_stage_prompt_messages",
    "is_stage_context_message",
    "prepare_stage_prompt_messages",
    "repair_split_stage_tool_boundaries",
    "retained_completed_stage_ids",
    "stage_prompt_prefix",
]
