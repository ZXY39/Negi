import json
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.memory_write import MemoryWriteTool
from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.json_schema_utils import get_attached_raw_parameters_schema
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor import ceo_agent_middleware, ceo_runner
from g3ku.runtime.frontdoor.prompt_cache_contract import (
    DEFAULT_CACHE_FAMILY_REVISION,
    FrontdoorPromptContract,
)
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted, initial_persistent_state
from g3ku.runtime.session_agent import RuntimeAgentSession
from main.runtime.chat_backend import build_prompt_cache_diagnostics


class _FakeGraphOutput:
    def __init__(self, *, value, interrupts=()):
        self.value = dict(value or {})
        self.interrupts = tuple(interrupts)


class _NonPrimitive:
    def __str__(self) -> str:
        return "non-primitive"


class _DemoTool(Tool):
    @property
    def name(self) -> str:
        return "demo_tool"

    @property
    def description(self) -> str:
        return "demo tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _CreateAsyncTaskLikeTool(Tool):
    @property
    def name(self) -> str:
        return "create_async_task"

    @property
    def description(self) -> str:
        return "create async task"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "core_requirement": {"type": "string"},
                "execution_policy": {"type": ["object", "string"]},
            },
            "required": ["task", "core_requirement", "execution_policy"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _MemoryWriteLikeTool(Tool):
    @property
    def name(self) -> str:
        return "memory_write"

    @property
    def description(self) -> str:
        return "write structured memory facts"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string"},
                            "entity": {"type": "string"},
                            "attribute": {"type": "string"},
                            "value": {
                                "type": ["string", "number", "boolean", "object", "array", "null"],
                                "description": "Canonical value for the fact.",
                            },
                            "observed_at": {"type": "string"},
                            "time_semantics": {"type": "string"},
                            "source_excerpt": {"type": "string"},
                        },
                        "required": [
                            "category",
                            "entity",
                            "attribute",
                            "value",
                            "observed_at",
                            "time_semantics",
                            "source_excerpt",
                        ],
                    },
                }
            },
            "required": ["facts"],
        }

    async def execute(self, **kwargs):
        return kwargs


class _ModelVisibleSchemaOverrideTool(Tool):
    @property
    def name(self) -> str:
        return "model_visible_schema_override_tool"

    @property
    def description(self) -> str:
        return "runtime-only description"

    @property
    def model_description(self) -> str:
        return "model-visible description"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "runtime_only": {"type": "string", "description": "runtime-only field"},
            },
            "required": ["runtime_only"],
        }

    @property
    def model_parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "model_only": {"type": "integer", "description": "model-only field"},
            },
            "required": ["model_only"],
        }

    def to_model_schema(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.model_description,
                "parameters": self.model_parameters,
            },
        }

    async def execute(self, **kwargs):
        return kwargs


class _BrokenValidationTool(Tool):
    @property
    def name(self) -> str:
        return "broken_validation_tool"

    @property
    def description(self) -> str:
        return "broken validation tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, **kwargs):
        raise AssertionError("execute should not be called when validation crashes")

    def validate_params(self, params: dict[str, object]) -> list[str]:
        _ = params
        raise TypeError("unhashable type: 'list'")


def test_memory_assembly_config_uses_frontdoor_global_summary_defaults() -> None:
    cfg = MemoryAssemblyConfig()

    assert not hasattr(cfg, "frontdoor_recent_message_count")
    assert not hasattr(cfg, "frontdoor_summary_trigger_message_count")
    assert not hasattr(cfg, "frontdoor_summarizer_trigger_message_count")
    assert not hasattr(cfg, "frontdoor_summarizer_keep_message_count")
    assert cfg.frontdoor_interrupt_approval_enabled is False
    assert cfg.frontdoor_interrupt_tool_names == ["message", "create_async_task", "continue_task"]
    assert cfg.frontdoor_global_summary_trigger_ratio == 0.50
    assert cfg.frontdoor_global_summary_target_ratio == 0.20
    assert cfg.frontdoor_global_summary_min_output_tokens == 2000
    assert cfg.frontdoor_global_summary_max_output_ratio == 0.05
    assert cfg.frontdoor_global_summary_max_output_tokens_ceiling == 12000
    assert cfg.frontdoor_global_summary_pressure_warn_ratio == 0.85
    assert cfg.frontdoor_global_summary_force_refresh_ratio == 0.95
    assert cfg.frontdoor_global_summary_min_delta_tokens == 2000
    assert cfg.frontdoor_global_summary_failure_cooldown_seconds == 600


def test_initial_persistent_state_tracks_stage_state_and_runtime_marker() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert "summary_text" not in state
    assert "summary_payload" not in state
    assert "summary_version" not in state
    assert "summary_model_key" not in state
    assert state["frontdoor_stage_state"] == {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [],
    }
    assert state["hydrated_tool_names"] == []
    assert state["agent_runtime"] == "create_agent"


@pytest.mark.asyncio
async def test_create_agent_runner_prompt_keeps_history_uncompacted_after_legacy_frontdoor_history_compaction_removal() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            _memory_runtime_settings=SimpleNamespace(
                assembly=SimpleNamespace(
                    core_tools=[],
                )
            ),
            main_task_service=None,
            memory_manager=None,
            provider_name="openai",
            model="gpt-test",
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
        )
    )

    state = {
        "session_key": "web:shared",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
        ],
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "compression_state": {"status": "", "text": "", "source": ""},
    }

    prepared = await runner._graph_prepare_turn(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace(session=None)),
    )

    assert prepared["messages"][0] == {"role": "system", "content": "system"}
    assert prepared["messages"][1:] == [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]


def test_build_ceo_agent_uses_create_agent_with_persistence(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_create_agent(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(ainvoke=None)

    monkeypatch.setattr(create_agent_impl, "create_agent", _fake_create_agent)
    monkeypatch.setattr(
        create_agent_impl.CreateAgentCeoFrontDoorRunner,
        "_resolve_ceo_model_refs",
        lambda self: ["openai:gpt-4.1"],
    )

    loop = SimpleNamespace(
        _checkpointer=object(),
        _store=object(),
        app_config=SimpleNamespace(get_role_model_keys=lambda role: ["openai:gpt-4.1"]),
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    runner._get_agent()

    kwargs = dict(captured["kwargs"] or {})
    assert kwargs["checkpointer"] is loop._checkpointer
    assert kwargs["store"] is loop._store
    assert kwargs["name"] == "ceo_frontdoor"
    assert kwargs["context_schema"].__name__ == "CeoRuntimeContext"
    assert kwargs["state_schema"].__name__ == "CeoPersistentState"
    assert kwargs["middleware"]


def test_create_agent_runner_resolve_ceo_model_refs_prefers_cache_capable_refs(monkeypatch) -> None:
    from g3ku.runtime.frontdoor import _ceo_support as ceo_support

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            app_config=SimpleNamespace(
                get_role_model_keys=lambda role: [
                    "openai:gpt-4.1",
                    "anthropic:claude-sonnet-4",
                    "openrouter:claude-3.7-sonnet",
                ]
            ),
            provider_name="openai",
            model="gpt-test",
        )
    )

    monkeypatch.setattr(ceo_support, "refresh_loop_runtime_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ceo_support,
        "build_provider_from_model_key",
        lambda _config, ref: SimpleNamespace(provider_id=str(ref).split(":", 1)[0]),
    )
    monkeypatch.setattr(
        ceo_support,
        "find_by_name",
        lambda name: SimpleNamespace(
            supports_prompt_caching=name in {"anthropic", "openrouter"}
        ),
    )

    assert runner._resolve_ceo_model_refs() == [
        "anthropic:claude-sonnet-4",
        "openrouter:claude-3.7-sonnet",
    ]


def test_build_prompt_cache_diagnostics_surfaces_cache_capability_and_prefix_reason() -> None:
    diagnostics = build_prompt_cache_diagnostics(
        stable_messages=[{"role": "system", "content": "stable system"}],
        dynamic_appendix_messages=[{"role": "assistant", "content": "dynamic appendix"}],
        tool_schemas=[],
        provider_model="claude-sonnet-4",
        scope="ceo_frontdoor",
        prompt_cache_key="cache-key",
        cache_family_revision="exp:rev-7",
        prompt_lane="ceo_frontdoor",
        prefix_invalidation_reason="cache_family_revision_changed",
    )

    assert diagnostics["provider_cache_capable"] is True
    assert diagnostics["prompt_lane"] == "ceo_frontdoor"
    assert diagnostics["cache_family_revision"] == "exp:rev-7"
    assert diagnostics["stable_prefix_hash"]
    assert diagnostics["dynamic_appendix_hash"]
    assert diagnostics["prefix_invalidation_reason"] == "cache_family_revision_changed"


def test_ceo_runner_always_selects_create_agent_impl(monkeypatch) -> None:
    class _New:
        def __init__(self, *, loop) -> None:
            self.loop = loop

    monkeypatch.setattr(ceo_runner, "CreateAgentCeoFrontDoorRunner", _New)

    loop = SimpleNamespace()
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    assert isinstance(runner._impl, _New)


def test_ceo_runner_invalidate_runtime_bindings_clears_cached_agent_and_graph() -> None:
    runner = ceo_runner.CeoFrontDoorRunner(loop=SimpleNamespace())
    runner._impl._agent = object()
    runner._impl._compiled_graph = object()

    runner.invalidate_runtime_bindings()

    assert runner._impl._agent is None
    assert runner._impl._compiled_graph is None


def test_create_agent_runner_invalidates_cached_bindings_when_checkpointer_generation_changes() -> None:
    original_checkpointer = object()
    next_checkpointer = object()
    loop = SimpleNamespace(_checkpointer=next_checkpointer)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    runner._agent = object()
    runner._compiled_graph = object()
    runner._agent_checkpointer_ref = original_checkpointer

    changed = runner._invalidate_cached_runtime_bindings_if_stale()

    assert changed is True
    assert runner._agent is None
    assert runner._compiled_graph is None
    assert runner._agent_checkpointer_ref is next_checkpointer


def test_create_agent_runner_keeps_cached_bindings_when_checkpointer_generation_is_unchanged() -> None:
    current_checkpointer = object()
    loop = SimpleNamespace(_checkpointer=current_checkpointer)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    cached_agent = object()
    cached_graph = object()
    runner._agent = cached_agent
    runner._compiled_graph = cached_graph
    runner._agent_checkpointer_ref = current_checkpointer

    changed = runner._invalidate_cached_runtime_bindings_if_stale()

    assert changed is False
    assert runner._agent is cached_agent
    assert runner._compiled_graph is cached_graph
    assert runner._agent_checkpointer_ref is current_checkpointer


@pytest.mark.asyncio
async def test_create_agent_runner_invalidates_cached_bindings_when_current_checkpointer_connection_is_inactive() -> None:
    current_checkpointer = object()
    readiness_calls: list[str] = []
    loop = SimpleNamespace(
        _checkpointer=current_checkpointer,
        _ensure_checkpointer_ready=lambda: readiness_calls.append("ready"),
        _sqlite_checkpointer_is_active=lambda value: False if value is current_checkpointer else True,
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    runner._agent = object()
    runner._compiled_graph = object()
    runner._agent_checkpointer_ref = current_checkpointer

    changed = await runner._ensure_runtime_bindings_ready()

    assert changed is True
    assert readiness_calls == ["ready", "ready"]
    assert runner._agent is None
    assert runner._compiled_graph is None
    assert runner._agent_checkpointer_ref is current_checkpointer


def test_create_agent_runner_build_prompt_context_uses_effective_turn_overlay(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_effective_turn_overlay_text", lambda state: "overlay-text")

    result = runner.build_prompt_context(
        state={"turn_overlay_text": "## Retrieved Context\n- memory", "repair_overlay_text": "repair"},
        runtime=SimpleNamespace(),
        tools=[],
    )

    assert result["system_overlay"].startswith("overlay-text")
    assert "submit_next_stage" in result["system_overlay"]


def test_create_agent_runner_visible_langchain_tools_uses_prepared_state(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}

    def _fake_build_langchain_tools_for_state(*, state, runtime):
        captured["state"] = state
        captured["runtime"] = runtime
        return ["tool-a"]

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", _fake_build_langchain_tools_for_state)
    runtime = SimpleNamespace(context=SimpleNamespace(session_key="web:shared"))
    state = {"tool_names": ["record_tool"]}

    result = runner.visible_langchain_tools(state=state, runtime=runtime)

    assert result == ["tool-a"]
    assert captured == {"state": state, "runtime": runtime}


@pytest.mark.asyncio
async def test_create_agent_langchain_tool_emits_tool_result_progress(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"demo_tool": _DemoTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _on_progress})

    async def _fake_execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, arguments, runtime_context
        await on_progress(
            f"{tool_name} started",
            event_kind="tool_start",
            event_data={"tool_name": tool_name, "tool_call_id": tool_call_id},
        )
        return "done", "success", "2026-04-05T11:00:00", "2026-04-05T11:00:01", 1.0

    monkeypatch.setattr(runner, "_execute_tool_call", _fake_execute_tool_call)

    tools = runner._build_langchain_tools_for_state(
        state={
            "tool_names": ["demo_tool"],
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Inspect the request",
                        "tool_round_budget": 2,
                        "tool_rounds_used": 0,
                        "status": "active",
                        "mode": "自主执行",
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "rounds": [],
                    }
                ],
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    result = await tools[0].ainvoke(
        {
            "type": "tool_call",
            "id": "call-demo-tool-1",
            "name": "demo_tool",
            "args": {"value": "alpha"},
        }
    )

    assert getattr(result, "tool_call_id", "") == "call-demo-tool-1"
    assert getattr(result, "status", "") == "success"
    assert [item[1] for item in progress_calls] == ["tool_start", "tool_result"]
    assert progress_calls[-1][0] == "done"
    assert progress_calls[0][2] == {"tool_name": "demo_tool", "tool_call_id": "call-demo-tool-1"}
    assert progress_calls[-1][2] == {"tool_name": "demo_tool", "tool_call_id": "call-demo-tool-1"}


@pytest.mark.asyncio
async def test_create_agent_graph_execute_tools_preserves_parallel_same_name_tool_call_ids(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"demo_tool": _DemoTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _on_progress})
    async def _fake_execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, runtime_context
        await on_progress(
            f"{tool_name} started for {arguments['value']}",
            event_kind="tool_start",
            event_data={"tool_name": tool_name, "tool_call_id": tool_call_id},
        )
        return (
            json.dumps({"value": arguments["value"]}),
            "success",
            "2026-04-05T11:00:00",
            "2026-04-05T11:00:01",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call", _fake_execute_tool_call)

    result = await runner._graph_execute_tools(
        {
            "tool_call_payloads": [
                {"id": "call-demo-tool-1", "name": "demo_tool", "arguments": {"value": "alpha"}},
                {"id": "call-demo-tool-2", "name": "demo_tool", "arguments": {"value": "beta"}},
            ],
            "messages": [],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": True,
            "max_parallel_tool_calls": 2,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "call_model"
    assert [(item[1], item[2]["tool_call_id"]) for item in progress_calls if item[1] == "tool_start"] == [
        ("tool_start", "call-demo-tool-1"),
        ("tool_start", "call-demo-tool-2"),
    ]
    assert sorted(item[2]["tool_call_id"] for item in progress_calls if item[1] == "tool_result") == [
        "call-demo-tool-1",
        "call-demo-tool-2",
    ]


def test_frontdoor_stage_state_after_tool_cycle_writes_precise_round_tools() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    result = runner._frontdoor_stage_state_after_tool_cycle(
        {
            "session_key": "web:shared",
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
                        "stage_goal": "inspect repository",
                        "completed_stage_summary": "",
                        "tool_round_budget": 4,
                        "tool_rounds_used": 0,
                        "created_at": "2026-04-14T20:20:54+08:00",
                        "finished_at": "",
                        "rounds": [],
                    }
                ],
            },
        },
        tool_call_payloads=[
            {"id": "call-exec-1", "name": "exec", "arguments": {"command": "pwd"}},
            {"id": "call-load-1", "name": "load_tool_context", "arguments": {"tool_id": "filesystem_write"}},
        ],
        tool_results=[
            {
                "tool_name": "exec",
                "status": "success",
                "result_text": '{"status":"success","head_preview":"D:\\\\NewProjects\\\\G3KU"}',
            },
            {
                "tool_name": "load_tool_context",
                "status": "success",
                "result_text": '{"ok":true,"tool_id":"filesystem_write","summary":"write file content"}',
            },
        ],
    )

    rounds = result["stages"][0]["rounds"]
    assert len(rounds) == 1
    assert rounds[0]["tool_call_ids"] == ["call-exec-1", "call-load-1"]
    assert rounds[0]["tool_names"] == ["exec", "load_tool_context"]
    assert [tool["tool_call_id"] for tool in rounds[0]["tools"]] == ["call-exec-1", "call-load-1"]
    assert [tool["tool_name"] for tool in rounds[0]["tools"]] == ["exec", "load_tool_context"]
    assert rounds[0]["tools"][0]["arguments_text"] == "exec (command=pwd)"
    assert rounds[0]["tools"][0]["status"] == "success"
    assert rounds[0]["tools"][0]["output_text"] == '{"status":"success","head_preview":"D:\\\\NewProjects\\\\G3KU"}'
    assert rounds[0]["tools"][1]["output_preview_text"] == "write file content"
    assert rounds[0]["tools"][1]["output_ref"] == ""


@pytest.mark.asyncio
async def test_create_agent_langchain_tool_normalizes_create_async_task_execution_policy(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}

    async def _on_progress(*args, **kwargs):
        _ = args, kwargs

    monkeypatch.setattr(
        runner,
        "_registered_tools_for_state",
        lambda state: {"create_async_task": _CreateAsyncTaskLikeTool()},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _on_progress})

    async def _fake_execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, tool_name, runtime_context, on_progress, tool_call_id
        captured["arguments"] = dict(arguments or {})
        return "created", "success", "2026-04-05T12:00:00", "2026-04-05T12:00:01", 1.0

    monkeypatch.setattr(runner, "_execute_tool_call", _fake_execute_tool_call)

    tools = runner._build_langchain_tools_for_state(
        state={
            "tool_names": ["create_async_task"],
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Dispatch follow-up work",
                        "tool_round_budget": 2,
                        "tool_rounds_used": 0,
                        "status": "active",
                        "mode": "自主执行",
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "rounds": [],
                    }
                ],
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    result = await tools[0].ainvoke(
        {
            "task": "continue task",
            "core_requirement": "finish the analysis",
            "execution_policy": "focus",
        }
    )

    assert result["status"] == "success"
    assert captured["arguments"]["execution_policy"] == {"mode": "focus"}


@pytest.mark.asyncio
async def test_create_agent_langchain_tool_degrades_validation_exception_to_tool_error(monkeypatch) -> None:
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    tool_context = SimpleNamespace(
        push_runtime_context=lambda context: object(),
        pop_runtime_context=lambda token: None,
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=tool_context,
            resource_manager=None,
            tool_execution_manager=None,
        )
    )
    monkeypatch.setattr(
        runner,
        "_registered_tools_for_state",
        lambda state: {"broken_validation_tool": _BrokenValidationTool()},
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _on_progress})

    tools = runner._build_langchain_tools_for_state(
        state={
            "tool_names": ["broken_validation_tool"],
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Validate broken input",
                        "tool_round_budget": 2,
                        "tool_rounds_used": 0,
                        "status": "active",
                        "mode": "自主执行",
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "rounds": [],
                    }
                ],
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    result = await tools[0].ainvoke({"value": "alpha"})

    assert result["status"] == "error"
    assert "Error validating broken_validation_tool" in result["result_text"]
    assert "unhashable type: 'list'" in result["result_text"]
    assert [item[1] for item in progress_calls] == ["tool_error"]


@pytest.mark.asyncio
async def test_create_agent_runner_passes_thread_id_and_context() -> None:
    captured: dict[str, object] = {}
    readiness_calls: list[str] = []
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"), _last_route_kind="task_dispatch")
    progress_calls: list[object] = []

    async def _on_progress(*args, **kwargs):
        progress_calls.append((args, kwargs))

    class _FakeAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v1"):
            captured["payload"] = payload
            captured["config"] = config
            captured["context"] = context
            captured["version"] = version
            return {"messages": [], "route_kind": "direct_reply", "final_output": "ok"}

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: readiness_calls.append("ready"))
    )
    runner._agent = _FakeAgent()

    output = await runner.run_turn(
        user_input=SimpleNamespace(content="hello", metadata={}),
        session=session,
        on_progress=_on_progress,
    )

    assert output == "ok"
    assert readiness_calls == ["ready"]
    assert session._last_route_kind == "direct_reply"
    assert captured["config"] == {"configurable": {"thread_id": "web:shared"}}
    assert getattr(captured["context"], "session_key") == "web:shared"
    assert getattr(captured["context"], "loop") is runner._loop
    assert getattr(captured["context"], "session") is session
    assert getattr(captured["context"], "on_progress") is _on_progress
    assert captured["payload"]["agent_runtime"] == "create_agent"
    assert progress_calls == []


@pytest.mark.asyncio
async def test_create_agent_runner_raises_structured_interrupt() -> None:
    readiness_calls: list[str] = []
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"), _last_route_kind="task_dispatch")

    class _InterruptingAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return _FakeGraphOutput(
                value={"approval_request": {"kind": "frontdoor_tool_approval"}},
                interrupts=(SimpleNamespace(id="interrupt-1", value={"kind": "frontdoor_tool_approval"}),),
            )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: readiness_calls.append("ready"))
    )
    runner._agent = _InterruptingAgent()

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=session,
            on_progress=None,
        )

    assert readiness_calls == ["ready"]
    assert session._last_route_kind == "direct_reply"
    assert exc_info.value.values == {"approval_request": {"kind": "frontdoor_tool_approval"}}
    assert [item.interrupt_id for item in exc_info.value.interrupts] == ["interrupt-1"]


@pytest.mark.asyncio
async def test_create_agent_runner_resume_uses_command_resume() -> None:
    captured: dict[str, object] = {}
    readiness_calls: list[str] = []
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"), _last_route_kind="task_dispatch")
    progress_calls: list[object] = []

    async def _on_progress(*args, **kwargs):
        progress_calls.append((args, kwargs))

    class _ResumeAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            captured["context"] = context
            captured["version"] = version
            captured["payload"] = payload
            captured["config"] = config
            return {"messages": [], "route_kind": "self_execute", "final_output": "approved"}

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: readiness_calls.append("ready"))
    )
    runner._agent = _ResumeAgent()

    result = await runner.resume_turn(
        session=session,
        resume_value={"decisions": [{"type": "approve"}]},
        on_progress=_on_progress,
    )

    assert result == "approved"
    assert readiness_calls == ["ready"]
    assert isinstance(captured["payload"], Command)
    assert getattr(captured["payload"], "resume", None) == {"decisions": [{"type": "approve"}]}
    assert captured["config"] == {"configurable": {"thread_id": "web:shared"}}
    assert getattr(captured["context"], "session_key") == "web:shared"
    assert getattr(captured["context"], "loop") is runner._loop
    assert getattr(captured["context"], "session") is session
    assert getattr(captured["context"], "on_progress") is _on_progress
    assert session._last_route_kind == "self_execute"
    assert progress_calls == []


@pytest.mark.asyncio
async def test_create_agent_runner_resume_raises_structured_interrupt() -> None:
    readiness_calls: list[str] = []
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"), _last_route_kind="task_dispatch")

    class _InterruptingResumeAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return _FakeGraphOutput(
                value={"approval_request": {"kind": "frontdoor_tool_approval"}, "final_output": "ignored"},
                interrupts=(SimpleNamespace(id="interrupt-2", value={"kind": "frontdoor_tool_approval"}),),
            )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: readiness_calls.append("ready"))
    )
    runner._agent = _InterruptingResumeAgent()

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.resume_turn(
            session=session,
            resume_value={"decisions": [{"type": "approve"}]},
            on_progress=None,
        )

    assert readiness_calls == ["ready"]
    assert session._last_route_kind == "task_dispatch"
    assert exc_info.value.values == {
        "approval_request": {"kind": "frontdoor_tool_approval"},
        "final_output": "ignored",
    }
    assert [item.interrupt_id for item in exc_info.value.interrupts] == ["interrupt-2"]


@pytest.mark.asyncio
async def test_create_agent_runner_run_turn_wires_frontdoor_stage_and_compression_to_session() -> None:
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _last_route_kind="task_dispatch",
        _frontdoor_stage_state={},
        _compression_state={},
        _frontdoor_hydrated_tool_names=[],
    )

    class _FakeAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return {
                "messages": [],
                "route_kind": "self_execute",
                "final_output": "ok",
                "verified_task_ids": ["task:demo-123"],
                "hydrated_tool_names": ["filesystem_write"],
                "frontdoor_stage_state": {
                    "active_stage_id": "frontdoor-stage-1",
                    "transition_required": False,
                    "stages": [{"stage_id": "frontdoor-stage-1", "rounds": []}],
                },
                "compression_state": {"status": "ready", "text": "全局上下文已压缩", "source": "semantic"},
                "semantic_context_state": {
                    "summary_text": "## 长期目标\n继续当前任务",
                    "coverage_history_source": "checkpoint",
                    "coverage_message_index": 3,
                    "coverage_stage_index": 1,
                    "needs_refresh": False,
                    "failure_cooldown_until": "",
                    "updated_at": "2026-04-13T18:00:00",
                },
            }

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _FakeAgent()

    output = await runner.run_turn(
        user_input=SimpleNamespace(content="hello", metadata={}),
        session=session,
        on_progress=None,
    )

    assert output == "ok"
    assert session._last_route_kind == "self_execute"
    assert session._last_verified_task_ids == ["task:demo-123"]
    assert session._frontdoor_hydrated_tool_names == ["filesystem_write"]
    assert session._frontdoor_stage_state["active_stage_id"] == "frontdoor-stage-1"
    assert session._compression_state == {"status": "ready", "text": "全局上下文已压缩", "source": "semantic"}
    assert session._semantic_context_state["summary_text"] == "## 长期目标\n继续当前任务"


@pytest.mark.asyncio
async def test_create_agent_runner_resume_turn_wires_frontdoor_stage_and_compression_to_session() -> None:
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _last_route_kind="task_dispatch",
        _frontdoor_stage_state={},
        _compression_state={},
        _frontdoor_hydrated_tool_names=[],
    )

    class _FakeAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return {
                "messages": [],
                "route_kind": "direct_reply",
                "final_output": "approved",
                "verified_task_ids": [],
                "hydrated_tool_names": ["filesystem_write"],
                "frontdoor_stage_state": {
                    "active_stage_id": "",
                    "transition_required": True,
                    "stages": [{"stage_id": "frontdoor-stage-1", "status": "completed", "rounds": []}],
                },
                "compression_state": {"status": "idle", "text": "", "source": ""},
            }

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _FakeAgent()

    output = await runner.resume_turn(
        session=session,
        resume_value={"decisions": [{"type": "approve"}]},
        on_progress=None,
    )

    assert output == "approved"
    assert session._last_route_kind == "direct_reply"
    assert session._frontdoor_hydrated_tool_names == ["filesystem_write"]
    assert session._frontdoor_stage_state["active_stage_id"] == ""
    assert session._frontdoor_stage_state["transition_required"] is False
    assert session._frontdoor_stage_state["stages"][0]["status"] == "completed"
    assert session._compression_state == {"status": "idle", "text": "", "source": ""}


@pytest.mark.asyncio
async def test_create_agent_postprocess_promotes_loaded_tool_context_into_next_turn_state() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "load_tool_context", "arguments": {"tool_id": "filesystem_write"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "load_tool_context", "arguments": '{"tool_id":"filesystem_write"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "load_tool_context",
                    "content": (
                        '{"ok": true, "tool_id": "filesystem_write", '
                        '"callable_now": true, "will_be_hydrated_next_turn": true, '
                        '"hydration_targets": ["filesystem_write"]}'
                    ),
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["load_tool_context"],
            "candidate_tool_names": ["filesystem_write", "agent_browser"],
            "hydrated_tool_names": [],
        }
    )

    assert result is not None
    assert result["hydrated_tool_names"] == ["filesystem_write"]
    assert result["tool_names"] == ["load_tool_context", "filesystem_write"]
    assert result["candidate_tool_names"] == ["agent_browser"]


def test_runtime_agent_session_inflight_snapshot_keeps_frontdoor_hydrated_tool_names() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._state.latest_message = "working"
    session._frontdoor_hydrated_tool_names = ["filesystem_write"]

    snapshot = session.inflight_turn_snapshot()

    assert isinstance(snapshot, dict)
    assert snapshot["hydrated_tool_names"] == ["filesystem_write"]


@pytest.mark.asyncio
async def test_create_agent_middleware_syncs_real_stage_state_before_running_tool_round(
    monkeypatch,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"

    lifecycle = ceo_agent_middleware.CeoTurnLifecycleMiddleware(runner=runner)
    model_output = ceo_agent_middleware.CeoModelOutputMiddleware(runner=runner)
    runtime = SimpleNamespace(context=SimpleNamespace(session=session))

    after_submit_state = {
        "session_key": "web:shared",
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-stage-1",
                        "type": "function",
                        "function": {
                            "name": "submit_next_stage",
                            "arguments": json.dumps(
                                {
                                    "stage_goal": "Inspect the repository structure",
                                    "tool_round_budget": 2,
                                }
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-stage-1",
                "name": "submit_next_stage",
                "content": json.dumps({"result_text": "stage accepted", "status": "success"}),
            },
        ],
        "tool_call_payloads": [
            {
                "id": "call-stage-1",
                "name": "submit_next_stage",
                "arguments": {
                    "stage_goal": "Inspect the repository structure",
                    "tool_round_budget": 2,
                },
            }
        ],
        "used_tools": [],
        "route_kind": "direct_reply",
        "frontdoor_stage_state": {
            "active_stage_id": "",
            "transition_required": False,
            "stages": [],
        },
        "compression_state": {"status": "running", "text": "compressing", "source": "user"},
    }

    lifecycle_update = await lifecycle.abefore_model(after_submit_state, runtime)
    running_after_submit = session.inflight_turn_snapshot()

    assert lifecycle_update is not None
    assert isinstance(running_after_submit, dict)
    assert running_after_submit["execution_trace_summary"]["active_stage_id"] == "frontdoor-stage-1"
    assert running_after_submit["execution_trace_summary"]["stages"][0]["stage_goal"] == "Inspect the repository structure"
    assert running_after_submit["execution_trace_summary"]["stages"][0]["tool_round_budget"] == 2
    assert "tool_events" not in running_after_submit
    assert running_after_submit["compression"]["status"] == "running"

    normalized_calls = [
        {
            "id": "call-memory-1",
            "name": "memory_search",
            "arguments": {"query": "repository structure"},
        }
    ]

    async def _fake_normalize_model_output(state, *, runtime):
        _ = state, runtime
        return {
            "analysis_text": "",
            "tool_call_payloads": list(normalized_calls),
            "approval_request": None,
            "approval_status": "",
            "synthetic_tool_calls_used": False,
            "xml_repair_attempt_count": 0,
            "xml_repair_excerpt": "",
            "xml_repair_tool_names": [],
            "xml_repair_last_issue": "",
            "next_step": "review_tool_calls",
        }

    monkeypatch.setattr(runner, "_graph_normalize_model_output", _fake_normalize_model_output)

    await model_output.aafter_model(
        {
            **after_submit_state,
            **dict(lifecycle_update or {}),
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-memory-1",
                            "name": "memory_search",
                            "args": {"query": "repository structure"},
                        }
                    ],
                )
            ],
        },
        runtime,
    )
    await session._handle_progress(
        "memory_search started",
        event_kind="tool_start",
        event_data={"tool_name": "memory_search", "tool_call_id": "call-memory-1"},
    )

    running_tool_snapshot = session.inflight_turn_snapshot()

    assert isinstance(running_tool_snapshot, dict)
    assert "tool_events" not in running_tool_snapshot
    stage = running_tool_snapshot["execution_trace_summary"]["stages"][0]
    assert stage["stage_goal"] == "Inspect the repository structure"
    assert stage["tool_round_budget"] == 2
    assert [round_item["tool_names"] for round_item in stage["rounds"]] == [["memory_search"]]
    assert [round_item["tool_call_ids"] for round_item in stage["rounds"]] == [["call-memory-1"]]
    assert [tool["tool_name"] for tool in stage["rounds"][0]["tools"]] == ["memory_search"]
    assert stage["rounds"][0]["tools"][0]["tool_call_id"] == "call-memory-1"
    assert stage["rounds"][0]["tools"][0]["status"] == "running"


@pytest.mark.asyncio
async def test_create_agent_lifecycle_abefore_model_syncs_only_postprocessed_stage_state(
    monkeypatch,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    lifecycle = ceo_agent_middleware.CeoTurnLifecycleMiddleware(runner=runner)
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    runtime = SimpleNamespace(context=SimpleNamespace(session=session))
    sync_calls: list[dict[str, object]] = []

    async def _fake_postprocess_completed_tool_cycle(*, state):
        _ = state
        return {
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Inspect the repository structure",
                        "tool_round_budget": 2,
                        "tool_rounds_used": 0,
                        "status": "active",
                        "rounds": [],
                    }
                ],
            },
            "compression_state": {"status": "running", "text": "compressing", "source": "user"},
        }

    monkeypatch.setattr(runner, "_postprocess_completed_tool_cycle", _fake_postprocess_completed_tool_cycle)

    def _record_sync(*, state, runtime=None, session=None, preview_pending_tool_round=False):
        _ = runtime, session, preview_pending_tool_round
        sync_calls.append(dict(state or {}))

    monkeypatch.setattr(runner, "_sync_runtime_session_frontdoor_state", _record_sync)

    update = await lifecycle.abefore_model(
        {
            "frontdoor_stage_state": {
                "active_stage_id": "",
                "transition_required": False,
                "stages": [],
            },
            "compression_state": {"status": "", "text": "", "source": ""},
            "tool_call_payloads": [
                {
                    "id": "call-stage-1",
                    "name": "submit_next_stage",
                    "arguments": {
                        "stage_goal": "Inspect the repository structure",
                        "tool_round_budget": 2,
                    },
                }
            ],
        },
        runtime,
    )

    assert update is not None
    assert len(sync_calls) == 1
    assert sync_calls[0]["frontdoor_stage_state"]["active_stage_id"] == "frontdoor-stage-1"
    assert sync_calls[0]["compression_state"] == {"status": "running", "text": "compressing", "source": "user"}


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_unverified_dispatch_text_from_model() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=SimpleNamespace(get_task=lambda task_id: None))
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "Claude Code Haha 项目分析任务已在后台成功续跑。新任务 ID: `task:fake-123`。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "Claude Code Haha 项目分析任务已在后台成功续跑。新任务 ID: `task:fake-123`。"
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_verified_dispatch_text_from_model() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    text = "后台修复任务已经建立，任务号 `task:demo-123`。我先继续排查，完成后直接把结果同步给你。"
    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": text,
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": ["create_async_task"],
            "route_kind": "task_dispatch",
            "verified_task_ids": ["task:demo-123"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == text
    assert result["route_kind"] == "task_dispatch"


@pytest.mark.asyncio
async def test_create_agent_runner_does_not_treat_continue_task_as_task_dispatch() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    text = "原任务已切换为续跑模式，系统会继续推进。"
    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": text,
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": ["continue_task"],
            "route_kind": "task_continuation",
            "verified_task_ids": ["task:demo-123"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == text
    assert result["route_kind"] == "task_continuation"


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_dispatch_text_for_unverified_task_id_even_when_verified_task_exists() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=SimpleNamespace(get_task=lambda task_id: None))
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "后台修复任务已经建立，任务号 `task:fake-123`。我先继续排查。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": ["create_async_task"],
            "route_kind": "task_dispatch",
            "verified_task_ids": ["task:demo-123"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "后台修复任务已经建立，任务号 `task:fake-123`。我先继续排查。"


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_heartbeat_success_reply_for_current_task_id() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    text = "已通过异步任务 `task:demo-terminal` 完成修复，结果：skill 已恢复可见。"
    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": text,
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
            "heartbeat_internal": True,
            "user_input": {
                "content": "[SESSION EVENTS]",
                "metadata": {
                    "heartbeat_internal": True,
                    "heartbeat_reason": "task_terminal",
                    "heartbeat_task_ids": ["task:demo-terminal"],
                },
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == text
    assert "未确认成功创建后台任务" not in result["final_output"]


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_heartbeat_dispatch_claim_for_unknown_task_id() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=SimpleNamespace(get_task=lambda task_id: None))
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "任务 `demo-old` 已完成但未通过验收，已续跑为 `task:new-123`。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
            "heartbeat_internal": True,
            "user_input": {
                "content": "[SESSION EVENTS]",
                "metadata": {
                    "heartbeat_internal": True,
                    "heartbeat_reason": "task_terminal",
                    "heartbeat_task_ids": ["task:demo-old"],
                },
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "任务 `demo-old` 已完成但未通过验收，已续跑为 `task:new-123`。"


@pytest.mark.asyncio
async def test_create_agent_postprocess_allows_unverified_task_dispatch_reply(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=SimpleNamespace(get_task=lambda task_id: None))
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "create_async_task",
                    "content": "创建任务成功task:fake-123",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
        }
    )

    assert result is not None
    assert "jump_to" not in result
    assert result["route_kind"] == "task_dispatch"
    assert result["verified_task_ids"] == []
    assert "repair_overlay_text" not in result


@pytest.mark.asyncio
async def test_create_agent_postprocess_continues_after_verified_async_dispatch(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "create_async_task",
                    "content": "创建任务成功task:demo-123",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["create_async_task"],
        }
    )

    assert result is not None
    assert "jump_to" not in result
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result
    assert result["verified_task_ids"] == ["task:demo-123"]
    assert result["route_kind"] == "task_dispatch"
    assert result["tool_call_payloads"] == []


@pytest.mark.asyncio
async def test_create_agent_postprocess_verifies_continue_task_recreate_before_reply(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {
                    "id": "call-1",
                    "name": "continue_task",
                    "arguments": {
                        "mode": "recreate",
                        "target_task_id": "task:demo-old",
                        "continuation_instruction": "Continue the old task with the recovered context.",
                    },
                }
            ],
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "continue_task", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "continue_task",
                    "content": '{"status":"completed","mode":"recreate","target_task_id":"task:demo-old","continuation_task":{"task_id":"task:demo-new"},"resumed_task":null}',
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["continue_task"],
        }
    )

    assert result is not None
    assert "jump_to" not in result
    assert result["route_kind"] == "task_continuation"
    assert result["tool_call_payloads"] == []
    assert result.get("tool_names") in (None, [])
    assert result.get("repair_overlay_text") in {None, ""}
    assert result["verified_task_ids"] == []


@pytest.mark.asyncio
async def test_create_agent_prompt_middleware_records_prompt_cache_diagnostics_from_real_request_shape(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_build_frontdoor_prompt_contract(**kwargs):
        captured["contract_kwargs"] = dict(kwargs)
        return FrontdoorPromptContract(
            request_messages=[
                {"role": "system", "content": "You are the CEO frontdoor agent."},
                {"role": "user", "content": "hello"},
            ],
            prompt_cache_key="cache-key",
            diagnostics={"stable_prompt_signature": "sig-1"},
            stable_prefix_hash="stable-hash",
            dynamic_appendix_hash="dynamic-hash",
            stable_messages=[
                {
                    "role": "system",
                    "content": "You are the CEO frontdoor agent.\n\nsubmit_next_stage guidance",
                },
                {"role": "user", "content": "hello"},
            ],
            dynamic_appendix_messages=[],
            diagnostic_dynamic_messages=[
                {"role": "assistant", "content": "submit_next_stage guidance"},
            ],
            cache_family_revision=DEFAULT_CACHE_FAMILY_REVISION,
        )

    monkeypatch.setattr(create_agent_impl, "build_frontdoor_prompt_contract", _fake_build_frontdoor_prompt_contract)

    initial_tool_schema = {
        "name": "stale_tool",
        "description": "stale tool that should not reach cache diagnostics",
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
    }
    exposed_tool_schema = {
        "name": "create_async_task",
        "description": "dispatch async task",
        "parameters": {"type": "object", "properties": {"task": {"type": "string"}}},
    }
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner.visible_langchain_tools = lambda **kwargs: [exposed_tool_schema]
    seen_request: dict[str, object] = {}

    async def _terminal_handler(request):
        seen_request["system_message"] = request.system_message
        seen_request["tools"] = list(request.tools or [])
        seen_request["model_settings"] = dict(request.model_settings or {})
        return ModelResponse(result=[AIMessage(content="ok")])

    handler = _terminal_handler
    for middleware in reversed(runner._middleware()):
        previous_handler = handler

        async def _wrap(request, handler=previous_handler, middleware=middleware):
            return await middleware.awrap_model_call(request, handler)

        handler = _wrap

    response = await handler(
        ModelRequest(
            model=SimpleNamespace(),
            system_message=SystemMessage(content="You are the CEO frontdoor agent."),
            messages=[HumanMessage(content="hello")],
            tools=[initial_tool_schema],
            state={
                "messages": [{"role": "user", "content": "hello"}],
            },
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
            model_settings={"temperature": 0.2},
        )
    )

    assert isinstance(response, ExtendedModelResponse)
    assert response.model_response.result == [AIMessage(content="ok")]
    assert isinstance(response.command, Command)
    assert response.command.update == {
        "prompt_cache_key": "cache-key",
        "prompt_cache_diagnostics": {"stable_prompt_signature": "sig-1"},
    }
    content_blocks = list(getattr(seen_request["system_message"], "content_blocks", []))
    assert content_blocks[0] == {"type": "text", "text": "You are the CEO frontdoor agent."}
    assert "submit_next_stage" in str(content_blocks[1]["text"])
    assert seen_request["tools"] == [exposed_tool_schema]
    assert seen_request["model_settings"] == {"temperature": 0.2, "prompt_cache_key": "cache-key"}
    assert captured["contract_kwargs"]["session_key"] == "web:shared"
    assert captured["contract_kwargs"]["provider_model"] == "openai:gpt-4.1"
    assert captured["contract_kwargs"]["scope"] == "ceo_frontdoor"
    assert captured["contract_kwargs"]["tool_schemas"] == [exposed_tool_schema]
    assert captured["contract_kwargs"]["stable_messages"][0] == {
        "role": "system",
        "content": "You are the CEO frontdoor agent.",
    }
    assert captured["contract_kwargs"]["stable_messages"][1] == {"role": "user", "content": "hello"}
    assert "submit_next_stage" in str(captured["contract_kwargs"]["overlay_text"])
    assert captured["contract_kwargs"]["overlay_section_count"] == 1


@pytest.mark.asyncio
async def test_create_agent_prompt_cache_key_contract_ignores_dynamic_appendix_messages() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner.visible_langchain_tools = lambda **kwargs: [
        {
            "name": "memory_write",
            "description": "",
            "parameters": {"type": "object"},
        }
    ]

    async def _invoke_with_overlay(overlay_text: str) -> dict[str, object]:
        async def _terminal_handler(request):
            return ModelResponse(result=[AIMessage(content="ok")])

        handler = _terminal_handler
        for middleware in reversed(runner._middleware()):
            previous_handler = handler

            async def _wrap(request, handler=previous_handler, middleware=middleware):
                return await middleware.awrap_model_call(request, handler)

            handler = _wrap

        response = await handler(
            ModelRequest(
                model=SimpleNamespace(),
                system_message=SystemMessage(content="You are the CEO frontdoor agent."),
                messages=[HumanMessage(content="原始用户问题")],
                tools=[],
                state={
                    "messages": [{"role": "user", "content": "原始用户问题"}],
                    "turn_overlay_text": overlay_text,
                },
                runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
            )
        )

        assert isinstance(response, ExtendedModelResponse)
        assert isinstance(response.command, Command)
        return dict(response.command.update or {})

    first = await _invoke_with_overlay("## Retrieved Context\n- authoritative memory A")
    second = await _invoke_with_overlay("## Retrieved Context\n- authoritative memory B")

    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert first["prompt_cache_diagnostics"]["dynamic_appendix_hash"] != second["prompt_cache_diagnostics"]["dynamic_appendix_hash"]


def test_create_agent_prompt_cache_key_contract_preserves_fallback_system_prompt_with_nonempty_state_messages() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    contract = runner._frontdoor_prompt_contract(
        state={
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "working"},
            ],
        },
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        fallback_system_message=SystemMessage(content="You are the CEO frontdoor agent."),
        fallback_messages=[HumanMessage(content="ignored fallback")],
        session_key="web:shared",
    )

    assert contract.stable_messages[0] == {
        "role": "system",
        "content": "You are the CEO frontdoor agent.",
    }
    assert contract.request_messages[0] == {
        "role": "system",
        "content": "You are the CEO frontdoor agent.",
    }


@pytest.mark.asyncio
async def test_create_agent_dynamic_appendix_request_preserves_live_assistant_and_tool_messages() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._frontdoor_default_overlay_text = lambda state: "default overlay"
    runner.visible_langchain_tools = lambda **kwargs: []
    seen_request: dict[str, object] = {}

    async def _terminal_handler(request):
        seen_request["system_message"] = request.system_message
        seen_request["messages"] = list(request.messages or [])
        return ModelResponse(result=[AIMessage(content="ok")])

    handler = _terminal_handler
    for middleware in reversed(runner._middleware()):
        previous_handler = handler

        async def _wrap(request, handler=previous_handler, middleware=middleware):
            return await middleware.awrap_model_call(request, handler)

        handler = _wrap

    response = await handler(
        ModelRequest(
            model=SimpleNamespace(),
            system_message=SystemMessage(content="You are the CEO frontdoor agent."),
            messages=[HumanMessage(content="fallback message should not define continuity")],
            tools=[],
            state={
                "messages": [
                    {"role": "system", "content": "stable system"},
                    {"role": "user", "content": "start"},
                    {"role": "assistant", "content": "working memory of the live turn"},
                    {
                        "role": "tool",
                        "name": "demo_tool",
                        "tool_call_id": "call-1",
                        "content": "{\"result_text\": \"tool finished\", \"status\": \"success\"}",
                    },
                ],
                "stable_messages": [
                    {"role": "system", "content": "stable system"},
                    {"role": "user", "content": "start"},
                ],
                "dynamic_appendix_messages": [
                    {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                ],
            },
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
        )
    )

    assert isinstance(response, ExtendedModelResponse)
    rendered_messages = list(seen_request["messages"] or [])
    contents = [str(getattr(message, "content", "") or "") for message in rendered_messages]

    assert contents == [
        "start",
        "working memory of the live turn",
        '{"result_text": "tool finished", "status": "success"}',
        "## Retrieved Context\n- authoritative memory",
    ]
    assert "default overlay" in str(getattr(seen_request["system_message"], "content", ""))


@pytest.mark.asyncio
async def test_create_agent_dynamic_appendix_request_does_not_duplicate_when_state_messages_already_combined() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._frontdoor_default_overlay_text = lambda state: "default overlay"
    runner.visible_langchain_tools = lambda **kwargs: []
    seen_request: dict[str, object] = {}

    async def _terminal_handler(request):
        seen_request["messages"] = list(request.messages or [])
        return ModelResponse(result=[AIMessage(content="ok")])

    handler = _terminal_handler
    for middleware in reversed(runner._middleware()):
        previous_handler = handler

        async def _wrap(request, handler=previous_handler, middleware=middleware):
            return await middleware.awrap_model_call(request, handler)

        handler = _wrap

    response = await handler(
        ModelRequest(
            model=SimpleNamespace(),
            system_message=SystemMessage(content="You are the CEO frontdoor agent."),
            messages=[HumanMessage(content="fallback")],
            tools=[],
            state={
                "messages": [
                    {"role": "system", "content": "stable system"},
                    {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
                    {"role": "user", "content": "start"},
                    {"role": "assistant", "content": "working memory of the live turn"},
                ],
                "stable_messages": [
                    {"role": "system", "content": "stable system"},
                    {"role": "user", "content": "start"},
                ],
                "dynamic_appendix_messages": [
                    {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                ],
            },
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
        )
    )

    assert isinstance(response, ExtendedModelResponse)
    contents = [str(getattr(message, "content", "") or "") for message in list(seen_request["messages"] or [])]
    assert contents == [
        "start",
        "working memory of the live turn",
        "## Retrieved Context\n- authoritative memory",
    ]


@pytest.mark.asyncio
async def test_create_agent_stable_prefix_request_coherent_after_dynamic_appendix_drift_path() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._frontdoor_default_overlay_text = lambda state: ""
    runner.visible_langchain_tools = lambda **kwargs: []
    seen_requests: list[dict[str, object]] = []

    async def _invoke(messages: list[dict[str, object]]) -> dict[str, object]:
        async def _terminal_handler(request):
            seen_requests.append(
                {
                    "system_message": request.system_message,
                    "messages": list(request.messages or []),
                }
            )
            return ModelResponse(result=[AIMessage(content="ok")])

        handler = _terminal_handler
        for middleware in reversed(runner._middleware()):
            previous_handler = handler

            async def _wrap(request, handler=previous_handler, middleware=middleware):
                return await middleware.awrap_model_call(request, handler)

            handler = _wrap

        response = await handler(
            ModelRequest(
                model=SimpleNamespace(),
                system_message=SystemMessage(content="You are the CEO frontdoor agent."),
                messages=[HumanMessage(content="fallback")],
                tools=[],
                state={
                    "messages": messages,
                    "stable_messages": [
                        {"role": "system", "content": "stable system"},
                        {"role": "user", "content": "start"},
                    ],
                    "dynamic_appendix_messages": [
                        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                    ],
                },
                runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
            )
        )
        assert isinstance(response, ExtendedModelResponse)
        return dict(getattr(response.command, "update", {}) or {})

    first = await _invoke(
        [
            {"role": "system", "content": "stable system"},
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "assistant drift A"},
        ]
    )
    second = await _invoke(
        [
            {"role": "system", "content": "stable system"},
            {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "assistant drift B"},
            {
                "role": "tool",
                "name": "demo_tool",
                "tool_call_id": "call-1",
                "content": "{\"result_text\": \"done\", \"status\": \"success\"}",
            },
        ]
    )

    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert [
        str(getattr(message, "content", "") or "")
        for message in list(seen_requests[0]["messages"] or [])
    ] == [
        "start",
        "assistant drift A",
        "## Retrieved Context\n- authoritative memory",
    ]
    assert [
        str(getattr(message, "content", "") or "")
        for message in list(seen_requests[1]["messages"] or [])
    ] == [
        "start",
        "assistant drift B",
        '{"result_text": "done", "status": "success"}',
        "## Retrieved Context\n- authoritative memory",
    ]
    first_blocks = list(getattr(seen_requests[0]["system_message"], "content_blocks", []))
    second_blocks = list(getattr(seen_requests[1]["system_message"], "content_blocks", []))
    assert first_blocks[0] == {"type": "text", "text": "stable system"}
    assert second_blocks[0] == {"type": "text", "text": "stable system"}


def test_create_agent_prompt_contract_avoids_duplicate_history_when_live_messages_are_compacted() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    contract = runner._frontdoor_prompt_contract(
        state={
            "messages": [
                {"role": "system", "content": "stable system"},
                {
                    "role": "assistant",
                    "content": "[G3KU_LONG_CONTEXT_SUMMARY_V1]\nsummary body",
                },
                {"role": "assistant", "content": "latest assistant"},
                {"role": "user", "content": "latest user"},
            ],
            "stable_messages": [
                {"role": "system", "content": "stable system"},
                {"role": "assistant", "content": "[G3KU_LONG_CONTEXT_SUMMARY_V1]\nsummary body"},
                {"role": "assistant", "content": "latest assistant"},
                {"role": "user", "content": "latest user"},
            ],
            "dynamic_appendix_messages": [
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
            ],
        },
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )

    assert contract.request_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "assistant", "content": "[G3KU_LONG_CONTEXT_SUMMARY_V1]\nsummary body"},
        {"role": "assistant", "content": "latest assistant"},
        {"role": "user", "content": "latest user"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]
    assert contract.stable_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "assistant", "content": "[G3KU_LONG_CONTEXT_SUMMARY_V1]\nsummary body"},
        {"role": "assistant", "content": "latest assistant"},
        {"role": "user", "content": "latest user"},
    ]


def test_create_agent_prompt_contract_does_not_treat_plain_short_recap_text_as_compaction() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    contract = runner._frontdoor_prompt_contract(
        state={
            "messages": [
                {"role": "system", "content": "stable system"},
                {"role": "assistant", "content": "Short recap: here's the answer you asked for."},
                {"role": "user", "content": "latest user"},
            ],
            "stable_messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "latest user"},
            ],
            "dynamic_appendix_messages": [
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
            ],
        },
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )

    assert contract.stable_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "latest user"},
    ]
    assert contract.request_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "assistant", "content": "Short recap: here's the answer you asked for."},
        {"role": "user", "content": "latest user"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]


@pytest.mark.asyncio
async def test_create_agent_prompt_cache_diagnostics_hash_tracks_actual_dynamic_appendix_and_overlay() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._frontdoor_default_overlay_text = lambda state: "default overlay"
    runner.visible_langchain_tools = lambda **kwargs: []

    async def _invoke(repair_overlay_text: str) -> dict[str, object]:
        async def _terminal_handler(request):
            return ModelResponse(result=[AIMessage(content="ok")])

        handler = _terminal_handler
        for middleware in reversed(runner._middleware()):
            previous_handler = handler

            async def _wrap(request, handler=previous_handler, middleware=middleware):
                return await middleware.awrap_model_call(request, handler)

            handler = _wrap

        response = await handler(
            ModelRequest(
                model=SimpleNamespace(),
                system_message=SystemMessage(content="You are the CEO frontdoor agent."),
                messages=[HumanMessage(content="fallback")],
                tools=[],
                state={
                    "messages": [
                        {"role": "system", "content": "stable system"},
                        {"role": "user", "content": "start"},
                    ],
                    "stable_messages": [
                        {"role": "system", "content": "stable system"},
                        {"role": "user", "content": "start"},
                    ],
                    "dynamic_appendix_messages": [
                        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                    ],
                    "repair_overlay_text": repair_overlay_text,
                    "cache_family_revision": DEFAULT_CACHE_FAMILY_REVISION,
                },
                runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
            )
        )

        assert isinstance(response, ExtendedModelResponse)
        return dict(getattr(response.command, "update", {}) or {})

    first = await _invoke("repair overlay A")
    second = await _invoke("repair overlay B")

    assert first["prompt_cache_key"] == second["prompt_cache_key"]
    assert (
        first["prompt_cache_diagnostics"]["dynamic_appendix_hash"]
        != second["prompt_cache_diagnostics"]["dynamic_appendix_hash"]
    )


@pytest.mark.asyncio
async def test_create_agent_frontdoor_exposes_memory_write_with_stringified_value_schema_only(
    monkeypatch,
) -> None:
    tool = _MemoryWriteLikeTool()

    async def _executor(_tool_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"result_text": "ok", "status": "success"}

    langchain_tool = ceo_runtime_ops._build_langchain_tool(tool, _executor)

    raw_schema = get_attached_raw_parameters_schema(langchain_tool)
    assert isinstance(raw_schema, dict)
    fact_properties = raw_schema["properties"]["facts"]["items"]["properties"]
    assert fact_properties["value"]["type"] == "string"
    assert "JSON-serialized string" in str(fact_properties["value"]["description"] or "")

    prompt_schema = ceo_agent_middleware._tool_schema(langchain_tool)
    assert isinstance(prompt_schema, dict)
    prompt_fact_properties = prompt_schema["parameters"]["properties"]["facts"]["items"]["properties"]
    assert prompt_fact_properties["value"]["type"] == "string"

    original_fact_properties = tool.parameters["properties"]["facts"]["items"]["properties"]
    assert original_fact_properties["value"]["type"] == ["string", "number", "boolean", "object", "array", "null"]


@pytest.mark.asyncio
async def test_create_agent_frontdoor_exposes_model_visible_schema_overrides() -> None:
    tool = _ModelVisibleSchemaOverrideTool()

    async def _executor(_tool_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"result_text": "ok", "status": "success"}

    langchain_tool = ceo_runtime_ops._build_langchain_tool(tool, _executor)

    assert langchain_tool.description == "model-visible description"

    raw_schema = get_attached_raw_parameters_schema(langchain_tool)
    assert raw_schema == tool.model_parameters

    prompt_schema = ceo_agent_middleware._tool_schema(langchain_tool)
    assert prompt_schema == {
        "name": tool.name,
        "description": tool.model_description,
        "parameters": tool.model_parameters,
    }

    assert "runtime_only" not in raw_schema["properties"]
    assert "model_only" in raw_schema["properties"]


@pytest.mark.asyncio
async def test_create_agent_frontdoor_execute_tool_call_normalizes_nested_array_object_arguments() -> None:
    tool = _MemoryWriteLikeTool()
    captured: dict[str, object] = {}

    async def _executor(_tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        captured["arguments"] = arguments
        return {"result_text": "ok", "status": "success"}

    langchain_tool = ceo_runtime_ops._build_langchain_tool(tool, _executor)
    await langchain_tool.ainvoke(
        {
            "facts": [
                {
                    "category": "preference",
                    "entity": "user",
                    "attribute": "default_document_save_location",
                    "value": "desktop",
                    "observed_at": "2026-04-09T13:05:00+08:00",
                    "time_semantics": "durable_until_replaced",
                    "source_excerpt": "remember this preference",
                }
            ]
        }
    )

    class _RuntimeToolStack:
        def push_runtime_context(self, _context):
            return "token"

        def pop_runtime_context(self, _token):
            return None

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=_RuntimeToolStack(),
            resource_manager=None,
            tool_execution_manager=None,
        )
    )

    result_text, status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=tool,
        tool_name="memory_write",
        arguments=dict(captured["arguments"] or {}),
        runtime_context={},
        on_progress=None,
    )

    payload = json.loads(result_text)
    assert status == "success"
    assert payload["facts"][0]["attribute"] == "default_document_save_location"


@pytest.mark.asyncio
async def test_create_agent_frontdoor_execute_tool_call_omits_unset_optional_nested_fields() -> None:
    class _FakeMemoryManager:
        async def upsert_structured_memory_facts(self, **kwargs):
            return {"ok": True, "facts": kwargs.get("facts")}

    captured: dict[str, object] = {}

    async def _executor(_tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        captured["arguments"] = arguments
        return {"result_text": "ok", "status": "success"}

    tool = MemoryWriteTool(manager=_FakeMemoryManager())
    langchain_tool = ceo_runtime_ops._build_langchain_tool(tool, _executor)
    await langchain_tool.ainvoke(
        {
            "facts": [
                {
                    "category": "preference",
                    "entity": "user",
                    "attribute": "default_document_save_location",
                    "value": "desktop",
                    "observed_at": "2026-04-09T13:46:20+08:00",
                    "time_semantics": "durable_until_replaced",
                    "source_excerpt": "remember this preference",
                }
            ]
        }
    )

    class _RuntimeToolStack:
        def push_runtime_context(self, _context):
            return "token"

        def pop_runtime_context(self, _token):
            return None

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=_RuntimeToolStack(),
            resource_manager=None,
            tool_execution_manager=None,
        )
    )

    result_text, status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=tool,
        tool_name="memory_write",
        arguments=dict(captured["arguments"] or {}),
        runtime_context={},
        on_progress=None,
    )

    payload = json.loads(result_text)
    assert status == "success"
    assert payload["ok"] is True
    assert payload["facts"] == [
        {
            "category": "preference",
            "entity": "user",
            "attribute": "default_document_save_location",
            "value": "desktop",
            "observed_at": "2026-04-09T13:46:20+08:00",
            "time_semantics": "durable_until_replaced",
            "source_excerpt": "remember this preference",
        }
    ]


@pytest.mark.asyncio
async def test_create_agent_prompt_middleware_emits_analysis_progress_for_model_retry(
    monkeypatch,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []
    attempts = {"count": 0}

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    middleware = ceo_agent_middleware.CeoPromptAssemblyMiddleware(runner=runner)

    async def _handler(request):
        _ = request
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError(ceo_agent_middleware.PUBLIC_PROVIDER_FAILURE_MESSAGE)
        return ModelResponse(result=[AIMessage(content="ok")])

    response = await middleware.awrap_model_call(
        ModelRequest(
            model=SimpleNamespace(),
            system_message=SystemMessage(content="You are the CEO frontdoor agent."),
            messages=[HumanMessage(content="hello")],
            tools=[],
            state={"messages": [{"role": "user", "content": "hello"}]},
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared", on_progress=_on_progress)),
        ),
        _handler,
    )

    assert isinstance(response, ExtendedModelResponse)
    assert response.model_response.result == [AIMessage(content="ok")]
    assert [item[1] for item in progress_calls] == ["analysis", "analysis"]
    assert "模型" in progress_calls[0][0]
    assert progress_calls[0][2]["phase"] == "model_call"
    assert "重试" in progress_calls[1][0]
    assert progress_calls[1][2]["phase"] == "provider_retry"


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_full_tool_call_payloads_when_approval_request_is_subset() -> None:
    class _InterruptingAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return _FakeGraphOutput(
                value={
                    "approval_request": {
                        "kind": "frontdoor_tool_approval",
                        "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
                    },
                },
                interrupts=(
                    SimpleNamespace(
                        id="interrupt-1",
                        value={
                            "kind": "frontdoor_tool_approval",
                            "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
                            "tool_call_payloads": [
                                {"name": "create_async_task", "arguments": {"task": "demo"}},
                                {"name": "memory_write", "arguments": {"content": "persist this"}},
                            ],
                        },
                    ),
                ),
            )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _InterruptingAgent()

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared")),
            on_progress=None,
        )

    assert exc_info.value.values["tool_call_payloads"] == [
        {"name": "create_async_task", "arguments": {"task": "demo"}},
        {"name": "memory_write", "arguments": {"content": "persist this"}},
    ]


@pytest.mark.asyncio
async def test_create_agent_runner_keeps_pending_interrupt_contract_coherent() -> None:
    class _InterruptingAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return _FakeGraphOutput(
                value={
                    "prompt_cache_key": "cache-key",
                    "prompt_cache_diagnostics": {
                        "stable_prompt_signature": "sig-1",
                        "tool_signature_count": 1,
                    },
                },
                interrupts=(
                    SimpleNamespace(
                        id="interrupt-1",
                        value={
                            "kind": "frontdoor_tool_approval",
                            "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
                        },
                    ),
                ),
            )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _InterruptingAgent()

    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"), _last_route_kind="task_dispatch")

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=session,
            on_progress=None,
        )

    assert session._last_route_kind == "direct_reply"
    assert exc_info.value.values == {
        "approval_request": {
            "kind": "frontdoor_tool_approval",
            "tool_calls": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
        },
        "tool_call_payloads": [{"name": "create_async_task", "arguments": {"task": "demo"}}],
        "prompt_cache_key": "cache-key",
        "prompt_cache_diagnostics": {
            "stable_prompt_signature": "sig-1",
            "tool_signature_count": 1,
        },
    }


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_full_tool_call_payload_batch_from_interrupt_state() -> None:
    subset_payloads = [{"id": "call-1", "name": "message", "arguments": {"content": "safe subset"}}]
    full_payloads = subset_payloads + [
        {"id": "call-2", "name": "create_async_task", "arguments": {"task": "risky full batch"}}
    ]

    class _InterruptingAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return _FakeGraphOutput(
                value={"prompt_cache_key": "cache-key"},
                interrupts=(
                    SimpleNamespace(
                        id="interrupt-1",
                        value={
                            "approval_request": {
                                "kind": "frontdoor_tool_approval",
                                "tool_calls": subset_payloads,
                            },
                            "tool_call_payloads": full_payloads,
                        },
                    ),
                ),
            )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _InterruptingAgent()

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared")),
            on_progress=None,
        )

    assert exc_info.value.values == {
        "approval_request": {
            "kind": "frontdoor_tool_approval",
            "tool_calls": subset_payloads,
        },
        "tool_call_payloads": full_payloads,
        "prompt_cache_key": "cache-key",
    }


@pytest.mark.asyncio
async def test_create_agent_runner_sanitizes_interrupt_payloads_before_raising() -> None:
    class _InterruptingAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return _FakeGraphOutput(
                value={"approval_request": {"details": _NonPrimitive()}, "items": {_NonPrimitive()}},
                interrupts=(
                    SimpleNamespace(
                        id="interrupt-1",
                        value={"kind": "frontdoor_tool_approval", "payload": _NonPrimitive(), "extra": {_NonPrimitive()}},
                    ),
                ),
            )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _InterruptingAgent()

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared")),
            on_progress=None,
        )

    assert exc_info.value.values == {
        "approval_request": {"details": "non-primitive"},
        "items": ["non-primitive"],
    }
    assert exc_info.value.interrupts[0].value == {
        "kind": "frontdoor_tool_approval",
        "payload": "non-primitive",
        "extra": ["non-primitive"],
    }
