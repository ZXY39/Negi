import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.memory_write import MemoryWriteTool
from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.core.messages import UserInputMessage
from g3ku.json_schema_utils import get_attached_raw_parameters_schema
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor import ceo_agent_middleware, ceo_runner
from g3ku.runtime.frontdoor import prompt_cache_contract
from g3ku.runtime.frontdoor.canonical_context import combine_canonical_context
from g3ku.runtime.frontdoor.prompt_cache_contract import (
    DEFAULT_CACHE_FAMILY_REVISION,
    FrontdoorPromptContract,
)
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted, initial_persistent_state
from g3ku.runtime.frontdoor.tool_contract import (
    frontdoor_tool_contract_payload_from_message,
    is_frontdoor_tool_contract_message,
)
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.session_agent import RuntimeAgentSession
from main.runtime.chat_backend import build_actual_request_diagnostics, build_prompt_cache_diagnostics


def _is_frontdoor_runtime_tool_contract_record(record: dict[str, object]) -> bool:
    return is_frontdoor_tool_contract_message(dict(record or {}))


def _message_role_for_contract_filter(message: object) -> str:
    raw_role = str(getattr(message, "role", "") or getattr(message, "type", "") or "").strip().lower()
    if raw_role == "ai":
        return "assistant"
    if raw_role == "human":
        return "user"
    return raw_role


def _web_ceo_uploaded_image_note(*, text: str, image_path: Path) -> str:
    note = "\n".join(
        [
            "Uploaded attachments:",
            f"- image: {image_path.name} (local path: {image_path})",
            "You may inspect the local file paths above when helpful.",
        ]
    )
    text_value = str(text or "").strip()
    return f"{text_value}\n\n{note}" if text_value else note


def _web_ceo_multimodal_image_note(*, text: str) -> str:
    note = (
        "For this turn, the uploaded image is attached directly in this request. "
        "Use direct visual reasoning on the attached image first."
    )
    text_value = str(text or "").strip()
    return f"{text_value}\n\n{note}" if text_value else note


def _web_ceo_upload_metadata(image_path: Path) -> dict[str, object]:
    return {
        "web_ceo_raw_text": "Please inspect this image",
        "web_ceo_uploads": [
            {
                "path": str(image_path),
                "name": image_path.name,
                "mime_type": "image/png",
                "kind": "image",
                "size": image_path.stat().st_size,
            }
        ],
    }


def _stub_live_runtime_model(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model_key: str = "ceo_primary",
    image_multimodal_enabled: bool,
    context_window_tokens: int = 128000,
) -> None:
    live_config = SimpleNamespace(
        get_managed_model=lambda key: (
            SimpleNamespace(
                image_multimodal_enabled=image_multimodal_enabled,
                context_window_tokens=context_window_tokens,
            )
            if key == model_key
            else None
        )
    )
    monkeypatch.setattr(
        ceo_runtime_ops,
        "get_runtime_config",
        lambda force=False: (live_config, 1, False),
    )


def _canonical_frontdoor_state(**overrides) -> dict[str, object]:
    state: dict[str, object] = {
        "messages": [],
        "tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "hydrated_tool_names": [],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": [],
        "frontdoor_stage_state": {
            "active_stage_id": "",
            "transition_required": False,
            "stages": [],
        },
    }
    state.update(dict(overrides))
    return state


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


class _NamedSchemaTool(Tool):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, object],
        model_description: str | None = None,
        model_parameters: dict[str, object] | None = None,
    ) -> None:
        self._name = name
        self._description = description
        self._parameters = dict(parameters)
        self._model_description = str(model_description or description)
        self._model_parameters = dict(model_parameters or parameters)

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def parameters(self) -> dict[str, object]:
        return dict(self._parameters)

    @property
    def model_description(self) -> str:
        return self._model_description

    @property
    def model_parameters(self) -> dict[str, object]:
        return dict(self._model_parameters)

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


_PARAMETER_GUIDANCE_TEMPLATE = (
    '请先调用 load_tool_context(tool_id="{tool_name}") 查看该工具的详细说明、参数契约和示例后，再重新使用该工具。'
)


class _ExecuteErrorTool(Tool):
    def __init__(self, *, name: str, exc: Exception) -> None:
        self._name = name
        self._exc = exc

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"{self._name} tool"

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
        _ = kwargs
        raise self._exc


def test_memory_assembly_config_no_longer_exposes_frontdoor_global_summary_settings() -> None:
    cfg = MemoryAssemblyConfig()

    assert not hasattr(cfg, "frontdoor_recent_message_count")
    assert not hasattr(cfg, "frontdoor_summary_trigger_message_count")
    assert not hasattr(cfg, "frontdoor_summarizer_trigger_message_count")
    assert not hasattr(cfg, "frontdoor_summarizer_keep_message_count")
    assert cfg.frontdoor_interrupt_approval_enabled is False
    assert cfg.frontdoor_interrupt_tool_names == ["create_async_task"]
    assert not hasattr(cfg, "frontdoor_global_summary_trigger_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_target_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_min_output_tokens")
    assert not hasattr(cfg, "frontdoor_global_summary_max_output_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_max_output_tokens_ceiling")
    assert not hasattr(cfg, "frontdoor_global_summary_pressure_warn_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_force_refresh_ratio")
    assert not hasattr(cfg, "frontdoor_global_summary_min_delta_tokens")
    assert not hasattr(cfg, "frontdoor_global_summary_failure_cooldown_seconds")


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


def test_build_ceo_agent_compiles_state_graph_with_persistence(monkeypatch) -> None:
    def _unexpected_create_agent(*args, **kwargs):
        _ = args, kwargs
        raise AssertionError("create_agent should not be used by the CEO frontdoor runner")

    monkeypatch.setattr(create_agent_impl, "create_agent", _unexpected_create_agent, raising=False)

    loop = SimpleNamespace(
        _checkpointer=InMemorySaver(),
        _store=object(),
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    compiled = runner._get_agent()

    assert compiled is runner._compiled_graph
    assert compiled.checkpointer is loop._checkpointer
    assert compiled.store is loop._store
    assert compiled.name == "ceo_frontdoor"
    assert sorted(compiled.builder.nodes.keys()) == [
        "call_model",
        "execute_tools",
        "finalize",
        "normalize_model_output",
        "prepare_turn",
        "review_tool_calls",
    ]


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


def test_build_prompt_cache_diagnostics_surfaces_prefix_reason_and_actual_request_fields() -> None:
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
        actual_request_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "assistant", "content": "dynamic appendix"},
            {"role": "user", "content": "current user turn"},
        ],
        actual_tool_schemas=[
            {
                "name": "exec",
                "description": "",
                "parameters": {"type": "object"},
            }
        ],
    )

    assert diagnostics["prompt_lane"] == "ceo_frontdoor"
    assert diagnostics["cache_family_revision"] == "exp:rev-7"
    assert diagnostics["stable_prefix_hash"]
    assert diagnostics["dynamic_appendix_hash"]
    assert diagnostics["prefix_invalidation_reason"] == "cache_family_revision_changed"
    assert diagnostics["actual_request_message_count"] == 3
    assert str(diagnostics["actual_request_hash"]).strip()
    assert str(diagnostics["actual_tool_schema_hash"]).strip()


def test_build_prompt_cache_diagnostics_accepts_flat_function_tool_schemas() -> None:
    diagnostics = build_prompt_cache_diagnostics(
        stable_messages=[{"role": "system", "content": "stable system"}],
        dynamic_appendix_messages=[{"role": "assistant", "content": "dynamic appendix"}],
        tool_schemas=[
            {
                "type": "function",
                "name": "exec",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ],
        provider_model="gpt-5.2",
        scope="ceo_inline_tool_reminder",
        prompt_cache_key="cache-key",
        actual_request_messages=[
            {"role": "system", "content": "stable system"},
            {"role": "assistant", "content": "dynamic appendix"},
            {"role": "user", "content": "current user turn"},
        ],
        actual_tool_schemas=[
            {
                "type": "function",
                "name": "exec",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            }
        ],
    )

    assert str(diagnostics["tool_signature_hash"]).strip()
    assert str(diagnostics["actual_tool_schema_hash"]).strip()
    assert diagnostics["tool_signature_hash"] == diagnostics["actual_tool_schema_hash"]


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
async def test_create_agent_runner_node_prepare_turn_replaces_messages_instead_of_appending() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    synced: dict[str, object] = {}

    async def _fake_graph_prepare_turn(state, runtime) -> dict[str, object]:
        _ = state, runtime
        return {
            "messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "latest user"},
            ],
            "stable_messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "latest user"},
            ],
            "dynamic_appendix_messages": [
                {"role": "assistant", "content": "## Retrieved Context\n- memory"}
            ],
        }

    runner._graph_prepare_turn = _fake_graph_prepare_turn  # type: ignore[method-assign]
    runner._sync_runtime_session_frontdoor_state = lambda *, state, runtime: synced.update(  # type: ignore[method-assign]
        {"state": state, "runtime": runtime}
    )

    update = await runner._node_prepare_turn(
        state={"messages": [{"role": "user", "content": "stale request bundle"}]},
        runtime=SimpleNamespace(),
    )

    assert getattr(update["messages"][0], "id", "") == REMOVE_ALL_MESSAGES
    assert update["messages"][1:] == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "latest user"},
    ]
    assert dict(synced["state"])["messages"] == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "latest user"},
    ]


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
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )
    progress_calls: list[tuple[str, str | None, dict[str, object]]] = []

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = kwargs
        progress_calls.append((str(content), event_kind, dict(event_data or {})))

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"demo_tool": _DemoTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _on_progress})
    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, runtime_context
        await on_progress(
            f"{tool_name} started for {arguments['value']}",
            event_kind="tool_start",
            event_data={"tool_name": tool_name, "tool_call_id": tool_call_id},
        )
        return (
            {"value": arguments["value"]},
            json.dumps({"value": arguments["value"]}),
            "success",
            "2026-04-05T11:00:00",
            "2026-04-05T11:00:01",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)

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
                "frontdoor_stage_state": {
                    "active_stage_id": "frontdoor-stage-1",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "frontdoor-stage-1",
                            "stage_index": 1,
                            "stage_goal": "Run the selected tool calls",
                            "tool_round_budget": 2,
                            "tool_rounds_used": 0,
                            "status": "active",
                            "mode": "自主执行",
                            "stage_kind": "normal",
                            "completed_stage_summary": "",
                            "key_refs": [],
                            "rounds": [],
                        }
                    ],
                },
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


@pytest.mark.asyncio
async def test_graph_execute_tools_grows_authoritative_request_body_baseline() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = content, event_kind, event_data, kwargs

    runner._build_tool_runtime_context = lambda **kwargs: {"on_progress": _on_progress}
    runner._registered_tools_for_state = lambda state: {"demo_tool": _DemoTool()}

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, tool_name, runtime_context, on_progress, tool_call_id
        return (
            {"value": arguments["value"]},
            json.dumps({"value": arguments["value"]}),
            "success",
            "2026-04-18T00:04:39+08:00",
            "2026-04-18T00:04:40+08:00",
            1.0,
        )

    runner._execute_tool_call_with_raw_result = _fake_execute_tool_call_with_raw_result

    state = {
        "tool_call_payloads": [
            {"id": "call-demo-tool-1", "name": "demo_tool", "arguments": {"value": "alpha"}},
        ],
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "retrieved context"},
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "memory hint"},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "message_type": "frontdoor_runtime_tool_contract",
                        "callable_tool_names": ["demo_tool"],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "frontdoor_request_body_messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "retrieved context"},
            {"role": "user", "content": "follow-up"},
            {"role": "assistant", "content": "memory hint"},
        ],
        "frontdoor_history_shrink_reason": "",
        "used_tools": [],
        "route_kind": "direct_reply",
        "parallel_enabled": False,
        "max_parallel_tool_calls": 1,
        "synthetic_tool_calls_used": False,
        "response_payload": {"content": "", "tool_calls": []},
        "frontdoor_stage_state": {
            "active_stage_id": "frontdoor-stage-1",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_index": 1,
                    "stage_goal": "Run the selected tool calls",
                    "tool_round_budget": 2,
                    "tool_rounds_used": 0,
                    "status": "active",
                    "mode": "自主执行",
                    "stage_kind": "normal",
                    "completed_stage_summary": "",
                    "key_refs": [],
                    "rounds": [],
                }
            ],
        },
    }

    result = await runner._graph_execute_tools(
        state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["frontdoor_request_body_messages"] == [
        *state["frontdoor_request_body_messages"],
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-demo-tool-1",
                    "type": "function",
                    "function": {
                        "name": "demo_tool",
                        "arguments": json.dumps({"value": "alpha"}, ensure_ascii=False),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-demo-tool-1",
            "name": "demo_tool",
            "content": json.dumps({"value": "alpha"}),
            "started_at": "2026-04-18T00:04:39+08:00",
            "finished_at": "2026-04-18T00:04:40+08:00",
            "elapsed_seconds": 1.0,
        },
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
    assert rounds[0]["tools"][0]["arguments"] == {"command": "pwd"}
    assert rounds[0]["tools"][1]["arguments"] == {"tool_id": "filesystem_write"}
    assert rounds[0]["tools"][0]["arguments_text"] == "exec (command=pwd)"
    assert rounds[0]["tools"][0]["status"] == "success"
    assert rounds[0]["tools"][0]["output_text"] == '{"status":"success","head_preview":"D:\\\\NewProjects\\\\G3KU"}'
    assert rounds[0]["tools"][1]["output_preview_text"] == "write file content"
    assert rounds[0]["tools"][1]["output_ref"] == ""


@pytest.mark.parametrize("loader_tool_name", ["load_tool_context", "load_skill_context"])
def test_frontdoor_stage_state_after_loader_only_tool_cycle_does_not_consume_budget(loader_tool_name: str) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    arguments = {"skill_id": "find-skills"} if "skill" in loader_tool_name else {"tool_id": "filesystem_write"}

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
                        "stage_goal": "inspect loader context",
                        "completed_stage_summary": "",
                        "tool_round_budget": 2,
                        "tool_rounds_used": 0,
                        "created_at": "2026-04-15T10:00:00+08:00",
                        "finished_at": "",
                        "rounds": [],
                    }
                ],
            },
        },
        tool_call_payloads=[
            {"id": "call-loader-1", "name": loader_tool_name, "arguments": arguments},
        ],
        tool_results=[
            {
                "tool_name": loader_tool_name,
                "status": "success",
                "result_text": '{"ok":true}',
            }
        ],
    )

    stage = result["stages"][0]
    assert stage["tool_rounds_used"] == 0
    assert len(stage["rounds"]) == 1
    assert stage["rounds"][0]["tool_names"] == [loader_tool_name]
    assert stage["rounds"][0]["budget_counted"] is False


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
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="broken_validation_tool") in result["result_text"]
    assert [item[1] for item in progress_calls] == ["tool_error"]


@pytest.mark.asyncio
async def test_create_agent_runner_passes_thread_id_and_context_with_minimal_initial_state() -> None:
    captured: dict[str, object] = {}
    readiness_calls: list[str] = []
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"), _last_route_kind="task_dispatch")
    progress_calls: list[object] = []

    async def _on_progress(*args, **kwargs):
        progress_calls.append((args, kwargs))

    class _FakeCompiledGraph:
        async def ainvoke(self, payload, config=None, *, context=None, version="v1"):
            captured["payload"] = payload
            captured["config"] = config
            captured["context"] = context
            captured["version"] = version
            return {"messages": [], "route_kind": "direct_reply", "final_output": "ok"}

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: readiness_calls.append("ready"))
    )
    runner._compiled_graph = _FakeCompiledGraph()

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
    assert captured["payload"] == initial_persistent_state(
        user_input={"content": "hello", "metadata": {}}
    )
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
                "compression_state": {"status": "", "text": "", "source": ""},
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
    assert session._compression_state == {"status": "", "text": "", "source": ""}
    assert session._semantic_context_state == {}


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
async def test_create_agent_postprocess_does_not_promote_loaded_tool_context_into_next_turn_state() -> None:
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
            "candidate_skill_ids": [],
            "visible_skill_ids": [],
            "rbac_visible_tool_names": ["load_tool_context", "filesystem_write", "agent_browser"],
            "rbac_visible_skill_ids": [],
        }
    )

    assert result is not None
    assert result["hydrated_tool_names"] == []
    assert result["tool_names"] == ["load_tool_context"]
    assert result["candidate_tool_names"] == ["filesystem_write", "agent_browser"]


@pytest.mark.asyncio
async def test_create_agent_graph_execute_tools_promotes_loaded_tool_context_into_next_turn_state() -> None:
    class _InlineLoadTool(Tool):
        @property
        def name(self) -> str:
            return "load_tool_context"

        @property
        def description(self) -> str:
            return "load tool context"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {"tool_id": {"type": "string"}},
                "required": ["tool_id"],
            }

        async def execute(self, tool_id: str, **kwargs):
            _ = kwargs
            return {
                "ok": True,
                "tool_id": tool_id,
                "callable_now": True,
                "will_be_hydrated_next_turn": True,
                "hydration_targets": [tool_id],
            }

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )
    runner._registered_tools_for_state = lambda state: {"load_tool_context": _InlineLoadTool()}
    runner._build_tool_runtime_context = lambda **kwargs: {
        "on_progress": None,
        "actor_role": "ceo",
        "session_key": "web:shared",
        "tool_contract_enforced": True,
        "candidate_tool_names": ["filesystem_write", "agent_browser"],
        "candidate_skill_ids": [],
    }

    result = await runner._graph_execute_tools(
        {
            **_canonical_frontdoor_state(
                tool_names=["load_tool_context"],
                candidate_tool_names=["filesystem_write", "agent_browser"],
                candidate_tool_items=[
                    {"tool_id": "filesystem_write", "description": "Write file content to disk."},
                    {"tool_id": "agent_browser", "description": "Browser automation via semantic shortlist."},
                ],
                hydrated_tool_names=[],
                visible_skill_ids=[],
                candidate_skill_ids=[],
                rbac_visible_tool_names=["load_tool_context", "filesystem_write", "agent_browser"],
                rbac_visible_skill_ids=[],
                dynamic_appendix_messages=[
                    {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                ],
            ),
            "tool_call_payloads": [
                {"id": "call-1", "name": "load_tool_context", "arguments": {"tool_id": "filesystem_write"}}
            ],
            "messages": [],
            "used_tools": [],
                "route_kind": "direct_reply",
                "parallel_enabled": False,
                "max_parallel_tool_calls": 1,
                "synthetic_tool_calls_used": False,
                "response_payload": {"content": "", "tool_calls": []},
                "compression_state": {"status": "running", "text": "compressing", "source": "user"},
                "semantic_context_state": {"summary_text": "summary", "needs_refresh": False},
                "frontdoor_stage_state": {
                    "active_stage_id": "frontdoor-stage-1",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "frontdoor-stage-1",
                            "stage_index": 1,
                            "stage_goal": "Load the filesystem tool context",
                            "tool_round_budget": 2,
                            "tool_rounds_used": 0,
                            "status": "active",
                            "mode": "自主执行",
                            "stage_kind": "normal",
                            "completed_stage_summary": "",
                            "key_refs": [],
                            "rounds": [],
                        }
                    ],
                },
            },
            runtime=SimpleNamespace(context=SimpleNamespace()),
        )

    assert result["next_step"] == "call_model"
    assert result["hydrated_tool_names"] == ["filesystem_write"]
    assert result["tool_names"] == ["load_tool_context", "filesystem_write"]
    assert result["candidate_tool_names"] == ["agent_browser"]
    assert result["candidate_tool_items"] == [
        {
            "tool_id": "agent_browser",
            "description": "Browser automation via semantic shortlist.",
        }
    ]
    assert result["candidate_skill_ids"] == []
    assert result["dynamic_appendix_messages"][0] == {
        "role": "assistant",
        "content": "## Retrieved Context\n- authoritative memory",
    }
    contract_messages = [
        item
        for item in list(result["dynamic_appendix_messages"])
        if _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    assert len(contract_messages) == 1
    contract_text = str(contract_messages[0]["content"] or "")
    assert "callable_tools: `load_tool_context`, `filesystem_write`" in contract_text
    assert "hydrated_tools: `filesystem_write`" in contract_text
    assert "candidate_tools:" in contract_text
    assert "`agent_browser`: Browser automation via semantic shortlist." in contract_text


def test_frontdoor_stage_state_after_tool_cycle_ignores_raw_result_when_writing_round_tools() -> None:
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
            {"id": "call-load-1", "name": "load_tool_context", "arguments": {"tool_id": "filesystem_write"}},
        ],
        tool_results=[
            {
                "tool_name": "load_tool_context",
                "status": "success",
                "result_text": '{"ok":true,"tool_id":"filesystem_write","summary":"write file content"}',
                "raw_result": {
                    "ok": True,
                    "tool_id": "filesystem_write",
                    "hydration_targets": ["filesystem_write"],
                },
            },
        ],
    )

    round_tool = result["stages"][0]["rounds"][0]["tools"][0]
    assert "raw_result" not in round_tool


def test_frontdoor_default_hydrated_tool_limit_is_16() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    assert runner._frontdoor_hydrated_tool_limit_value() == 16


@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_seeds_session_hydrated_tools() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_hydrated_tool_names = ["filesystem_write"]

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )
    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["submit_next_stage", "load_tool_context", "filesystem_write"],
        }

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            model_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            stable_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            dynamic_appendix_messages=[],
            tool_names=["submit_next_stage", "load_tool_context", "filesystem_write"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {
                    "queries": {
                        "raw_query": "hello",
                        "skill_query": "hello skill",
                        "tool_query": "hello tool",
                    },
                    "dense": {
                        "skills": [{"record_id": "skill:demo"}],
                        "tools": [{"record_id": "tool:filesystem", "tool_id": "filesystem_write"}],
                    },
                },
                "tool_selection": {
                    "mode": "dense_only",
                    "candidate_tool_names": [],
                },
                "capability_snapshot": {
                    "visible_tool_ids": ["submit_next_stage", "load_tool_context", "filesystem_write"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]

    prepared = await runner._graph_prepare_turn(
        state=initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert prepared["hydrated_tool_names"] == ["filesystem_write"]
    assert prepared["tool_names"] == ["submit_next_stage", "load_tool_context", "filesystem_write"]
    assert prepared["frontdoor_selection_debug"] == {
        "query_text": "hello",
        "raw_turn_query_text": "hello",
        "semantic_frontdoor": {
            "queries": {
                "raw_query": "hello",
                "skill_query": "hello skill",
                "tool_query": "hello tool",
            },
            "dense": {
                "skills": [{"record_id": "skill:demo"}],
                "tools": [{"record_id": "tool:filesystem", "tool_id": "filesystem_write"}],
            },
        },
        "tool_selection": {
            "mode": "dense_only",
            "candidate_tool_names": [],
        },
        "capability_snapshot": {
            "visible_tool_ids": ["submit_next_stage", "load_tool_context", "filesystem_write"],
            "visible_skill_ids": [],
        },
        "selected_skills": [],
        "callable_tool_names": ["submit_next_stage"],
        "candidate_tool_names": [],
        "hydrated_tool_names": ["filesystem_write"],
    }
    contract_messages = [
        dict(item)
        for item in list(prepared["dynamic_appendix_messages"])
        if _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    assert len(contract_messages) == 1
    contract_text = str(contract_messages[0]["content"] or "")
    assert "callable_tools: `submit_next_stage`" in contract_text
    assert "candidate_tools: none" in contract_text
    assert "visible_skill_ids" not in contract_text
    assert "rbac_visible_tool_names" not in contract_text
    assert "rbac_visible_skill_ids" not in contract_text


@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_keeps_cron_internal_event_out_of_user_messages() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )
    captured: dict[str, object] = {}

    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["cron"],
        }

    async def _build_for_ceo(**kwargs):
        captured["request_body_seed_messages"] = list(kwargs.get("request_body_seed_messages") or [])
        captured["user_content"] = kwargs.get("user_content")
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        return SimpleNamespace(
            model_messages=list(stable_messages),
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            tool_names=["cron"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["cron"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]

    prepared = await runner._graph_prepare_turn(
        state=initial_persistent_state(
            user_input={
                "content": "Report the current time to the user.",
                "metadata": {
                    "cron_internal": True,
                    "cron_job_id": "job-77",
                    "cron_max_runs": 3,
                    "cron_delivery_index": 2,
                    "cron_delivered_runs": 1,
                    "cron_reminder_text": "Report the current time to the user.",
                },
            }
        ),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert captured["user_content"] == ""
    seed_messages = [dict(item) for item in list(captured["request_body_seed_messages"] or [])]
    assert [message["role"] for message in seed_messages] == ["system", "system"]
    assert str(seed_messages[0]["content"]).startswith("You are handling a cron-internal structured reminder turn.")
    assert str(seed_messages[1]["content"]).startswith("[CRON INTERNAL EVENT]")
    assert not any(
        str(item.get("role") or "").strip().lower() == "user"
        and str(item.get("content") or "").strip() == "Report the current time to the user."
        for item in list(prepared["frontdoor_request_body_messages"] or [])
        if isinstance(item, dict)
    )


@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_keeps_candidate_tools_visible_without_valid_stage() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )

    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["submit_next_stage", "load_tool_context", "filesystem_write"],
        }

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            model_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            stable_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            dynamic_appendix_messages=[],
            tool_names=["submit_next_stage", "load_tool_context", "filesystem_write"],
            candidate_tool_names=["filesystem_write"],
            candidate_tool_items=[{"tool_id": "filesystem_write", "description": "Write file content to disk."}],
            trace={
                "selected_skills": [{"skill_id": "memory"}],
                "semantic_frontdoor": {},
                "tool_selection": {
                    "mode": "dense_only",
                    "candidate_tool_names": ["filesystem_write"],
                },
                "capability_snapshot": {
                    "visible_tool_ids": ["submit_next_stage", "load_tool_context", "filesystem_write"],
                    "visible_skill_ids": ["memory"],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]

    prepared = await runner._graph_prepare_turn(
        state=initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert prepared["tool_names"] == ["submit_next_stage", "load_tool_context", "filesystem_write"]
    assert prepared["candidate_tool_names"] == ["filesystem_write"]
    assert prepared["candidate_skill_ids"] == ["memory"]
    assert prepared["visible_skill_ids"] == ["memory"]
    contract_messages = [
        dict(item)
        for item in list(prepared["dynamic_appendix_messages"])
        if _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    assert len(contract_messages) == 1
    contract_text = str(contract_messages[0]["content"] or "")
    assert "callable_tools: `submit_next_stage`" in contract_text
    assert "- `filesystem_write`: Write file content to disk." in contract_text
    assert "candidate_skills: `memory`" in contract_text
    assert "visible_skill_ids" not in contract_text
    assert "rbac_visible_tool_names" not in contract_text
    assert "rbac_visible_skill_ids" not in contract_text


@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_persists_request_body_without_tool_contract() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda session_key: SimpleNamespace(session_key=session_key)),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )

    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["submit_next_stage", "load_tool_context", "filesystem_write"],
        }

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            model_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            stable_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "hello"},
            ],
            dynamic_appendix_messages=[
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
            ],
            tool_names=["submit_next_stage", "load_tool_context", "filesystem_write"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {"mode": "dense_only", "candidate_tool_names": []},
                "capability_snapshot": {
                    "visible_tool_ids": ["submit_next_stage", "load_tool_context", "filesystem_write"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="## Retrieved Context\n- authoritative memory",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = (
        lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]
    )

    prepared = await runner._graph_prepare_turn(
        state=initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert prepared["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]
    assert len(prepared["dynamic_appendix_messages"]) == 1
    contract_message = dict(prepared["dynamic_appendix_messages"][0] or {})
    assert is_frontdoor_tool_contract_message(contract_message)
    assert contract_message["role"] == "assistant"
    assert str(contract_message.get("content") or "").startswith("## Runtime Tool Contract")


@pytest.mark.asyncio
async def test_create_agent_runner_graph_prepare_turn_recovers_paused_manual_context_for_new_turn() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._set_paused_execution_context(
        {
            "status": "paused",
            "user_message": {"content": "Original paused request"},
            "canonical_context": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Continue the paused investigation",
                        "representation": "raw",
                        "status": "active",
                        "stage_kind": "normal",
                        "tool_round_budget": 6,
                        "tool_rounds_used": 2,
                        "rounds": [],
                    }
                ],
            },
            "frontdoor_canonical_context": {
                "active_stage_id": "",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-0",
                        "stage_index": 0,
                        "stage_goal": "Earlier completed context",
                        "representation": "summary",
                        "status": "completed",
                        "stage_kind": "normal",
                        "tool_round_budget": 4,
                        "tool_rounds_used": 4,
                        "rounds": [],
                    }
                ],
            },
            "compression": {
                "status": "running",
                "text": "??????",
                "source": "token_compression",
                "needs_recheck": False,
            },
            "hydrated_tool_names": ["filesystem_write"],
        }
    )
    session._frontdoor_stage_state = {}
    session._frontdoor_canonical_context = {}
    session._compression_state = {}
    session._semantic_context_state = {}
    session._frontdoor_hydrated_tool_names = []

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(
                get_or_create=lambda session_key: SimpleNamespace(
                    session_key=session_key,
                    messages=[
                        {
                            "role": "user",
                            "content": "Original paused request",
                            "metadata": {"_transcript_state": "paused"},
                        }
                    ],
                )
            ),
            main_task_service=None,
            tools=SimpleNamespace(get=lambda *_: None, tool_names=[]),
            provider_name="openai",
            model="gpt-test",
        )
    )

    async def _resolve_for_actor(**kwargs):
        _ = kwargs
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["submit_next_stage", "load_tool_context", "filesystem_write"],
        }

    captured_builder_kwargs: dict[str, object] = {}

    async def _build_for_ceo(**kwargs):
        captured_builder_kwargs.update(kwargs)
        return SimpleNamespace(
            model_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "continue"},
            ],
            stable_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "continue"},
            ],
            dynamic_appendix_messages=[],
            tool_names=["submit_next_stage", "load_tool_context", "filesystem_write"],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {"mode": "dense_only", "candidate_tool_names": []},
                "capability_snapshot": {
                    "visible_tool_ids": ["submit_next_stage", "load_tool_context", "filesystem_write"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    runner._selected_tool_schemas = (
        lambda tool_names: [{"name": name, "parameters": {"type": "object"}} for name in list(tool_names or [])]
    )

    prepared = await runner._graph_prepare_turn(
        state=initial_persistent_state(user_input={"content": "continue", "metadata": {}}),
        runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
    )

    assert captured_builder_kwargs["frontdoor_stage_state"]["active_stage_id"] == "frontdoor-stage-1"
    assert captured_builder_kwargs["frontdoor_canonical_context"]["stages"][0]["stage_id"] == "frontdoor-stage-0"
    assert captured_builder_kwargs["semantic_context_state"] == {}
    assert captured_builder_kwargs["hydrated_tool_names"] == ["filesystem_write"]
    assert prepared["frontdoor_stage_state"]["active_stage_id"] == "frontdoor-stage-1"
    assert prepared["compression_state"]["status"] == "running"
    assert "semantic_context_state" not in prepared
    assert prepared["hydrated_tool_names"] == ["filesystem_write"]
    contract_messages = [
        dict(item)
        for item in list(prepared["dynamic_appendix_messages"])
        if _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    assert len(contract_messages) == 1
    contract_text = str(contract_messages[0]["content"] or "")
    assert "callable_tools: `submit_next_stage`, `load_tool_context`, `filesystem_write`" in contract_text
    assert "hydrated_tools: `filesystem_write`" in contract_text


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


def test_runtime_agent_session_paused_snapshot_keeps_frontdoor_runtime_context() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_goal": "Continue the paused investigation",
                "representation": "raw",
                "status": "active",
                "stage_kind": "normal",
                "tool_round_budget": 6,
                "tool_rounds_used": 2,
                "rounds": [],
            }
        ],
    }
    session._frontdoor_canonical_context = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-0",
                "stage_index": 0,
                "stage_goal": "Earlier completed context",
                "representation": "summary",
                "status": "completed",
                "stage_kind": "normal",
                "tool_round_budget": 4,
                "tool_rounds_used": 4,
                "rounds": [],
            }
        ],
    }
    session._compression_state = {
        "status": "running",
        "text": "??????",
        "source": "token_compression",
        "needs_recheck": False,
    }
    session._semantic_context_state = {}
    session._frontdoor_hydrated_tool_names = ["filesystem_write"]

    snapshot = session._build_execution_context_snapshot(
        allow_manual_pause=True,
        status_override="paused",
    )

    assert isinstance(snapshot, dict)
    assert snapshot["canonical_context"]["active_stage_id"] == "frontdoor-stage-1"
    assert snapshot["frontdoor_canonical_context"]["stages"][0]["stage_id"] == "frontdoor-stage-0"
    assert snapshot["compression"]["source"] == "token_compression"
    assert "semantic_context_state" not in snapshot
    assert snapshot["hydrated_tool_names"] == ["filesystem_write"]


def test_runtime_agent_session_persists_paused_request_body_baseline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from g3ku.runtime import web_ceo_sessions

    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_request_body_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
    ]
    session._frontdoor_history_shrink_reason = "stage_compaction"

    paused_snapshot = session._build_execution_context_snapshot(
        allow_manual_pause=True,
        status_override="paused",
    )
    session._set_paused_execution_context(paused_snapshot)

    persisted = web_ceo_sessions.read_paused_execution_context("web:shared")

    assert isinstance(persisted, dict)
    assert persisted["frontdoor_request_body_messages"] == session._frontdoor_request_body_messages
    assert persisted["frontdoor_history_shrink_reason"] == "stage_compaction"


def test_runtime_agent_session_persists_paused_repair_required_lists(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from g3ku.runtime import web_ceo_sessions

    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._frontdoor_repair_required_tool_items = [
        {
            "tool_id": "agent_browser",
            "description": "Browser automation",
            "reason": "missing required paths",
        }
    ]
    session._frontdoor_repair_required_skill_items = [
        {
            "skill_id": "writing-skills",
            "description": "Skill maintenance workflow",
            "reason": "missing required bins",
        }
    ]

    paused_snapshot = session._build_execution_context_snapshot(
        allow_manual_pause=True,
        status_override="paused",
    )
    session._set_paused_execution_context(paused_snapshot)

    persisted = web_ceo_sessions.read_paused_execution_context("web:shared")

    assert isinstance(persisted, dict)
    assert persisted["repair_required_tool_items"] == session._frontdoor_repair_required_tool_items
    assert persisted["repair_required_skill_items"] == session._frontdoor_repair_required_skill_items

    restored = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    restored_snapshot = restored.paused_execution_context_snapshot()

    assert isinstance(restored_snapshot, dict)
    assert restored_snapshot["repair_required_tool_items"] == session._frontdoor_repair_required_tool_items
    assert restored_snapshot["repair_required_skill_items"] == session._frontdoor_repair_required_skill_items


@pytest.mark.asyncio
async def test_graph_finalize_turn_appends_direct_reply_after_runtime_context_assistant() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    result = await runner._graph_finalize_turn(
        {
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "## Retrieved Context\n- memory"},
            ],
            "final_output": "final answer",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "hello",
        }
    )

    assert result["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "## Retrieved Context\n- memory"},
        {"role": "assistant", "content": "final answer"},
    ]


@pytest.mark.asyncio
async def test_graph_finalize_turn_strips_all_frontdoor_tool_contract_snapshots_from_durable_messages() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    contract_text = json.dumps(
        {
            "message_type": "frontdoor_runtime_tool_contract",
            "callable_tool_names": ["submit_next_stage"],
            "candidate_tools": [],
            "hydrated_tool_names": [],
            "candidate_skill_ids": [],
            "stage_summary": {"active_stage_id": "", "transition_required": False},
            "contract_revision": "frontdoor:v1",
        },
        ensure_ascii=False,
    )

    result = await runner._graph_finalize_turn(
        {
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "hello"},
                {"role": "user", "content": contract_text},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "submit_next_stage", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "submit_next_stage",
                    "tool_call_id": "call-1",
                    "content": '{"status":"success"}',
                },
                {"role": "user", "content": contract_text},
            ],
            "final_output": "final answer",
            "route_kind": "direct_reply",
            "heartbeat_internal": False,
            "query_text": "hello",
        }
    )

    assert result["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "submit_next_stage",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
        {"role": "assistant", "content": "final answer"},
    ]


def test_create_agent_runner_syncs_frontdoor_selection_debug_into_inflight_snapshot() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._state.latest_message = "working"
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "hydrated_tool_names": ["filesystem_write"],
            "frontdoor_selection_debug": {
                "query_text": "create a markdown file",
                "semantic_frontdoor": {
                    "queries": {
                        "raw_query": "create a markdown file",
                        "tool_query": "filesystem write markdown file",
                    },
                    "dense": {
                        "tools": [{"record_id": "tool:filesystem", "tool_id": "filesystem_write"}],
                    },
                },
                "tool_selection": {"candidate_tool_names": ["filesystem_write"]},
            },
        },
        session=session,
    )

    snapshot = session.inflight_turn_snapshot()

    assert isinstance(snapshot, dict)
    assert snapshot["frontdoor_selection_debug"] == {
        "query_text": "create a markdown file",
        "semantic_frontdoor": {
            "queries": {
                "raw_query": "create a markdown file",
                "tool_query": "filesystem write markdown file",
            },
            "dense": {
                "tools": [{"record_id": "tool:filesystem", "tool_id": "filesystem_write"}],
            },
        },
        "tool_selection": {"candidate_tool_names": ["filesystem_write"]},
    }


def test_create_agent_runner_syncs_frontdoor_actual_request_trace_into_inflight_snapshot() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._state.is_running = True
    session._state.status = "running"
    session._state.latest_message = "working"
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "hydrated_tool_names": [],
            "frontdoor_actual_request_path": str(Path("D:/tmp/frontdoor-request.json")),
            "frontdoor_actual_request_history": [
                {
                    "path": str(Path("D:/tmp/frontdoor-request.json")),
                    "turn_id": "turn-frontdoor-1",
                    "actual_request_hash": "request-hash",
                    "actual_request_message_count": 6,
                    "actual_tool_schema_hash": "tool-hash",
                    "prompt_cache_key_hash": "family-hash",
                }
            ],
            "prompt_cache_diagnostics": {
                "prompt_cache_key_hash": "family-hash",
                "actual_request_hash": "request-hash",
                "actual_request_message_count": 6,
                "actual_tool_schema_hash": "tool-hash",
            },
        },
        session=session,
    )

    snapshot = session.inflight_turn_snapshot()

    assert isinstance(snapshot, dict)
    assert snapshot["actual_request_path"] == str(Path("D:/tmp/frontdoor-request.json"))
    assert snapshot["prompt_cache_key_hash"] == "family-hash"
    assert snapshot["actual_request_hash"] == "request-hash"
    assert snapshot["actual_request_message_count"] == 6
    assert snapshot["actual_tool_schema_hash"] == "tool-hash"
    assert snapshot["actual_request_history"] == [
        {
            "path": str(Path("D:/tmp/frontdoor-request.json")),
            "turn_id": "turn-frontdoor-1",
            "actual_request_hash": "request-hash",
            "actual_request_message_count": 6,
            "actual_tool_schema_hash": "tool-hash",
            "prompt_cache_key_hash": "family-hash",
        }
    ]


def test_create_agent_runner_sync_keeps_previous_actual_request_baseline_for_prepare_only_state() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_request_body_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    session._frontdoor_actual_request_path = ""
    session._frontdoor_actual_request_history = []
    session._frontdoor_prompt_cache_key_hash = ""
    session._frontdoor_actual_request_hash = ""
    session._frontdoor_actual_request_message_count = 0
    session._frontdoor_actual_tool_schema_hash = ""
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "hydrated_tool_names": [],
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "new paused user"},
            ],
            "prompt_cache_diagnostics": {
                "prompt_cache_key_hash": "family-hash-planned",
                "actual_request_hash": "request-hash-planned",
                "actual_request_message_count": 4,
                "actual_tool_schema_hash": "tool-hash-planned",
            },
        },
        session=session,
    )

    assert session._frontdoor_request_body_messages == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    assert session._frontdoor_actual_request_path == ""
    assert session._frontdoor_actual_request_history == []
    assert session._frontdoor_prompt_cache_key_hash == "family-hash-planned"
    assert session._frontdoor_actual_request_hash == ""
    assert session._frontdoor_actual_request_message_count == 0
    assert session._frontdoor_actual_tool_schema_hash == ""


def test_create_agent_runner_sync_allows_finalize_to_extend_existing_actual_request_baseline() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_request_body_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "tool thinking"},
    ]
    session._frontdoor_actual_request_path = str(Path("D:/tmp/frontdoor-request-current.json"))
    session._frontdoor_actual_request_history = [
        {
            "path": str(Path("D:/tmp/frontdoor-request-current.json")),
            "turn_id": "turn-frontdoor-current",
            "actual_request_hash": "request-hash-current",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-hash-current",
            "prompt_cache_key_hash": "family-hash-current",
        }
    ]
    session._frontdoor_prompt_cache_key_hash = "family-hash-current"
    session._frontdoor_actual_request_hash = "request-hash-current"
    session._frontdoor_actual_request_message_count = 3
    session._frontdoor_actual_tool_schema_hash = "tool-hash-current"
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    runner._sync_runtime_session_frontdoor_state(
        state={
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "hydrated_tool_names": [],
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "tool thinking"},
                {"role": "assistant", "content": "final answer"},
            ],
        },
        session=session,
    )

    assert session._frontdoor_request_body_messages == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "tool thinking"},
        {"role": "assistant", "content": "final answer"},
    ]
    assert session._frontdoor_actual_request_path == str(Path("D:/tmp/frontdoor-request-current.json"))
    assert session._frontdoor_actual_request_history == [
        {
            "path": str(Path("D:/tmp/frontdoor-request-current.json")),
            "turn_id": "turn-frontdoor-current",
            "actual_request_hash": "request-hash-current",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-hash-current",
            "prompt_cache_key_hash": "family-hash-current",
        }
    ]
    assert session._frontdoor_actual_request_hash == "request-hash-current"
    assert session._frontdoor_actual_request_message_count == 3
    assert session._frontdoor_actual_tool_schema_hash == "tool-hash-current"


def test_runtime_agent_session_preserves_previous_actual_request_trace_for_next_visible_turn() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_actual_request_path = str(Path("D:/tmp/frontdoor-request-current.json"))
    session._frontdoor_actual_request_history = [
        {
            "path": str(Path("D:/tmp/frontdoor-request-current.json")),
            "turn_id": "turn-frontdoor-current",
            "actual_request_hash": "request-hash-current",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-hash-current",
            "prompt_cache_key_hash": "family-hash-current",
        }
    ]

    session._preserve_frontdoor_actual_request_trace_for_next_visible_turn()

    assert session._frontdoor_previous_actual_request_path == str(Path("D:/tmp/frontdoor-request-current.json"))
    assert session._frontdoor_previous_actual_request_history == [
        {
            "path": str(Path("D:/tmp/frontdoor-request-current.json")),
            "turn_id": "turn-frontdoor-current",
            "actual_request_hash": "request-hash-current",
            "actual_request_message_count": 3,
            "actual_tool_schema_hash": "tool-hash-current",
            "prompt_cache_key_hash": "family-hash-current",
        }
    ]


def test_fresh_turn_live_request_messages_reuses_previous_actual_request_prefix(tmp_path: Path) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = tmp_path / "frontdoor-request-previous.json"
    previous_record_path.write_text(
        json.dumps(
            {
                "request_messages": [
                    {"role": "system", "content": "SYSTEM"},
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old retrieved"},
                    {
                        "role": "user",
                        "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["submit_next_stage"]}',
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "submit_next_stage", "arguments": "{}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "name": "submit_next_stage",
                        "tool_call_id": "call-1",
                        "content": '{"status":"success"}',
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        _frontdoor_previous_actual_request_path=str(previous_record_path),
        _frontdoor_previous_actual_request_history=[
            {
                "path": str(previous_record_path),
                "turn_id": "turn-frontdoor-previous",
            }
        ],
    )

    scaffold = runner._fresh_turn_live_request_messages_from_previous_actual_request(
        session=session,
        stable_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old retrieved"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "submit_next_stage", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "submit_next_stage",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
        ],
        live_request_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old retrieved"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "submit_next_stage", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "submit_next_stage",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
            {"role": "user", "content": "next user"},
        ],
    )

    assert scaffold == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old retrieved"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["submit_next_stage"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "submit_next_stage",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "next user"},
    ]


def test_fresh_turn_live_request_messages_reuses_previous_actual_request_prefix_despite_trailing_whitespace_drift(
    tmp_path: Path,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = tmp_path / "frontdoor-request-previous.json"
    previous_request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["submit_next_stage"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "submit_next_stage",
            "tool_call_id": "call-1",
            "content": '{"status":"success"} ',
        },
    ]
    previous_record_path.write_text(
        json.dumps({"request_messages": previous_request_messages}, ensure_ascii=False),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        _frontdoor_previous_actual_request_path=str(previous_record_path),
        _frontdoor_previous_actual_request_history=[
            {
                "path": str(previous_record_path),
                "turn_id": "turn-frontdoor-previous",
            }
        ],
    )

    scaffold = runner._fresh_turn_live_request_messages_from_previous_actual_request(
        session=session,
        stable_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "submit_next_stage", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "submit_next_stage",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
        ],
        live_request_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "submit_next_stage", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "submit_next_stage",
                "tool_call_id": "call-1",
                "content": '{"status":"success"}',
            },
            {"role": "assistant", "content": "final answer"},
            {"role": "user", "content": "next user"},
        ],
    )

    assert scaffold == [
        *previous_request_messages,
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "next user"},
    ]


def test_fresh_turn_tool_schema_seed_does_not_expand_previous_actual_request_schemas_when_current_is_superset(
    tmp_path: Path,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = tmp_path / "frontdoor-request-previous.json"
    previous_record_path.write_text(
        json.dumps(
            {
                "tool_schemas": [
                    {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
                    {
                        "type": "function",
                        "function": {"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}},
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        _frontdoor_previous_actual_request_path=str(previous_record_path),
        _frontdoor_previous_actual_request_history=[
            {
                "path": str(previous_record_path),
                "turn_id": "turn-frontdoor-previous",
            }
        ],
    )

    seeded_schemas, seeded_names = runner._fresh_turn_tool_schema_seed_from_previous_actual_request(
        session=session,
        tool_schemas=[
            {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
            {
                "type": "function",
                "function": {"name": "web_fetch", "description": "", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}},
            },
        ],
    )

    assert seeded_names is None
    assert seeded_schemas == [
        {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
        {
            "type": "function",
            "function": {"name": "web_fetch", "description": "", "parameters": {"type": "object"}},
        },
        {
            "type": "function",
            "function": {"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}},
        },
    ]


def test_frontdoor_provider_tool_exposure_only_commits_on_token_compression() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    frozen = runner._resolve_frontdoor_provider_tool_exposure(
        active_provider_tool_names=["exec", "submit_next_stage"],
        pending_provider_tool_names=[],
        desired_provider_tool_names=["exec", "web_fetch", "submit_next_stage"],
        commit_reason="",
    )

    assert frozen["provider_tool_names"] == ["exec", "submit_next_stage"]
    assert frozen["pending_provider_tool_names"] == ["exec", "web_fetch", "submit_next_stage"]
    assert frozen["provider_tool_exposure_pending"] is True
    assert frozen["provider_tool_exposure_commit_reason"] == ""

    stage_compaction = runner._resolve_frontdoor_provider_tool_exposure(
        active_provider_tool_names=["exec", "submit_next_stage"],
        pending_provider_tool_names=["exec", "web_fetch", "submit_next_stage"],
        desired_provider_tool_names=["exec", "web_fetch", "submit_next_stage"],
        commit_reason="stage_compaction",
    )

    assert stage_compaction["provider_tool_names"] == ["exec", "submit_next_stage"]
    assert stage_compaction["pending_provider_tool_names"] == ["exec", "web_fetch", "submit_next_stage"]
    assert stage_compaction["provider_tool_exposure_pending"] is True
    assert stage_compaction["provider_tool_exposure_commit_reason"] == ""

    token_compaction = runner._resolve_frontdoor_provider_tool_exposure(
        active_provider_tool_names=["exec", "submit_next_stage"],
        pending_provider_tool_names=["exec", "web_fetch", "submit_next_stage"],
        desired_provider_tool_names=["exec", "web_fetch", "submit_next_stage"],
        commit_reason="token_compression",
    )

    assert token_compaction["provider_tool_names"] == ["exec", "web_fetch", "submit_next_stage"]
    assert token_compaction["pending_provider_tool_names"] == []
    assert token_compaction["provider_tool_exposure_pending"] is False
    assert token_compaction["provider_tool_exposure_commit_reason"] == "token_compression"


def test_build_frontdoor_request_artifact_payload_includes_provider_tool_exposure_fields() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    payload = runner._build_frontdoor_request_artifact_payload(
        state={
            "model_refs": ["gpt-5.2"],
            "frontdoor_history_shrink_reason": "token_compression",
            "frontdoor_token_preflight_diagnostics": {"provider_model": "gpt-5.2"},
            "provider_tool_exposure_revision": "pte:frontdoor-revision",
            "provider_tool_exposure_commit_reason": "token_compression",
        },
        session_key="web:shared",
        turn_id="turn-1",
        request_messages=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "user"},
        ],
        tool_schemas=[
            {"type": "function", "function": {"name": "exec", "parameters": {"type": "object"}}},
        ],
        prompt_cache_key="cache-key",
        prompt_cache_diagnostics=build_actual_request_diagnostics(
            request_messages=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "user"},
            ],
            tool_schemas=[
                {"type": "function", "function": {"name": "exec", "parameters": {"type": "object"}}},
            ],
        ),
        parallel_tool_calls=False,
        provider_request_meta={"provider": "responses"},
        provider_request_body={"model": "gpt-5.2"},
        usage={"input_tokens": 10, "output_tokens": 5},
        request_kind="frontdoor_actual_request",
        request_lane="visible_frontdoor",
    )

    assert payload["provider_tool_exposure_revision"] == "pte:frontdoor-revision"
    assert payload["provider_tool_exposure_commit_reason"] == "token_compression"


def test_runtime_agent_session_completed_continuity_bridge_requires_exact_visible_sets() -> None:
    session = RuntimeAgentSession(
        SimpleNamespace(model="demo", reasoning_effort=None, multi_agent_runner=None),
        session_key="web:shared",
        channel="web",
        chat_id="shared",
    )
    session._frontdoor_completed_continuity_bridge_pending = True
    session._frontdoor_capability_snapshot_exposure_revision = "exp:demo"
    session._frontdoor_visible_tool_ids = ["exec", "web_fetch"]
    session._frontdoor_visible_skill_ids = ["web-access"]
    session._frontdoor_provider_tool_schema_names = ["exec", "web_fetch"]

    matched = session._consume_completed_continuity_bridge(
        current_visible_tool_ids=["web_fetch", "exec"],
        current_visible_skill_ids=["web-access"],
    )

    assert matched == {
        "pending": True,
        "enabled": True,
        "exposure_revision": "exp:demo",
        "provider_tool_schema_names": ["exec", "web_fetch"],
    }

    session._frontdoor_completed_continuity_bridge_pending = True
    mismatched = session._consume_completed_continuity_bridge(
        current_visible_tool_ids=["exec", "web_fetch", "filesystem_write"],
        current_visible_skill_ids=["web-access"],
    )

    assert mismatched == {
        "pending": True,
        "enabled": False,
        "exposure_revision": "",
        "provider_tool_schema_names": [],
    }


def test_fresh_turn_tool_schema_seed_skips_expected_bridge_when_previous_names_do_not_match(
    tmp_path: Path,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    previous_record_path = tmp_path / "frontdoor-request-previous.json"
    previous_record_path.write_text(
        json.dumps(
            {
                "tool_schemas": [
                    {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
                    {
                        "type": "function",
                        "function": {"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}},
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        _frontdoor_previous_actual_request_path=str(previous_record_path),
        _frontdoor_previous_actual_request_history=[
            {
                "path": str(previous_record_path),
                "turn_id": "turn-frontdoor-previous",
            }
        ],
    )
    current_schemas = [
        {"type": "function", "function": {"name": "exec", "description": "", "parameters": {"type": "object"}}},
        {
            "type": "function",
            "function": {"name": "web_fetch", "description": "", "parameters": {"type": "object"}},
        },
        {
            "type": "function",
            "function": {"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}},
        },
    ]

    seeded_schemas, seeded_names = runner._fresh_turn_tool_schema_seed_from_previous_actual_request(
        session=session,
        tool_schemas=current_schemas,
        expected_schema_names=["exec", "web_fetch"],
    )

    assert seeded_names is None
    assert seeded_schemas == current_schemas


def test_live_request_message_records_for_state_prefers_explicit_frontdoor_live_request_messages() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    records = runner._live_request_message_records_for_state(
        state={
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "stable baseline"},
            ],
            "frontdoor_live_request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "previous actual request"},
                {"role": "assistant", "content": "final answer"},
                {"role": "user", "content": "next user"},
            ],
        }
    )

    assert records == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "previous actual request"},
        {"role": "assistant", "content": "final answer"},
        {"role": "user", "content": "next user"},
    ]


@pytest.mark.asyncio
async def test_graph_call_model_fresh_turn_reuses_previous_message_artifact_prefix_after_message_tool_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)
    captured_model_messages: list[dict[str, object]] = []

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    previous_request_path = tmp_path / "frontdoor-request-previous.json"
    previous_request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "older answer"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["exec","submit_next_stage"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-exec-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-exec-1",
            "content": "Command finished for final:ceo-demo",
        },
    ]
    previous_request_path.write_text(
        json.dumps(
            {
                "request_messages": previous_request_messages,
                "tool_schemas": [
                    {
                        "type": "function",
                        "function": {"name": "exec", "description": "", "parameters": {"type": "object"}},
                    },
                    {
                        "type": "function",
                        "function": {"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec", "submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = str(kwargs.get("user_content") or "").strip()
        model_messages = [*stable_messages, {"role": "user", "content": user_content}]
        return SimpleNamespace(
            tool_names=["exec", "submit_next_stage"],
            model_messages=model_messages,
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec", "submit_next_stage"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    async def _call_model_with_tools(**kwargs):
        captured_model_messages[:] = list(kwargs.get("messages") or [])
        return SimpleNamespace()

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])
    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_model": "openai_codex:gpt-test",
            "context_window_tokens": 128000,
        },
    )
    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(
        runner,
        "_model_response_view",
        lambda _message: SimpleNamespace(content="ok", tool_calls=[], provider_request_meta={}, provider_request_body={}),
    )
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "older answer"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-exec-1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "exec",
                "tool_call_id": "call-exec-1",
                "content": "Command finished for final:ceo-demo",
            },
            {"role": "assistant", "content": "final answer"},
        ],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
        _frontdoor_previous_actual_request_path=str(previous_request_path),
        _frontdoor_previous_actual_request_history=[{"path": str(previous_request_path), "turn_id": "turn-old"}],
        _frontdoor_actual_request_path="",
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "next question", "metadata": {}}),
        runtime=runtime,
    )
    await runner._graph_call_model(
        prepared,
        runtime=runtime,
    )

    assert captured_model_messages[: len(previous_request_messages)] == previous_request_messages
    assert {"role": "assistant", "content": "final answer"} in captured_model_messages
    final_answer_index = captured_model_messages.index({"role": "assistant", "content": "final answer"})
    next_user_index = captured_model_messages.index({"role": "user", "content": "next question"})
    assert final_answer_index < next_user_index


@pytest.mark.asyncio
async def test_graph_call_model_runs_token_preflight_after_fresh_turn_seed_and_before_provider_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)
    captured_model_messages: list[dict[str, object]] = []
    observed: dict[str, object] = {}

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    previous_request_path = tmp_path / "frontdoor-request-previous.json"
    previous_request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "older answer"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["exec","submit_next_stage"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-exec-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-exec-1",
            "content": "Command finished for final:ceo-demo",
        },
    ]
    previous_request_path.write_text(
        json.dumps(
            {
                "request_messages": previous_request_messages,
                "tool_schemas": [
                    {
                        "type": "function",
                        "function": {"name": "exec", "description": "", "parameters": {"type": "object"}},
                    },
                    {
                        "type": "function",
                        "function": {"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec", "submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = str(kwargs.get("user_content") or "").strip()
        model_messages = [*stable_messages, {"role": "user", "content": user_content}]
        return SimpleNamespace(
            tool_names=["exec", "submit_next_stage"],
            model_messages=model_messages,
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec", "submit_next_stage"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    async def _call_model_with_tools(**kwargs):
        captured_model_messages[:] = list(kwargs.get("messages") or [])
        return SimpleNamespace()

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_model": "openai_codex:gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )
    monkeypatch.setattr(
        runner,
        "_selected_tool_schemas",
        lambda tool_names: [
            {
                "type": "function",
                "function": {"name": str(name), "description": "", "parameters": {"type": "object"}},
            }
            for name in list(tool_names or [])
        ],
    )
    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(
        runner,
        "_model_response_view",
        lambda _message: SimpleNamespace(content="ok", tool_calls=[], provider_request_meta={}, provider_request_body={}),
    )
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})
    monkeypatch.setattr(
        runner,
        "_estimate_frontdoor_send_total_tokens",
        lambda **kwargs: observed.update(
            {
                "request_messages": list(kwargs["request_messages"]),
                "tool_schemas": list(kwargs["tool_schemas"]),
            }
        ) or 12345,
        raising=False,
    )

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "older answer"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-exec-1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "exec",
                "tool_call_id": "call-exec-1",
                "content": "Command finished for final:ceo-demo",
            },
            {"role": "assistant", "content": "final answer"},
        ],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
        _frontdoor_previous_actual_request_path=str(previous_request_path),
        _frontdoor_previous_actual_request_history=[{"path": str(previous_request_path), "turn_id": "turn-old"}],
        _frontdoor_actual_request_path="",
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "next question", "metadata": {}}),
        runtime=runtime,
    )
    await runner._graph_call_model(
        prepared,
        runtime=runtime,
    )

    assert observed["request_messages"][: len(previous_request_messages)] == previous_request_messages
    assert observed["tool_schemas"]
    assert captured_model_messages[: len(previous_request_messages)] == previous_request_messages


@pytest.mark.asyncio
async def test_prepare_turn_promotes_uploaded_image_only_into_live_request_when_binding_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=True)
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    merged_text = _web_ceo_uploaded_image_note(text="Please inspect this image", image_path=image_path)
    multimodal_text = _web_ceo_multimodal_image_note(text="Please inspect this image")

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["submit_next_stage"],
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["submit_next_stage"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["ceo_primary"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_id": "responses",
            "provider_model": "responses:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(
            user_input={
                "content": merged_text,
                "metadata": _web_ceo_upload_metadata(image_path),
            }
        ),
        runtime=runtime,
    )
    preflight = runner._frontdoor_send_preflight_snapshot(
        state=prepared,
        runtime=runtime,
        langchain_tools=[],
    )

    assert prepared["frontdoor_request_body_messages"][-1]["content"] == multimodal_text
    assert isinstance(prepared["frontdoor_live_request_messages"][-1]["content"], list)
    live_blocks = [
        block
        for block in list(prepared["frontdoor_live_request_messages"][-1]["content"] or [])
        if isinstance(block, dict)
    ]
    assert live_blocks[0] == {"type": "text", "text": multimodal_text}
    assert any(block.get("type") == "image_url" for block in live_blocks)
    assert "local path" not in json.dumps(prepared["frontdoor_live_request_messages"], ensure_ascii=False)
    assert "inspect the local file paths" not in json.dumps(
        prepared["frontdoor_live_request_messages"], ensure_ascii=False
    )
    assert "input_image" in json.dumps(preflight["provider_request_body"], ensure_ascii=False)
    assert "local path" not in json.dumps(preflight["provider_request_body"], ensure_ascii=False)
    assert prepared["query_text"] == "Please inspect this image"


@pytest.mark.asyncio
async def test_prepare_turn_keeps_uploaded_image_as_text_only_when_binding_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=False)
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
        app_config=SimpleNamespace(get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=False)),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    image_path = tmp_path / "demo.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    merged_text = _web_ceo_uploaded_image_note(text="Please inspect this image", image_path=image_path)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["submit_next_stage"],
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["submit_next_stage"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["ceo_primary"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_id": "responses",
            "provider_model": "responses:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(
            user_input={
                "content": merged_text,
                "metadata": _web_ceo_upload_metadata(image_path),
            }
        ),
        runtime=runtime,
    )
    preflight = runner._frontdoor_send_preflight_snapshot(
        state=prepared,
        runtime=runtime,
        langchain_tools=[],
    )

    assert prepared["frontdoor_request_body_messages"][-1]["content"] == merged_text
    assert prepared["frontdoor_live_request_messages"][-1]["content"] == merged_text
    assert "input_image" not in json.dumps(preflight["provider_request_body"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_follow_up_uploaded_image_stays_multimodal_without_local_path_when_binding_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=True)
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    image_path = tmp_path / "follow-up.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    merged_text = _web_ceo_uploaded_image_note(text="Please inspect this image", image_path=image_path)
    multimodal_text = _web_ceo_multimodal_image_note(text="Please inspect this image")

    queued_input = UserInputMessage(
        content=merged_text,
        metadata=_web_ceo_upload_metadata(image_path),
    )
    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        take_follow_up_batch_for_call_model=lambda: [queued_input],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )
    monkeypatch.setattr(runner, "_sync_runtime_session_frontdoor_state", lambda **_: None)

    update = await runner._consume_session_follow_up_messages_before_call_model(
        state={
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "assistant", "content": "Still working"},
            ],
            "frontdoor_request_body_messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "assistant", "content": "Still working"},
            ],
            "model_refs": ["ceo_primary"],
            "query_text": "Original request",
        },
        runtime=runtime,
    )

    assert update["frontdoor_request_body_messages"][-1]["content"] == multimodal_text
    live_blocks = [
        block
        for block in list(update["frontdoor_live_request_messages"][-1]["content"] or [])
        if isinstance(block, dict)
    ]
    assert live_blocks[0] == {"type": "text", "text": multimodal_text}
    assert any(block.get("type") == "image_url" for block in live_blocks)
    assert "local path" not in json.dumps(update["frontdoor_live_request_messages"], ensure_ascii=False)
    assert "inspect the local file paths" not in json.dumps(update, ensure_ascii=False)
    assert update["query_text"] == "Original request\n\nPlease inspect this image"


def test_frontdoor_send_preflight_snapshot_adds_content_open_image_overlay_only_to_live_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=True)
    loop = SimpleNamespace(
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs["state"].get("messages") or []),
            prompt_cache_key="cache-key",
            diagnostics={},
        ),
    )
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_id": "responses",
            "provider_model": "responses:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )

    image_path = tmp_path / "opened.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "Please inspect the reopened image"},
        ],
        "model_refs": ["ceo_primary"],
        "pending_content_open_image_payloads": [
            {
                "ok": True,
                "operation": "open",
                "content_kind": "image",
                "mime_type": "image/png",
                "summary": "图片已通过 content_open 打开，视觉内容将在下一轮请求中附带。",
                "multimodal_open_pending": True,
                "runtime_image_target": {"path": str(image_path), "mime_type": "image/png", "source_ref": ""},
            }
        ],
    }

    snapshot = runner._frontdoor_send_preflight_snapshot(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace(session=None, session_key="web:shared")),
        langchain_tools=[],
    )

    live_blocks = [
        block
        for block in list(snapshot["request_messages"][-1]["content"] or [])
        if isinstance(block, dict)
    ]
    assert live_blocks[0] == {"type": "text", "text": "图片已通过 content_open 打开，视觉内容已附带在本轮上下文中"}
    assert any(block.get("type") == "image_url" for block in live_blocks)
    assert snapshot["durable_request_messages"][-1]["content"] == "图片已通过 content_open 打开，视觉内容已附带在本轮上下文中"


def test_frontdoor_send_preflight_snapshot_rejects_content_open_image_overlay_without_multimodal_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=False)
    loop = SimpleNamespace(
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=False)
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs["state"].get("messages") or []),
            prompt_cache_key="cache-key",
            diagnostics={},
        ),
    )

    image_path = tmp_path / "opened.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
    state = {
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "Please inspect the reopened image"},
        ],
        "model_refs": ["ceo_primary"],
        "pending_content_open_image_payloads": [
            {
                "ok": True,
                "operation": "open",
                "content_kind": "image",
                "mime_type": "image/png",
                "summary": "图片已通过 content_open 打开，视觉内容将在下一轮请求中附带。",
                "multimodal_open_pending": True,
                "runtime_image_target": {"path": str(image_path), "mime_type": "image/png", "source_ref": ""},
            }
        ],
    }

    with pytest.raises(ceo_runtime_ops.FrontdoorCompressionRuntimeError, match="非多模态模型无法打开图片"):
        runner._frontdoor_send_preflight_snapshot(
            state=state,
            runtime=SimpleNamespace(context=SimpleNamespace(session=None, session_key="web:shared")),
            langchain_tools=[],
        )


def test_ceo_image_multimodal_enabled_for_model_refs_prefers_live_runtime_config_over_loop_app_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = SimpleNamespace(
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=False)
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)
    live_config = SimpleNamespace(
        get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
    )

    monkeypatch.setattr(
        ceo_runtime_ops,
        "get_runtime_config",
        lambda force=False: (live_config, 1, False),
    )

    assert runner._ceo_image_multimodal_enabled_for_model_refs(["ceo_primary"]) is True


def test_frontdoor_build_tool_runtime_context_includes_image_multimodal_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_session = SimpleNamespace(session_key="web:shared")
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _channel="web",
        _chat_id="shared",
        _memory_channel="web",
        _memory_chat_id="shared",
        inflight_turn_snapshot=lambda: {},
        _current_turn_id=lambda: "turn-1",
        _active_cancel_token=None,
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
            workspace=None,
            temp_dir="",
        )
    )

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(runner, "_session_task_defaults", lambda runtime_session: {})
    monkeypatch.setattr(
        runner,
        "_ceo_image_multimodal_enabled_for_model_refs",
        lambda refs: list(refs or []) == ["ceo_primary"],
    )

    context = runner._build_tool_runtime_context(
        state={
            "user_input": {"metadata": {}},
            "candidate_tool_names": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": [],
            "rbac_visible_skill_ids": [],
            "model_refs": ["ceo_primary"],
        },
        runtime=SimpleNamespace(
            context=SimpleNamespace(
                session=session,
                session_key="web:shared",
                on_progress=None,
            )
        ),
    )

    assert context["model_refs"] == ["ceo_primary"]
    assert context["image_multimodal_enabled"] is True


@pytest.mark.asyncio
async def test_prepare_turn_rejects_oversized_uploaded_image_when_binding_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_live_runtime_model(monkeypatch, image_multimodal_enabled=True)
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
        app_config=SimpleNamespace(
            get_managed_model=lambda key: SimpleNamespace(image_multimodal_enabled=(key == "ceo_primary"))
        ),
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    image_path = tmp_path / "huge.png"
    image_path.write_bytes(b"0" * (web_ceo_sessions.WEB_CEO_IMAGE_UPLOAD_MAX_BYTES + 1))
    merged_text = _web_ceo_uploaded_image_note(text="Please inspect this image", image_path=image_path)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["submit_next_stage"],
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": merged_text},
            ],
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["submit_next_stage"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["ceo_primary"])

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    with pytest.raises(ceo_runtime_ops.FrontdoorCompressionRuntimeError, match="5 MiB"):
        await runner._graph_prepare_turn(
            initial_persistent_state(
                user_input={
                    "content": merged_text,
                    "metadata": _web_ceo_upload_metadata(image_path),
                }
            ),
            runtime=runtime,
        )


def test_persist_frontdoor_actual_request_keeps_multimodal_artifact_but_strips_durable_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = ceo_runner.CeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}

    def _fake_persist(session_id: str, *, payload: dict[str, object]):
        captured["session_id"] = session_id
        captured["payload"] = payload
        return {
            "path": "D:/tmp/frontdoor-request.json",
            "actual_request_hash": "request-hash",
            "actual_request_message_count": 2,
            "actual_tool_schema_hash": "tool-hash",
        }

    monkeypatch.setattr(ceo_runtime_ops, "persist_frontdoor_actual_request", _fake_persist)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(context=SimpleNamespace(session=session, session_key="web:shared"))
    request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Please inspect this image"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ]

    result = runner._persist_frontdoor_actual_request(
        state={"session_key": "web:shared", "frontdoor_actual_request_history": []},
        runtime=runtime,
        request_messages=request_messages,
        tool_schemas=[],
        prompt_cache_key="cache-key",
        prompt_cache_diagnostics={},
        parallel_tool_calls=None,
    )

    payload = dict(captured["payload"] or {})
    assert payload["request_messages"][1]["content"][1]["type"] == "image_url"
    assert result["frontdoor_request_body_messages"][1]["content"] == "Please inspect this image"
    assert session._frontdoor_request_body_messages[1]["content"] == "Please inspect this image"


@pytest.mark.asyncio
async def test_graph_call_model_fresh_turn_reuses_previous_message_artifact_prefix_despite_trailing_whitespace_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)
    captured_model_messages: list[dict[str, object]] = []

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})

    previous_request_path = tmp_path / "frontdoor-request-previous.json"
    previous_request_messages = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "old question"},
        {
            "role": "user",
            "content": '{"message_type":"frontdoor_runtime_tool_contract","callable_tool_names":["exec","submit_next_stage"]}',
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-exec-1",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "exec",
            "tool_call_id": "call-exec-1",
            "content": "Command finished for final:ceo-demo ",
        },
    ]
    previous_request_path.write_text(
        json.dumps(
            {
                "request_messages": previous_request_messages,
                "tool_schemas": [
                    {
                        "type": "function",
                        "function": {"name": "exec", "description": "", "parameters": {"type": "object"}},
                    },
                    {
                        "type": "function",
                        "function": {"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec", "submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        stable_messages = list(kwargs.get("request_body_seed_messages") or [])
        user_content = str(kwargs.get("user_content") or "").strip()
        model_messages = [*stable_messages, {"role": "user", "content": user_content}]
        return SimpleNamespace(
            tool_names=["exec", "submit_next_stage"],
            model_messages=model_messages,
            stable_messages=list(stable_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec", "submit_next_stage"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    async def _call_model_with_tools(**kwargs):
        captured_model_messages[:] = list(kwargs.get("messages") or [])
        return SimpleNamespace()

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_model": "openai_codex:gpt-test",
            "context_window_tokens": 128000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(
        runner,
        "_model_response_view",
        lambda _message: SimpleNamespace(content="ok", tool_calls=[], provider_request_meta={}, provider_request_body={}),
    )
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    session = SimpleNamespace(
        state=SimpleNamespace(session_key=session_key),
        _memory_channel="web",
        _memory_chat_id="shared",
        _channel="web",
        _chat_id="shared",
        _active_cancel_token=None,
        inflight_turn_snapshot=lambda: None,
        _frontdoor_request_body_messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "old question"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-exec-1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "exec",
                "tool_call_id": "call-exec-1",
                "content": "Command finished for final:ceo-demo",
            },
            {"role": "assistant", "content": "final answer"},
        ],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
        _frontdoor_previous_actual_request_path=str(previous_request_path),
        _frontdoor_previous_actual_request_history=[{"path": str(previous_request_path), "turn_id": "turn-old"}],
        _frontdoor_actual_request_path="",
        _frontdoor_actual_request_history=[],
    )
    runtime = SimpleNamespace(
        context=SimpleNamespace(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "next question", "metadata": {}}),
        runtime=runtime,
    )
    await runner._graph_call_model(
        prepared,
        runtime=runtime,
    )

    assert captured_model_messages[: len(previous_request_messages)] == previous_request_messages
    assert {"role": "assistant", "content": "final answer"} in captured_model_messages
    final_answer_index = captured_model_messages.index({"role": "assistant", "content": "final answer"})
    next_user_index = captured_model_messages.index({"role": "user", "content": "next question"})
    assert final_answer_index < next_user_index


def test_create_agent_runner_sync_preserves_durable_frontdoor_canonical_context() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    session = SimpleNamespace()
    durable_canonical_context = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_goal": "Completed context",
                "representation": "raw",
                "status": "completed",
                "stage_kind": "normal",
                "tool_round_budget": 6,
                "tool_rounds_used": 1,
                "rounds": [],
            }
        ],
    }
    current_stage_state = {
        "active_stage_id": "frontdoor-stage-2",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-2",
                "stage_index": 2,
                "stage_goal": "Current active stage",
                "representation": "raw",
                "status": "active",
                "stage_kind": "normal",
                "tool_round_budget": 8,
                "tool_rounds_used": 2,
                "rounds": [],
            }
        ],
    }

    runner._sync_runtime_session_frontdoor_state(
        state={
            "frontdoor_stage_state": current_stage_state,
            "frontdoor_canonical_context": durable_canonical_context,
            "compression_state": {"status": "", "text": "", "source": "", "needs_recheck": False},
            "semantic_context_state": {},
            "hydrated_tool_names": [],
        },
        session=session,
    )

    assert session._frontdoor_stage_state["active_stage_id"] == "frontdoor-stage-2"
    assert [stage["stage_id"] for stage in session._frontdoor_stage_state["stages"]] == ["frontdoor-stage-2"]
    assert session._frontdoor_canonical_context["active_stage_id"] == ""
    assert [stage["stage_id"] for stage in session._frontdoor_canonical_context["stages"]] == ["frontdoor-stage-1"]


def test_combine_canonical_context_skips_overlapping_completed_stage_from_turn_state() -> None:
    durable_canonical_context = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_goal": "Completed context",
                "representation": "raw",
                "status": "completed",
                "stage_kind": "normal",
                "tool_round_budget": 6,
                "tool_rounds_used": 1,
                "completed_stage_summary": "verified sources collected",
                "created_at": "2026-04-17T22:49:22+08:00",
                "finished_at": "2026-04-17T22:49:57+08:00",
                "rounds": [
                    {
                        "round_id": "frontdoor-stage-1:round-1",
                        "round_index": 1,
                        "created_at": "2026-04-17T22:49:28+08:00",
                        "budget_counted": True,
                        "tool_names": ["content_search"],
                        "tool_call_ids": ["call-1"],
                        "tools": [],
                    }
                ],
            }
        ],
    }
    current_stage_state = {
        "active_stage_id": "frontdoor-stage-2",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_goal": "Completed context",
                "representation": "raw",
                "status": "completed",
                "stage_kind": "normal",
                "tool_round_budget": 6,
                "tool_rounds_used": 1,
                "completed_stage_summary": "verified sources collected",
                "created_at": "2026-04-17T22:49:22+08:00",
                "finished_at": "2026-04-17T22:49:57+08:00",
                "rounds": [
                    {
                        "round_id": "frontdoor-stage-1:round-1",
                        "round_index": 1,
                        "created_at": "2026-04-17T22:49:28+08:00",
                        "budget_counted": True,
                        "tool_names": ["content_search"],
                        "tool_call_ids": ["call-1"],
                        "tools": [],
                    }
                ],
            },
            {
                "stage_id": "frontdoor-stage-2",
                "stage_index": 2,
                "stage_goal": "Active stage",
                "representation": "raw",
                "status": "active",
                "stage_kind": "normal",
                "tool_round_budget": 8,
                "tool_rounds_used": 0,
                "completed_stage_summary": "",
                "created_at": "2026-04-17T22:50:30+08:00",
                "finished_at": "",
                "rounds": [],
            },
        ],
    }

    combined = combine_canonical_context(durable_canonical_context, current_stage_state)

    assert combined["active_stage_id"] == "frontdoor-stage-2"
    assert [stage["stage_id"] for stage in combined["stages"]] == [
        "frontdoor-stage-1",
        "frontdoor-stage-2",
    ]
    assert combined["stages"][0]["status"] == "completed"
    assert combined["stages"][1]["status"] == "active"


@pytest.mark.asyncio
async def test_prepare_turn_keeps_provider_schema_hash_stable_when_web_fetch_is_promoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:frontdoor-cache"
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    tools = {
        "exec": _NamedSchemaTool(
            name="exec",
            description="Run a command in the current workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute."},
                },
                "required": ["command"],
            },
        ),
        "submit_next_stage": _NamedSchemaTool(
            name="submit_next_stage",
            description="Start the next stage for the current node.",
            parameters={
                "type": "object",
                "properties": {
                    "stage_goal": {"type": "string", "description": "Goal for the next stage."},
                },
                "required": ["stage_goal"],
            },
        ),
        "web_fetch": _NamedSchemaTool(
            name="web_fetch",
            description="Lightweight HTTP fetch for reading public web pages.",
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http(s) URL to fetch."},
                    "timeout_ms": {
                        "type": "integer",
                        "description": "Network timeout in milliseconds.",
                    },
                },
                "required": ["url"],
            },
        ),
    }
    loop = SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools=tools,
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai:gpt-test"])

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {
            "skills": [],
            "tool_families": [],
            "tool_names": ["exec", "submit_next_stage", "web_fetch"],
        }

    captured_builds: list[dict[str, object]] = []
    assembly_tool_name_sets = [
        ["exec", "submit_next_stage"],
        ["exec", "submit_next_stage", "web_fetch"],
    ]
    assembly_candidate_sets = [
        ["web_fetch"],
        [],
    ]
    assembly_hydrated_sets = [
        [],
        ["web_fetch"],
    ]

    async def _build_for_ceo(**kwargs):
        build_index = len(captured_builds)
        captured_builds.append(dict(kwargs))
        user_content = str(kwargs.get("user_content") or "")
        return SimpleNamespace(
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": user_content},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "bootstrap"},
            ],
            dynamic_appendix_messages=[],
            tool_names=list(assembly_tool_name_sets[build_index]),
            candidate_tool_names=list(assembly_candidate_sets[build_index]),
            candidate_tool_items=[
                {"tool_id": tool_name, "description": f"{tool_name} description"}
                for tool_name in assembly_candidate_sets[build_index]
            ],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["exec", "submit_next_stage", "web_fetch"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    runner._resolver = SimpleNamespace(resolve_for_actor=_resolve_for_actor)
    runner._builder = SimpleNamespace(build_for_ceo=_build_for_ceo)

    def _session_with_hydrated_names(names: list[str]) -> SimpleNamespace:
        return SimpleNamespace(
            state=SimpleNamespace(session_key=session_key),
            _memory_channel="web",
            _memory_chat_id="shared",
            _channel="web",
            _chat_id="shared",
            _active_cancel_token=None,
            inflight_turn_snapshot=lambda: None,
            _frontdoor_request_body_messages=[],
            _frontdoor_history_shrink_reason="",
            _frontdoor_stage_state={},
            _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
            _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
            _semantic_context_state={"summary_text": "", "needs_refresh": False},
            _frontdoor_hydrated_tool_names=list(names),
            _frontdoor_selection_debug={},
        )

    first_prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "collect sources", "metadata": {}}),
        runtime=SimpleNamespace(
            context=SimpleNamespace(session=_session_with_hydrated_names(assembly_hydrated_sets[0]))
        ),
    )
    second_prepared = await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "collect sources", "metadata": {}}),
        runtime=SimpleNamespace(
            context=SimpleNamespace(session=_session_with_hydrated_names(assembly_hydrated_sets[1]))
        ),
    )

    assert first_prepared["provider_tool_names"] == ["exec", "submit_next_stage", "web_fetch"]
    assert second_prepared["provider_tool_names"] == ["exec", "submit_next_stage", "web_fetch"]
    assert (
        first_prepared["prompt_cache_diagnostics"]["actual_tool_schema_hash"]
        == second_prepared["prompt_cache_diagnostics"]["actual_tool_schema_hash"]
    )


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
                                    "tool_round_budget": 5,
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
                    "tool_round_budget": 5,
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
    assert running_after_submit["canonical_context"]["active_stage_id"] == "frontdoor-stage-1"
    assert running_after_submit["canonical_context"]["stages"][0]["stage_goal"] == "Inspect the repository structure"
    assert running_after_submit["canonical_context"]["stages"][0]["tool_round_budget"] == 5
    assert "tool_events" not in running_after_submit
    assert running_after_submit["compression"]["status"] == "running"

    normalized_calls = [
        {
            "id": "call-memory-1",
            "name": "memory_note",
            "arguments": {"ref": "note_repo"},
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
                            "name": "memory_note",
                            "args": {"ref": "note_repo"},
                        }
                    ],
                )
            ],
        },
        runtime,
    )
    await session._handle_progress(
        "memory_note started",
        event_kind="tool_start",
        event_data={"tool_name": "memory_note", "tool_call_id": "call-memory-1"},
    )

    running_tool_snapshot = session.inflight_turn_snapshot()

    assert isinstance(running_tool_snapshot, dict)
    assert "tool_events" not in running_tool_snapshot
    stage = running_tool_snapshot["canonical_context"]["stages"][0]
    assert stage["stage_goal"] == "Inspect the repository structure"
    assert stage["tool_round_budget"] == 5
    assert [round_item["tool_names"] for round_item in stage["rounds"]] == [["memory_note"]]
    assert [round_item["tool_call_ids"] for round_item in stage["rounds"]] == [["call-memory-1"]]
    assert stage["rounds"][0]["tools"] == []


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
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
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
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id)),
            tools=SimpleNamespace(get=lambda *_: None),
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
async def test_create_agent_runner_reprompts_when_active_stage_has_no_substantive_progress() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "我现在继续写文件。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
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
                        "stage_goal": "write the file",
                        "completed_stage_summary": "",
                        "tool_round_budget": 5,
                        "tool_rounds_used": 0,
                        "rounds": [],
                    }
                ],
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "call_model"
    assert result["final_output"] == ""
    assert "repair_overlay_text" in result
    assert "submit_next_stage" in result["repair_overlay_text"]
    assert "普通工具" in result["repair_overlay_text"]


@pytest.mark.asyncio
async def test_create_agent_runner_allows_finalize_after_substantive_stage_progress() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "文件已经写好并核对完成。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": ["filesystem_write"],
            "route_kind": "self_execute",
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
                        "stage_goal": "write the file",
                        "completed_stage_summary": "",
                        "tool_round_budget": 5,
                        "tool_rounds_used": 1,
                        "rounds": [
                            {
                                "round_id": "frontdoor-stage-1:round-1",
                                "round_index": 1,
                                "created_at": "2026-04-16T01:00:00+08:00",
                                "budget_counted": True,
                                "tool_names": ["filesystem_write"],
                                "tool_call_ids": ["call-1"],
                            }
                        ],
                    }
                ],
            },
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "finalize"
    assert result["final_output"] == "文件已经写好并核对完成。"
    assert result["route_kind"] == "self_execute"


@pytest.mark.asyncio
async def test_create_agent_runner_keeps_heartbeat_finalize_even_with_empty_active_stage_progress() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
    )

    result = await runner._graph_normalize_model_output(
        {
            "response_payload": {
                "content": "后台任务已完成。",
                "tool_calls": [],
                "finish_reason": "stop",
                "error_text": "",
                "reasoning_content": None,
                "thinking_blocks": None,
            },
            "used_tools": [],
            "route_kind": "direct_reply",
            "heartbeat_internal": True,
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
                        "stage_goal": "write the file",
                        "completed_stage_summary": "",
                        "tool_round_budget": 5,
                        "tool_rounds_used": 0,
                        "rounds": [],
                    }
                ],
            },
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
    assert result["final_output"] == "后台任务已完成。"


@pytest.mark.asyncio
async def test_create_agent_runner_preserves_dispatch_text_for_unverified_task_id_even_when_verified_task_exists() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
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
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id)),
            tools=SimpleNamespace(get=lambda *_: None),
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
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: None),
            tools=SimpleNamespace(get=lambda *_: None),
        )
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
    assert result["route_kind"] == "direct_reply"
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
async def test_create_agent_postprocess_duplicate_rejection_with_old_task_id_does_not_count_as_dispatch() -> None:
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
                    "content": "任务未创建：与进行中任务 task:demo-123 高度重复。原因：core_requirement exact match",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["create_async_task"],
        }
    )

    assert result["verified_task_ids"] == []
    assert result["route_kind"] == "direct_reply"
    assert "repair_overlay_text" not in result


def test_parse_create_async_task_result_recognizes_task_append_notice_guidance() -> None:
    parsed = ceo_runtime_ops.CeoFrontDoorRuntimeOps._parse_create_async_task_result(
        "任务未创建：现有任务 task:demo-123 请改用 task_append_notice 更新该任务。原因：new constraints"
    )

    assert parsed["created"] is False
    assert parsed["created_task_ids"] == []
    assert parsed["rejection_kind"] == "append_notice"


@pytest.mark.asyncio
async def test_create_agent_postprocess_accumulates_multiple_verified_task_ids() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo-1"}},
                {"id": "call-2", "name": "create_async_task", "arguments": {"task": "demo-2"}},
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
                        },
                        {
                            "id": "call-2",
                            "type": "function",
                            "function": {"name": "create_async_task", "arguments": "{}"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "create_async_task",
                    "content": "创建任务成功task:demo-123",
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-2",
                    "name": "create_async_task",
                    "content": "创建任务成功task:demo-456",
                },
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "tool_names": ["create_async_task"],
        }
    )

    assert result["verified_task_ids"] == ["task:demo-123", "task:demo-456"]
    assert result["route_kind"] == "task_dispatch"


@pytest.mark.asyncio
async def test_create_agent_graph_execute_tools_does_not_mark_duplicate_rejection_as_dispatch(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )
    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"create_async_task": _CreateAsyncTaskLikeTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, tool_name, arguments, runtime_context, on_progress, tool_call_id
        return (
            None,
            "任务未创建：与进行中任务 task:demo-123 高度重复。原因：core_requirement exact match",
            "success",
            "2026-04-18T23:00:00",
            "2026-04-18T23:00:01",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)
    monkeypatch.setattr(runner, "_task_id_exists", lambda task_id: True)

    result = await runner._graph_execute_tools(
        {
            **_canonical_frontdoor_state(
                tool_names=["create_async_task"],
                rbac_visible_tool_names=["create_async_task"],
                frontdoor_stage_state={
                    "active_stage_id": "frontdoor-stage-1",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "frontdoor-stage-1",
                            "stage_index": 1,
                            "stage_goal": "Dispatch the async task",
                            "tool_round_budget": 2,
                            "tool_rounds_used": 0,
                            "status": "active",
                            "mode": "自主执行",
                            "stage_kind": "normal",
                            "completed_stage_summary": "",
                            "key_refs": [],
                            "rounds": [],
                        }
                    ],
                },
            ),
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo"}}
            ],
            "messages": [],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": False,
            "max_parallel_tool_calls": 1,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["verified_task_ids"] == []
    assert result["route_kind"] == "direct_reply"


@pytest.mark.asyncio
async def test_create_agent_graph_execute_tools_accumulates_multiple_verified_task_ids(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )
    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"create_async_task": _CreateAsyncTaskLikeTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, tool_name, arguments, runtime_context, on_progress
        result_text = "创建任务成功task:demo-123" if tool_call_id == "call-1" else "创建任务成功task:demo-456"
        return (
            None,
            result_text,
            "success",
            "2026-04-18T23:00:00",
            "2026-04-18T23:00:01",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)
    monkeypatch.setattr(runner, "_task_id_exists", lambda task_id: True)

    result = await runner._graph_execute_tools(
        {
            **_canonical_frontdoor_state(
                tool_names=["create_async_task"],
                rbac_visible_tool_names=["create_async_task"],
                frontdoor_stage_state={
                    "active_stage_id": "frontdoor-stage-1",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "frontdoor-stage-1",
                            "stage_index": 1,
                            "stage_goal": "Dispatch the async tasks",
                            "tool_round_budget": 3,
                            "tool_rounds_used": 0,
                            "status": "active",
                            "mode": "自主执行",
                            "stage_kind": "normal",
                            "completed_stage_summary": "",
                            "key_refs": [],
                            "rounds": [],
                        }
                    ],
                },
            ),
            "tool_call_payloads": [
                {"id": "call-1", "name": "create_async_task", "arguments": {"task": "demo-1"}},
                {"id": "call-2", "name": "create_async_task", "arguments": {"task": "demo-2"}},
            ],
            "messages": [],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": False,
            "max_parallel_tool_calls": 1,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["verified_task_ids"] == ["task:demo-123", "task:demo-456"]
    assert result["route_kind"] == "task_dispatch"


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
            state=_canonical_frontdoor_state(
                messages=[{"role": "user", "content": "hello"}],
            ),
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
                state=_canonical_frontdoor_state(
                    messages=[{"role": "user", "content": "原始用户问题"}],
                    turn_overlay_text=overlay_text,
                ),
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
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "working"},
            ],
        ),
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


def test_create_agent_request_render_serializes_frontdoor_tool_contract_message() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    system_message, request_messages = runner._render_request_records(
        [
            {"role": "system", "content": "stable system"},
            {
                "role": "user",
                "content": {
                    "message_type": "frontdoor_runtime_tool_contract",
                    "callable_tool_names": ["exec"],
                    "candidate_tool_names": ["filesystem_write"],
                    "hydrated_tool_names": [],
                    "visible_skill_ids": ["memory"],
                    "stage_summary": {"active_stage_id": "stage:1", "transition_required": False},
                    "contract_revision": "frontdoor:v1",
                },
            },
        ]
    )

    assert str(getattr(system_message, "content", "") or "") == "stable system"
    assert len(request_messages) == 1
    rendered_content = str(getattr(request_messages[0], "content", "") or "")
    payload = json.loads(rendered_content)
    assert payload["message_type"] == "frontdoor_runtime_tool_contract"
    assert payload["callable_tool_names"] == ["exec"]
    assert payload["candidate_tool_names"] == ["filesystem_write"]


def test_create_agent_request_render_repairs_null_tool_call_arguments() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    _, request_messages = runner._render_request_records(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "submit_next_stage",
                            "arguments": None,
                        },
                    }
                ],
            }
        ]
    )

    assert len(request_messages) == 1
    assert getattr(request_messages[0], "tool_calls", None) == [
        {
            "name": "submit_next_stage",
            "args": {},
            "id": "call-1",
            "type": "tool_call",
        }
    ]


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
            state=_canonical_frontdoor_state(
                messages=[
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
                stable_messages=[
                    {"role": "system", "content": "stable system"},
                    {"role": "user", "content": "start"},
                ],
                dynamic_appendix_messages=[
                    {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                ],
            ),
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
        )
    )

    assert isinstance(response, ExtendedModelResponse)
    rendered_messages = list(seen_request["messages"] or [])
    contents = [
        str(getattr(message, "content", "") or "")
        for message in rendered_messages
        if not _is_frontdoor_runtime_tool_contract_record(
            {
                "role": _message_role_for_contract_filter(message),
                "content": getattr(message, "content", ""),
            }
        )
    ]

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
            state=_canonical_frontdoor_state(
                messages=[
                    {"role": "system", "content": "stable system"},
                    {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
                    {"role": "user", "content": "start"},
                    {"role": "assistant", "content": "working memory of the live turn"},
                ],
                stable_messages=[
                    {"role": "system", "content": "stable system"},
                    {"role": "user", "content": "start"},
                ],
                dynamic_appendix_messages=[
                    {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                ],
            ),
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
        )
    )

    assert isinstance(response, ExtendedModelResponse)
    contents = [
        str(getattr(message, "content", "") or "")
        for message in list(seen_request["messages"] or [])
        if not _is_frontdoor_runtime_tool_contract_record(
            {
                "role": _message_role_for_contract_filter(message),
                "content": getattr(message, "content", ""),
            }
        )
    ]
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
                state=_canonical_frontdoor_state(
                    messages=messages,
                    stable_messages=[
                        {"role": "system", "content": "stable system"},
                        {"role": "user", "content": "start"},
                    ],
                    dynamic_appendix_messages=[
                        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                    ],
                ),
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
        if not _is_frontdoor_runtime_tool_contract_record(
            {
                "role": _message_role_for_contract_filter(message),
                "content": getattr(message, "content", ""),
            }
        )
    ] == [
        "start",
        "assistant drift A",
        "## Retrieved Context\n- authoritative memory",
    ]
    assert [
        str(getattr(message, "content", "") or "")
        for message in list(seen_requests[1]["messages"] or [])
        if not _is_frontdoor_runtime_tool_contract_record(
            {
                "role": _message_role_for_contract_filter(message),
                "content": getattr(message, "content", ""),
            }
        )
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
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "system", "content": "stable system"},
                {
                    "role": "assistant",
                    "content": "[G3KU_LONG_CONTEXT_SUMMARY_V1]\nsummary body",
                },
                {"role": "assistant", "content": "latest assistant"},
                {"role": "user", "content": "latest user"},
            ],
            stable_messages=[
                {"role": "system", "content": "stable system"},
                {"role": "assistant", "content": "[G3KU_LONG_CONTEXT_SUMMARY_V1]\nsummary body"},
                {"role": "assistant", "content": "latest assistant"},
                {"role": "user", "content": "latest user"},
            ],
            dynamic_appendix_messages=[
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
            ],
        ),
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )

    non_contract_request_messages = [
        item
        for item in list(contract.request_messages or [])
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    assert non_contract_request_messages == [
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
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "system", "content": "stable system"},
                {"role": "assistant", "content": "Short recap: here's the answer you asked for."},
                {"role": "user", "content": "latest user"},
            ],
            stable_messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "latest user"},
            ],
            dynamic_appendix_messages=[
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
            ],
        ),
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
    non_contract_request_messages = [
        item
        for item in list(contract.request_messages or [])
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    assert non_contract_request_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "assistant", "content": "Short recap: here's the answer you asked for."},
        {"role": "user", "content": "latest user"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]


def test_create_agent_prompt_contract_keeps_request_body_prefix_before_same_turn_tool_growth() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    first = runner._frontdoor_prompt_contract(
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
            ],
            stable_messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
            ],
            dynamic_appendix_messages=[
                {"role": "user", "content": '{"message_type":"frontdoor_runtime_tool_contract"}'},
            ],
        ),
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )
    second = runner._frontdoor_prompt_contract(
        state=_canonical_frontdoor_state(
            messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "submit_next_stage", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "name": "submit_next_stage",
                    "tool_call_id": "call-1",
                    "content": '{"status":"success"}',
                },
            ],
            stable_messages=[
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
            ],
            dynamic_appendix_messages=[
                {"role": "user", "content": '{"message_type":"frontdoor_runtime_tool_contract"}'},
            ],
        ),
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:shared",
    )

    first_non_contract_messages = [
        dict(item)
        for item in list(first.request_messages or [])
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]
    second_non_contract_messages = [
        dict(item)
        for item in list(second.request_messages or [])
        if not _is_frontdoor_runtime_tool_contract_record(dict(item))
    ]

    assert first_non_contract_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
    ]
    assert second_non_contract_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "submit_next_stage", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "name": "submit_next_stage",
            "tool_call_id": "call-1",
            "content": '{"status":"success"}',
        },
    ]
    assert second_non_contract_messages[: len(first_non_contract_messages)] == first_non_contract_messages


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
                state=_canonical_frontdoor_state(
                    messages=[
                        {"role": "system", "content": "stable system"},
                        {"role": "user", "content": "start"},
                    ],
                    stable_messages=[
                        {"role": "system", "content": "stable system"},
                        {"role": "user", "content": "start"},
                    ],
                    dynamic_appendix_messages=[
                        {"role": "assistant", "content": "## Retrieved Context\n- authoritative memory"}
                    ],
                    repair_overlay_text=repair_overlay_text,
                    cache_family_revision=DEFAULT_CACHE_FAMILY_REVISION,
                ),
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
    assert "description" not in fact_properties["value"]

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

    assert langchain_tool.description == ""

    raw_schema = get_attached_raw_parameters_schema(langchain_tool)
    assert raw_schema == {
        "type": "object",
        "properties": {
            "model_only": {"type": "integer"},
        },
        "required": ["model_only"],
    }

    prompt_schema = ceo_agent_middleware._tool_schema(langchain_tool)
    assert prompt_schema == {
        "name": tool.name,
        "description": "",
        "parameters": raw_schema,
    }

    assert "runtime_only" not in raw_schema["properties"]
    assert "model_only" in raw_schema["properties"]
    assert "description" not in raw_schema["properties"]["model_only"]


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
        async def enqueue_write_request(self, **kwargs):
            return {
                "ok": True,
                "session_key": kwargs.get("session_key"),
                "decision_source": kwargs.get("decision_source"),
                "payload_text": kwargs.get("payload_text"),
                "trigger_source": kwargs.get("trigger_source"),
            }

    captured: dict[str, object] = {}

    async def _executor(_tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        captured["arguments"] = arguments
        return {"result_text": "ok", "status": "success"}

    tool = MemoryWriteTool(manager=_FakeMemoryManager())
    langchain_tool = ceo_runtime_ops._build_langchain_tool(tool, _executor)
    await langchain_tool.ainvoke(
        {
            "content": "remember this preference",
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
        runtime_context={"session_key": "web:shared"},
        on_progress=None,
    )

    payload = json.loads(result_text)
    assert status == "success"
    assert payload["ok"] is True
    assert payload["session_key"] == "web:shared"
    assert payload["decision_source"] == "user"
    assert payload["payload_text"] == "remember this preference"
    assert payload["trigger_source"] == "memory_write_tool"


@pytest.mark.asyncio
async def test_create_agent_frontdoor_execute_tool_call_appends_loader_guidance_for_parameter_like_execute_errors() -> None:
    class _RuntimeToolStack:
        def push_runtime_context(self, _context):
            return "token"

        def pop_runtime_context(self, _token):
            return None

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=_RuntimeToolStack(),
            resource_manager=None,
            inline_tool_execution_registry=None,
        )
    )

    value_error_text, value_error_status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=_ExecuteErrorTool(name="value_error_tool", exc=ValueError("value must be an absolute path")),
        tool_name="value_error_tool",
        arguments={"value": "demo"},
        runtime_context={},
        on_progress=None,
    )
    type_error_text, type_error_status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=_ExecuteErrorTool(name="type_error_tool", exc=TypeError("value must be a string scalar")),
        tool_name="type_error_tool",
        arguments={"value": "demo"},
        runtime_context={},
        on_progress=None,
    )
    runtime_error_text, runtime_error_status, _started_at, _finished_at, _elapsed_seconds = await runner._execute_tool_call(
        tool=_ExecuteErrorTool(name="runtime_error_tool", exc=RuntimeError("runtime execution failed")),
        tool_name="runtime_error_tool",
        arguments={"value": "demo"},
        runtime_context={},
        on_progress=None,
    )

    assert value_error_status == "error"
    assert "Error executing value_error_tool: value must be an absolute path" in value_error_text
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="value_error_tool") in value_error_text
    assert type_error_status == "error"
    assert "Error executing type_error_tool: value must be a string scalar" in type_error_text
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="type_error_tool") in type_error_text
    assert runtime_error_status == "error"
    assert "Error executing runtime_error_tool: runtime execution failed" in runtime_error_text
    assert _PARAMETER_GUIDANCE_TEMPLATE.format(tool_name="runtime_error_tool") not in runtime_error_text


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
            state=_canonical_frontdoor_state(messages=[{"role": "user", "content": "hello"}]),
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
async def test_graph_call_model_restarts_with_refreshed_model_refs_after_runtime_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = SimpleNamespace(main_task_service=None, _runtime_model_revision=1)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=loop)
    seen_model_refs: list[list[str]] = []

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **_: [])
    monkeypatch.setattr(
        runner,
        "_frontdoor_prompt_contract",
        lambda **kwargs: SimpleNamespace(
            request_messages=list(kwargs["state"].get("messages") or []),
            prompt_cache_key=f"pk:{kwargs['provider_model']}",
            diagnostics={},
        ),
    )
    monkeypatch.setattr(
        runner,
        "_resolve_ceo_model_refs",
        lambda: ["new-model"] if int(getattr(loop, "_runtime_model_revision", 0) or 0) >= 2 else ["old-model"],
    )
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **kwargs: {
            "model_key": str((kwargs.get("model_refs") or [""])[0] or ""),
            "provider_model": str((kwargs.get("model_refs") or [""])[0] or ""),
            "context_window_tokens": 128000,
        },
    )

    def _refresh_runtime_config(_loop, *, force: bool = False, reason: str = "runtime") -> bool:
        _ = force, reason
        if int(getattr(_loop, "_runtime_model_revision", 0) or 0) < 2:
            _loop._runtime_model_revision = 2
            return True
        return False

    monkeypatch.setattr(ceo_runtime_ops, "refresh_loop_runtime_config", _refresh_runtime_config)

    async def _call_model_with_tools(**kwargs):
        model_refs = list(kwargs.get("model_refs") or [])
        seen_model_refs.append(model_refs)
        if model_refs == ["old-model"]:
            raise RuntimeError(ceo_runtime_ops.PUBLIC_PROVIDER_FAILURE_MESSAGE)
        return SimpleNamespace()

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    monkeypatch.setattr(
        runner,
        "_model_response_view",
        lambda _message: SimpleNamespace(
            content="ok",
            tool_calls=[],
            provider_request_meta={},
            provider_request_body={},
        ),
    )
    monkeypatch.setattr(runner, "_checkpoint_safe_model_response_payload", lambda _message: {"ok": True})
    monkeypatch.setattr(runner, "_persist_frontdoor_actual_request", lambda **_: {})

    state = _canonical_frontdoor_state(
        messages=[
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "hello"},
        ],
        model_refs=["old-model"],
        prompt_cache_key="",
        prompt_cache_diagnostics={},
        session_key="web:shared",
        parallel_enabled=False,
    )

    result = await runner._graph_call_model(
        state,
        runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared", session=SimpleNamespace())),
    )

    assert seen_model_refs == [["old-model"], ["new-model"]]
    assert result["model_refs"] == ["new-model"]
    assert result["prompt_cache_key"] == "pk:new-model"


@pytest.mark.asyncio
async def test_create_agent_prompt_middleware_accepts_legacy_dict_tool_contract_message() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    runner._resolve_ceo_model_refs = lambda: ["openai:gpt-4.1"]
    middleware = ceo_agent_middleware.CeoPromptAssemblyMiddleware(runner=runner)

    async def _handler(request):
        _ = request
        return ModelResponse(result=[AIMessage(content="ok")])

    response = await middleware.awrap_model_call(
        ModelRequest(
            model=SimpleNamespace(),
            system_message=SystemMessage(content="You are the CEO frontdoor agent."),
            messages=[HumanMessage(content="hello")],
            tools=[],
            state=_canonical_frontdoor_state(
                messages=[{"role": "user", "content": "hello"}],
                tool_names=["submit_next_stage"],
                candidate_tool_names=["filesystem_write"],
                hydrated_tool_names=[],
                visible_skill_ids=["memory"],
                frontdoor_stage_state={
                    "active_stage_id": "",
                    "transition_required": False,
                    "stages": [],
                },
                dynamic_appendix_messages=[
                    {
                        "role": "user",
                        "content": {
                            "message_type": "frontdoor_runtime_tool_contract",
                            "callable_tool_names": ["exec"],
                            "candidate_tool_names": ["filesystem_write"],
                            "hydrated_tool_names": [],
                            "visible_skill_ids": ["memory"],
                            "stage_summary": {"active_stage_id": "", "transition_required": False},
                            "contract_revision": "frontdoor:v1",
                        },
                    }
                ],
            ),
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
        ),
        _handler,
    )

    assert isinstance(response, ExtendedModelResponse)
    assert response.model_response.result == [AIMessage(content="ok")]


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
    subset_payloads = [{"id": "call-1", "name": "exec", "arguments": {"command": "safe subset"}}]
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
