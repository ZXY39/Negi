from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

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
from g3ku.runtime.context.types import ContextAssemblyResult
from g3ku.runtime.api import websocket_ceo
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor import checkpoint_inspection
from g3ku.runtime.frontdoor import prompt_cache_contract
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.frontdoor.state_models import (
    CeoFrontdoorInterrupted,
    CeoPersistentState,
    CeoRuntimeContext,
)
from g3ku.session.manager import SessionManager


def _frontdoor_tool_contract_payload(message: dict[str, object]) -> dict[str, object] | None:
    if str(message.get("role") or "").strip().lower() != "user":
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("message_type") or "").strip() != "frontdoor_runtime_tool_contract":
        return None
    return dict(payload)


def test_ceo_snapshot_keeps_execution_trace_summary_and_compression_payloads() -> None:
    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {
                "role": "assistant",
                "content": "stage running",
                "execution_trace_summary": {
                    "active_stage_id": "frontdoor-stage-1",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "frontdoor-stage-1",
                            "stage_goal": "inspect repository",
                            "rounds": [{"round_index": 1, "tools": [{"tool_name": "filesystem"}]}],
                        }
                    ],
                },
                "compression": {"status": "running", "text": "上下文压缩中", "source": "user"},
                "tool_events": [{"tool_name": "skill-installer", "status": "running"}],
            }
        ]
    )

    assert snapshot[0]["execution_trace_summary"]["stages"][0]["stage_goal"] == "inspect repository"
    assert snapshot[0]["compression"]["status"] == "running"
    assert "tool_events" not in snapshot[0]


def test_ceo_snapshot_keeps_legacy_tool_events_when_new_trace_fields_absent() -> None:
    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_events": [
                    {
                        "status": "running",
                        "tool_name": "skill-installer",
                        "text": "starting install",
                        "tool_call_id": "skill-installer:1",
                        "source": "user",
                    }
                ],
            }
        ]
    )

    assert len(snapshot) == 1
    assert "execution_trace_summary" not in snapshot[0]
    assert snapshot[0]["tool_events"][0]["tool_name"] == "skill-installer"
    assert snapshot[0]["tool_events"][0]["status"] == "running"


def test_execution_snapshot_history_keeps_legacy_tool_events_without_stage_trace() -> None:
    runtime_session = SimpleNamespace(
        inflight_turn_snapshot=lambda: {
            "status": "running",
            "user_message": {"content": "install weather skill"},
            "tool_events": [
                {
                    "status": "success",
                    "tool_name": "skill-installer",
                    "text": "installed weather",
                    "tool_call_id": "skill-installer:1",
                    "source": "user",
                }
            ],
        },
        paused_execution_context_snapshot=lambda: None,
        state=SimpleNamespace(session_key="web:shared"),
    )

    history, source = web_ceo_sessions.extract_execution_live_raw_tail(
        runtime_session,
        None,
        require_active_stage=False,
    )

    assert source == "live_runtime"
    assert history[0] == {"role": "user", "content": "install weather skill"}
    assert history[1]["tool_events"][0]["tool_name"] == "skill-installer"
    assert "Recent tool results:" in history[1]["content"]


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
        ceo_runtime_ops,
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


def test_ceo_frontdoor_approval_request_disabled_by_default() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

    result = runner._approval_request_for_tool_calls(
        [{"name": "create_async_task", "arguments": {"task": "demo"}}]
    )

    assert result is None


def test_ceo_frontdoor_approval_request_respects_enabled_flag() -> None:
    loop = SimpleNamespace(
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(
                frontdoor_interrupt_approval_enabled=True,
                frontdoor_interrupt_tool_names=["create_async_task"],
            )
        )
    )
    runner = CeoFrontDoorRunner(loop=loop)

    result = runner._approval_request_for_tool_calls(
        [{"name": "create_async_task", "arguments": {"task": "demo"}}]
    )

    assert result == {
        "kind": "frontdoor_tool_approval",
        "question": "Approve the CEO frontdoor tool execution?",
        "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
    }


def test_ceo_frontdoor_get_compiled_graph_uses_explicit_state_graph_with_checkpointer_and_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = SimpleNamespace(_checkpointer=InMemorySaver(), _store=object())
    runner = CeoFrontDoorRunner(loop=loop)

    result = runner._get_compiled_graph()

    assert result is runner._compiled_graph
    assert result.checkpointer is loop._checkpointer
    assert result.store is loop._store
    assert result.name == "ceo_frontdoor"
    assert result.builder.state_schema is CeoPersistentState
    assert result.builder.context_schema is CeoRuntimeContext


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_keeps_runtime_only_objects_out_of_checkpointed_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

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

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

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
async def test_ceo_frontdoor_call_model_rebuilds_request_messages_from_stable_and_dynamic_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])

    async def _call_model_with_tools(**kwargs):
        captured.update(kwargs)
        return AIMessage(content="plain reply", response_metadata={"finish_reason": "stop"})

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    update = await runner._graph_call_model(
        {
            "messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "hello"},
            ],
            "stable_messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "hello"},
            ],
            "dynamic_appendix_messages": [
                {"role": "assistant", "content": "## Retrieved Context\n- memory"},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message_type": "frontdoor_runtime_tool_contract",
                            "callable_tool_names": ["submit_next_stage"],
                            "candidate_tool_names": [],
                            "hydrated_tool_names": [],
                            "visible_skill_ids": [],
                            "candidate_skill_ids": [],
                            "rbac_visible_tool_names": ["submit_next_stage"],
                            "rbac_visible_skill_ids": [],
                            "stage_summary": {
                                "active_stage_id": "",
                                "transition_required": False,
                                "active_stage": None,
                            },
                            "contract_revision": "exp:test",
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "turn_overlay_text": "## Retrieved Context\n- memory",
            "repair_overlay_text": "repair only",
            "tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "hydrated_tool_names": [],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["submit_next_stage"],
            "rbac_visible_skill_ids": [],
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "model_refs": ["openai_codex:gpt-test"],
            "parallel_enabled": False,
            "prompt_cache_key": "cache-key",
            "iteration": 0,
            "max_iterations": 4,
            "session_key": "web:shared",
        },
        runtime=SimpleNamespace(context=CeoRuntimeContext(loop=None, session=None, session_key="web:shared", on_progress=None)),
    )

    request_messages = list(captured["messages"] or [])
    assert any(_frontdoor_tool_contract_payload(dict(message)) for message in request_messages)
    assert any(str(message.get("content") or "").startswith("## Retrieved Context") for message in request_messages)
    assert not any(
        "System note for this turn only:\n## Retrieved Context" in str(message.get("content") or "")
        for message in request_messages
    )
    assert any(
        "System note for this turn only:\nrepair only" in str(message.get("content") or "")
        for message in request_messages
        if str(message.get("role") or "").strip().lower() == "user"
    )
    assert captured["prompt_cache_key"] == "cache-key"
    assert update["iteration"] == 1


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


def test_memory_assembly_config_exposes_frontdoor_runtime_defaults() -> None:
    config = MemoryAssemblyConfig()

    assert not hasattr(config, "frontdoor_recent_message_count")
    assert not hasattr(config, "frontdoor_summary_trigger_message_count")
    assert not hasattr(config, "frontdoor_summarizer_trigger_message_count")
    assert not hasattr(config, "frontdoor_summarizer_keep_message_count")
    assert config.frontdoor_interrupt_approval_enabled is False
    assert config.frontdoor_interrupt_tool_names == ["message", "create_async_task", "continue_task"]


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_keeps_messages_uncompacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

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
            assembly=SimpleNamespace()
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
    assert messages == [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "question one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "question two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "question three"},
    ]
    contract_payloads = [
        payload
        for payload in (
            _frontdoor_tool_contract_payload(dict(message))
            for message in list(state_update["dynamic_appendix_messages"] or [])
        )
        if isinstance(payload, dict)
    ]
    assert len(contract_payloads) == 1
    assert contract_payloads[0]["callable_tool_names"] == ["submit_next_stage"]
    assert "summary_text" not in state_update
    assert "summary_payload" not in state_update
    assert "summary_model_key" not in state_update


@pytest.mark.asyncio
async def test_ceo_frontdoor_finalize_turn_returns_stage_only_updates(tmp_path) -> None:
    loop = SimpleNamespace(
        sessions=SessionManager(tmp_path),
    )
    runner = CeoFrontDoorRunner(loop=loop)

    messages = [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "question one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "question two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "question three"},
    ]

    result = await runner._graph_finalize_turn(
        {
            "messages": messages,
            "final_output": "final answer",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "question three",
        }
    )

    assert result["messages"][-1] == {"role": "assistant", "content": "final answer"}
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


@pytest.mark.asyncio
async def test_ceo_frontdoor_prepare_turn_prompt_cache_key_changes_when_stable_prefix_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

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

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

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
    assert diagnostics["tool_signature_count"] == 0
    assert str(diagnostics["tool_signature_hash"] or "").strip() == ""
    assert diagnostics["overlay_present"] is True
    assert diagnostics["overlay_section_count"] == 1
    assert str(diagnostics["overlay_text_hash"] or "").strip()


@pytest.mark.asyncio
async def test_graph_prepare_turn_does_not_call_removed_summary_model() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    assert not hasattr(runner, "_invoke_summary_model")

    result = await runner._graph_prepare_turn(
        {
            "messages": [{"role": "user", "content": f"message {idx}"} for idx in range(10)],
            "user_input": {"content": "follow up", "metadata": {}},
        },
        runtime=SimpleNamespace(context=SimpleNamespace(session=None)),
    )

    assert result["messages"] == [{"role": "user", "content": f"message {idx}"} for idx in range(10)]
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


@pytest.mark.asyncio
async def test_graph_prepare_turn_no_longer_emits_removed_compaction_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []
    loop = SimpleNamespace(
        sessions=SessionManager(tmp_path),
        tools=SimpleNamespace(get=lambda _name: None),
        main_task_service=None,
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": []}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return ContextAssemblyResult(
            model_messages=[
                {"role": "system", "content": "SYSTEM PROMPT"},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
                {"role": "assistant", "content": "four"},
            ],
            tool_names=[],
            trace={},
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai:gpt-4.1"])

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
        context=CeoRuntimeContext(loop=loop, session=session, session_key="web:shared", on_progress=_on_progress)
    )

    await runner._graph_prepare_turn(
        {"user_input": {"content": "follow up", "metadata": {}}},
        runtime=runtime,
    )

    assert progress_calls == []


@pytest.mark.asyncio
async def test_graph_prepare_turn_real_session_path_drops_summary_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    async def _noop_ready() -> None:
        return None

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

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

    result = await runner._graph_prepare_turn(
        {"user_input": SimpleNamespace(content="question three", metadata={})},
        runtime=runtime,
    )

    assert list(result["messages"] or []) == [
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "question one"},
        {"role": "assistant", "content": "answer one"},
        {"role": "user", "content": "question two"},
        {"role": "assistant", "content": "answer two"},
        {"role": "user", "content": "question three"},
    ]
    contract_payloads = [
        payload
        for payload in (
            _frontdoor_tool_contract_payload(dict(message))
            for message in list(result["dynamic_appendix_messages"] or [])
        )
        if isinstance(payload, dict)
    ]
    assert len(contract_payloads) == 1
    assert contract_payloads[0]["callable_tool_names"] == ["submit_next_stage"]
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


@pytest.mark.asyncio
async def test_graph_finalize_turn_ignores_stale_summary_fields_on_direct_reply() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

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

    assert result["messages"][-1] == {"role": "assistant", "content": "final answer"}
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


@pytest.mark.asyncio
async def test_graph_finalize_turn_completes_active_frontdoor_stage_for_direct_reply() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_finalize_turn(
        {
            "messages": [{"role": "user", "content": "remember this"}],
            "final_output": "记住了。以后默认把文档保存到桌面。",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "记住，文档保存到桌面",
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-2",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-2",
                        "stage_index": 2,
                        "stage_kind": "normal",
                        "mode": "自主执行",
                        "status": "active",
                        "stage_goal": "save memory",
                        "completed_stage_summary": "",
                        "tool_round_budget": 2,
                        "tool_rounds_used": 1,
                        "created_at": "2026-04-09T13:46:30+08:00",
                        "finished_at": "",
                        "rounds": [
                            {
                                "round_id": "frontdoor-stage-2:round-1",
                                "round_index": 1,
                                "created_at": "2026-04-09T13:46:36+08:00",
                                "budget_counted": True,
                                "tool_names": ["memory_write"],
                                "tool_call_ids": ["call-1"],
                            }
                        ],
                    }
                ],
            },
        }
    )

    stage_state = dict(result.get("frontdoor_stage_state") or {})
    assert stage_state["active_stage_id"] == ""
    assert stage_state["transition_required"] is False
    stage = stage_state["stages"][0]
    assert stage["status"] == "completed"
    assert stage["finished_at"]


@pytest.mark.asyncio
async def test_graph_finalize_turn_completes_active_frontdoor_stage_for_self_execute() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

    result = await runner._graph_finalize_turn(
        {
            "messages": [{"role": "user", "content": "write the file and verify it"}],
            "final_output": "The file has been written and verified.",
            "route_kind": "self_execute",
            "heartbeat_internal": False,
            "query_text": "write the file and verify it",
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_kind": "normal",
                        "mode": "自主执行",
                        "status": "active",
                        "stage_goal": "write the file and verify it",
                        "completed_stage_summary": "",
                        "tool_round_budget": 6,
                        "tool_rounds_used": 2,
                        "created_at": "2026-04-14T17:38:36+08:00",
                        "finished_at": "",
                        "rounds": [
                            {
                                "round_id": "frontdoor-stage-1:round-1",
                                "round_index": 1,
                                "created_at": "2026-04-14T17:38:55+08:00",
                                "budget_counted": True,
                                "tool_names": ["filesystem_write"],
                                "tool_call_ids": ["call-1"],
                            }
                        ],
                    }
                ],
            },
        }
    )

    stage_state = dict(result.get("frontdoor_stage_state") or {})
    assert stage_state["active_stage_id"] == ""
    assert stage_state["transition_required"] is False
    stage = stage_state["stages"][0]
    assert stage["status"] == "completed"
    assert stage["completed_stage_summary"] == "The file has been written and verified."
    assert stage["finished_at"]


@pytest.mark.asyncio
async def test_checkpoint_inspection_uses_runner_graph_surface(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Graph:
        async def aget_state(self, config, subgraphs=False):
            captured["config"] = config
            captured["subgraphs"] = subgraphs
            return SimpleNamespace(
                config=config,
                parent_config={},
                values={},
                next=(),
                metadata={},
                created_at="",
                tasks=(),
            )

    class _Runner:
        def __init__(self, *, loop) -> None:
            _ = loop

        def _get_compiled_graph(self):
            return _Graph()

    monkeypatch.setattr(checkpoint_inspection, "CeoFrontDoorRunner", _Runner, raising=False)

    result = await checkpoint_inspection.get_frontdoor_checkpoint(
        SimpleNamespace(_ensure_checkpointer_ready=lambda: None),
        session_id="web:shared",
        checkpoint_id="checkpoint-1",
        subgraphs=True,
    )

    assert captured["config"] == {
        "configurable": {"thread_id": "web:shared", "checkpoint_id": "checkpoint-1"}
    }
    assert captured["subgraphs"] is True
    assert result == {
        "thread_id": "web:shared",
        "checkpoint_id": "checkpoint-1",
        "checkpoint_ns": "",
        "parent_checkpoint_id": "",
        "values": {},
        "next": [],
        "metadata": {},
        "created_at": "",
        "tasks": [],
        "has_interrupts": False,
    }


@pytest.mark.asyncio
async def test_checkpoint_inspection_supports_wrapper_selected_create_agent_runner(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _CreateAgentGraph:
        async def aget_state(self, config, subgraphs=False):
            captured["config"] = config
            captured["subgraphs"] = subgraphs
            return SimpleNamespace(
                config=config,
                parent_config={},
                values={"agent_runtime": "create_agent"},
                next=(),
                metadata={},
                created_at="",
                tasks=(),
            )

    graph = _CreateAgentGraph()

    monkeypatch.setattr(
        "g3ku.runtime.frontdoor._ceo_create_agent_impl.CreateAgentCeoFrontDoorRunner._get_agent",
        lambda self: graph,
    )

    loop = SimpleNamespace(_ensure_checkpointer_ready=lambda: None)

    result = await checkpoint_inspection.get_frontdoor_checkpoint(
        loop,
        session_id="web:shared",
        checkpoint_id="checkpoint-1",
        subgraphs=True,
    )

    assert captured["config"] == {
        "configurable": {"thread_id": "web:shared", "checkpoint_id": "checkpoint-1"}
    }
    assert captured["subgraphs"] is True
    assert result == {
        "thread_id": "web:shared",
        "checkpoint_id": "checkpoint-1",
        "checkpoint_ns": "",
        "parent_checkpoint_id": "",
        "values": {"agent_runtime": "create_agent"},
        "next": [],
        "metadata": {},
        "created_at": "",
        "tasks": [],
        "has_interrupts": False,
    }
