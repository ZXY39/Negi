from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from main.runtime.stage_budget import STAGE_TOOL_NAME


def test_initial_persistent_state_tracks_frontdoor_stage_state() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["route_kind"] == "direct_reply"
    assert state["frontdoor_stage_state"] == {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [],
    }


def test_initial_persistent_state_tracks_compression_state() -> None:
    state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})

    assert state["compression_state"] == {
        "status": "",
        "text": "",
        "source": "",
        "needs_recheck": False,
    }


class _RecordingTool(Tool):
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    @property
    def name(self) -> str:
        return "record_tool"

    @property
    def description(self) -> str:
        return "record a value"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
            },
            "required": ["value"],
        }

    async def execute(self, value: str, **kwargs) -> str:
        _ = kwargs
        self._sink.append(str(value))
        return json.dumps({"ok": True, "value": str(value)}, ensure_ascii=False)


def _active_frontdoor_stage_state(*, budget: int, used: int = 0, transition_required: bool = False) -> dict[str, object]:
    return {
        "active_stage_id": "stage-1",
        "transition_required": bool(transition_required),
        "stages": [
            {
                "stage_id": "stage-1",
                "stage_index": 1,
                "stage_goal": "Inspect the current request",
                "tool_round_budget": int(budget),
                "tool_rounds_used": int(used),
                "status": "active",
                "mode": "自主执行",
                "completed_stage_summary": "",
                "key_refs": [],
                "rounds": [],
            }
        ],
    }


def _tool_call_payload(*, call_id: str, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": call_id,
        "name": tool_name,
        "arguments": dict(arguments),
    }


def _assistant_tool_call_record(*, call_id: str, tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        ],
    }


def _tool_message(*, call_id: str, tool_name: str, result_text: str, status: str = "success") -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": result_text,
        "status": status,
    }


@pytest.mark.asyncio
async def test_frontdoor_stage_tool_is_visible_and_stage_creation_persists_in_state(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[str] = []

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    async def _execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress):
        _ = tool_name, runtime_context, on_progress
        return await tool.execute(**arguments), "success", "2026-04-08T10:00:00", "2026-04-08T10:00:01", 1.0

    async def _fake_summarize_messages(*, messages, state):
        _ = state
        return {
            "messages": list(messages),
            "summary_text": "",
            "summary_payload": {},
            "summary_version": 0,
            "summary_model_key": "",
        }

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"record_tool": _RecordingTool(executed)})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call", _execute_tool_call)
    monkeypatch.setattr(runner, "_summarize_messages", _fake_summarize_messages)

    base_state = initial_persistent_state(user_input={"content": "hello", "metadata": {}})
    tools = runner._build_langchain_tools_for_state(
        state={**base_state, "tool_names": ["record_tool"]},
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    tools_by_name = {str(getattr(tool, "name", "") or ""): tool for tool in tools}

    assert set(tools_by_name) == {STAGE_TOOL_NAME, "record_tool"}

    stage_result = await tools_by_name[STAGE_TOOL_NAME].ainvoke(
        {
            "stage_goal": "Inspect the current request",
            "tool_round_budget": 1,
        }
    )
    stage_payload = json.loads(str(stage_result["result_text"]))

    result = await runner._postprocess_completed_tool_cycle(
        state={
            **base_state,
            "tool_names": ["record_tool"],
            "tool_call_payloads": [
                _tool_call_payload(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    arguments={
                        "stage_goal": "Inspect the current request",
                        "tool_round_budget": 1,
                    },
                )
            ],
            "messages": [
                {"role": "user", "content": "hello"},
                _assistant_tool_call_record(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    arguments={
                        "stage_goal": "Inspect the current request",
                        "tool_round_budget": 1,
                    },
                ),
                _tool_message(
                    call_id="call-stage-1",
                    tool_name=STAGE_TOOL_NAME,
                    result_text=str(stage_result["result_text"]),
                ),
            ],
        }
    )

    assert stage_payload["stage_goal"] == "Inspect the current request"
    assert stage_payload["tool_round_budget"] == 1
    assert result is not None
    assert result["frontdoor_stage_state"] == {
        "active_stage_id": stage_payload["stage_id"],
        "transition_required": False,
        "stages": [stage_payload],
    }
    assert executed == []


@pytest.mark.asyncio
async def test_frontdoor_stage_gate_blocks_ordinary_tools_before_first_stage(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[str] = []

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    async def _execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress):
        _ = tool_name, runtime_context, on_progress
        return await tool.execute(**arguments), "success", "2026-04-08T10:00:00", "2026-04-08T10:00:01", 1.0

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"record_tool": _RecordingTool(executed)})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call", _execute_tool_call)

    state = {
        **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        "tool_names": ["record_tool"],
    }
    tools = runner._build_langchain_tools_for_state(
        state=state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    tools_by_name = {str(getattr(tool, "name", "") or ""): tool for tool in tools}

    result = await tools_by_name["record_tool"].ainvoke({"value": "alpha"})

    assert result["status"] == "error"
    assert result["result_text"].startswith(
        "Error: no active stage; call submit_next_stage before using other tools"
    )
    assert executed == []


@pytest.mark.asyncio
async def test_frontdoor_stage_budget_exhaustion_updates_gate_and_blocks_next_ordinary_tool(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[str] = []

    async def _noop_progress(*args, **kwargs) -> None:
        _ = args, kwargs

    async def _execute_tool_call(*, tool, tool_name, arguments, runtime_context, on_progress):
        _ = tool_name, runtime_context, on_progress
        return await tool.execute(**arguments), "success", "2026-04-08T10:00:00", "2026-04-08T10:00:01", 1.0

    async def _fake_summarize_messages(*, messages, state):
        _ = state
        return {
            "messages": list(messages),
            "summary_text": "",
            "summary_payload": {},
            "summary_version": 0,
            "summary_model_key": "",
        }

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"record_tool": _RecordingTool(executed)})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": _noop_progress})
    monkeypatch.setattr(runner, "_execute_tool_call", _execute_tool_call)
    monkeypatch.setattr(runner, "_summarize_messages", _fake_summarize_messages)

    active_state = {
        **initial_persistent_state(user_input={"content": "hello", "metadata": {}}),
        "tool_names": ["record_tool"],
        "frontdoor_stage_state": _active_frontdoor_stage_state(budget=1),
    }
    tools = runner._build_langchain_tools_for_state(
        state=active_state,
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    tools_by_name = {str(getattr(tool, "name", "") or ""): tool for tool in tools}

    ordinary_result = await tools_by_name["record_tool"].ainvoke({"value": "alpha"})

    updated = await runner._postprocess_completed_tool_cycle(
        state={
            **active_state,
            "tool_call_payloads": [
                _tool_call_payload(
                    call_id="call-tool-1",
                    tool_name="record_tool",
                    arguments={"value": "alpha"},
                )
            ],
            "messages": [
                {"role": "user", "content": "hello"},
                _assistant_tool_call_record(
                    call_id="call-tool-1",
                    tool_name="record_tool",
                    arguments={"value": "alpha"},
                ),
                _tool_message(
                    call_id="call-tool-1",
                    tool_name="record_tool",
                    result_text=str(ordinary_result["result_text"]),
                ),
            ],
        }
    )

    assert ordinary_result["status"] == "success"
    assert updated is not None
    assert updated["frontdoor_stage_state"]["transition_required"] is True
    assert updated["frontdoor_stage_state"]["stages"][0]["tool_rounds_used"] == 1
    assert executed == ["alpha"]

    exhausted_tools = runner._build_langchain_tools_for_state(
        state={
            **active_state,
            "frontdoor_stage_state": updated["frontdoor_stage_state"],
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )
    exhausted_tools_by_name = {str(getattr(tool, "name", "") or ""): tool for tool in exhausted_tools}
    blocked = await exhausted_tools_by_name["record_tool"].ainvoke({"value": "beta"})

    assert blocked["status"] == "error"
    assert blocked["result_text"].startswith(
        "Error: current stage budget is exhausted; call submit_next_stage before using other tools"
    )
    assert executed == ["alpha"]
