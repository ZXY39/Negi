from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest
from langgraph.types import Command
from langchain_core.messages import AIMessage

if "litellm" not in sys.modules:
    litellm_stub = types.ModuleType("litellm")

    async def _unreachable_acompletion(*args, **kwargs):
        raise AssertionError("litellm acompletion should not be used in CEO persistence tests")

    litellm_stub.acompletion = _unreachable_acompletion
    litellm_stub.api_base = None
    litellm_stub.suppress_debug_info = True
    litellm_stub.drop_params = True
    sys.modules["litellm"] = litellm_stub

from g3ku.agent.tools.base import Tool
from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.runtime.frontdoor import _ceo_langgraph_impl as ceo_langgraph_impl
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.history_compaction import (
    FRONTDOOR_HISTORY_SUMMARY_MARKER,
    compact_frontdoor_history,
)
from g3ku.runtime.frontdoor.state_models import (
    CeoFrontdoorInterrupted,
    CeoPersistentState,
    CeoRuntimeContext,
)
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


class _FakeGraphOutput:
    def __init__(self, *, value: dict[str, object], interrupts=()) -> None:
        self.value = dict(value or {})
        self.interrupts = tuple(interrupts)


class _InterruptingCompiledGraph:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ainvoke(self, input, config=None, *, context=None, version="v1", **kwargs):
        self.calls.append(
            {
                "input": input,
                "config": config,
                "context": context,
                "version": version,
                "kwargs": kwargs,
            }
        )
        if isinstance(input, Command):
            return _FakeGraphOutput(
                value={"final_output": "approved reply", "route_kind": "direct_reply"},
                interrupts=(),
            )
        return _FakeGraphOutput(
            value={
                "route_kind": "direct_reply",
                "tool_call_payloads": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
            },
            interrupts=(SimpleNamespace(id="interrupt-1", value={"kind": "frontdoor_tool_approval"}),),
        )


class _MessageTool(Tool):
    @property
    def name(self) -> str:
        return "message"

    @property
    def description(self) -> str:
        return "send message"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        }

    async def execute(self, **kwargs):
        return kwargs


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


@pytest.mark.asyncio
async def test_ceo_frontdoor_run_turn_raises_structured_interrupt() -> None:
    async def _noop_ready() -> None:
        return None

    loop = SimpleNamespace(_ensure_checkpointer_ready=_noop_ready)
    runner = CeoFrontDoorRunner(loop=loop)
    runner._compiled_graph = _InterruptingCompiledGraph()
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"))

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=session,
            on_progress=None,
        )

    assert exc_info.value.interrupts[0].interrupt_id == "interrupt-1"
    assert exc_info.value.interrupts[0].value["kind"] == "frontdoor_tool_approval"


@pytest.mark.asyncio
async def test_ceo_frontdoor_run_turn_serializes_interrupt_payloads() -> None:
    class _OpaqueArg:
        def __str__(self) -> str:
            return "opaque-arg"

    class _OpaqueInterruptingCompiledGraph:
        async def ainvoke(self, input, config=None, *, context=None, version="v1", **kwargs):
            _ = input, config, context, version, kwargs
            payloads = [{"name": "create_async_task", "arguments": {"task": _OpaqueArg()}}]
            return _FakeGraphOutput(
                value={
                    "approval_request": {"kind": "frontdoor_tool_approval", "tool_calls": payloads},
                    "tool_call_payloads": payloads,
                },
                interrupts=(
                    SimpleNamespace(
                        id="interrupt-1",
                        value={"kind": "frontdoor_tool_approval", "tool_calls": payloads},
                    ),
                ),
            )

    async def _noop_ready() -> None:
        return None

    loop = SimpleNamespace(_ensure_checkpointer_ready=_noop_ready)
    runner = CeoFrontDoorRunner(loop=loop)
    runner._compiled_graph = _OpaqueInterruptingCompiledGraph()
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"))

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=session,
            on_progress=None,
        )

    assert exc_info.value.interrupts[0].interrupt_id == "interrupt-1"
    assert exc_info.value.interrupts[0].value == {
        "kind": "frontdoor_tool_approval",
        "tool_calls": [{"name": "create_async_task", "arguments": {"task": "opaque-arg"}}],
    }
    assert exc_info.value.values == {
        "approval_request": {
            "kind": "frontdoor_tool_approval",
            "tool_calls": [{"name": "create_async_task", "arguments": {"task": "opaque-arg"}}],
        },
        "tool_call_payloads": [{"name": "create_async_task", "arguments": {"task": "opaque-arg"}}],
    }
    json.dumps(
        {
            "interrupts": [
                {
                    "id": exc_info.value.interrupts[0].interrupt_id,
                    "value": exc_info.value.interrupts[0].value,
                }
            ],
            "values": exc_info.value.values,
        }
    )


@pytest.mark.asyncio
async def test_ceo_frontdoor_resume_turn_uses_command_resume_on_same_thread() -> None:
    async def _noop_ready() -> None:
        return None

    graph = _InterruptingCompiledGraph()
    loop = SimpleNamespace(_ensure_checkpointer_ready=_noop_ready)
    runner = CeoFrontDoorRunner(loop=loop)
    runner._compiled_graph = graph
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"))

    output = await runner.resume_turn(
        session=session,
        resume_value={"approved": True},
        on_progress=None,
    )

    assert output == "approved reply"
    assert isinstance(graph.calls[0]["input"], Command)
    assert graph.calls[0]["config"] == {"configurable": {"thread_id": "web:shared"}}
    assert graph.calls[0]["version"] == "v2"


def test_ceo_frontdoor_review_tool_calls_ignores_resume_payload_tool_call_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    original_payloads = [{"name": "create_async_task", "arguments": {"task": "original"}}]
    override_payloads = [{"name": "message", "arguments": {"content": "mutated"}}]

    monkeypatch.setattr(
        ceo_langgraph_impl,
        "interrupt",
        lambda value: {"approved": True, "tool_calls": override_payloads},
    )

    result = runner._graph_review_tool_calls(
        {
            "approval_request": {"kind": "frontdoor_tool_approval", "tool_calls": original_payloads},
            "tool_call_payloads": original_payloads,
        }
    )

    assert result["approval_status"] == "approved"
    assert result["tool_call_payloads"] == original_payloads


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
        _graph_review_tool_calls=object(),
        _graph_execute_tools=object(),
        _graph_finalize_turn=object(),
        _graph_next_step=object(),
    )

    result = ceo_langgraph_impl._build_langgraph_ceo_graph(runner)

    assert result is compiled_graph
    assert captured["state_schema"] is ceo_langgraph_impl.CeoPersistentState
    assert captured["state_schema"] is CeoPersistentState
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


@pytest.mark.asyncio
async def test_ceo_frontdoor_call_model_returns_json_safe_response_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])

    async def _call_model_with_tools(**kwargs):
        _ = kwargs
        return AIMessage(
            content="tool reply",
            tool_calls=[{"id": "call-1", "name": "filesystem", "args": {"path": "."}}],
            response_metadata={"finish_reason": "tool_calls"},
            additional_kwargs={
                "reasoning_content": "reasoning trace",
                "thinking_blocks": [{"type": "thinking", "text": "step one"}],
            },
        )

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    update = await runner._graph_call_model(
        {
            "messages": [{"role": "user", "content": "list files"}],
            "turn_overlay_text": None,
            "repair_overlay_text": None,
            "model_refs": ["openai_codex:gpt-test"],
            "parallel_enabled": False,
            "prompt_cache_key": "cache-key",
            "iteration": 0,
            "max_iterations": 4,
        },
        runtime=SimpleNamespace(context=CeoRuntimeContext(loop=None, session=None, session_key="web:shared", on_progress=None)),
    )

    assert update["iteration"] == 1
    assert update["repair_overlay_text"] is None
    assert "response_message" not in update
    assert "response_content" not in update
    assert update["response_payload"] == {
        "content": "tool reply",
        "tool_calls": [{"id": "call-1", "name": "filesystem", "arguments": {"path": "."}}],
        "finish_reason": "tool_calls",
        "error_text": "",
        "reasoning_content": "reasoning trace",
        "thinking_blocks": [{"type": "thinking", "text": "step one"}],
    }
    json.dumps(update)


@pytest.mark.asyncio
async def test_ceo_frontdoor_finalize_turn_persists_direct_reply_into_checkpoint_messages(tmp_path) -> None:
    loop = SimpleNamespace(
        sessions=SessionManager(tmp_path),
    )
    runner = CeoFrontDoorRunner(loop=loop)

    result = await runner._graph_finalize_turn(
        {
            "messages": [
                {"role": "system", "content": "SYSTEM PROMPT"},
                {"role": "user", "content": "plain question"},
            ],
            "final_output": "plain answer",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "plain question",
        }
    )

    assert result["final_output"] == "plain answer"
    assert result["messages"] == [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "plain question"},
        {"role": "assistant", "content": "plain answer"},
    ]


def test_memory_assembly_config_exposes_frontdoor_compaction_defaults() -> None:
    config = MemoryAssemblyConfig()

    assert config.frontdoor_recent_message_count == 8
    assert config.frontdoor_summary_trigger_message_count == 24
    assert config.frontdoor_interrupt_tool_names == ["message", "create_async_task"]


def test_frontdoor_history_compaction_inserts_summary_marker_and_keeps_recent_tail() -> None:
    messages = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "assistant", "content": "## Retrieved Context\n- prior memory"},
        {"role": "user", "content": "question one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "question two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "question three"},
    ]

    compacted = compact_frontdoor_history(
        messages,
        recent_message_count=3,
        summary_trigger_message_count=4,
    )

    assert compacted[:2] == messages[:2]
    assert compacted[-3:] == messages[-3:]
    assert len(compacted) == 6

    summary_message = compacted[2]
    assert summary_message["role"] == "assistant"
    assert FRONTDOOR_HISTORY_SUMMARY_MARKER in str(summary_message["content"])
    assert summary_message["metadata"]["summary_version"] == 1
    assert "question one" in str(summary_message["content"])
    assert "answer one" in str(summary_message["content"])
    assert "question two" not in str(summary_message["content"])


def test_frontdoor_history_compaction_preserves_existing_summary_content_on_repeated_passes() -> None:
    first_messages = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "question one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "question two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "question three"},
    ]
    first_compacted = compact_frontdoor_history(
        first_messages,
        recent_message_count=3,
        summary_trigger_message_count=4,
    )
    first_summary = dict(first_compacted[1])
    first_summary_text = str(first_summary["content"])
    assert "question one" in first_summary_text
    assert "answer one" in first_summary_text
    assert first_summary["metadata"]["compacted_message_count"] == 2

    second_messages = [
        *first_compacted,
        {"role": "assistant", "content": "answer three"},
        {"role": "user", "content": "question four"},
        {"role": "assistant", "content": "answer four"},
    ]

    second_compacted = compact_frontdoor_history(
        second_messages,
        recent_message_count=3,
        summary_trigger_message_count=4,
    )

    second_summary = dict(second_compacted[1])
    second_summary_text = str(second_summary["content"])
    assert FRONTDOOR_HISTORY_SUMMARY_MARKER in second_summary_text
    assert "prior frontdoor history was already compacted" not in second_summary_text
    assert first_summary_text in second_summary_text
    assert "question two" in second_summary_text
    assert "answer three" not in second_summary_text
    assert second_summary["metadata"]["compacted_message_count"] == 5
    assert second_compacted[-3:] == [
        {"role": "assistant", "content": "answer three"},
        {"role": "user", "content": "question four"},
        {"role": "assistant", "content": "answer four"},
    ]


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_compacts_history_into_summary_block(
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
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                frontdoor_recent_message_count=3,
                frontdoor_summary_trigger_message_count=4,
            )
        ),
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["message"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["message"],
            model_messages=[
                {"role": "system", "content": "SYSTEM PROMPT"},
                {"role": "user", "content": "question one"},
                {"role": "assistant", "content": "answer one"},
                {"role": "user", "content": "question two"},
                {"role": "assistant", "content": "answer two"},
            ],
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
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    state_update = await runner._graph_prepare_turn(
        {"user_input": SimpleNamespace(content="question three", metadata={})},
        runtime=runtime,
    )

    messages = list(state_update["messages"] or [])
    assert messages[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert FRONTDOOR_HISTORY_SUMMARY_MARKER in str(messages[1]["content"])
    assert FRONTDOOR_HISTORY_SUMMARY_MARKER in str(state_update["summary_text"])
    assert state_update["summary_version"] == 1
    assert messages[-3:] == [
        {"role": "user", "content": "question two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "question three"},
    ]


@pytest.mark.asyncio
async def test_ceo_frontdoor_finalize_turn_preserves_summary_state_from_compacted_messages(tmp_path) -> None:
    loop = SimpleNamespace(
        sessions=SessionManager(tmp_path),
    )
    runner = CeoFrontDoorRunner(loop=loop)

    messages = compact_frontdoor_history(
        [
            {"role": "system", "content": "SYSTEM PROMPT"},
            {"role": "assistant", "content": "## Retrieved Context\n- prior memory"},
            {"role": "user", "content": "question one"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "question two"},
            {"role": "assistant", "content": "answer two"},
            {"role": "user", "content": "question three"},
        ],
        recent_message_count=3,
        summary_trigger_message_count=4,
    )

    result = await runner._graph_finalize_turn(
        {
            "messages": messages,
            "final_output": "final answer",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "question three",
        }
    )

    assert FRONTDOOR_HISTORY_SUMMARY_MARKER in str(result["summary_text"])
    assert result["summary_version"] == 1


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_prompt_cache_key_changes_when_stable_prefix_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_langgraph_impl, "current_project_environment", lambda workspace_root=None: {})

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

    prompt_variants = iter(
        [
            SimpleNamespace(
                tool_names=["message"],
                model_messages=[
                    {"role": "system", "content": "SYSTEM PROMPT A"},
                    {"role": "user", "content": "same question"},
                ],
            ),
            SimpleNamespace(
                tool_names=["message"],
                model_messages=[
                    {"role": "system", "content": "SYSTEM PROMPT B"},
                    {"role": "user", "content": "same question"},
                ],
            ),
        ]
    )

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return next(prompt_variants)

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
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    first = await runner._graph_prepare_turn(
        {"user_input": SimpleNamespace(content="same question", metadata={})},
        runtime=runtime,
    )
    second = await runner._graph_prepare_turn(
        {"user_input": SimpleNamespace(content="same question", metadata={})},
        runtime=runtime,
    )

    assert str(first["prompt_cache_key"] or "").strip()
    assert str(second["prompt_cache_key"] or "").strip()
    assert first["prompt_cache_key"] != second["prompt_cache_key"]


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_records_prompt_cache_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    class _MessageTool(Tool):
        @property
        def name(self) -> str:
            return "message"

        @property
        def description(self) -> str:
            return "send message"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            }

        async def execute(self, **kwargs):
            return kwargs

    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_langgraph_impl, "current_project_environment", lambda workspace_root=None: {})

    loop = SimpleNamespace(
        _ensure_checkpointer_ready=_noop_ready,
        sessions=SessionManager(tmp_path),
        _checkpointer=None,
        _store=None,
        main_task_service=None,
        tools={"message": _MessageTool()},
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
            model_messages=[
                {"role": "system", "content": "SYSTEM PROMPT"},
                {"role": "user", "content": "question one"},
            ],
            turn_overlay_text="## Retrieved Context\n- memory",
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
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=loop, session=session, session_key="web:shared", on_progress=None)
    )

    state_update = await runner._graph_prepare_turn(
        {"user_input": SimpleNamespace(content="question one", metadata={})},
        runtime=runtime,
    )

    diagnostics = dict(state_update["prompt_cache_diagnostics"] or {})
    assert str(diagnostics["stable_prompt_signature"] or "").strip()
    assert diagnostics["tool_signature_count"] == 1
    assert str(diagnostics["tool_signature_hash"] or "").strip()
    assert diagnostics["overlay_present"] is True
    assert diagnostics["overlay_section_count"] == 1
    assert str(diagnostics["overlay_text_hash"] or "").strip()


@pytest.mark.asyncio
async def test_graph_prepare_turn_uses_model_summarizer_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = SimpleNamespace(
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                frontdoor_summarizer_enabled=True,
                frontdoor_summarizer_model_key="summary-model",
                frontdoor_summarizer_trigger_message_count=6,
                frontdoor_summarizer_keep_message_count=4,
            )
        )
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _fake_model_invoke(prompt: dict[str, object]) -> dict[str, object]:
        assert prompt["messages"]
        return {
            "stable_preferences": ["reply concisely"],
            "stable_facts": ["fact"],
            "open_loops": ["follow up"],
            "recent_actions": ["summarized history"],
            "narrative": "CEO frontdoor durable context.",
        }

    monkeypatch.setattr(runner, "_invoke_summary_model", _fake_model_invoke)

    result = await runner._graph_prepare_turn(
        {
            "messages": [{"role": "user", "content": f"message {idx}"} for idx in range(10)],
            "user_input": {"content": "follow up", "metadata": {}},
        },
        runtime=SimpleNamespace(context=SimpleNamespace(session=None)),
    )

    assert "## CEO Durable Summary" in result["summary_text"]
    assert result["summary_payload"] == {
        "stable_preferences": ["reply concisely"],
        "stable_facts": ["fact"],
        "open_loops": ["follow up"],
        "recent_actions": ["summarized history"],
        "narrative": "CEO frontdoor durable context.",
    }
    assert result["summary_model_key"] == "summary-model"
    assert "## CEO Durable Summary" in str(result["messages"][0]["content"])


@pytest.mark.asyncio
async def test_invoke_summary_model_uses_explicit_model_key_and_parses_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}
    fake_config = object()

    class _FakeModel:
        async def ainvoke(self, messages):
            captured["messages"] = list(messages)
            return SimpleNamespace(
                content='```json\n{"stable_facts":["fact"],"narrative":"brief","stable_preferences":[],"open_loops":[],"recent_actions":[]}\n```'
            )

    def _fake_get_runtime_config(force: bool = False):
        captured["force"] = force
        return fake_config, 7, False

    def _fake_build_chat_model(config, *, role=None, model_key=None):
        captured["config"] = config
        captured["role"] = role
        captured["model_key"] = model_key
        return _FakeModel()

    monkeypatch.setattr("g3ku.config.live_runtime.get_runtime_config", _fake_get_runtime_config)
    monkeypatch.setattr("g3ku.providers.chatmodels.build_chat_model", _fake_build_chat_model)

    result = await runner._invoke_summary_model(
        {
            "previous_summary_text": "",
            "previous_summary_payload": {},
            "messages": [{"role": "user", "content": "message 1"}],
        },
        explicit_model_key="summary-model",
    )

    assert result == {
        "stable_facts": ["fact"],
        "narrative": "brief",
        "stable_preferences": [],
        "open_loops": [],
        "recent_actions": [],
    }
    assert captured["force"] is False
    assert captured["config"] is fake_config
    assert captured["model_key"] == "summary-model"
    assert captured["role"] is None
    assert "strict JSON" in str(captured["messages"][0]["content"])


@pytest.mark.asyncio
async def test_graph_prepare_turn_real_session_path_carries_model_summary_state(
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
        tools={"message": _MessageTool()},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                frontdoor_summarizer_enabled=True,
                frontdoor_summarizer_model_key="summary-model",
                frontdoor_summarizer_trigger_message_count=4,
                frontdoor_summarizer_keep_message_count=3,
            )
        ),
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["message"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["message"],
            model_messages=[
                {"role": "system", "content": "SYSTEM PROMPT"},
                {"role": "user", "content": "question one"},
                {"role": "assistant", "content": "answer one"},
                {"role": "user", "content": "question two"},
                {"role": "assistant", "content": "answer two"},
            ],
        )

    async def _fake_model_invoke(prompt: dict[str, object]) -> dict[str, object]:
        assert prompt["messages"]
        return {
            "stable_preferences": ["reply concisely"],
            "stable_facts": ["fact"],
            "open_loops": ["follow up"],
            "recent_actions": ["summarized history"],
            "narrative": "CEO frontdoor durable context.",
        }

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])
    monkeypatch.setattr(runner, "_invoke_summary_model", _fake_model_invoke)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
    )
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(
            loop=loop,
            session=session,
            session_key="web:shared",
            on_progress=None,
        )
    )

    result = await runner._graph_prepare_turn(
        {"user_input": SimpleNamespace(content="question three", metadata={})},
        runtime=runtime,
    )

    assert "## CEO Durable Summary" in str(result["summary_text"])
    assert result["summary_payload"] == {
        "stable_preferences": ["reply concisely"],
        "stable_facts": ["fact"],
        "open_loops": ["follow up"],
        "recent_actions": ["summarized history"],
        "narrative": "CEO frontdoor durable context.",
    }
    assert result["summary_model_key"] == "summary-model"
    assert "## CEO Durable Summary" in str(result["messages"][1]["content"])


@pytest.mark.asyncio
async def test_graph_finalize_turn_preserves_model_summary_state_on_direct_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = SimpleNamespace(
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                frontdoor_summarizer_enabled=True,
                frontdoor_summarizer_model_key="summary-model",
                frontdoor_summarizer_trigger_message_count=4,
                frontdoor_summarizer_keep_message_count=3,
            )
        )
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _fake_model_invoke(prompt: dict[str, object]) -> dict[str, object]:
        assert prompt["messages"]
        return {
            "stable_preferences": ["reply concisely"],
            "stable_facts": ["fact"],
            "open_loops": ["follow up"],
            "recent_actions": ["finalized reply"],
            "narrative": "CEO frontdoor durable context.",
        }

    monkeypatch.setattr(runner, "_invoke_summary_model", _fake_model_invoke)

    result = await runner._graph_finalize_turn(
        {
            "messages": [{"role": "user", "content": f"message {idx}"} for idx in range(6)],
            "final_output": "final answer",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "message 5",
            "summary_payload": {"stable_facts": ["old fact"]},
            "summary_model_key": "summary-model",
        }
    )

    assert "## CEO Durable Summary" in str(result["summary_text"])
    assert result["summary_payload"] == {
        "stable_preferences": ["reply concisely"],
        "stable_facts": ["fact"],
        "open_loops": ["follow up"],
        "recent_actions": ["finalized reply"],
        "narrative": "CEO frontdoor durable context.",
    }
    assert result["summary_model_key"] == "summary-model"
