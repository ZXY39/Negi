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


def _node_context_selection_module():
    return importlib.import_module("g3ku.runtime.context.node_context_selection")


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
    assert result.candidate_skill_ids == ["skill-a", "skill-b"]
    assert result.candidate_tool_names == ["filesystem", "exec"]


@pytest.mark.asyncio
async def test_node_selector_without_memory_search_permission_still_uses_dense_skill_and_tool_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _node_context_selection_module()
    build_node_context_selection = _build_node_context_selection()
    memory_manager = _DenseMemoryManager()
    captured: dict[str, object] = {}

    async def _fake_frontdoor_catalog_selection(**kwargs):
        captured.update(kwargs)
        return {
            "available": True,
            "skill_ids": ["skill-b"],
            "tool_ids": ["exec"],
            "trace": {"queries": {"raw_query": str(kwargs.get("query_text") or "")}},
        }

    monkeypatch.setattr(module, "build_frontdoor_catalog_selection", _fake_frontdoor_catalog_selection)

    result = await build_node_context_selection(
        loop=SimpleNamespace(),
        memory_manager=memory_manager,
        prompt="inspect browser workflow",
        goal="inspect browser workflow",
        core_requirement="inspect browser workflow",
        visible_skills=[SimpleNamespace(skill_id="skill-a"), SimpleNamespace(skill_id="skill-b")],
        visible_tool_families=[SimpleNamespace(tool_id="filesystem"), SimpleNamespace(tool_id="exec")],
        visible_tool_names=["filesystem"],
    )

    assert captured["query_text"] == (
        "Prompt: inspect browser workflow\n"
        "Goal: inspect browser workflow\n"
        "Core requirement: inspect browser workflow"
    )
    assert result.mode == "dense_rerank"
    assert result.selected_skill_ids == ["skill-b"]
    assert result.selected_tool_names == []
    assert result.candidate_skill_ids == ["skill-b"]
    assert result.candidate_tool_names == []


@pytest.mark.asyncio
async def test_node_selector_dense_rerank_remains_catalog_only_even_with_extra_visible_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _node_context_selection_module()
    build_node_context_selection = _build_node_context_selection()
    memory_manager = _DenseMemoryManager()
    captured: dict[str, object] = {}

    async def _fake_frontdoor_catalog_selection(**kwargs):
        captured.update(kwargs)
        return {
            "available": True,
            "skill_ids": ["skill-a"],
            "tool_ids": ["filesystem"],
            "trace": {"queries": {"raw_query": str(kwargs.get("query_text") or "")}},
        }

    monkeypatch.setattr(module, "build_frontdoor_catalog_selection", _fake_frontdoor_catalog_selection)

    result = await build_node_context_selection(
        loop=SimpleNamespace(),
        memory_manager=memory_manager,
        prompt="inspect browser workflow",
        goal="inspect browser workflow",
        core_requirement="inspect browser workflow",
        visible_skills=[SimpleNamespace(skill_id="skill-a")],
        visible_tool_families=[SimpleNamespace(tool_id="filesystem")],
        visible_tool_names=["filesystem", "memory_note"],
    )

    assert result.mode == "dense_rerank"
    assert captured["query_text"] == (
        "Prompt: inspect browser workflow\n"
        "Goal: inspect browser workflow\n"
        "Core requirement: inspect browser workflow"
    )
    assert result.selected_skill_ids == ["skill-a"]
    assert result.selected_tool_names == ["filesystem"]
    assert result.candidate_skill_ids == ["skill-a"]
    assert result.candidate_tool_names == ["filesystem"]


@pytest.mark.asyncio
async def test_node_selector_dense_rerank_applies_separate_tool_and_skill_top_k(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _node_context_selection_module()
    build_node_context_selection = _build_node_context_selection()
    memory_manager = _DenseMemoryManager()

    async def _fake_frontdoor_catalog_selection(**kwargs):
        return {
            "available": True,
            "skill_ids": [f"skill-{index:02d}" for index in range(20)],
            "tool_ids": [f"tool-{index:02d}" for index in range(20)],
            "trace": {"queries": {"raw_query": str(kwargs.get("query_text") or "")}},
        }

    monkeypatch.setattr(module, "build_frontdoor_catalog_selection", _fake_frontdoor_catalog_selection)

    visible_skills = [SimpleNamespace(skill_id=f"skill-{index:02d}") for index in range(20)]
    visible_tools = [f"tool-{index:02d}" for index in range(20)]

    result = await build_node_context_selection(
        loop=SimpleNamespace(),
        memory_manager=memory_manager,
        prompt="inspect browser workflow",
        goal="inspect browser workflow",
        core_requirement="inspect browser workflow",
        visible_skills=visible_skills,
        visible_tool_families=[SimpleNamespace(tool_id=name) for name in visible_tools],
        visible_tool_names=visible_tools,
    )

    assert len(result.candidate_skill_ids) == 16
    assert len(result.candidate_tool_names) == 16
    assert result.candidate_skill_ids[0] == "skill-00"
    assert result.candidate_tool_names[0] == "tool-00"


@pytest.mark.asyncio
async def test_node_selector_dense_rerank_excludes_fixed_builtin_tools_from_candidate_top_k(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _node_context_selection_module()
    build_node_context_selection = _build_node_context_selection()
    memory_manager = _DenseMemoryManager()

    async def _fake_frontdoor_catalog_selection(**kwargs):
        return {
            "available": True,
            "skill_ids": [],
            "tool_ids": [
                "exec",
                "content_open",
                "load_tool_context",
                *[f"tool-{index:02d}" for index in range(20)],
            ],
            "trace": {"queries": {"raw_query": str(kwargs.get("query_text") or "")}},
        }

    monkeypatch.setattr(module, "build_frontdoor_catalog_selection", _fake_frontdoor_catalog_selection)

    visible_tools = [
        "exec",
        "content_open",
        "load_tool_context",
        *[f"tool-{index:02d}" for index in range(20)],
    ]

    result = await build_node_context_selection(
        loop=SimpleNamespace(),
        memory_manager=memory_manager,
        prompt="inspect browser workflow",
        goal="inspect browser workflow",
        core_requirement="inspect browser workflow",
        visible_skills=[],
        visible_tool_families=[SimpleNamespace(tool_id=name) for name in visible_tools],
        visible_tool_names=visible_tools,
    )

    assert result.candidate_tool_names == [f"tool-{index:02d}" for index in range(16)]
