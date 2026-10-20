from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    selected_skill_ids: list[str] = field(default_factory=list)
    selected_tool_names: list[str] = field(default_factory=list)
    candidate_skill_ids: list[str] = field(default_factory=list)
    candidate_tool_names: list[str] = field(default_factory=list)


async def build_node_context_selection(
    *,
    visible_skills: list[Any],
    visible_tool_names: list[str],
) -> NodeContextSelectionResult:
    visible_skill_ids = _visible_ids(visible_skills, key="skill_id")
    normalized_tool_names = _tool_names(visible_tool_names)

    return NodeContextSelectionResult(
        selected_skill_ids=visible_skill_ids,
        selected_tool_names=normalized_tool_names,
        candidate_skill_ids=visible_skill_ids,
        candidate_tool_names=normalized_tool_names,
    )
