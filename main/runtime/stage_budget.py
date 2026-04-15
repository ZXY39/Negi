from __future__ import annotations

from collections.abc import Iterable
from typing import Any


STAGE_TOOL_NAME = "submit_next_stage"
FINAL_RESULT_TOOL_NAME = "submit_final_result"
SPAWN_CHILD_NODES_TOOL_NAME = "spawn_child_nodes"
STAGE_TOOL_ROUND_BUDGET_MIN = 5
STAGE_TOOL_ROUND_BUDGET_MAX = 15
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
