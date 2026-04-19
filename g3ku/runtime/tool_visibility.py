from __future__ import annotations

from typing import Any


CEO_FIXED_BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "create_async_task",
    "task_append_notice",
    "message",
    "task_summary",
    "task_list",
    "task_progress",
    "load_skill_context",
    "load_tool_context",
    "content_open",
    "content_search",
    "exec",
    "memory_write",
    "memory_delete",
    "memory_note",
)

NODE_FIXED_BUILTIN_TOOL_NAMES: tuple[str, ...] = (
    "submit_next_stage",
    "submit_final_result",
    "spawn_child_nodes",
    "content_describe",
    "content_open",
    "content_search",
    "exec",
    "load_skill_context",
    "load_tool_context",
)


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


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


def filter_tool_names_for_semantic_top_k(
    tool_names: list[str] | tuple[str, ...] | None,
    *,
    excluded_tool_names: list[str] | tuple[str, ...] | set[str] | None,
) -> list[str]:
    excluded = normalized_tool_name_set(excluded_tool_names)
    return [
        name
        for name in normalized_tool_name_list(list(tool_names or []))
        if name not in excluded
    ]


def filter_visible_tool_families_for_semantic_top_k(
    visible_tool_families: list[Any] | None,
    *,
    excluded_tool_names: list[str] | tuple[str, ...] | set[str] | None,
) -> list[dict[str, Any]]:
    excluded = normalized_tool_name_set(excluded_tool_names)
    filtered: list[dict[str, Any]] = []
    for family in list(visible_tool_families or []):
        tool_id = _normalized_tool_name(_item_value(family, "tool_id"))
        actions_payload: list[dict[str, Any]] = []
        for action in list(_item_value(family, "actions") or []):
            executor_names = [
                name
                for name in normalized_tool_name_list(list(_item_value(action, "executor_names") or []))
                if name not in excluded
            ]
            if not executor_names:
                continue
            action_payload: dict[str, Any] = {"executor_names": list(executor_names)}
            action_id = _normalized_tool_name(_item_value(action, "action_id"))
            if action_id:
                action_payload["action_id"] = action_id
            actions_payload.append(action_payload)
        if actions_payload:
            filtered.append({"tool_id": tool_id, "actions": actions_payload})
            continue
        if tool_id and tool_id not in excluded:
            filtered.append({"tool_id": tool_id, "actions": []})
    return filtered


__all__ = [
    "CEO_FIXED_BUILTIN_TOOL_NAMES",
    "NODE_FIXED_BUILTIN_TOOL_NAMES",
    "filter_tool_names_for_semantic_top_k",
    "filter_visible_tool_families_for_semantic_top_k",
    "fixed_builtin_tool_name_set_for_actor_role",
    "normalized_tool_name_list",
    "normalized_tool_name_set",
]
