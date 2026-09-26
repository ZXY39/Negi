from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.runtime.tool_history import extract_call_id

STAGE_COMPACT_PREFIX = "[G3KU_STAGE_COMPACT_V1]"
STAGE_EXTERNALIZED_PREFIX = "[G3KU_STAGE_EXTERNALIZED_V1]"
STAGE_RAW_PREFIX = "[G3KU_STAGE_RAW_V1]"

# 静默收尾工具名，对齐 main/runtime/stage_budget.py 的 SILENT_TOOL_NAME；本模块不成文
# 地不 import main.*（同下方 DEFAULT_STAGE_MODE 的处理）。含这个调用的 assistant 行必须
# 活过阶段过期压缩 —— 它那行的全部用途就是让后续轮次看见"上次对哪个任务选了沉默"，
# 被裁掉等于这条判据从未存在过（实盘：裸 tool_call 行在两条压缩车道下 94% 会消失）。
SILENT_TRACE_TOOL_NAME = "silent"

# 阶段模式的默认值（对齐 main/models.py 的 StageMode 默认 "自主执行"）。块渲染时
# 等于默认值就不写进 payload——阶段块每轮全量重发，常量字段的重复是纯骨架开销。
DEFAULT_STAGE_MODE = "自主执行"

# 回显裁剪总开关（同时作用于 `strip_stage_block_echo` 与
# `strip_frontdoor_tool_contract_echo` 两处调用点：CEO 最终回复收口
# `_graph_normalize_model_output` 与渠道出站 `sanitize_channel_outbound_text`）。
# 事故背景：裁剪按"任意位置子串匹配块前缀"实现，会把用户答案中合法引用的
# `[G3KU_STAGE_*]` 齐根截断（web:ceo-ad3daa814d19 两轮回复被截）。
# 恢复开关前必须先替换为指纹比对实现——仅剥离与当前注入真块逐字一致的回显，
# 并同步恢复下列回归测试的裁剪断言：
#   - tests/resources/test_ceo_frontdoor_regressions.py（trailing stage block echo）
#   - tests/test_session_keys.py（sanitize_channel_outbound_text 两类回显裁剪）
# standalone 打回守卫（is_*_echo_text + 私有修复提示）不在此开关内，始终生效。
ECHO_STRIP_ENABLED = False

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
    # 注入的阶段块已经以 system 角色落地（压缩元数据、非对话内容，避免模型把块
    # 当成"自己上一轮说的话"而在续写位置仿造/回显）；存量 durable baseline /
    # continuity sidecar / 续跑 seed / actual-request scaffold 里仍可能有
    # assistant 角色的旧块，模型回显的块残留也是 assistant 文本，两类都识别
    # （对齐 is_node_dynamic_contract_message 的双角色做法）。旧块在下一次
    # 压缩渲染回插时自然收敛为 system 角色，过渡期不得因识别不到旧块而
    # 造成块重复或丢失。
    if _message_role(message) not in {"assistant", "system"}:
        return False
    # 恰好以阶段块开头、但同时携带 tool_calls 的工具调用回合不是阶段块本身：
    # 整块丢弃会连带丢掉 assistant 的工具调用声明，使其 role=tool 结果成为
    # 孤儿工具结果（生产事故：ext 会话连续 5 天每轮携带同一对孤儿结果）。
    # 语义对齐 node_prompt_contract 的 _message_declares_tool_calls 守卫。
    if list((message or {}).get("tool_calls") or []):
        return False
    content = str((message or {}).get("content") or "")
    return (
        content.startswith(STAGE_COMPACT_PREFIX)
        or content.startswith(STAGE_EXTERNALIZED_PREFIX)
        or content.startswith(STAGE_RAW_PREFIX)
    )


def _stage_block_prefix_index(text: str) -> int:
    """Return the earliest position where a stage-block prefix begins, or -1."""
    normalized = str(text or "")
    positions = [
        index
        for prefix in (STAGE_COMPACT_PREFIX, STAGE_EXTERNALIZED_PREFIX, STAGE_RAW_PREFIX)
        if (index := normalized.find(prefix)) >= 0
    ]
    return min(positions) if positions else -1


def is_stage_block_echo_text(text: Any) -> bool:
    """Whether the text is a standalone stage-compaction-block echo.

    Stage blocks are runtime-injected context markers; a model must never
    emit them as its own reply. Judged on the text prefix alone — regardless
    of message role — because the frontdoor guard runs on the raw model text
    before any message shaping.
    """
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return (
        normalized.startswith(STAGE_COMPACT_PREFIX)
        or normalized.startswith(STAGE_EXTERNALIZED_PREFIX)
        or normalized.startswith(STAGE_RAW_PREFIX)
    )


def strip_stage_block_echo(text: Any) -> str:
    """Remove a stage-compaction-block echo from user-facing text.

    A standalone block echo becomes empty. If a model puts a visible answer
    before an echoed block fragment, preserve the answer and remove only the
    internal suffix (symmetric to the frontdoor tool-contract echo stripper).
    """
    normalized = str(text or "")
    if is_stage_block_echo_text(normalized):
        return ""
    index = _stage_block_prefix_index(normalized)
    if index >= 0:
        return normalized[:index].strip()
    return normalized.strip()


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


def keep_stage_blocks_off_continuation_tail(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """不变量守卫：阶段块不得出现在最后一条 user 消息之后（占据模型续写位）。

    请求末位保持当前 user 回合（或其后的合法 assistant/tool 序列）。当前用户
    回合已在历史里、而压缩块/workset 块落尾时，模型会在块之后续写，把整块
    JSON 当成"自己上一轮说的话"仿造/回显（事故 ext:qq-official:f8a8001865631301
    的请求形态）。这里把落在最后一条 user 之后的阶段块整体前移到该 user 之前
    （保持块间相对顺序），对齐运行时工具契约"插到最新 user 之前"的既有规则。
    无 user 消息或无越界块时原样返回（不做任何重排，保持前缀缓存中性）。
    """
    items = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
    last_user_index = next(
        (
            index
            for index in range(len(items) - 1, -1, -1)
            if _message_role(items[index]) == "user"
        ),
        None,
    )
    if last_user_index is None:
        return items
    tail = items[last_user_index:]
    trailing_blocks = [item for item in tail if is_stage_context_message(item)]
    if not trailing_blocks:
        return items
    kept_tail = [item for item in tail if not is_stage_context_message(item)]
    return [*items[:last_user_index], *trailing_blocks, *kept_tail]


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
        if _stage_get(stage, "context_visible", True) is False:
            # 收口阶段不占保留窗口名额（与 raw_stage_renderer 同一规则）。
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
        if _stage_get(stage, "context_visible", True) is False:
            # 收口阶段：正文已进过摘要，块不再逐轮重渲染。账本保留（UI 时间线照旧），
            # 只有 provider 可见层不再携带它——这是压缩真正能收缩阶段体积的支点。
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
                    # system 角色：阶段块是运行时标注的已完成阶段摘要（压缩元数据、
                    # 非对话内容）。assistant 角色会让模型把块当成"自己上一轮说的话"，
                    # 在续写位置仿造/回显整块 JSON（事故 ext:qq-official:f8a8001865631301）。
                    # 对齐 FrontdoorToolContract（94fb313a）与节点契约（3529f1eb）的
                    # system 角色合同；[G3KU_TOKEN_COMPACT_V2] 例外保持 assistant
                    # （正文是自然语言会话摘要，语义上属于对话延续）。
                    "role": "system",
                    "content": f"{STAGE_EXTERNALIZED_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}",
                }
            )
            continue
        stage_goal = str(_stage_get(stage, "stage_goal", "") or "")
        completed_summary = str(_stage_get(stage, "completed_stage_summary", "") or "")
        stage_key_refs = [
            normalized
            for normalized in (
                _normalize_key_ref(item)
                for item in list(_stage_get(stage, "key_refs", []) or [])
            )
            if normalized is not None
        ]
        stage_mode = str(_stage_get(stage, "mode", "") or "").strip()
        round_budget = int(_stage_get(stage, "tool_round_budget", 0) or 0)
        rounds_used = int(_stage_get(stage, "tool_rounds_used", 0) or 0)
        # 骨架精简:本分支恒为普通完成阶段,stage_kind/status 是常量,不再逐块重发;
        # 空字段(摘要/引用)整体省略——摘要留空是正常形态(相应回合的最终回复就紧邻在
        # 块之后,不需要指针复述)。stage_index 必须保留:_stage_block_stage_index
        # 依赖它做块位置记忆与存量块去重,删掉会导致块跳位。
        payload: dict[str, Any] = {"stage_index": int(_stage_get(stage, "stage_index", 0) or 0)}
        if stage_goal:
            payload["stage_goal"] = stage_goal
        if completed_summary:
            payload["completed_stage_summary"] = completed_summary
        if stage_key_refs:
            payload["key_refs"] = stage_key_refs
        if stage_mode and stage_mode != DEFAULT_STAGE_MODE:
            payload["mode"] = stage_mode
        if bool(_stage_get(stage, "system_generated", False)):
            payload["system_generated"] = True
        if round_budget or rounds_used:
            payload["tool_rounds"] = f"{rounds_used}/{round_budget}"
        compacted.append(
            {
                # system 角色，理由同 externalized 块（见上方注释）。
                "role": "system",
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
            "expired_call_ids": set(),
            "removed_message_count": 0,
            "stage_compaction_applied": False,
        }

    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    retained_ids = retained_completed_stage_ids(stage_state, keep_latest=keep_latest_completed_stages)

    # 1) 阶段划分：过期阶段 = 终态普通阶段 − 保留集；收集其 rounds 的 tool_call_ids。
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
        if stage_id == active_stage_id or not stage_is_terminal(stage):
            continue
        if stage_id in retained_ids:
            continue
        compacted_ids.add(stage_id)
        expired_ids.add(stage_id)
        # 账本条目在两条车道上形态不同（前门 dict、节点 pydantic 模型），统一走
        # stage_round_call_ids；按序登记，保证同一 call id 跨阶段重复时锚点稳定。
        for normalized_call_id in sorted(stage_round_call_ids(stage)):
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
                if tool_name == SILENT_TRACE_TOOL_NAME:
                    # 钉住整行：静默痕迹不参与阶段过期，无论它的 call_id 是否已进
                    # expired_call_ids。配对的 tool 结果行由下方成对清理按 remove_flags
                    # 决定，本行不删 ⇒ 结果行也不会被单独删，不产生孤儿。
                    removable_all = False
                elif tool_name == normalized_stage_tool:
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

    # 4) 块回插：按当前 stage_state 重新渲染，位置 = 记忆位置 → 本次首条被移除帧位置
    #    → 邻居夹逼（见下）。
    blocks = completed_stage_blocks(stage_state, skip_stage_ids=retained_ids)
    block_by_stage_index: dict[int, dict[str, Any]] = {}
    for block in blocks:
        block_by_stage_index[_stage_block_stage_index(block) or 0] = dict(block)
    rendered_block_stage_indexes = set(block_by_stage_index)

    known_anchors: dict[int, int] = {}
    for stage_index in sorted(rendered_block_stage_indexes):
        if stage_index in remembered_positions:
            known_anchors[stage_index] = remembered_positions[stage_index]
        elif stage_index in first_removed_by_stage:
            known_anchors[stage_index] = first_removed_by_stage[stage_index]

    # 兜底不再是 0：阶段自身的定位信息（本次被移除的帧、块记忆位置）双双缺失时
    # （请求体被重建过，帧与旧块一起没了），0 会把块甩到请求最前面、脱离它自己的
    # 对话（实测 ext:qq-official:f8a8001865631301 有 6 个块被永久钉在队首）。
    # 改为夹逼在前一个已确定锚点之后，并强制锚点相对 stage_index 单调不减——
    # 于是"用户回合 → 块 → 该阶段最终回复"的相对顺序在任何情形下都成立；退化的
    # 只有精确邻接（原始定位信息已不存在，无法复原它原先夹在哪条消息边上）。
    ordered_indexes = sorted(rendered_block_stage_indexes)
    resolved_anchors: dict[int, int] = {}
    cursor = 0
    for position, stage_index in enumerate(ordered_indexes):
        if stage_index in known_anchors:
            anchor = max(known_anchors[stage_index], cursor)
        else:
            upper = len(remainder)
            for later_index in ordered_indexes[position + 1 :]:
                if later_index in known_anchors:
                    upper = known_anchors[later_index]
                    break
            anchor = min(cursor, max(0, upper))
        resolved_anchors[stage_index] = anchor
        cursor = anchor

    anchors: list[tuple[int, int, dict[str, Any]]] = []
    structural_change_points: list[int] = []
    for stage_index in ordered_indexes:
        anchor = resolved_anchors[stage_index]
        if stage_index not in remembered_positions or str(
            block_by_stage_index[stage_index].get("content") or ""
        ) != block_contents.get(stage_index, ""):
            structural_change_points.append(anchor)
        anchors.append((anchor, stage_index, block_by_stage_index[stage_index]))
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
        # 本次判为过期阶段的工具调用 id 全集（不等于真正被删的那些）：调用方用它
        # 判断"当前发送基线里是否还留着这些肉身"，即这次压缩是不是一个过期点。
        "expired_call_ids": expired_call_ids,
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


def stage_ref_candidates(
    stage_state: Any,
    *,
    stage_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """压缩时的证据引用候选：按 stage_index 顺序取出候选阶段的 `key_refs`，同一 ref 只留最后一次。

    候选清单带编号交给摘要模型挑选，模型只回编号、不复述路径；运行时按编号逐字回填
    （LLM 复述路径的逐字节保真度实测只有 30%，不透明 id 更是 10%）。
    `stage_ids` 为 None 时覆盖全部仍可见的完成普通阶段。"""
    wanted = {
        str(item or "").strip()
        for item in list(stage_ids or [])
        if str(item or "").strip()
    }
    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    collected: list[dict[str, Any]] = []
    stages = sorted(
        list(_stage_get(stage_state, "stages", []) or []),
        key=lambda item: int(_stage_get(item, "stage_index", 0) or 0),
    )
    for stage in stages:
        stage_id = str(_stage_get(stage, "stage_id", "") or "").strip()
        if not stage_id or stage_id == active_stage_id:
            continue
        if str(_stage_get(stage, "stage_kind", "normal") or "normal").strip().lower() != "normal":
            continue
        if str(_stage_get(stage, "status", "") or "").strip().lower() == "active":
            continue
        if _stage_get(stage, "context_visible", True) is False:
            continue
        if wanted and stage_id not in wanted:
            continue
        stage_index = int(_stage_get(stage, "stage_index", 0) or 0)
        for key_ref in list(_stage_get(stage, "key_refs", []) or []):
            normalized = _normalize_key_ref(key_ref) or {}
            ref = str(normalized.get("ref") or "").strip()
            if not ref:
                continue
            collected.append(
                {
                    "ref": ref,
                    "note": str(normalized.get("note") or "").strip(),
                    "stage_index": stage_index,
                    "stage_id": stage_id,
                }
            )
    latest_by_ref: dict[str, dict[str, Any]] = {}
    for item in collected:
        latest_by_ref[item["ref"]] = item
    ordered = sorted(latest_by_ref.values(), key=lambda item: (item["stage_index"], item["ref"]))
    for position, item in enumerate(ordered, start=1):
        item["candidate_id"] = position
    return ordered


# --- 压缩信封（两车道共用：CEO/frontdoor 与 execution/acceptance 节点） ----------
#
# 一次内联 `token_compression` 除摘要正文外还要产出三件东西：交给模型挑号的证据引用
# 候选清单、模型选完后由运行时逐字回填的索引小节、被收口阶段的落档指针。让模型自己
# 抄引用不可靠（跨代实测：路径逐字保真约 30%、不透明 id 约 10%），所以选择权给模型、
# 逐字性归运行时；两车道共用同一份实现，避免同一合同出现第二套写法后各自漂移。

TOKEN_COMPACT_V2_PREFIX = "[G3KU_TOKEN_COMPACT_V2]"
STAGE_REF_INDEX_HEADING = "## 证据索引"
STAGE_ARCHIVE_HEADING = "## 阶段归档"
STAGE_REF_CANDIDATE_HEADING = "【证据引用候选】"
STAGE_REF_SELECTION_RULE = (
    "上文末尾的【证据引用候选】列出历史阶段登记过的证据引用（编号 + 引用 + 说明）。\n"
    "要保留哪几条，就在正文末尾另起一节「## 证据索引」，每行只写一个 `- [#编号]`；"
    "引用的完整内容由系统按编号逐字回填。\n"
    "不要抄写、改写或凭记忆补造引用内容与编号；没有值得保留的引用就不要输出这一节。"
)

_FILE_LINE_SUFFIX = re.compile(r":\d+(-\d+)?$")
_REF_NUMBER_IN_LINE = re.compile(r"\d{1,4}")


def is_filesystem_ref(ref: Any) -> bool:
    """区分文件系统路径与运行时句柄，只有前者做存在性复核。

    `task:` / `artifact:` / `node:` / 裸 id 由运行时对象解析，按文件系统判定会被
    误杀成死链；Windows 盘符路径的 ``:`` 是路径的一部分而不是命名空间，必须单独认。"""
    text = str(ref or "").strip()
    if not text:
        return False
    if len(text) > 2 and text[1] == ":" and text[2] in {"/", "\\"}:
        return True
    stripped = _FILE_LINE_SUFFIX.sub("", text)
    scheme_index = stripped.find(":")
    separator_index = min((index for index in (stripped.find("\\"), stripped.find("/")) if index >= 0), default=-1)
    if scheme_index >= 0 and (separator_index < 0 or scheme_index < separator_index):
        return False
    return separator_index >= 0


def stage_ref_is_dead(ref: Any) -> bool:
    """引用指向的文件已不存在时为 True；非路径句柄一律不判（无法按文件系统否定）。"""
    if not is_filesystem_ref(ref):
        return False
    text = _FILE_LINE_SUFFIX.sub("", str(ref).strip())
    try:
        return not Path(text).exists()
    except Exception:
        return False


def render_stage_ref_candidate_block(candidates: list[dict[str, Any]]) -> str:
    """交给摘要模型挑选的带编号候选清单；没有候选时返回空串（不输出空标题）。"""
    lines = [STAGE_REF_CANDIDATE_HEADING]
    for item in list(candidates or []):
        ref = str((item or {}).get("ref") or "").strip()
        if not ref:
            continue
        note = str((item or {}).get("note") or "").strip()
        lines.append(f"[#{int((item or {}).get('candidate_id') or 0)}] {ref}" + (f" — {note}" if note else ""))
    return "\n".join(lines) if len(lines) > 1 else ""


def split_stage_ref_selection(compressed_text: Any) -> tuple[str, list[int]]:
    """摘出模型写的 `## 证据索引` 小节并取回编号，正文里由运行时重写该小节。

    每行只认第一个数字：模型若违规把引用正文抄在旁边，抄的那段会被丢掉，回填的仍是
    逐字原文；越界编号在 `render_stage_ref_index` 阶段丢弃。"""
    text = str(compressed_text or "")
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith(STAGE_REF_INDEX_HEADING)
        ),
        None,
    )
    if start is None:
        return text, []
    end = start + 1
    while end < len(lines) and not lines[end].strip().startswith("#"):
        end += 1
    selected: list[int] = []
    for line in lines[start + 1 : end]:
        found = _REF_NUMBER_IN_LINE.search(line)
        if not found:
            continue
        value = int(found.group(0))
        if value not in selected:
            selected.append(value)
    remainder = "\n".join([*lines[:start], *lines[end:]]).strip()
    return remainder, selected


def render_stage_ref_index(candidates: list[dict[str, Any]], selected_ids: list[int]) -> tuple[str, int, int]:
    """按编号逐字回填索引小节，返回 (小节文本, 保留条数, 因死链丢弃条数)。"""
    by_id = {int((item or {}).get("candidate_id") or 0): dict(item) for item in list(candidates or [])}
    lines: list[str] = []
    dropped_dead = 0
    for value in list(selected_ids or []):
        item = by_id.get(int(value or 0))
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "").strip()
        if not ref:
            continue
        if stage_ref_is_dead(ref):
            dropped_dead += 1
            continue
        note = str(item.get("note") or "").strip()
        lines.append(f"- stage {int(item.get('stage_index') or 0)} | {ref}" + (f" — {note}" if note else ""))
    if not lines:
        return "", 0, dropped_dead
    return "\n".join([STAGE_REF_INDEX_HEADING, *lines]), len(lines), dropped_dead


def _token_compact_payload(message: Any) -> dict[str, Any]:
    content = str((message or {}).get("content") or "") if isinstance(message, dict) else ""
    if not content.startswith(TOKEN_COMPACT_V2_PREFIX):
        return {}
    lines = content.split("\n", 2)
    if len(lines) < 2:
        return {}
    try:
        payload = json.loads(lines[1])
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def stage_created_at_within_watermark(created_at: Any, watermark: Any) -> bool:
    """阶段创建时间是否落在收口水位线之前（含）。

    两边都出自 `now_iso()`，同时区同精度时字典序即时序；偏移一旦不同字典序就会错，
    所以能按 ISO 解析就按解析比。任一为空永不命中——宁可少收口一轮，也不能把还在
    上下文里的阶段收进摘要够不到的地方。"""
    left = str(created_at or "").strip()
    right = str(watermark or "").strip()
    if not left or not right:
        return False
    try:
        return datetime.fromisoformat(left) <= datetime.fromisoformat(right)
    except ValueError:
        return left <= right


def stage_created_at_ceiling(stages: list[dict[str, Any]]) -> str:
    """这批阶段里最晚的 created_at，也就是收口水位线；一个可用时间戳都没有时返回空串。"""
    ceiling = ""
    for stage in list(stages or []):
        if not isinstance(stage, dict):
            continue
        candidate = str(stage.get("created_at") or "").strip()
        if candidate and (not ceiling or stage_created_at_within_watermark(ceiling, candidate)):
            ceiling = candidate
    return ceiling


def stage_archive_selector_from_request_messages(request_messages: Any) -> dict[str, Any]:
    """读回"这一版摘要该收口哪批阶段"：created_at 水位线 + 存量显式清单。

    清单曾经是唯一口径，但 375 条 stage_id 要 8,469 字符，比它守着的摘要正文还长，
    而且每轮随块重发。改成一个时间戳上限后还有额外好处：两份账本各维护一套 stage_id
    序号（实测交集为 0），靠 id 匹配必须再绕一层内容身份，而 created_at 是同一条逻辑
    阶段在两边共享的同一个值。存量块里的 `stage_ids` 继续读到它被下一次压缩改写为止。
    历史里可能同时存在多份压缩块（回显残留等），水位线取最新、清单取并集，应用幂等。"""
    collected_ids: list[str] = []
    watermark = ""
    for message in list(request_messages or []):
        archive = _token_compact_payload(message).get("stage_archive")
        if not isinstance(archive, dict):
            continue
        for item in list(archive.get("stage_ids") or []):
            stage_id = str(item or "").strip()
            if stage_id and stage_id not in collected_ids:
                collected_ids.append(stage_id)
        candidate = str(archive.get("archived_through_created_at") or "").strip()
        if candidate and (not watermark or stage_created_at_within_watermark(watermark, candidate)):
            watermark = candidate
    return {"stage_ids": collected_ids, "archived_through_created_at": watermark}


def stage_message_call_ids(messages: Any) -> set[str]:
    """一批消息里出现过的工具调用 id（assistant 声明的与 tool 回执的都算）。"""
    collected: set[str] = set()
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        for tool_call in list(message.get("tool_calls") or []):
            call_id = extract_call_id((tool_call or {}).get("id"))
            if call_id:
                collected.add(call_id)
        call_id = extract_call_id(message.get("tool_call_id"))
        if call_id:
            collected.add(call_id)
    return {item for item in collected if item}


def stage_block_indexes(
    messages: Any,
    *,
    prefixes: tuple[str, ...] = (STAGE_COMPACT_PREFIX, STAGE_EXTERNALIZED_PREFIX, STAGE_RAW_PREFIX),
) -> set[int]:
    """请求体里还翻得出来的阶段块编号；`prefixes` 用来只认某一类块。"""
    indexes: set[int] = set()
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        content = str(message.get("content") or "")
        if not content.startswith(prefixes):
            continue
        try:
            payload = json.loads(content.split("\n", 1)[1])
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        try:
            value = int(payload.get("stage_index") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            indexes.add(value)
    return indexes


def stage_round_call_ids(stage: Any) -> set[str]:
    """该阶段登记过的工具调用 id；账本条目可能是 dict（前门）或模型（节点），统一走 `_stage_get`。"""
    collected: set[str] = set()
    for round_item in list(_stage_get(stage, "rounds", []) or []):
        for call_id in list(_stage_get(round_item, "tool_call_ids", []) or []):
            normalized = extract_call_id(call_id)
            if normalized:
                collected.add(normalized)
        for tool in list(_stage_get(round_item, "tools", []) or []):
            normalized = extract_call_id(_stage_get(tool, "tool_call_id", ""))
            if normalized:
                collected.add(normalized)
    return collected


_TERMINAL_STAGE_STATUSES = frozenset({"completed", "failed", "完成", "失败"})


def stage_is_terminal(stage: Any) -> bool:
    """阶段是否已终态：两车道各写一套状态词表（前门 `completed`，节点 `完成` / `失败`），都要认。

    判定"能不能收口/归档"用终态白名单，而不是排除某个"进行中"字样：认不出的状态一律当作
    还在跑——少收一轮只是多花 token，多收一轮就是把还在写的阶段收进摘要够不到的地方。"""
    return str(_stage_get(stage, "status", "") or "").strip().lower() in _TERMINAL_STAGE_STATUSES


def stage_is_swallowable(
    stage: Any,
    *,
    active_stage_id: str,
    retained_ids: set[str],
    body_stage_indexes: set[int],
    body_call_ids: set[str],
) -> bool:
    """这条阶段是否已经不必再逐轮渲染：终态的普通阶段，且肉身和块都不在场。

    工具轮次还留在请求体里、或它自己的阶段块还翻得出来，收口它就是直接在上下文里挖洞；
    `body_stage_indexes` 的口径由调用方给（压缩时认全部块，提交点只认 raw 块）。
    `retained_ids` 是近场 raw 窗口（保住最近几条执行细节）让出来的名额。"""
    if not isinstance(stage, dict) and not hasattr(stage, "stage_id"):
        return False
    if str(_stage_get(stage, "stage_kind", "normal") or "normal").strip().lower() != "normal":
        return False
    if not stage_is_terminal(stage):
        return False
    stage_id = str(_stage_get(stage, "stage_id", "") or "").strip()
    if not stage_id or stage_id == active_stage_id or stage_id in retained_ids:
        return False
    if _stage_get(stage, "context_visible", True) is False:
        return False
    try:
        stage_index = int(_stage_get(stage, "stage_index", 0) or 0)
    except (TypeError, ValueError):
        stage_index = 0
    if stage_index in body_stage_indexes:
        return False
    return not (stage_round_call_ids(stage) & body_call_ids)


def summarized_stage_ids(stage_state: Any, *, body_messages: Any, keep_latest: int = 3) -> list[str]:
    """一次压缩真正吞掉的阶段：完成普通阶段 − 近场 raw 窗口 − 仍在体内字面存在的阶段。"""
    retained_ids = retained_completed_stage_ids(stage_state, keep_latest=keep_latest)
    body_indexes = stage_block_indexes(body_messages)
    body_call_ids = stage_message_call_ids(body_messages)
    active_stage_id = str(_stage_get(stage_state, "active_stage_id", "") or "").strip()
    swallowed: list[str] = []
    for stage in list(_stage_get(stage_state, "stages", []) or []):
        if not stage_is_swallowable(
            stage,
            active_stage_id=active_stage_id,
            retained_ids=retained_ids,
            body_stage_indexes=body_indexes,
            body_call_ids=body_call_ids,
        ):
            continue
        stage_id = str(_stage_get(stage, "stage_id", "") or "").strip()
        if stage_id and stage_id not in swallowed:
            swallowed.append(stage_id)
    return swallowed


def stage_record_dict(stage: Any) -> dict[str, Any]:
    """账本条目统一成 dict：前门是 dict，节点是 pydantic 记录。"""
    if isinstance(stage, dict):
        return dict(stage)
    dumper = getattr(stage, "model_dump", None)
    if not callable(dumper):
        return {}
    try:
        payload = dumper(mode="json")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_stage_archive_document(
    *,
    kind: str,
    owner: str,
    created_at: Any,
    stages: list[Any],
) -> dict[str, Any]:
    """被收口阶段的完整账本（逐条 key_refs、轮次都在），构造为落档文档。

    只产出内容不碰磁盘：目录解析属于车道职责（前门 session_temp_dir、节点
    task_temp_dir），共享层不决定文件落哪儿；`kind` / `owner` 让打开文件的模型
    与维护者一眼分清这是哪条车道的哪份账本。"""
    records = [item for item in (stage_record_dict(stage) for stage in list(stages or [])) if item]
    return {
        "kind": str(kind or "").strip(),
        "owner": str(owner or "").strip(),
        "created_at": str(created_at or "").strip(),
        "stage_count": len(records),
        "stages": records,
    }


__all__ = [
    "DEFAULT_INTERNAL_RULE_MARKERS",
    "DEFAULT_STAGE_MODE",
    "ECHO_STRIP_ENABLED",
    "STAGE_ARCHIVE_HEADING",
    "STAGE_COMPACT_PREFIX",
    "STAGE_EXTERNALIZED_PREFIX",
    "STAGE_RAW_PREFIX",
    "STAGE_REF_CANDIDATE_HEADING",
    "STAGE_REF_INDEX_HEADING",
    "STAGE_REF_SELECTION_RULE",
    "TOKEN_COMPACT_V2_PREFIX",
    "build_stage_archive_document",
    "compact_stage_prompt_messages_in_place",
    "completed_stage_blocks",
    "current_stage_active_window",
    "decompose_stage_prompt_messages",
    "is_filesystem_ref",
    "is_stage_block_echo_text",
    "is_stage_context_message",
    "keep_stage_blocks_off_continuation_tail",
    "prepare_stage_prompt_messages",
    "render_stage_ref_candidate_block",
    "render_stage_ref_index",
    "repair_split_stage_tool_boundaries",
    "retained_completed_stage_ids",
    "split_stage_ref_selection",
    "stage_archive_selector_from_request_messages",
    "stage_block_indexes",
    "stage_created_at_ceiling",
    "stage_created_at_within_watermark",
    "stage_is_swallowable",
    "stage_is_terminal",
    "stage_message_call_ids",
    "stage_prompt_prefix",
    "stage_record_dict",
    "stage_ref_candidates",
    "stage_ref_is_dead",
    "stage_round_call_ids",
    "strip_stage_block_echo",
    "summarized_stage_ids",
]
