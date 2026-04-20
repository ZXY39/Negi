from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.agent.tools.base import Tool


class _EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo_tool"

    @property
    def description(self) -> str:
        return "echo tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    async def execute(self, value: str, **kwargs):
        _ = kwargs
        return {"ok": True, "value": value}


def test_graph_review_tool_calls_interrupt_payload_includes_runtime_contract_and_snapshot_fields(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake_interrupt(payload):
        captured["payload"] = payload
        return {"approved": True}

    monkeypatch.setattr(ceo_runtime_ops, "interrupt", _fake_interrupt)

    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    runtime = SimpleNamespace(context=SimpleNamespace(session=None))

    result = runner._graph_review_tool_calls(
        {
            "approval_request": {
                "kind": "frontdoor_tool_approval",
                "tool_calls": [{"id": "call-1", "name": "load_tool_context"}],
            },
            "tool_call_payloads": [
                {
                    "id": "call-1",
                    "name": "load_tool_context",
                    "arguments": {"tool_id": "filesystem_write"},
                }
            ],
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Write a file",
                        "tool_round_budget": 2,
                        "tool_rounds_used": 0,
                        "status": "active",
                        "rounds": [],
                    }
                ],
            },
            "compression_state": {"status": "running", "text": "compressing", "source": "user"},
            "semantic_context_state": {"summary_text": "summary", "needs_refresh": False},
            "hydrated_tool_names": ["filesystem_write"],
            "frontdoor_selection_debug": {"tool_selection": {"candidate_tool_names": ["filesystem_write"]}},
        },
        runtime=runtime,
    )

    assert result == {
        "approval_request": None,
        "approval_status": "approved",
        "tool_call_payloads": [
            {
                "id": "call-1",
                "name": "load_tool_context",
                "arguments": {"tool_id": "filesystem_write"},
            }
        ],
        "next_step": "execute_tools",
    }
    payload = dict(captured["payload"] or {})
    assert payload["kind"] == "frontdoor_tool_approval"
    assert payload["tool_calls"] == [{"id": "call-1", "name": "load_tool_context"}]
    assert payload["compression_state"] == {"status": "running", "text": "compressing", "source": "user"}
    assert "semantic_context_state" not in payload
    assert payload["hydrated_tool_names"] == ["filesystem_write"]
    assert payload["tool_call_payloads"] == [
        {
            "id": "call-1",
            "name": "load_tool_context",
            "arguments": {"tool_id": "filesystem_write"},
        }
    ]
    assert payload["frontdoor_selection_debug"] == {
        "tool_selection": {"candidate_tool_names": ["filesystem_write"]}
    }
    assert payload["frontdoor_stage_state"]["active_stage_id"] == "frontdoor-stage-1"
    assert payload["frontdoor_stage_state"]["transition_required"] is False
    assert payload["frontdoor_stage_state"]["stages"][0]["stage_goal"] == "Write a file"
    assert payload["frontdoor_stage_state"]["stages"][0]["tool_round_budget"] == 2


def test_frontdoor_tool_state_after_tool_results_skips_fixed_builtin_hydration_targets() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    result = runner._frontdoor_tool_state_after_tool_results(
        state={
            "tool_names": ["load_tool_context", "exec"],
            "candidate_tool_names": ["agent_browser"],
            "hydrated_tool_names": [],
            "rbac_visible_tool_names": ["load_tool_context", "exec", "agent_browser"],
        },
        tool_results=[
            {
                "tool_name": "load_tool_context",
                "raw_result": {"ok": True, "hydration_targets": ["exec"]},
            }
        ],
    )

    assert result == {
        "tool_names": ["load_tool_context", "exec"],
        "candidate_tool_names": ["agent_browser"],
        "candidate_tool_items": [{"tool_id": "agent_browser", "description": ""}],
        "hydrated_tool_names": [],
    }


@pytest.mark.asyncio
async def test_graph_execute_tools_executes_runtime_submit_next_stage_and_persists_stage_state(
    monkeypatch,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool_name, runtime_context, on_progress, tool_call_id
        raw_result = await tool.execute(**arguments)
        return (
            raw_result,
            json.dumps(raw_result, ensure_ascii=False),
            "success",
            "2026-04-15T00:00:00+08:00",
            "2026-04-15T00:00:01+08:00",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)

    result = await runner._graph_execute_tools(
        {
            "messages": [],
            "tool_names": [],
            "candidate_tool_names": [],
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
            "tool_call_payloads": [
                {
                    "id": "call-stage-1",
                    "name": ceo_runtime_ops.STAGE_TOOL_NAME,
                    "arguments": {
                        "stage_goal": "Create a stage before using tools",
                        "tool_round_budget": 5,
                    },
                }
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": False,
            "max_parallel_tool_calls": 1,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
            "session_key": "web:shared",
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    assert result["next_step"] == "call_model"
    assert result["frontdoor_stage_state"]["active_stage_id"] == "frontdoor-stage-1"
    assert result["frontdoor_stage_state"]["transition_required"] is False
    assert result["frontdoor_stage_state"]["stages"] == [
        {
            "stage_id": "frontdoor-stage-1",
            "stage_index": 1,
            "stage_goal": "Create a stage before using tools",
            "tool_round_budget": 5,
            "tool_rounds_used": 0,
            "status": "active",
            "mode": "自主执行",
            "stage_kind": "normal",
            "system_generated": False,
            "completed_stage_summary": "",
            "final_stage": False,
            "key_refs": [],
            "archive_ref": "",
            "archive_stage_index_start": 0,
            "archive_stage_index_end": 0,
            "rounds": [],
            "created_at": result["frontdoor_stage_state"]["stages"][0]["created_at"],
            "finished_at": "",
        }
    ]


@pytest.mark.asyncio
async def test_graph_execute_tools_blocks_ordinary_tool_without_active_stage(
    monkeypatch,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"echo_tool": _EchoTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, runtime_context, on_progress, tool_call_id
        executed.append((tool_name, dict(arguments)))
        return (
            {"ok": True},
            json.dumps({"ok": True}),
            "success",
            "2026-04-15T00:00:00+08:00",
            "2026-04-15T00:00:01+08:00",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)

    result = await runner._graph_execute_tools(
        {
            "messages": [],
            "tool_names": ["echo_tool"],
            "candidate_tool_names": [],
            "hydrated_tool_names": [],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["echo_tool"],
            "rbac_visible_skill_ids": [],
            "frontdoor_stage_state": {
                "active_stage_id": "",
                "transition_required": False,
                "stages": [],
            },
            "tool_call_payloads": [
                {"id": "call-echo-1", "name": "echo_tool", "arguments": {"value": "alpha"}}
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": False,
            "max_parallel_tool_calls": 1,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
            "session_key": "web:shared",
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    tool_messages = [
        dict(message)
        for message in list(result["messages"])
        if str(message.get("role") or "").strip().lower() == "tool"
    ]
    assert executed == []
    assert result["next_step"] == "call_model"
    assert len(tool_messages) == 1
    assert str(tool_messages[0]["content"]).startswith(
        "Error: no active stage; call submit_next_stage before using other tools"
    )


@pytest.mark.asyncio
async def test_graph_execute_tools_allows_submit_next_stage_mixed_with_other_tools(
    monkeypatch,
) -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    executed: list[str] = []

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"echo_tool": _EchoTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, runtime_context, on_progress, tool_call_id
        executed.append(tool_name)
        raw_result = await tool.execute(**arguments)
        return (
            raw_result,
            json.dumps(raw_result, ensure_ascii=False),
            "success",
            "2026-04-15T00:00:00+08:00",
            "2026-04-15T00:00:01+08:00",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)

    result = await runner._graph_execute_tools(
        {
            "messages": [],
            "tool_names": ["echo_tool"],
            "candidate_tool_names": [],
            "hydrated_tool_names": [],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["echo_tool"],
            "rbac_visible_skill_ids": [],
            "frontdoor_stage_state": {
                "active_stage_id": "",
                "transition_required": False,
                "stages": [],
            },
            "tool_call_payloads": [
                {
                    "id": "call-stage-1",
                    "name": ceo_runtime_ops.STAGE_TOOL_NAME,
                    "arguments": {
                        "stage_goal": "Create a stage before using tools",
                        "tool_round_budget": 2,
                    },
                },
                {"id": "call-echo-1", "name": "echo_tool", "arguments": {"value": "alpha"}},
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": True,
            "max_parallel_tool_calls": 2,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
            "session_key": "web:shared",
        },
        runtime=SimpleNamespace(context=SimpleNamespace()),
    )

    tool_messages = [
        dict(message)
        for message in list(result["messages"])
        if str(message.get("role") or "").strip().lower() == "tool"
    ]
    assert executed == [ceo_runtime_ops.STAGE_TOOL_NAME, "echo_tool"]
    assert result["frontdoor_stage_state"]["active_stage_id"] == "frontdoor-stage-1"
    assert result["frontdoor_stage_state"]["transition_required"] is False
    assert len(result["frontdoor_stage_state"]["stages"]) == 1
    assert result["frontdoor_stage_state"]["stages"][0]["tool_rounds_used"] == 1
    assert len(result["frontdoor_stage_state"]["stages"][0]["rounds"]) == 1
    assert result["frontdoor_stage_state"]["stages"][0]["rounds"][0]["tool_names"] == ["echo_tool"]
    assert len(tool_messages) == 2
    assert tool_messages[0]["name"] == ceo_runtime_ops.STAGE_TOOL_NAME
    assert tool_messages[1]["name"] == "echo_tool"
