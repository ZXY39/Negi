from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from g3ku.runtime.context.frontdoor_catalog_selection import build_frontdoor_catalog_selection


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


def _cap_ordered(values: list[str], *, limit: int) -> list[str]:
    capped_limit = max(int(limit or 0), 0)
    if capped_limit <= 0:
        return []
    return list(values[:capped_limit])


def _build_memory_query(*, prompt: str, goal: str, core_requirement: str) -> str:
    return "\n".join(
        [
            f"Prompt: {_normalized_text(prompt)}",
            f"Goal: {_normalized_text(goal)}",
            f"Core requirement: {_normalized_text(core_requirement)}",
        ]
    ).strip()


def _memory_retrieval_scope(*, enabled: bool) -> dict[str, Any]:
    memory_context_types = ["memory"] if enabled else []
    return {
        "search_context_types": list(memory_context_types),
        "allowed_context_types": list(memory_context_types),
        "allowed_resource_record_ids": [],
        "allowed_skill_record_ids": [],
    }


@dataclass(slots=True)
class NodeContextSelectionResult:
    mode: Literal["dense_rerank", "visible_only"]
    memory_search_visible: bool
    selected_skill_ids: list[str] = field(default_factory=list)
    selected_tool_names: list[str] = field(default_factory=list)
    candidate_skill_ids: list[str] = field(default_factory=list)
    candidate_tool_names: list[str] = field(default_factory=list)
    memory_query: str = ""
    retrieval_scope: dict[str, Any] = field(default_factory=dict)
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
    visible_skill_ids = _visible_ids(visible_skills, key="skill_id")
    normalized_tool_names = _tool_names(visible_tool_names)
    memory_search_visible = "memory_search" in set(normalized_tool_names)
    selection_query = _build_memory_query(
        prompt=prompt,
        goal=goal,
        core_requirement=core_requirement,
    )
    memory_query = (
        selection_query
        if memory_search_visible
        else ""
    )
    retrieval_scope = _memory_retrieval_scope(enabled=memory_search_visible)

    dense_enabled = bool(getattr(getattr(memory_manager, "store", None), "_dense_enabled", False))
    dense_available = bool(
        dense_enabled
        and memory_manager is not None
        and hasattr(memory_manager, "semantic_search_context_records")
    )
    if not dense_available:
        return NodeContextSelectionResult(
            mode="visible_only",
            memory_search_visible=memory_search_visible,
            selected_skill_ids=visible_skill_ids,
            selected_tool_names=normalized_tool_names,
            candidate_skill_ids=visible_skill_ids,
            candidate_tool_names=normalized_tool_names,
            memory_query=memory_query,
            retrieval_scope=retrieval_scope,
            trace={
                "mode": "visible_only",
                "dense_enabled": dense_enabled,
                "dense_available": False,
                "memory_search_visible": memory_search_visible,
                "selection_query": selection_query,
                "visible_skill_ids": list(visible_skill_ids),
                "visible_tool_names": list(normalized_tool_names),
            },
        )

    dense_selection = await build_frontdoor_catalog_selection(
        loop=loop,
        memory_manager=memory_manager,
        query_text=selection_query,
        visible_skills=visible_skills,
        visible_families=visible_tool_families,
        skill_limit=min(max(len(visible_skill_ids), 1), SKILL_CANDIDATE_TOP_K),
        tool_limit=min(max(len(normalized_tool_names), 1), TOOL_CANDIDATE_TOP_K),
    )
    if not bool((dense_selection or {}).get("available")):
        return NodeContextSelectionResult(
            mode="visible_only",
            memory_search_visible=memory_search_visible,
            selected_skill_ids=visible_skill_ids,
            selected_tool_names=normalized_tool_names,
            candidate_skill_ids=visible_skill_ids,
            candidate_tool_names=normalized_tool_names,
            memory_query=memory_query,
            retrieval_scope=retrieval_scope,
            trace={
                "mode": "visible_only",
                "dense_enabled": dense_enabled,
                "dense_available": False,
                "memory_search_visible": memory_search_visible,
                "selection_query": selection_query,
                "dense_selection": dict(dense_selection or {}),
                "visible_skill_ids": list(visible_skill_ids),
                "visible_tool_names": list(normalized_tool_names),
            },
        )

    visible_tool_name_set = set(normalized_tool_names)
    selected_skill_ids = _cap_ordered(
        _tool_names(list((dense_selection or {}).get("skill_ids") or [])),
        limit=SKILL_CANDIDATE_TOP_K,
    )
    selected_tool_names = _cap_ordered([
        tool_name
        for tool_name in list((dense_selection or {}).get("tool_ids") or [])
        if tool_name in visible_tool_name_set
    ], limit=TOOL_CANDIDATE_TOP_K)
    candidate_tool_names = list(selected_tool_names)
    return NodeContextSelectionResult(
        mode="dense_rerank",
        memory_search_visible=memory_search_visible,
        selected_skill_ids=selected_skill_ids,
        selected_tool_names=selected_tool_names,
        candidate_skill_ids=selected_skill_ids,
        candidate_tool_names=candidate_tool_names,
        memory_query=memory_query,
        retrieval_scope=retrieval_scope,
        trace={
            "mode": "dense_rerank",
            "dense_enabled": dense_enabled,
            "dense_available": True,
                "memory_search_visible": memory_search_visible,
                "selection_query": selection_query,
                "dense_selection": dict(dense_selection or {}),
                "visible_skill_ids": list(visible_skill_ids),
                "visible_tool_names": list(normalized_tool_names),
            },
        )
