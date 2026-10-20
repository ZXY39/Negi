from __future__ import annotations

from typing import Any


CEO_FIXED_BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "create_async_task",
    "task_append_notice",
    "task_summary",
    "task_list",
    "task_progress",
    "load_skill_context",
    "load_tool_context",
    "exec",
    "memory_update",
    "memory_note",
)

NODE_FIXED_BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "submit_next_stage",
    "submit_final_result",
    "spawn_child_nodes",
    "exec",
    "load_skill_context",
    "load_tool_context",
)


def _normalized_tool_name(value: Any) -> str:
    return str(value or "").strip()


def normalized_tool_name_list(values: list[Any] | tuple[Any, ...] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_value in list(values or []):
        value = _normalized_tool_name(raw_value)
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def normalized_tool_name_set(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> set[str]:
    return set(normalized_tool_name_list(list(values or [])))


def fixed_builtin_tool_name_set_for_actor_role(actor_role: str) -> set[str]:
    normalized_role = _normalized_tool_name(actor_role).lower()
    if normalized_role == "ceo":
        return normalized_tool_name_set(CEO_FIXED_BUILTIN_TOOL_NAMES)
    if normalized_role in {"execution", "inspection"}:
        return normalized_tool_name_set(NODE_FIXED_BUILTIN_TOOL_NAMES)
    return set()


__all__ = [
    "CEO_FIXED_BUILTIN_TOOL_NAMES",
    "NODE_FIXED_BUILTIN_TOOL_NAMES",
    "fixed_builtin_tool_name_set_for_actor_role",
    "normalized_tool_name_list",
    "normalized_tool_name_set",
]
