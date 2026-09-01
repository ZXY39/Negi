from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from g3ku.core.messages import UserInputMessage
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.bridge import SessionRuntimeBridge
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext


@pytest.mark.asyncio
async def test_first_frontdoor_request_records_inbound_to_send_latency(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_selected_tool_schemas",
        lambda tool_names: [{"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}}],
    )
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:test",
            "context_window_tokens": 32000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1200, raising=False)

    async def _call_model_with_tools(**kwargs):
        return AIMessage(content="reply", response_metadata={"finish_reason": "stop"})

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="china:qqbot:default:dm"),
        _active_turn_id="turn-1",
        _frontdoor_actual_request_history=[],
        _current_turn_id=lambda prompt=None: "turn-1",
    )
    inbound_at = datetime.now().astimezone() - timedelta(seconds=5)

    update = await runner._graph_call_model(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "stable_messages": [{"role": "user", "content": "hello"}],
            "dynamic_appendix_messages": [],
            "user_input": {
                "content": "hello",
                "metadata": {"turn_inbound_received_at": inbound_at.isoformat()},
            },
            "tool_names": ["submit_next_stage"],
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "model_refs": ["openai:test"],
            "parallel_enabled": False,
            "iteration": 0,
            "max_iterations": 4,
            "session_key": "china:qqbot:default:dm",
        },
        runtime=SimpleNamespace(
            context=CeoRuntimeContext(
                loop=None,
                session=session,
                session_key="china:qqbot:default:dm",
                on_progress=None,
            )
        ),
    )

    with open(str(update["frontdoor_actual_request_path"]), encoding="utf-8") as handle:
        artifact = json.load(handle)
    assert artifact["turn_inbound_received_at"] == inbound_at.isoformat()
    assert datetime.fromisoformat(artifact["provider_request_started_at"]) > inbound_at
    assert 4 <= artifact["inbound_to_request_start_seconds"] < 10


@pytest.mark.asyncio
async def test_request_timing_is_null_without_inbound_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_selected_tool_schemas",
        lambda tool_names: [{"name": "submit_next_stage", "description": "", "parameters": {"type": "object"}}],
    )
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {"model_key": "ceo", "provider_model": "openai:test", "context_window_tokens": 32000},
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1200, raising=False)

    async def _call_model_with_tools(**kwargs):
        return AIMessage(content="reply", response_metadata={"finish_reason": "stop"})

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:test"),
        _active_turn_id="turn-2",
        _frontdoor_actual_request_history=[],
        _current_turn_id=lambda prompt=None: "turn-2",
    )

    update = await runner._graph_call_model(
        {
            "messages": [{"role": "user", "content": "hello"}],
            "stable_messages": [{"role": "user", "content": "hello"}],
            "dynamic_appendix_messages": [],
            "tool_names": ["submit_next_stage"],
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "model_refs": ["openai:test"],
            "parallel_enabled": False,
            "iteration": 0,
            "max_iterations": 4,
            "session_key": "web:test",
        },
        runtime=SimpleNamespace(
            context=CeoRuntimeContext(loop=None, session=session, session_key="web:test", on_progress=None)
        ),
    )

    with open(str(update["frontdoor_actual_request_path"]), encoding="utf-8") as handle:
        artifact = json.load(handle)
    assert artifact["turn_inbound_received_at"] == ""
    assert artifact["inbound_to_request_start_seconds"] is None
    assert artifact["provider_request_started_at"]


@pytest.mark.asyncio
async def test_bridge_stamps_inbound_time_once_for_user_input_messages() -> None:
    captured: list[object] = []

    class _Session:
        state = SimpleNamespace(session_key="china:qqbot:default:dm")

        async def prompt(self, message, **kwargs):
            captured.append(message)
            return SimpleNamespace(output="ok")

    manager = SimpleNamespace(
        get_or_create=lambda **kwargs: _Session(),
        bind_live_context=lambda session, **kwargs: {},
    )
    bridge = SessionRuntimeBridge(manager)
    message = UserInputMessage(content="hello", metadata={"existing": True})

    await bridge.prompt(
        message,
        session_key="china:qqbot:default:dm",
        channel="qqbot",
        chat_id="dm",
    )

    stamped = captured[0]
    assert isinstance(stamped, UserInputMessage)
    first_value = stamped.metadata["turn_inbound_received_at"]
    assert first_value
    assert stamped.metadata["existing"] is True

    await bridge.prompt(
        stamped,
        session_key="china:qqbot:default:dm",
        channel="qqbot",
        chat_id="dm",
    )
    assert captured[1].metadata["turn_inbound_received_at"] == first_value


def test_bridge_keeps_plain_string_inputs_unwrapped() -> None:
    stamped = SessionRuntimeBridge._stamp_inbound_received_at("plain text")
    assert stamped == "plain text"
