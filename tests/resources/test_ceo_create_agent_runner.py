from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command

from g3ku.agent.tools.base import Tool
from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import ceo_agent_middleware, ceo_runner
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted, initial_persistent_state


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


def test_memory_assembly_config_uses_stage_budget_defaults() -> None:
    cfg = MemoryAssemblyConfig()

    assert cfg.frontdoor_recent_message_count == 20
    assert cfg.frontdoor_summary_trigger_message_count == 10
    assert cfg.frontdoor_interrupt_approval_enabled is False
    assert cfg.frontdoor_interrupt_tool_names == ["message", "create_async_task"]

    for removed_flag in (
        "frontdoor_create_agent_enabled",
        "frontdoor_create_agent_shadow_mode",
        "frontdoor_summarizer_enabled",
        "frontdoor_summarizer_model_key",
    ):
        with pytest.raises(AttributeError):
            getattr(cfg, removed_flag)


def test_initial_persistent_state_contains_summary_payload_and_runtime_marker() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["summary_text"] == ""
    assert state["summary_payload"] == {}
    assert state["summary_version"] == 0
    assert state["summary_model_key"] == ""
    assert state["agent_runtime"] == "create_agent"


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


def test_ceo_runner_always_selects_create_agent_impl(monkeypatch) -> None:
    class _New:
        def __init__(self, *, loop) -> None:
            self.loop = loop

    monkeypatch.setattr(ceo_runner, "CreateAgentCeoFrontDoorRunner", _New)

    loop = SimpleNamespace(
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(frontdoor_create_agent_enabled=False)
        )
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    assert isinstance(runner._impl, _New)


def test_ceo_runner_invalidate_runtime_bindings_clears_cached_agent_and_graph() -> None:
    runner = ceo_runner.CeoFrontDoorRunner(loop=SimpleNamespace())
    runner._impl._agent = object()
    runner._impl._compiled_graph = object()

    runner.invalidate_runtime_bindings()

    assert runner._impl._agent is None
    assert runner._impl._compiled_graph is None


def test_create_agent_runner_build_prompt_context_uses_effective_turn_overlay(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_effective_turn_overlay_text", lambda state: "overlay-text")

    result = runner.build_prompt_context(
        state={"turn_overlay_text": "## Retrieved Context\n- memory", "repair_overlay_text": "repair"},
        runtime=SimpleNamespace(),
        tools=[],
    )

    assert result == {"system_overlay": "overlay-text"}


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

    async def _fake_execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress):
        _ = tool, arguments, runtime_context
        await on_progress(
            f"{tool_name} started",
            event_kind="tool_start",
            event_data={"tool_name": tool_name},
        )
        return "done", "success", "2026-04-05T11:00:00", "2026-04-05T11:00:01", 1.0

    monkeypatch.setattr(runner, "_execute_tool_call", _fake_execute_tool_call)

    tools = runner._build_langchain_tools_for_state(
        state={"tool_names": ["demo_tool"]},
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    result = await tools[0].ainvoke({"value": "alpha"})

    assert result["status"] == "success"
    assert [item[1] for item in progress_calls] == ["tool_start", "tool_result"]
    assert progress_calls[-1][0] == "done"
    assert progress_calls[-1][2] == {"tool_name": "demo_tool"}


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

    async def _fake_execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress):
        _ = tool, tool_name, runtime_context, on_progress
        captured["arguments"] = dict(arguments or {})
        return "created", "success", "2026-04-05T12:00:00", "2026-04-05T12:00:01", 1.0

    monkeypatch.setattr(runner, "_execute_tool_call", _fake_execute_tool_call)

    tools = runner._build_langchain_tools_for_state(
        state={"tool_names": ["create_async_task"]},
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
        state={"tool_names": ["broken_validation_tool"]},
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
async def test_create_agent_runner_rejects_unverified_dispatch_text_from_model() -> None:
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
    assert "未确认成功创建后台任务" in result["final_output"]
    assert "task:fake-123" in result["final_output"]


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
async def test_create_agent_runner_rejects_dispatch_text_for_unverified_task_id_even_when_verified_task_exists() -> None:
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
    assert "未确认成功创建后台任务" in result["final_output"]
    assert "task:fake-123" in result["final_output"]


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
async def test_create_agent_runner_rejects_heartbeat_dispatch_claim_for_unknown_task_id() -> None:
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
    assert "未确认成功创建后台任务" in result["final_output"]
    assert "task:new-123" in result["final_output"]


@pytest.mark.asyncio
async def test_create_agent_postprocess_requires_persisted_task_for_dispatch_reply(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(main_task_service=SimpleNamespace(get_task=lambda task_id: None))
    )

    async def _fake_summarize_messages(*, messages, state):
        _ = state
        return {
            "messages": list(messages),
            "summary_text": "",
            "summary_payload": {},
            "summary_version": 0,
            "summary_model_key": "",
        }

    monkeypatch.setattr(runner, "_summarize_messages", _fake_summarize_messages)

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
    assert result["jump_to"] == "end"
    assert "未确认成功创建后台任务" in result["final_output"]
    assert "task:fake-123" in result["final_output"]


@pytest.mark.asyncio
async def test_create_agent_postprocess_continues_after_verified_async_dispatch(monkeypatch) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            main_task_service=SimpleNamespace(get_task=lambda task_id: SimpleNamespace(task_id=task_id))
        )
    )

    async def _fake_summarize_messages(*, messages, state):
        _ = state
        return {
            "messages": list(messages),
            "summary_text": "",
            "summary_payload": {},
            "summary_version": 0,
            "summary_model_key": "",
        }

    monkeypatch.setattr(runner, "_summarize_messages", _fake_summarize_messages)

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
    assert result["verified_task_ids"] == ["task:demo-123"]
    assert result["route_kind"] == "task_dispatch"
    assert result["tool_call_payloads"] == []


@pytest.mark.asyncio
async def test_create_agent_prompt_middleware_records_prompt_cache_diagnostics_from_real_request_shape(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_build_session_prompt_cache_key(**kwargs):
        captured["cache_key_kwargs"] = dict(kwargs)
        return "cache-key"

    def _fake_build_prompt_cache_diagnostics(**kwargs):
        captured["diagnostics_kwargs"] = dict(kwargs)
        return {"stable_prompt_signature": "sig-1"}

    monkeypatch.setattr(ceo_agent_middleware, "build_session_prompt_cache_key", _fake_build_session_prompt_cache_key)
    monkeypatch.setattr(ceo_agent_middleware, "build_prompt_cache_diagnostics", _fake_build_prompt_cache_diagnostics)

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
                "summary_text": "## Retrieved Context\n- memory",
            },
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
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
    assert content_blocks == [
        {"type": "text", "text": "You are the CEO frontdoor agent."},
        {"type": "text", "text": "Use the existing CEO layered context rules.\n\n## Retrieved Context\n- memory"},
    ]
    assert seen_request["tools"] == [exposed_tool_schema]
    assert captured["cache_key_kwargs"] == {
        "session_key": "web:shared",
        "provider_model": "openai:gpt-4.1",
        "scope": "ceo_frontdoor",
        "stable_messages": [
            {
                "role": "system",
                "content": (
                    "You are the CEO frontdoor agent.\n\n"
                    "Use the existing CEO layered context rules.\n\n"
                    "## Retrieved Context\n- memory"
                ),
            },
            {"role": "user", "content": "hello"},
        ],
        "tool_schemas": [exposed_tool_schema],
    }
    assert captured["diagnostics_kwargs"] == {
        "stable_messages": [
            {
                "role": "system",
                "content": (
                    "You are the CEO frontdoor agent.\n\n"
                    "Use the existing CEO layered context rules.\n\n"
                    "## Retrieved Context\n- memory"
                ),
            },
            {"role": "user", "content": "hello"},
        ],
        "tool_schemas": [exposed_tool_schema],
        "provider_model": "openai:gpt-4.1",
        "scope": "ceo_frontdoor",
        "prompt_cache_key": "cache-key",
        "overlay_text": "Use the existing CEO layered context rules.\n\n## Retrieved Context\n- memory",
        "overlay_section_count": 2,
    }


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
