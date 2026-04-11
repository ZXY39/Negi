from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import g3ku.runtime.context.frontdoor_catalog_selection as selection_module
import g3ku.runtime.context.semantic_scope as semantic_scope_module


class _MemoryManagerRecorder:
    def __init__(self) -> None:
        self.semantic_calls: list[dict[str, Any]] = []
        self._responses: dict[str, list[Any]] = {}
        self.store = SimpleNamespace(_dense_enabled=True)

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
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = dict(response or {})

    async def __call__(
        self,
        *,
        memory_manager: Any,
        query_text: str,
        records: list[dict[str, Any]],
        top_n: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "memory_manager": memory_manager,
                "query_text": str(query_text),
                "records": list(records),
                "top_n": int(top_n),
            }
        )
        return {
            "records": list(self.response.get("records") or []),
            "trace": dict(self.response.get("trace") or {}),
        }


class _ConfigStub:
    def resolve_role_model_key(self, role: str) -> str:
        assert role == "ceo"
        return "responses:gpt-5.1-mini"


@pytest.mark.asyncio
async def test_rewrite_frontdoor_catalog_queries_uses_model_backed_rewrite_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(selection_module, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def _invoke_model_rewrite(**kwargs) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "skill_query": "browser automation helper skill workflow browser-workflow",
            "tool_query": "browser automation helper tool resource agent_browser",
            "model": "responses:gpt-5.1-mini",
        }

    monkeypatch.setattr(selection_module, "_invoke_frontdoor_catalog_rewrite_model", _invoke_model_rewrite)

    result = await selection_module.rewrite_frontdoor_catalog_queries(
        loop=SimpleNamespace(workspace="D:/NewProjects/G3KU"),
        memory_manager=SimpleNamespace(store=SimpleNamespace(_dense_enabled=True), workspace="D:/NewProjects/G3KU"),
        query_text="find browser automation helpers",
        visible_skills=[SimpleNamespace(skill_id="browser-workflow")],
        visible_families=[SimpleNamespace(tool_id="agent_browser")],
    )

    assert len(calls) == 1
    assert calls[0]["query_text"] == "find browser automation helpers"
    assert calls[0]["visible_skill_ids"] == ["browser-workflow"]
    assert calls[0]["visible_tool_ids"] == ["agent_browser"]
    assert result["raw_query"] == "find browser automation helpers"
    assert result["status"] == "rewritten"
    assert result["model"] == "responses:gpt-5.1-mini"
    assert str(result["skill_query"] or "").strip()
    assert str(result["tool_query"] or "").strip()
    assert result["skill_query"] != result["raw_query"]
    assert result["tool_query"] != result["raw_query"]
    assert result["skill_query"] != result["tool_query"]
    assert "browser-workflow" in result["skill_query"]
    assert "agent_browser" in result["tool_query"]
    assert "browser" in result["skill_query"].lower()
    assert "browser" in result["tool_query"].lower()


@pytest.mark.asyncio
async def test_rewrite_frontdoor_catalog_queries_fallback_does_not_claim_model_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selection_module, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def _invoke_model_rewrite(**kwargs) -> dict[str, Any]:
        raise RuntimeError("rewrite model unavailable")

    monkeypatch.setattr(selection_module, "_invoke_frontdoor_catalog_rewrite_model", _invoke_model_rewrite)

    result = await selection_module.rewrite_frontdoor_catalog_queries(
        loop=SimpleNamespace(workspace="D:/NewProjects/G3KU"),
        memory_manager=SimpleNamespace(store=SimpleNamespace(_dense_enabled=True), workspace="D:/NewProjects/G3KU"),
        query_text="find browser automation helpers",
        visible_skills=[SimpleNamespace(skill_id="browser-workflow")],
        visible_families=[SimpleNamespace(tool_id="agent_browser")],
    )

    assert result["raw_query"] == "find browser automation helpers"
    assert result["status"] == "fallback"
    assert result["model"] == ""
    assert result["skill_query"] != result["raw_query"]
    assert result["tool_query"] != result["raw_query"]


@pytest.mark.asyncio
async def test_rewrite_frontdoor_catalog_queries_keeps_valid_skill_side_when_tool_side_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selection_module, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def _invoke_model_rewrite(**kwargs) -> dict[str, Any]:
        _ = kwargs
        return {
            "skill_query": "focused browser workflow browser-workflow",
            "tool_query": "",
            "model": "responses:gpt-5.1-mini",
        }

    monkeypatch.setattr(selection_module, "_invoke_frontdoor_catalog_rewrite_model", _invoke_model_rewrite)

    result = await selection_module.rewrite_frontdoor_catalog_queries(
        loop=SimpleNamespace(workspace="D:/NewProjects/G3KU"),
        memory_manager=SimpleNamespace(store=SimpleNamespace(_dense_enabled=True), workspace="D:/NewProjects/G3KU"),
        query_text="find browser automation helpers",
        visible_skills=[SimpleNamespace(skill_id="browser-workflow")],
        visible_families=[],
    )

    assert result["status"] == "rewritten"
    assert result["model"] == "responses:gpt-5.1-mini"
    assert result["skill_query"] == "focused browser workflow browser-workflow"
    assert str(result["tool_query"] or "").strip()


@pytest.mark.asyncio
async def test_rewrite_frontdoor_catalog_queries_keeps_valid_tool_side_when_skill_side_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selection_module, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def _invoke_model_rewrite(**kwargs) -> dict[str, Any]:
        _ = kwargs
        return {
            "skill_query": "",
            "tool_query": "focused browser tool agent_browser",
            "model": "responses:gpt-5.1-mini",
        }

    monkeypatch.setattr(selection_module, "_invoke_frontdoor_catalog_rewrite_model", _invoke_model_rewrite)

    result = await selection_module.rewrite_frontdoor_catalog_queries(
        loop=SimpleNamespace(workspace="D:/NewProjects/G3KU"),
        memory_manager=SimpleNamespace(store=SimpleNamespace(_dense_enabled=True), workspace="D:/NewProjects/G3KU"),
        query_text="find browser automation helpers",
        visible_skills=[],
        visible_families=[SimpleNamespace(tool_id="agent_browser")],
    )

    assert result["status"] == "rewritten"
    assert result["model"] == "responses:gpt-5.1-mini"
    assert str(result["skill_query"] or "").strip()
    assert result["tool_query"] == "focused browser tool agent_browser"


@pytest.mark.asyncio
async def test_rewrite_frontdoor_catalog_queries_accepts_same_rewritten_query_on_both_sides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(selection_module, "_frontdoor_query_rewrite_enabled", lambda: True)

    async def _invoke_model_rewrite(**kwargs) -> dict[str, Any]:
        _ = kwargs
        return {
            "skill_query": "browser automation selector",
            "tool_query": "browser automation selector",
            "model": "responses:gpt-5.1-mini",
        }

    monkeypatch.setattr(selection_module, "_invoke_frontdoor_catalog_rewrite_model", _invoke_model_rewrite)

    result = await selection_module.rewrite_frontdoor_catalog_queries(
        loop=SimpleNamespace(workspace="D:/NewProjects/G3KU"),
        memory_manager=SimpleNamespace(store=SimpleNamespace(_dense_enabled=True), workspace="D:/NewProjects/G3KU"),
        query_text="find browser automation helpers",
        visible_skills=[SimpleNamespace(skill_id="browser-workflow")],
        visible_families=[SimpleNamespace(tool_id="agent_browser")],
    )

    assert result["status"] == "rewritten"
    assert result["skill_query"] == "browser automation selector"
    assert result["tool_query"] == "browser automation selector"


@pytest.mark.asyncio
async def test_invoke_frontdoor_catalog_rewrite_model_parses_valid_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Model:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, Any]]] = []

        async def ainvoke(self, messages: list[dict[str, Any]]) -> str:
            self.calls.append(list(messages))
            return json.dumps(
                {
                    "skill_query": "focused skill browser-workflow",
                    "tool_query": "focused tool agent_browser",
                }
            )

    model = _Model()
    monkeypatch.setattr(selection_module, "get_runtime_config", lambda force=False: (_ConfigStub(), 1, False))
    monkeypatch.setattr(selection_module, "build_chat_model", lambda config, role=None: model)

    result = await selection_module._invoke_frontdoor_catalog_rewrite_model(
        loop=SimpleNamespace(),
        memory_manager=None,
        query_text="find browser automation helpers",
        visible_skill_ids=["browser-workflow"],
        visible_tool_ids=["agent_browser"],
    )

    assert result == {
        "skill_query": "focused skill browser-workflow",
        "tool_query": "focused tool agent_browser",
        "model": "responses:gpt-5.1-mini",
    }
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_invoke_frontdoor_catalog_rewrite_model_returns_empty_queries_on_malformed_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Model:
        async def ainvoke(self, messages: list[dict[str, Any]]) -> str:
            _ = messages
            return "Sure, here is the rewrite:\nnot valid json"

    monkeypatch.setattr(selection_module, "get_runtime_config", lambda force=False: (_ConfigStub(), 1, False))
    monkeypatch.setattr(selection_module, "build_chat_model", lambda config, role=None: _Model())

    result = await selection_module._invoke_frontdoor_catalog_rewrite_model(
        loop=SimpleNamespace(),
        memory_manager=None,
        query_text="find browser automation helpers",
        visible_skill_ids=["browser-workflow"],
        visible_tool_ids=["agent_browser"],
    )

    assert result == {
        "skill_query": "",
        "tool_query": "",
        "model": "responses:gpt-5.1-mini",
    }


@pytest.mark.asyncio
async def test_build_frontdoor_catalog_selection_uses_rewritten_queries_for_dense_search(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = object()
    memory_manager = _MemoryManagerRecorder()
    rewrite_calls: list[dict[str, Any]] = []

    async def _rewrite_queries(**kwargs) -> dict[str, str]:
        rewrite_calls.append(dict(kwargs))
        return {
            "raw_query": "find browser automation helpers",
            "skill_query": "rewritten skill query",
            "tool_query": "rewritten tool query",
            "status": "rewritten",
            "model": "frontdoor-query-rewriter",
        }

    async def _rerank_passthrough(**kwargs) -> dict[str, Any]:
        records = kwargs.get("records")
        return {
            "records": list(records if isinstance(records, list) else []),
            "trace": {
                "status": "passthrough",
                "model": "",
                "top_n": int(kwargs.get("top_n") or 0),
                "scores": [],
            },
        }

    monkeypatch.setattr(selection_module, "rewrite_frontdoor_catalog_queries", _rewrite_queries)
    monkeypatch.setattr(selection_module, "rerank_frontdoor_catalog_records", _rerank_passthrough)

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
    assert result["mode"] == "dense_only"
    assert result["available"] is True
    assert result["skill_ids"] == []
    assert result["tool_ids"] == []
    assert result["trace"]["queries"] == {
        "raw_query": "find browser automation helpers",
        "skill_query": "rewritten skill query",
        "tool_query": "rewritten tool query",
        "status": "rewritten",
        "model": "frontdoor-query-rewriter",
    }
    assert set(result["trace"]) == {"queries", "dense", "rerank"}


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
            "raw_query": "browser-focused capabilities only",
            "skill_query": "rewritten skill query",
            "tool_query": "rewritten tool query",
            "status": "rewritten",
            "model": "frontdoor-query-rewriter",
        }

    rerank = _RerankRecorder(
        response={
            "records": [
                {"record_id": "skill:visible-alpha", "score": 0.81},
                {"record_id": "tool:visible-browser", "score": 0.73},
            ],
            "trace": {
                "status": "configured",
                "model": "dashscope:qwen3-vl-rerank",
                "scores": [
                    {"record_id": "skill:visible-alpha", "score": 0.81},
                    {"record_id": "tool:visible-browser", "score": 0.73},
                ],
            },
        }
    )
    monkeypatch.setattr(selection_module, "rewrite_frontdoor_catalog_queries", _rewrite_queries)
    monkeypatch.setattr(selection_module, "rerank_frontdoor_catalog_records", rerank)

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
    assert result["mode"] == "dense_only"
    assert result["available"] is True
    assert result["skill_ids"] == ["visible-alpha"]
    assert result["tool_ids"] == ["visible-browser"]
    assert result["trace"]["dense"]["skills"] == [
        {"record_id": "skill:visible-alpha", "skill_id": "visible-alpha", "dense_rank": 1}
    ]
    assert result["trace"]["dense"]["tools"] == [
        {
            "record_id": "tool:visible-browser",
            "tool_id": "visible-browser",
            "executor_name": "visible-browser",
            "family_id": "visible-browser",
            "dense_rank": 1,
        }
    ]
    assert result["trace"]["rerank"]["skills"]["model"] == "dashscope:qwen3-vl-rerank"
    assert result["trace"]["rerank"]["tools"]["status"] == "configured"


@pytest.mark.asyncio
async def test_build_frontdoor_catalog_selection_enforces_unavailable_when_dense_backend_missing() -> None:
    memory_manager = _MemoryManagerRecorder()
    memory_manager.store = SimpleNamespace(_dense_enabled=False)

    result = await selection_module.build_frontdoor_catalog_selection(
        loop=object(),
        memory_manager=memory_manager,
        query_text="find browser automation helpers",
        visible_skills=[SimpleNamespace(skill_id="browser-workflow")],
        visible_families=[SimpleNamespace(tool_id="agent_browser")],
        skill_limit=7,
        tool_limit=5,
    )

    assert result["mode"] == "unavailable"
    assert result["available"] is False
    assert result["skill_ids"] == []
    assert result["tool_ids"] == []


@pytest.mark.asyncio
async def test_plan_retrieval_scope_falls_back_to_visible_ids_when_dense_hits_are_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_manager = _MemoryManagerRecorder()
    memory_manager.set_response(
        context_type="skill",
        records=[SimpleNamespace(record_id="skill:hidden-zeta", score=0.99)],
    )
    memory_manager.set_response(
        context_type="resource",
        records=[SimpleNamespace(record_id="tool:hidden-admin", score=0.98)],
    )

    async def _rewrite_queries(**kwargs) -> dict[str, str]:
        _ = kwargs
        return {
            "raw_query": "browser-focused capabilities only",
            "skill_query": "rewritten skill query",
            "tool_query": "rewritten tool query",
            "status": "rewritten",
            "model": "frontdoor-query-rewriter",
        }

    async def _rerank_passthrough(**kwargs) -> dict[str, Any]:
        records = kwargs.get("records")
        return {
            "records": list(records if isinstance(records, list) else []),
            "trace": {
                "status": "passthrough",
                "model": "",
                "top_n": int(kwargs.get("top_n") or 0),
                "scores": [],
            },
        }

    monkeypatch.setattr(selection_module, "rewrite_frontdoor_catalog_queries", _rewrite_queries)
    monkeypatch.setattr(selection_module, "rerank_frontdoor_catalog_records", _rerank_passthrough)

    semantic_frontdoor = await selection_module.build_frontdoor_catalog_selection(
        loop=object(),
        memory_manager=memory_manager,
        query_text="browser-focused capabilities only",
        visible_skills=[{"skill_id": "visible-alpha"}],
        visible_families=[SimpleNamespace(tool_id="visible-browser")],
        skill_limit=1,
        tool_limit=1,
    )
    retrieval_scope = semantic_scope_module.plan_retrieval_scope(
        visible_skills=[{"skill_id": "visible-alpha"}],
        visible_families=[SimpleNamespace(tool_id="visible-browser")],
        semantic_frontdoor=semantic_frontdoor,
    )

    assert semantic_frontdoor["available"] is True
    assert semantic_frontdoor["skill_ids"] == []
    assert semantic_frontdoor["tool_ids"] == []
    assert retrieval_scope == {
        "mode": "dense_only",
        "search_context_types": ["memory", "skill", "resource"],
        "allowed_context_types": ["memory", "skill", "resource"],
        "allowed_resource_record_ids": ["tool:visible-browser"],
        "allowed_skill_record_ids": ["skill:visible-alpha"],
    }


@pytest.mark.asyncio
async def test_rerank_frontdoor_catalog_records_returns_records_and_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeReranker:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def rerank(self, *, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
            self.calls.append(
                {
                    "query": str(query),
                    "documents": list(documents),
                    "top_n": int(top_n),
                }
            )
            return [(1, 0.97), (0, 0.41)]

    fake_reranker = _FakeReranker()

    def _resolve(memory_manager: Any) -> tuple[Any, str, str]:
        assert memory_manager is not None
        return fake_reranker, "dashscope:qwen3-vl-rerank", "configured"

    monkeypatch.setattr(selection_module, "_resolve_frontdoor_catalog_reranker", _resolve)

    result = await selection_module.rerank_frontdoor_catalog_records(
        memory_manager=SimpleNamespace(),
        query_text="focused browser workflow",
        records=[
            {"record_id": "skill:first", "l1": "first doc"},
            {"record_id": "skill:second", "l1": "second doc"},
        ],
        top_n=1,
    )

    assert result["records"] == [
        {"record_id": "skill:second", "l1": "second doc"},
        {"record_id": "skill:first", "l1": "first doc"},
    ]
    assert result["trace"] == {
        "status": "configured",
        "model": "dashscope:qwen3-vl-rerank",
        "top_n": 1,
        "scores": [
            {"record_id": "skill:second", "score": 0.97, "rerank_rank": 1},
        ],
    }
    assert fake_reranker.calls == [
        {
            "query": "focused browser workflow",
            "documents": ["first doc", "second doc"],
            "top_n": 1,
        }
    ]
