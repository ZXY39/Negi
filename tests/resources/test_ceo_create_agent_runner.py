from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest, ModelResponse
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command

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


def test_memory_assembly_config_exposes_create_agent_and_summarizer_defaults() -> None:
    cfg = MemoryAssemblyConfig()

    assert cfg.frontdoor_create_agent_enabled is False
    assert cfg.frontdoor_create_agent_shadow_mode is False
    assert cfg.frontdoor_summarizer_enabled is True
    assert cfg.frontdoor_summarizer_model_key is None
    assert cfg.frontdoor_summarizer_trigger_message_count == 24
    assert cfg.frontdoor_summarizer_keep_message_count == 8


def test_initial_persistent_state_contains_summary_payload_and_runtime_marker() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["summary_text"] == ""
    assert state["summary_payload"] == {}
    assert state["summary_version"] == 0
    assert state["summary_model_key"] == ""
    assert state["agent_runtime"] == "langgraph"


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


def test_ceo_runner_selects_create_agent_impl_when_flag_enabled(monkeypatch) -> None:
    class _Legacy:
        def __init__(self, *, loop) -> None:
            self.loop = loop

    class _New:
        def __init__(self, *, loop) -> None:
            self.loop = loop

    monkeypatch.setattr(ceo_runner, "LegacyCeoFrontDoorRunner", _Legacy)
    monkeypatch.setattr(ceo_runner, "CreateAgentCeoFrontDoorRunner", _New)

    loop = SimpleNamespace(
        _memory_runtime_settings=SimpleNamespace(
            assembly=SimpleNamespace(frontdoor_create_agent_enabled=True)
        )
    )
    runner = ceo_runner.CeoFrontDoorRunner(loop=loop)

    assert isinstance(runner._impl, _New)


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


def test_create_agent_prompt_middleware_records_prompt_cache_diagnostics_from_real_request_shape(
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

    runner = SimpleNamespace(
        _resolve_ceo_model_refs=lambda: ["openai:gpt-4.1"],
        build_prompt_context=lambda **kwargs: {
            "system_overlay": "Use the existing CEO layered context rules.\n\n## Retrieved Context\n- memory"
        },
        visible_langchain_tools=lambda **kwargs: [],
    )
    middleware = ceo_agent_middleware.CeoPromptAssemblyMiddleware(runner=runner)

    tool_schema = {
        "name": "create_async_task",
        "description": "dispatch async task",
        "parameters": {"type": "object", "properties": {"task": {"type": "string"}}},
    }
    seen_request: dict[str, object] = {}

    def _handler(request):
        seen_request["system_message"] = request.system_message
        seen_request["tools"] = list(request.tools or [])
        return ModelResponse(result=[AIMessage(content="ok")])

    response = middleware.wrap_model_call(
        ModelRequest(
            model=SimpleNamespace(),
            system_message=SystemMessage(content="You are the CEO frontdoor agent."),
            messages=[HumanMessage(content="hello")],
            tools=[tool_schema],
            state={"messages": [{"role": "user", "content": "hello"}]},
            runtime=SimpleNamespace(context=SimpleNamespace(session_key="web:shared")),
        ),
        _handler,
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
    assert seen_request["tools"] == [tool_schema]
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
        "tool_schemas": [tool_schema],
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
        "tool_schemas": [tool_schema],
        "provider_model": "openai:gpt-4.1",
        "scope": "ceo_frontdoor",
        "prompt_cache_key": "cache-key",
        "overlay_text": "Use the existing CEO layered context rules.\n\n## Retrieved Context\n- memory",
        "overlay_section_count": 2,
    }


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
