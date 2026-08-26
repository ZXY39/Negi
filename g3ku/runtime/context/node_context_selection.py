from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TOOL_CANDIDATE_TOP_K = 16
SKILL_CANDIDATE_TOP_K = 16


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _normalized_text(value: Any) -> str:
    return str(value or "").strip()


def _visible_ids(items: list[Any], *, key: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(items or []):
        value = _normalized_text(_item_value(item, key))
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _tool_names(items: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(items or []):
        value = _normalized_text(item)
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


@dataclass(slots=True)
class NodeContextSelectionResult:
    mode: Literal["dense_rerank", "visible_only"]
    selected_skill_ids: list[str] = field(default_factory=list)
    selected_tool_names: list[str] = field(default_factory=list)
    candidate_skill_ids: list[str] = field(default_factory=list)
    candidate_tool_names: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)


async def build_node_context_selection(
    *,
    loop: Any,
    memory_manager: Any | None,
    prompt: str,
    goal: str,
    core_requirement: str,
    visible_skills: list[Any],
    visible_tool_families: list[Any],
    visible_tool_names: list[str],
) -> NodeContextSelectionResult:
    del loop, memory_manager, goal, core_requirement, visible_tool_families
    visible_skill_ids = _visible_ids(visible_skills, key="skill_id")
    normalized_tool_names = _tool_names(visible_tool_names)
    selection_query = f"Prompt: {_normalized_text(prompt)}".strip()

    return NodeContextSelectionResult(
        mode="visible_only",
        selected_skill_ids=visible_skill_ids,
        selected_tool_names=normalized_tool_names,
        candidate_skill_ids=visible_skill_ids,
        candidate_tool_names=normalized_tool_names,
        trace={
            "mode": "visible_only",
            "dense_enabled": False,
            "dense_available": False,
            "selection_query": selection_query,
            "visible_skill_ids": list(visible_skill_ids),
            "visible_tool_names": list(normalized_tool_names),
        },
    )
