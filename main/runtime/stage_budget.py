from __future__ import annotations

from collections.abc import Iterable
from typing import Any


STAGE_TOOL_NAME = "submit_next_stage"
FINAL_RESULT_TOOL_NAME = "submit_final_result"
SPAWN_CHILD_NODES_TOOL_NAME = "spawn_child_nodes"
STAGE_TOOL_ROUND_BUDGET_MIN = 1
STAGE_TOOL_ROUND_BUDGET_MAX = 20
CONTROL_STAGE_TOOL_NAMES = frozenset({"wait_tool_execution", "stop_tool_execution"})
DEFAULT_STAGE_GATE_BYPASS_TOOLS = frozenset(
    {STAGE_TOOL_NAME, FINAL_RESULT_TOOL_NAME, SPAWN_CHILD_NODES_TOOL_NAME, *CONTROL_STAGE_TOOL_NAMES}
)
CONTEXT_LOADER_STAGE_TOOL_NAMES = frozenset(
    {
        "load_tool_context",
        "load_tool_context_v2",
        "load_skill_context",
        "load_skill_context_v2",
    }
)
DEFAULT_NON_BUDGET_STAGE_TOOLS = frozenset(
    {
        STAGE_TOOL_NAME,
        FINAL_RESULT_TOOL_NAME,
        SPAWN_CHILD_NODES_TOOL_NAME,
        *CONTROL_STAGE_TOOL_NAMES,
        *CONTEXT_LOADER_STAGE_TOOL_NAMES,
    }
)

# 轮末收尾用指针摘要:回复全文已作为 assistant 消息完整保留在历史里(阶段压缩永不删除
# 纯文本回复),阶段记账只存一行指针,避免阶段过期压缩后同文在上下文出现两份。
STAGE_TURN_END_SUMMARY_POINTER = "本阶段结论已随当轮最终回复交付，全文见紧随本阶段归档位置之后的助手回复。"

# 宽限执行(free-pass)提醒:撞闸工具照常执行、结果正常返回时追加到 result_text 的说明。
# 无活动阶段与预算耗尽两情形文案不同(记账归属分别为待入账孤儿轮 / 本阶段溢出轮)。
STAGELESS_FREE_PASS_REMINDER = (
    "[阶段闸门提醒] 本次调用在无活动阶段下宽限执行，结果已正常返回并记入待入账轮次。"
    "下一轮如需继续调用工具，必须同批提交 submit_next_stage：这些待入账调用将并入新阶段并消耗其预算，"
    "请在 stage_goal / completed_stage_summary 中涵盖它们，并把 tool_round_budget 设为不小于待入账轮数 + 后续所需轮数；"
    "否则下一轮调用将被拦截。"
)
STAGE_BUDGET_EXHAUSTED_FREE_PASS_REMINDER = (
    "[阶段闸门提醒] 当前阶段预算已耗尽，本次调用宽限执行并记为本阶段的溢出轮次（不占预算）。"
    "下一轮如需继续调用工具，必须同批提交 submit_next_stage 开启下一阶段，否则调用将被拦截。"
)
STAGE_BUDGET_EXHAUSTION_PREDICTED_REMINDER_TEMPLATE = (
    "[阶段预算] 本轮结束后当前阶段预算将耗尽（{used}/{budget} 将用满）。"
    "下一轮如需继续调用工具，必须同批提交 submit_next_stage，否则调用将被拦截。"
)


def normalize_non_budget_stage_tools(extra_non_budget_tools: Iterable[str] | None = None) -> set[str]:
    names = {
        str(item or "").strip()
        for item in list(DEFAULT_NON_BUDGET_STAGE_TOOLS) + list(extra_non_budget_tools or [])
        if str(item or "").strip()
    }
    return names


def normalize_stage_gate_bypass_tools(extra_allowed_tools: Iterable[str] | None = None) -> set[str]:
    names = {
        str(item or "").strip()
        for item in list(DEFAULT_STAGE_GATE_BYPASS_TOOLS) + list(extra_allowed_tools or [])
        if str(item or "").strip()
    }
    return names


def tool_call_counts_against_stage_budget(
    tool_call: dict[str, Any],
    *,
    extra_non_budget_tools: Iterable[str] | None = None,
) -> bool:
    tool_name = str((tool_call or {}).get("name") or "").strip()
    if not tool_name:
        return False
    return tool_name not in normalize_non_budget_stage_tools(extra_non_budget_tools)


def response_tool_calls_count_against_stage_budget(
    tool_calls: list[dict[str, Any]],
    *,
    extra_non_budget_tools: Iterable[str] | None = None,
) -> bool:
    return any(
        tool_call_counts_against_stage_budget(item, extra_non_budget_tools=extra_non_budget_tools)
        for item in list(tool_calls or [])
        if isinstance(item, dict)
    )


def callable_tool_names_for_stage_iteration(
    tool_names: list[str] | None,
    *,
    has_active_stage: bool,
    transition_required: bool,
    stage_tool_name: str = STAGE_TOOL_NAME,
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(tool_names or []):
        normalized = str(item or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    if not has_active_stage or transition_required:
        normalized_stage_tool_name = str(stage_tool_name or "").strip() or STAGE_TOOL_NAME
        return [normalized_stage_tool_name]
    return ordered


def visible_tools_for_stage_iteration(
    tools: dict[str, Any],
    *,
    has_active_stage: bool,
    transition_required: bool,
    stage_tool_name: str = STAGE_TOOL_NAME,
) -> dict[str, Any]:
    _ = has_active_stage, transition_required, stage_tool_name
    # Keep all tools visible to the model so it can plan against the real capability set.
    # Stage rules are enforced at execution time via `stage_gate_error_for_tool`.
    return dict(tools or {})


def stage_gate_error_for_tool(
    tool_name: str,
    *,
    has_active_stage: bool,
    transition_required: bool,
    extra_allowed_tools: Iterable[str] | None = None,
    stage_tool_name: str = STAGE_TOOL_NAME,
) -> str:
    normalized_tool_name = str(tool_name or "").strip()
    allowed_tools = normalize_stage_gate_bypass_tools(extra_allowed_tools)
    allowed_tools.add(str(stage_tool_name or "").strip())
    if normalized_tool_name in allowed_tools:
        return ""
    if not has_active_stage:
        return f"no active stage; call {stage_tool_name} before using other tools"
    if transition_required:
        return f"current stage budget is exhausted; call {stage_tool_name} before using other tools"
    return ""
