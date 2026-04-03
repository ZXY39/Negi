from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

if "litellm" not in sys.modules:
    litellm_stub = types.ModuleType("litellm")

    async def _unreachable_acompletion(*args, **kwargs):
        raise AssertionError("litellm acompletion should not be used in CEO persistence tests")

    litellm_stub.acompletion = _unreachable_acompletion
    litellm_stub.api_base = None
    litellm_stub.suppress_debug_info = True
    litellm_stub.drop_params = True
    sys.modules["litellm"] = litellm_stub

from g3ku.runtime.frontdoor import _ceo_langgraph_impl as ceo_langgraph_impl
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext
from g3ku.session.manager import SessionManager


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
    user_input = SimpleNamespace(content="persist this turn", metadata={"cron_job_id": "cron-1"})

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
    assert graph_input["user_input"] == {
        "content": "persist this turn",
        "metadata": {"cron_job_id": "cron-1"},
    }
    json.dumps(graph_input)
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


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_keeps_runtime_only_objects_out_of_checkpointed_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_langgraph_impl, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(ceo_langgraph_impl, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["message"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["message"],
            model_messages=[{"role": "system", "content": "SYSTEM PROMPT"}],
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=object(),
        inflight_turn_snapshot=lambda: {"snapshot": True},
    )
    user_input = SimpleNamespace(content="persist safely", metadata={"cron_job_id": "cron-1"})
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=lambda *args, **kwargs: None,
        )
    )

    state_update = await runner._graph_prepare_turn(
        {"user_input": user_input},
        runtime=runtime,
    )

    assert state_update["query_text"] == "persist safely"
    assert state_update["user_input"] == {
        "content": "persist safely",
        "metadata": {"cron_job_id": "cron-1"},
    }
    assert state_update["tool_names"] == ["message"]
    assert state_update["prompt_cache_key"] == "cache-key"
    assert "runtime_context" not in state_update
    assert "visible_tools" not in state_update
    assert "langchain_tools" not in state_update
    assert "langchain_tool_map" not in state_update
    checkpoint_state = {"user_input": user_input}
    checkpoint_state.update(state_update)
    json.dumps(checkpoint_state)


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_passes_checkpoint_messages_to_builder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_langgraph_impl, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(ceo_langgraph_impl, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
    )
    runner = CeoFrontDoorRunner(loop=loop)
    captured: dict[str, object] = {}

    runtime_session = loop.sessions.get_or_create("web:shared")
    runtime_session.add_message("user", "bootstrap transcript question")

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["message"]}

    async def _build_for_ceo(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            tool_names=["message"],
            model_messages=[{"role": "system", "content": "SYSTEM PROMPT"}],
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )
    user_input = SimpleNamespace(content="new question", metadata={"_transcript_turn_id": "turn-2"})
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )
    checkpoint_messages = [
        {"role": "system", "content": "OLD SYSTEM"},
        {"role": "user", "content": "checkpoint question"},
        {"role": "assistant", "content": "checkpoint answer"},
    ]

    await runner._graph_prepare_turn(
        {"user_input": user_input, "messages": checkpoint_messages},
        runtime=runtime,
    )

    assert captured["persisted_session"] is runtime_session
    assert captured["checkpoint_messages"] == checkpoint_messages
