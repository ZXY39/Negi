from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


class _DenseMemoryManager:
    def __init__(self) -> None:
        self.store = SimpleNamespace(_dense_enabled=True)
        self.skill_records: list[object] = []
        self.tool_records: list[object] = []
        self.calls: list[dict[str, object]] = []

    async def semantic_search_context_records(
        self,
        *,
        namespace_prefix=None,
        query: str,
        limit: int = 8,
        context_type: str | None = None,
    ):
        self.calls.append(
            {
                "namespace_prefix": namespace_prefix,
                "query": query,
                "limit": limit,
                "context_type": context_type,
            }
        )
        records = self.skill_records if context_type == "skill" else self.tool_records
        return list(records)[:limit]


def _build_node_context_selection():
    module = importlib.import_module("g3ku.runtime.context.node_context_selection")
    return getattr(module, "build_node_context_selection")


@pytest.mark.asyncio
async def test_node_selector_dense_unavailable_returns_full_rbac_visible_sets() -> None:
    build_node_context_selection = _build_node_context_selection()
    memory_manager = SimpleNamespace(store=SimpleNamespace(_dense_enabled=False))

    result = await build_node_context_selection(
        loop=SimpleNamespace(),
        memory_manager=memory_manager,
        prompt="inspect browser workflow",
        goal="inspect browser workflow",
        core_requirement="inspect browser workflow",
        visible_skills=[SimpleNamespace(skill_id="skill-a"), SimpleNamespace(skill_id="skill-b")],
        visible_tool_families=[SimpleNamespace(tool_id="filesystem"), SimpleNamespace(tool_id="exec")],
        visible_tool_names=["filesystem", "exec"],
    )

    assert result.mode == "visible_only"
    assert result.selected_skill_ids == ["skill-a", "skill-b"]
    assert result.selected_tool_names == ["filesystem", "exec"]


@pytest.mark.asyncio
async def test_node_selector_without_memory_search_permission_emits_no_memory_query() -> None:
    build_node_context_selection = _build_node_context_selection()
    memory_manager = _DenseMemoryManager()
    memory_manager.skill_records = [SimpleNamespace(record_id="skill:skill-a")]
    memory_manager.tool_records = [SimpleNamespace(record_id="tool:filesystem")]

    result = await build_node_context_selection(
        loop=SimpleNamespace(),
        memory_manager=memory_manager,
        prompt="inspect browser workflow",
        goal="inspect browser workflow",
        core_requirement="inspect browser workflow",
        visible_skills=[SimpleNamespace(skill_id="skill-a")],
        visible_tool_families=[SimpleNamespace(tool_id="filesystem")],
        visible_tool_names=["filesystem"],
    )

    assert result.memory_search_visible is False
    assert result.memory_query == ""
    assert memory_manager.calls == []
    assert result.retrieval_scope == {
        "search_context_types": [],
        "allowed_context_types": [],
        "allowed_resource_record_ids": [],
        "allowed_skill_record_ids": [],
    }


@pytest.mark.asyncio
async def test_node_selector_with_memory_search_permission_emits_memory_only_retrieval_scope() -> None:
    build_node_context_selection = _build_node_context_selection()
    memory_manager = _DenseMemoryManager()
    memory_manager.skill_records = [SimpleNamespace(record_id="skill:skill-a")]
    memory_manager.tool_records = [SimpleNamespace(record_id="tool:filesystem")]

    result = await build_node_context_selection(
        loop=SimpleNamespace(),
        memory_manager=memory_manager,
        prompt="inspect browser workflow",
        goal="inspect browser workflow",
        core_requirement="inspect browser workflow",
        visible_skills=[SimpleNamespace(skill_id="skill-a")],
        visible_tool_families=[SimpleNamespace(tool_id="filesystem")],
        visible_tool_names=["filesystem", "memory_search"],
    )

    assert result.memory_search_visible is True
    assert result.memory_query
    assert "inspect browser workflow" in result.memory_query
    assert "Core requirement" in result.memory_query
    assert result.retrieval_scope == {
        "search_context_types": ["memory"],
        "allowed_context_types": ["memory"],
        "allowed_resource_record_ids": [],
        "allowed_skill_record_ids": [],
    }
