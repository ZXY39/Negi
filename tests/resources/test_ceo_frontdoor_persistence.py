from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor import _ceo_langgraph_impl as ceo_langgraph_impl
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner


class _CompiledGraphRecorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ainvoke(self, input, config=None, *, context=None, **kwargs):
        self.calls.append(
            {
                "input": input,
                "config": config,
                "context": context,
                "kwargs": kwargs,
            }
        )
        return {"final_output": "ok", "route_kind": "tool_result"}


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_passes_thread_id_and_runtime_context() -> None:
    ready_calls: list[str] = []

    async def _noop_ready() -> None:
        ready_calls.append("ready")

    loop = SimpleNamespace(_ensure_checkpointer_ready=_noop_ready)
    runner = CeoFrontDoorRunner(loop=loop)
    compiled_graph = _CompiledGraphRecorder()
    runner._compiled_graph = compiled_graph

    async def _on_progress(content: str, **kwargs) -> None:
        _ = content, kwargs

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
    )
    user_input = SimpleNamespace(content="persist this turn")

    output = await runner.run_turn(
        user_input=user_input,
        session=session,
        on_progress=_on_progress,
    )

    assert output == "ok"
    assert ready_calls == ["ready"]
    assert getattr(session, "_last_route_kind") == "tool_result"

    assert len(compiled_graph.calls) == 1
    call = compiled_graph.calls[0]
    assert call["config"] == {"configurable": {"thread_id": "web:shared"}}

    runtime_context = call["context"]
    assert runtime_context is not None
    assert getattr(runtime_context, "session_key") == "web:shared"

    graph_input = dict(call["input"] or {})
    assert graph_input["user_input"] is user_input
    assert "session" not in graph_input
    assert "on_progress" not in graph_input


def test_ceo_frontdoor_graph_compiles_with_checkpointer_and_store(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    compiled_graph = object()

    class _FakeStateGraph:
        def __init__(self, state_schema, **kwargs) -> None:
            captured["state_schema"] = state_schema
            captured["init_kwargs"] = dict(kwargs)

        def add_node(self, name, node) -> None:
            return None

        def add_edge(self, start, end) -> None:
            return None

        def add_conditional_edges(self, start, path, path_map) -> None:
            return None

        def compile(self, **kwargs):
            captured["compile_kwargs"] = dict(kwargs)
            return compiled_graph

    monkeypatch.setattr(ceo_langgraph_impl, "StateGraph", _FakeStateGraph)

    loop = SimpleNamespace(_checkpointer=object(), _store=object())
    runner = SimpleNamespace(
        _loop=loop,
        _graph_prepare_turn=object(),
        _graph_call_model=object(),
        _graph_normalize_model_output=object(),
        _graph_execute_tools=object(),
        _graph_finalize_turn=object(),
        _graph_next_step=object(),
    )

    result = ceo_langgraph_impl._build_langgraph_ceo_graph(runner)

    assert result is compiled_graph
    assert captured["state_schema"] is ceo_langgraph_impl.CeoGraphState
    assert captured["init_kwargs"] == {"context_schema": ceo_langgraph_impl.CeoRuntimeContext}
    assert captured["compile_kwargs"] == {
        "name": "ceo-frontdoor",
        "checkpointer": loop._checkpointer,
        "store": loop._store,
    }
