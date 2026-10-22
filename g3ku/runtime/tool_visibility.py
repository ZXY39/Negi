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
    "submit_final_result",
    "spawn_child_nodes",
    "exec",
    "load_skill_context",
    "load_tool_context",
)

# 循环自己注入的控制信号：正文由代码写好，模型没有可读的 toolskill，因此「契约在场」
# 与「暂存入参」对它们都不成立。它们不在 `NODE_FIXED_BUILTIN_TOOL_NAMES` 里——那颗名单
# 同时是 RBAC/可见性口径的"常驻内置"，把控制信号塞进去会改到别的判据。
NODE_CONTROL_SIGNAL_TOOL_NAMES: tuple[str, ...] = (
    "submit_message_distribution",
    "submit_notice_inspection_decision",
)

# 两条车道共用的无契约信号：静默/异步控制 + 加载器（加载器自己就是送契约的那一跳）。
#
# 这几个名字的正本在 `main/runtime/tool_names.py`。这里不 import 它：
# `base → tool_visibility → main.runtime…` 会经 `main/__init__.py` 绕回 `base`，是个真循环。
# 等价性由 `tests/resources/test_reuse_args_surface.py` 逐名钉住——漂移在测试里红，而不是
# 悄悄少加/多加一个字段。
_NO_CONTRACT_SIGNAL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "silent",
        "wait_tool_execution",
        "stop_tool_execution",
        "load_tool_context",
        "load_tool_context_v2",
        "load_skill_context",
        "load_skill_context_v2",
    }
)

# 按车道的答案：节点道**不能**用前门那份名单去豁免。`task_summary` / `memory_note` /
# `exec` 这些在前门是常驻内置，在节点道是带 toolskill 的资源工具；把它们当"永不带契约"
# 就等于让它们在冲动路径上永远拿不到契约，Stage 5 的在场闸门一上就成批失能（D13）。
CEO_NEVER_CONTRACT_TOOL_NAMES: frozenset[str] = (
    frozenset(CEO_FIXED_BUILTIN_TOOL_NAMES) | _NO_CONTRACT_SIGNAL_TOOL_NAMES
)
NODE_NEVER_CONTRACT_TOOL_NAMES: frozenset[str] = (
    frozenset(NODE_FIXED_BUILTIN_TOOL_NAMES)
    | frozenset(NODE_CONTROL_SIGNAL_TOOL_NAMES)
    | _NO_CONTRACT_SIGNAL_TOOL_NAMES
)

# `reuse_args` 的豁免面用两条车道的并集：字段只是入参传输优化，多豁免一个名字
# 只是少一次优化，误加入一个有契约的名字则是给模型一个永不兑现的承诺。
NON_HYDRATABLE_TOOL_NAMES: frozenset[str] = (
    CEO_NEVER_CONTRACT_TOOL_NAMES | NODE_NEVER_CONTRACT_TOOL_NAMES
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
    "CEO_NEVER_CONTRACT_TOOL_NAMES",
    "NODE_CONTROL_SIGNAL_TOOL_NAMES",
    "NODE_FIXED_BUILTIN_TOOL_NAMES",
    "NODE_NEVER_CONTRACT_TOOL_NAMES",
    "NON_HYDRATABLE_TOOL_NAMES",
    "fixed_builtin_tool_name_set_for_actor_role",
    "normalized_tool_name_list",
    "normalized_tool_name_set",
]
