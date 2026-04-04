from types import SimpleNamespace

import pytest
from langgraph.types import Command

from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor.state_models import CeoFrontdoorInterrupted, initial_persistent_state


class _FakeGraphOutput:
    def __init__(self, *, value, interrupts=()):
        self.value = dict(value or {})
        self.interrupts = tuple(interrupts)


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


@pytest.mark.asyncio
async def test_create_agent_runner_passes_thread_id_and_context() -> None:
    captured: dict[str, object] = {}

    class _FakeAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v1"):
            captured["payload"] = payload
            captured["config"] = config
            captured["context"] = context
            captured["version"] = version
            return {"messages": [], "route_kind": "direct_reply", "final_output": "ok"}

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _FakeAgent()

    output = await runner.run_turn(
        user_input=SimpleNamespace(content="hello", metadata={}),
        session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared")),
        on_progress=None,
    )

    assert output == "ok"
    assert captured["config"] == {"configurable": {"thread_id": "web:shared"}}
    assert getattr(captured["context"], "session_key") == "web:shared"
    assert captured["payload"]["agent_runtime"] == "create_agent"


@pytest.mark.asyncio
async def test_create_agent_runner_raises_structured_interrupt() -> None:
    class _InterruptingAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = payload, config, context, version
            return _FakeGraphOutput(
                value={"approval_request": {"kind": "frontdoor_tool_approval"}},
                interrupts=(SimpleNamespace(id="interrupt-1", value={"kind": "frontdoor_tool_approval"}),),
            )

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _InterruptingAgent()

    with pytest.raises(CeoFrontdoorInterrupted):
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared")),
            on_progress=None,
        )


@pytest.mark.asyncio
async def test_create_agent_runner_resume_uses_command_resume() -> None:
    captured: dict[str, object] = {}

    class _ResumeAgent:
        async def ainvoke(self, payload, config=None, *, context=None, version="v2"):
            _ = context, version
            captured["payload"] = payload
            captured["config"] = config
            return {"messages": [], "route_kind": "direct_reply", "final_output": "approved"}

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(_ensure_checkpointer_ready=lambda: None)
    )
    runner._agent = _ResumeAgent()

    result = await runner.resume_turn(
        session=SimpleNamespace(state=SimpleNamespace(session_key="web:shared")),
        resume_value={"decisions": [{"type": "approve"}]},
        on_progress=None,
    )

    assert result == "approved"
    assert isinstance(captured["payload"], Command)
    assert captured["config"] == {"configurable": {"thread_id": "web:shared"}}
