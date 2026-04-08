from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import g3ku.runtime.context.frontdoor_catalog_selection as selection_module


class _MemoryManagerRecorder:
    def __init__(self) -> None:
        self.semantic_calls: list[dict[str, Any]] = []
        self._responses: dict[str, list[Any]] = {}

    def set_response(self, *, context_type: str, records: list[Any]) -> None:
        self._responses[str(context_type)] = list(records)

    async def semantic_search_context_records(
        self,
        *,
        namespace_prefix: tuple[str, ...],
        query: str,
        limit: int,
        context_type: str,
    ) -> list[dict[str, Any]]:
        self.semantic_calls.append(
            {
                "namespace_prefix": tuple(namespace_prefix),
                "context_type": str(context_type),
                "query": str(query),
                "limit": int(limit),
            }
        )
        return list(self._responses.get(str(context_type), []))


class _RerankRecorder:
    def __init__(self, response: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = list(response or [])

    async def __call__(
        self,
        *,
        memory_manager: Any,
        query_text: str,
        records: list[dict[str, Any]],
        top_n: int,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "memory_manager": memory_manager,
                "query_text": str(query_text),
                "records": list(records),
                "top_n": int(top_n),
            }
        )
        return list(self.response)


@pytest.mark.asyncio
async def test_build_frontdoor_catalog_selection_uses_rewritten_queries_for_dense_search(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = object()
    memory_manager = _MemoryManagerRecorder()
    rewrite_calls: list[dict[str, Any]] = []

    async def _rewrite_queries(**kwargs) -> dict[str, str]:
        rewrite_calls.append(dict(kwargs))
        return {
            "skill_query": "rewritten skill query",
            "tool_query": "rewritten tool query",
        }

    async def _rerank_passthrough(**kwargs) -> list[dict[str, Any]]:
        records = kwargs.get("records")
        return list(records if isinstance(records, list) else [])

    monkeypatch.setattr(selection_module, "rewrite_frontdoor_catalog_queries", _rewrite_queries)
    monkeypatch.setattr(selection_module, "rerank_frontdoor_catalog_records", _rerank_passthrough)

    # Planned output contract for Task 3 implementation:
    # The selector should return a mapping that includes:
    expected_contract_keys = {"skill_ids", "tool_ids", "trace"}
    result = await selection_module.build_frontdoor_catalog_selection(
        loop=loop,
        memory_manager=memory_manager,
        query_text="find browser automation helpers",
        visible_skills=[SimpleNamespace(skill_id="browser-workflow")],
        visible_families=[SimpleNamespace(tool_id="agent_browser")],
        skill_limit=7,
        tool_limit=5,
    )

    assert len(rewrite_calls) == 1
    assert rewrite_calls[0]["loop"] is loop
    assert rewrite_calls[0]["memory_manager"] is memory_manager
    assert [call["context_type"] for call in memory_manager.semantic_calls] == ["skill", "resource"]
    assert all(call["namespace_prefix"] == ("catalog", "global") for call in memory_manager.semantic_calls)
    assert memory_manager.semantic_calls[0]["query"] == "rewritten skill query"
    assert memory_manager.semantic_calls[1]["query"] == "rewritten tool query"
    assert memory_manager.semantic_calls[0]["limit"] == 7
    assert memory_manager.semantic_calls[1]["limit"] == 5
    assert expected_contract_keys.issubset(set(result))


@pytest.mark.asyncio
async def test_build_frontdoor_catalog_selection_reranks_visible_dense_hits_and_applies_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = object()
    memory_manager = _MemoryManagerRecorder()
    memory_manager.set_response(
        context_type="skill",
        records=[
            {"record_id": "skill:visible-alpha", "score": 0.21},  # dict-shaped
            SimpleNamespace(record_id="skill:hidden-zeta", score=0.99),  # object-shaped
        ],
    )
    memory_manager.set_response(
        context_type="resource",
        records=[
            {"record_id": "tool:visible-browser", "score": 0.33},
            {"record_id": "tool:hidden-admin", "score": 0.98},
        ],
    )

    async def _rewrite_queries(**kwargs) -> dict[str, str]:
        _ = kwargs
        return {
            "skill_query": "rewritten skill query",
            "tool_query": "rewritten tool query",
        }

    rerank = _RerankRecorder(response=[])
    monkeypatch.setattr(selection_module, "rewrite_frontdoor_catalog_queries", _rewrite_queries)
    monkeypatch.setattr(selection_module, "rerank_frontdoor_catalog_records", rerank)

    expected_contract_keys = {"skill_ids", "tool_ids", "trace"}
    result = await selection_module.build_frontdoor_catalog_selection(
        loop=loop,
        memory_manager=memory_manager,
        query_text="browser-focused capabilities only",
        visible_skills=[{"skill_id": "visible-alpha"}],
        visible_families=[SimpleNamespace(tool_id="visible-browser")],
        skill_limit=1,
        tool_limit=1,
    )

    assert len(rerank.calls) >= 1
    assert all(call["namespace_prefix"] == ("catalog", "global") for call in memory_manager.semantic_calls)
    reranked_ids = {
        str(record.get("record_id") or "").strip()
        for call in rerank.calls
        for record in list(call["records"] or [])
    }
    assert "skill:hidden-zeta" not in reranked_ids
    assert "tool:hidden-admin" not in reranked_ids
    assert "skill:visible-alpha" in reranked_ids
    assert "tool:visible-browser" in reranked_ids
    assert all(int(call["top_n"]) == 1 for call in rerank.calls)
    assert expected_contract_keys.issubset(set(result))
