from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage


from g3ku.agent.tools.base import Tool
from g3ku.core.messages import UserInputMessage
from g3ku.config.schema import MemoryAssemblyConfig
from g3ku.runtime.context.types import ContextAssemblyResult
from g3ku.runtime.api import websocket_ceo
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor import prompt_cache_contract
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.frontdoor.state_models import (
    CeoFrontdoorInterrupted,
    CeoPersistentState,
    CeoRuntimeContext,
)
from g3ku.runtime.frontdoor.tool_contract import (
    frontdoor_tool_contract_payload_from_message,
    is_frontdoor_tool_contract_message,
)
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import SessionManager


def _frontdoor_tool_contract_payload(message: dict[str, object]) -> dict[str, object] | None:
    payload = frontdoor_tool_contract_payload_from_message(dict(message or {}))
    if not isinstance(payload, dict):
        return None
    return dict(payload)


def test_ceo_snapshot_keeps_canonical_context_and_compression_payloads() -> None:
    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {
                "role": "assistant",
                "content": "stage running",
                "canonical_context": {
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
            }
        ]
    )

    assert snapshot[0]["canonical_context"]["stages"][0]["stage_goal"] == "inspect repository"
    assert snapshot[0]["compression"]["status"] == "running"
    assert "execution_trace_summary" not in snapshot[0]
    assert "tool_events" not in snapshot[0]


def test_ceo_snapshot_includes_message_local_canonical_context_delta() -> None:
    snapshot = websocket_ceo._build_ceo_snapshot(
        [
            {
                "role": "assistant",
                "content": "first reply",
                "canonical_context": {
                    "active_stage_id": "frontdoor-stage-1",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "frontdoor-stage-1",
                            "stage_goal": "collect sources",
                            "completed_stage_summary": "stage one complete",
                            "rounds": [
                                {
                                    "round_id": "round-1",
                                    "tools": [
                                        {"tool_call_id": "filesystem:1", "tool_name": "filesystem", "status": "success"}
                                    ],
                                }
                            ],
                        }
                    ],
                },
            },
            {
                "role": "assistant",
                "content": "second reply",
                "canonical_context": {
                    "active_stage_id": "frontdoor-stage-2",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "frontdoor-stage-1",
                            "stage_goal": "collect sources",
                            "completed_stage_summary": "stage one complete",
                            "rounds": [
                                {
                                    "round_id": "round-1",
                                    "tools": [
                                        {"tool_call_id": "filesystem:1", "tool_name": "filesystem", "status": "success"}
                                    ],
                                }
                            ],
                        },
                        {
                            "stage_id": "frontdoor-stage-2",
                            "stage_goal": "rank candidates",
                            "completed_stage_summary": "stage two complete",
                            "rounds": [
                                {
                                    "round_id": "round-2",
                                    "tools": [
                                        {"tool_call_id": "web_search:2", "tool_name": "web_search", "status": "success"}
                                    ],
                                }
                            ],
                        },
                    ],
                },
            },
        ]
    )

    assert "canonical_context_delta" in snapshot[0]
    assert snapshot[0]["canonical_context_delta"]["stages"][0]["stage_id"] == "frontdoor-stage-1"
    assert "canonical_context_delta" in snapshot[1]
    delta_stages = snapshot[1]["canonical_context_delta"]["stages"]
    assert [stage["stage_id"] for stage in delta_stages] == ["frontdoor-stage-2"]


def test_ceo_live_turn_payload_includes_inflight_canonical_context_delta() -> None:
    persisted_session = SimpleNamespace(
        messages=[
            {
                "role": "assistant",
                "content": "older reply",
                "canonical_context": {
                    "active_stage_id": "frontdoor-stage-1",
                    "transition_required": False,
                    "stages": [
                        {
                            "stage_id": "frontdoor-stage-1",
                            "stage_goal": "collect sources",
                            "completed_stage_summary": "stage one complete",
                            "rounds": [
                                {
                                    "round_id": "round-1",
                                    "tools": [
                                        {"tool_call_id": "filesystem:1", "tool_name": "filesystem", "status": "success"}
                                    ],
                                }
                            ],
                        }
                    ],
                },
            }
        ]
    )
    session = SimpleNamespace(
        inflight_turn_snapshot=lambda: {
            "turn_id": "turn-current",
            "source": "user",
            "status": "running",
            "assistant_text": "working",
            "canonical_context": {
                "active_stage_id": "frontdoor-stage-2",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_goal": "collect sources",
                        "completed_stage_summary": "stage one complete",
                        "rounds": [
                            {
                                "round_id": "round-1",
                                "tools": [
                                    {"tool_call_id": "filesystem:1", "tool_name": "filesystem", "status": "success"}
                                ],
                            }
                        ],
                    },
                    {
                        "stage_id": "frontdoor-stage-2",
                        "stage_goal": "rank candidates",
                        "completed_stage_summary": "stage two complete",
                        "rounds": [
                            {
                                "round_id": "round-2",
                                "tools": [
                                    {"tool_call_id": "web_search:2", "tool_name": "web_search", "status": "success"}
                                ],
                            }
                        ],
                    },
                ],
            },
        }
    )

    payload = websocket_ceo._build_live_turn_payload(session, "web:shared", persisted_session)

    assert payload["inflight_turn"]["canonical_context"]["stages"][1]["stage_id"] == "frontdoor-stage-2"
    assert payload["inflight_turn"]["canonical_context_delta"]["stages"][0]["stage_id"] == "frontdoor-stage-2"


def test_ceo_snapshot_ignores_legacy_tool_events_without_canonical_context() -> None:
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

    assert snapshot == []


def test_ceo_frontdoor_refresh_dynamic_contract_state_keeps_repair_required_lists() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    runner._selected_tool_schemas = lambda tool_names: [
        {"name": name, "description": "", "parameters": {"type": "object"}}
        for name in list(tool_names or [])
    ]

    refreshed = runner._refresh_frontdoor_dynamic_contract_state(
        state={
            "session_key": "web:shared",
            "messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "repair this"},
            ],
            "stable_messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "repair this"},
            ],
            "dynamic_appendix_messages": [],
            "tool_names": ["submit_next_stage", "exec"],
            "provider_tool_names": ["submit_next_stage", "exec"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "hydrated_tool_names": [],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["submit_next_stage", "exec", "agent_browser"],
            "rbac_visible_skill_ids": ["writing-skills"],
            "repair_required_tool_items": [
                {
                    "tool_id": "agent_browser",
                    "description": "Browser automation",
                    "reason": "missing required paths",
                }
            ],
            "repair_required_skill_items": [
                {
                    "skill_id": "writing-skills",
                    "description": "Skill maintenance workflow",
                    "reason": "missing required bins",
                }
            ],
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "model_refs": ["openai:gpt-4.1"],
            "cache_family_revision": "frontdoor:v1",
            "turn_overlay_text": "",
        }
    )

    contract_messages = [dict(message) for message in list(refreshed["dynamic_appendix_messages"] or [])]
    assert len(contract_messages) == 1
    assert is_frontdoor_tool_contract_message(contract_messages[0])
    assert "repair_required_tools:" in str(contract_messages[0]["content"] or "")
    assert "repair_required_skills:" in str(contract_messages[0]["content"] or "")
    assert "agent_browser" in str(contract_messages[0]["content"] or "")
    assert "writing-skills" in str(contract_messages[0]["content"] or "")
    assert refreshed["repair_required_tool_items"] == [
        {
            "tool_id": "agent_browser",
            "description": "Browser automation",
            "reason": "missing required paths",
        }
    ]
    assert refreshed["repair_required_skill_items"] == [
        {
            "skill_id": "writing-skills",
            "description": "Skill maintenance workflow",
            "reason": "missing required bins",
        }
    ]


def test_execution_snapshot_history_uses_canonical_context_without_legacy_tool_events() -> None:
    runtime_session = SimpleNamespace(
        inflight_turn_snapshot=lambda: {
            "status": "running",
            "user_message": {"content": "install weather skill"},
            "canonical_context": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": False,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "install weather skill",
                        "representation": "raw",
                        "status": "active",
                        "stage_kind": "normal",
                        "tool_round_budget": 3,
                        "tool_rounds_used": 1,
                        "rounds": [
                            {
                                "round_index": 1,
                                "budget_counted": True,
                                "tool_names": ["skill-installer"],
                                "tool_call_ids": ["skill-installer:1"],
                                "tools": [
                                    {
                                        "tool_call_id": "skill-installer:1",
                                        "tool_name": "skill-installer",
                                        "status": "success",
                                        "arguments": {"skill_id": "weather"},
                                        "arguments_text": "{\"skill_id\": \"weather\"}",
                                        "output_text": "installed weather",
                                        "output_preview_text": "",
                                        "output_ref": "",
                                        "source": "user",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            },
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
    assert history[1]["canonical_context"]["stages"][0]["rounds"][0]["tools"][0]["tool_name"] == "skill-installer"
    assert "tool_events" not in history[1]


def test_inflight_snapshot_uses_current_turn_canonical_context_not_durable_history() -> None:
    loop = SimpleNamespace(model="gpt-test", reasoning_effort=None)
    session = RuntimeAgentSession(loop, session_key="web:shared", channel="web", chat_id="shared")
    session._state.is_running = True
    session._state.status = "running"
    session._last_prompt = UserInputMessage(content="new request")
    session._frontdoor_canonical_context = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-old",
                "stage_index": 1,
                "stage_goal": "old durable stage",
                "representation": "raw",
                "status": "completed",
                "stage_kind": "normal",
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
                "rounds": [],
            }
        ],
    }

    snapshot = session.inflight_turn_snapshot()

    assert isinstance(snapshot, dict)
    assert "canonical_context" not in snapshot

    session._frontdoor_stage_state = {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_goal": "current visible stage",
                "status": "active",
                "stage_kind": "normal",
                "tool_round_budget": 3,
                "tool_rounds_used": 0,
                "rounds": [],
            }
        ],
    }

    snapshot_with_stage = session.inflight_turn_snapshot()

    assert snapshot_with_stage["canonical_context"]["stages"][0]["stage_goal"] == "current visible stage"
    assert snapshot_with_stage["canonical_context"]["stages"][0]["stage_id"] == "frontdoor-stage-1"


class _ExecTool(Tool):
    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "run command"

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }

    async def execute(self, **kwargs):
        return kwargs


@pytest.mark.asyncio
async def test_ceo_frontdoor_runner_passes_thread_id_and_runtime_context(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_run_step_loop(*, state, runtime, entry, resume_decision=None):
        captured["state"] = state
        captured["runtime"] = runtime
        captured["entry"] = entry
        return {"final_output": "ok", "route_kind": "tool_result", "verified_task_ids": []}

    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner._impl, "_run_step_loop", _fake_run_step_loop)

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
    assert getattr(session, "_last_route_kind") == "tool_result"
    assert captured["entry"] == "prepare_turn"

    runtime_context = getattr(captured["runtime"], "context")
    assert runtime_context is not None
    assert getattr(runtime_context, "session_key") == "web:shared"

    graph_input = dict(captured["state"] or {})
    assert graph_input["user_input"] == {
        "content": "persist this turn",
        "metadata": {"cron_job_id": "cron-1"},
    }
    json.dumps(graph_input)
    assert "session" not in graph_input
    assert "on_progress" not in graph_input


@pytest.mark.asyncio
async def test_ceo_frontdoor_run_turn_raises_structured_interrupt(monkeypatch) -> None:
    async def _raise_interrupt(*, state, runtime, entry, resume_decision=None):
        _ = state, runtime, entry
        raise CeoFrontdoorInterrupted(
            interrupts=[
                SimpleNamespace(interrupt_id="interrupt-1", value={"kind": "frontdoor_tool_approval"})
            ],
            values={"route_kind": "direct_reply"},
            resume_state={},
        )

    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner._impl, "_run_step_loop", _raise_interrupt)
    session = SimpleNamespace(state=SimpleNamespace(session_key="web:shared"))

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        await runner.run_turn(
            user_input=SimpleNamespace(content="create a task", metadata={}),
            session=session,
            on_progress=None,
        )

    assert exc_info.value.interrupts[0].interrupt_id == "interrupt-1"
    assert exc_info.value.interrupts[0].value["kind"] == "frontdoor_tool_approval"


def test_ceo_frontdoor_run_turn_serializes_interrupt_payloads() -> None:
    class _OpaqueArg:
        def __str__(self) -> str:
            return "opaque-arg"

    payloads = [{"name": "create_async_task", "arguments": {"task": _OpaqueArg()}}]

    with pytest.raises(CeoFrontdoorInterrupted) as exc_info:
        ceo_runtime_ops.raise_frontdoor_approval_interrupt(
            state={
                "approval_request": {"kind": "frontdoor_tool_approval", "tool_calls": payloads},
                "tool_call_payloads": payloads,
            },
            payload={"kind": "frontdoor_tool_approval", "tool_calls": payloads},
        )

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
async def test_ceo_frontdoor_resume_turn_uses_command_resume_on_same_thread(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_run_step_loop(*, state, runtime, entry, resume_decision=None):
        captured["state"] = state
        captured["entry"] = entry
        captured["resume_decision"] = resume_decision
        return {"final_output": "approved reply", "route_kind": "direct_reply", "verified_task_ids": []}

    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner._impl, "_run_step_loop", _fake_run_step_loop)
    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        paused_execution_context_snapshot=lambda: {
            "graph_state": {"version": 2, "state": {"approval_request": {"kind": "frontdoor_tool_approval"}}}
        },
    )

    output = await runner.resume_turn(
        session=session,
        resume_value={"approved": True},
        on_progress=None,
    )

    assert output == "approved reply"
    assert captured["entry"] == "review_tool_calls"
    assert captured["resume_decision"] == {"approved": True}


def test_ceo_frontdoor_review_tool_calls_ignores_resume_payload_tool_call_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    original_payloads = [{"name": "create_async_task", "arguments": {"task": "original"}}]
    override_payloads = [{"name": "exec", "arguments": {"command": "mutated"}}]

    monkeypatch.setattr(
        ceo_runtime_ops,
        "raise_frontdoor_approval_interrupt",
        lambda *, state, payload: {"approved": True, "tool_calls": override_payloads},
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
                frontdoor_interrupt_tool_names=["create_async_task", "exec"],
            )
        )
    )
    runner = CeoFrontDoorRunner(loop=loop)

    result = runner._approval_request_for_tool_calls(
        [
            {"id": "call-low-1", "name": "content_open", "arguments": {"path": "/tmp/a"}},
            {"id": "call-risk-1", "name": "create_async_task", "arguments": {"task": "demo"}},
            {"id": "call-risk-2", "name": "exec", "arguments": {"command": "echo hi"}},
        ]
    )

    assert result == {
        "kind": "frontdoor_tool_approval_batch",
        "batch_id": result["batch_id"],
        "mode": "regulatory_review",
        "submission_mode": "batch_submit_only",
        "tool_calls": [
            {"id": "call-low-1", "name": "content_open", "arguments": {"path": "/tmp/a"}},
            {"id": "call-risk-1", "name": "create_async_task", "arguments": {"task": "demo"}},
            {"id": "call-risk-2", "name": "exec", "arguments": {"command": "echo hi"}},
        ],
        "review_items": [
            {
                "tool_call_id": "call-risk-1",
                "name": "create_async_task",
                "risk_level": "high",
                "arguments": {"task": "demo"},
            },
            {
                "tool_call_id": "call-risk-2",
                "name": "exec",
                "risk_level": "high",
                "arguments": {"command": "echo hi"},
            },
        ],
        "pass_through_tool_call_ids": ["call-low-1"],
    }


def test_ceo_frontdoor_approval_request_uses_dynamic_governance_risk_map() -> None:
    captured: dict[str, object] = {}

    class _Service:
        def frontdoor_reviewable_tool_risk_map(self, *, actor_role: str, session_id: str) -> dict[str, str]:
            captured["actor_role"] = actor_role
            captured["session_id"] = session_id
            return {
                "exec": "high",
                "memory_write": "medium",
            }

    runner = CeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=_Service()))

    result = runner._approval_request_for_tool_calls(
        [
            {"id": "call-low-1", "name": "content_open", "arguments": {"path": "/tmp/a"}},
            {"id": "call-risk-1", "name": "exec", "arguments": {"command": "echo hi"}},
            {"id": "call-risk-2", "name": "memory_write", "arguments": {"content": "remember this"}},
        ],
        session_key="web:ceo-dynamic",
    )

    assert captured == {
        "actor_role": "ceo",
        "session_id": "web:ceo-dynamic",
    }
    assert result == {
        "kind": "frontdoor_tool_approval_batch",
        "batch_id": result["batch_id"],
        "mode": "regulatory_review",
        "submission_mode": "batch_submit_only",
        "tool_calls": [
            {"id": "call-low-1", "name": "content_open", "arguments": {"path": "/tmp/a"}},
            {"id": "call-risk-1", "name": "exec", "arguments": {"command": "echo hi"}},
            {"id": "call-risk-2", "name": "memory_write", "arguments": {"content": "remember this"}},
        ],
        "review_items": [
            {
                "tool_call_id": "call-risk-1",
                "name": "exec",
                "risk_level": "high",
                "arguments": {"command": "echo hi"},
            },
            {
                "tool_call_id": "call-risk-2",
                "name": "memory_write",
                "risk_level": "medium",
                "arguments": {"content": "remember this"},
            },
        ],
        "pass_through_tool_call_ids": ["call-low-1"],
    }




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
        return {"skills": [], "tool_families": [], "tool_names": ["exec"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["exec"],
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
    assert state_update["tool_names"] == ["exec"]
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
        return {"skills": [], "tool_families": [], "tool_names": ["exec"]}

    async def _build_for_ceo(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            tool_names=["exec"],
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
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_id": "openai_codex",
            "provider_model": "openai_codex:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 32000,
        },
        raising=False,
    )

    async def _call_model_with_tools(**kwargs):
        _ = kwargs
        return AIMessage(
            content="tool reply",
            tool_calls=[{"id": "call-1", "name": "filesystem", "args": {"path": "."}}],
            response_metadata={
                "finish_reason": "tool_calls",
                "provider_request_meta": {
                    "provider": "responses",
                    "endpoint": "https://example.test/v1/responses",
                },
                "provider_request_body": {
                    "model": "gpt-5.4-mini",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "list files"}]}],
                    "tool_choice": "auto",
                },
            },
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
    replaced_messages = list(update["messages"] or [])
    assert replaced_messages == [{"role": "user", "content": "list files"}]
    assert update["response_payload"] == {
        "content": "tool reply",
        "tool_calls": [{"id": "call-1", "name": "filesystem", "arguments": {"path": "."}}],
        "finish_reason": "tool_calls",
        "error_text": "",
        "reasoning_content": "reasoning trace",
        "thinking_blocks": [{"type": "thinking", "text": "step one"}],
        "provider_request_meta": {
            "provider": "responses",
            "endpoint": "https://example.test/v1/responses",
        },
        "provider_request_body": {
            "model": "gpt-5.4-mini",
            "tool_choice": "auto",
            "input_count": 1,
            "tools_count": 0,
            "contains_multimodal": False,
        },
    }
    json.dumps(update["response_payload"])


@pytest.mark.asyncio
async def test_ceo_frontdoor_call_model_rebuilds_request_messages_from_stable_and_dynamic_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        prompt_cache_contract,
        "build_session_prompt_cache_key",
        lambda **kwargs: "rebuilt-cache-key",
    )

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_id": "openai_codex",
            "provider_model": "openai_codex:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 32000,
        },
        raising=False,
    )

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
    assert any(is_frontdoor_tool_contract_message(dict(message)) for message in request_messages)
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
    assert captured["prompt_cache_key"] == "rebuilt-cache-key"
    assert update["iteration"] == 1
    assert update["prompt_cache_key"] == "rebuilt-cache-key"
    assert str(update["prompt_cache_diagnostics"]["prompt_cache_key_hash"] or "").strip()
    assert str(update["prompt_cache_diagnostics"]["actual_request_hash"] or "").strip()
    assert update["prompt_cache_diagnostics"]["actual_request_message_count"] == len(request_messages)


@pytest.mark.asyncio
async def test_ceo_frontdoor_call_model_persists_actual_request_to_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_selected_tool_schemas",
        lambda tool_names: [
            {
                "name": str((list(tool_names or []) or ["submit_next_stage"])[0]),
                "description": "",
                "parameters": {"type": "object"},
            }
        ],
    )
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 32000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1200, raising=False)

    async def _call_model_with_tools(**kwargs):
        return AIMessage(
            content="plain reply",
            response_metadata={
                "finish_reason": "stop",
                "usage": {
                    "input_tokens": 123,
                    "output_tokens": 9,
                    "cache_hit_tokens": 45,
                },
                "provider_request_meta": {
                    "provider": "responses",
                    "endpoint": "https://example.test/v1/responses",
                },
                "provider_request_body": {
                    "model": "gpt-5.4-mini",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                    "prompt_cache_key": "cache-key",
                    "tool_choice": "auto",
                },
            },
        )

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _active_turn_id="turn-frontdoor-1",
        _frontdoor_actual_request_history=[],
        _current_turn_id=lambda prompt=None: "turn-frontdoor-1",
    )

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
                {"role": "user", "content": '{"message_type":"frontdoor_runtime_tool_contract"}'},
            ],
            "turn_overlay_text": "## Retrieved Context\n- memory",
            "tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
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
        runtime=SimpleNamespace(context=CeoRuntimeContext(loop=None, session=session, session_key="web:shared", on_progress=None)),
    )

    request_path = Path(str(update["frontdoor_actual_request_path"]))
    assert request_path.exists()
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload["session_id"] == "web:shared"
    assert payload["turn_id"] == "turn-frontdoor-1"
    assert "messages" not in payload
    assert payload["prompt_cache_key"] == update["prompt_cache_key"]
    assert payload["actual_request_hash"] == update["prompt_cache_diagnostics"]["actual_request_hash"]
    assert payload["actual_request_message_count"] == update["prompt_cache_diagnostics"]["actual_request_message_count"]
    assert payload["frontdoor_token_preflight_diagnostics"] == update["frontdoor_token_preflight_diagnostics"]
    assert payload["frontdoor_history_shrink_reason"] == update["frontdoor_history_shrink_reason"]
    assert payload["request_messages"]
    assert payload["tool_schemas"]
    assert payload["provider_request_meta"] == {
        "provider": "responses",
        "endpoint": "https://example.test/v1/responses",
    }
    assert payload["provider_request_body"] == {
        "model": "gpt-5.4-mini",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "prompt_cache_key": "cache-key",
        "tool_choice": "auto",
    }
    assert payload["usage"] == {
        "input_tokens": 123,
        "output_tokens": 9,
        "cache_hit_tokens": 45,
    }
    assert update["frontdoor_actual_request_history"]


@pytest.mark.asyncio
async def test_frontdoor_actual_request_trace_round_trips_usage_ground_truth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_selected_tool_schemas",
        lambda tool_names: [
            {
                "name": str((list(tool_names or []) or ["submit_next_stage"])[0]),
                "description": "",
                "parameters": {"type": "object"},
            }
        ],
    )
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 32000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1200, raising=False)

    async def _call_model_with_tools(**kwargs):
        return AIMessage(
            content="plain reply",
            response_metadata={
                "finish_reason": "stop",
                "usage": {
                    "input_tokens": 123,
                    "output_tokens": 9,
                    "cache_read_tokens": 45,
                },
                "provider_request_meta": {
                    "provider": "responses",
                    "endpoint": "https://example.test/v1/responses",
                },
                "provider_request_body": {
                    "model": "gpt-5.4-mini",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                    "prompt_cache_key": "cache-key",
                    "tool_choice": "auto",
                },
            },
        )

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _active_turn_id="turn-frontdoor-usage-1",
        _frontdoor_actual_request_history=[],
        _current_turn_id=lambda prompt=None: "turn-frontdoor-usage-1",
    )

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
                {"role": "user", "content": '{"message_type":"frontdoor_runtime_tool_contract"}'},
            ],
            "turn_overlay_text": "## Retrieved Context\n- memory",
            "tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
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
        runtime=SimpleNamespace(context=CeoRuntimeContext(loop=None, session=session, session_key="web:shared", on_progress=None)),
    )

    request_path = Path(str(update["frontdoor_actual_request_path"]))
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    expected_truth = {
        "effective_input_tokens": 168,
        "input_tokens": 123,
        "cache_hit_tokens": 45,
        "provider_model": "openai:gpt-5.2",
        "actual_request_hash": str(payload["actual_request_hash"] or "").strip(),
        "source": "provider_usage",
    }

    assert payload["observed_input_truth"] == expected_truth
    assert payload["frontdoor_token_preflight_diagnostics"]["observed_input_truth"] == expected_truth
    assert payload["frontdoor_token_preflight_diagnostics"]["effective_input_tokens"] == 168
    assert update["frontdoor_token_preflight_diagnostics"]["observed_input_truth"] == expected_truth
    assert update["frontdoor_token_preflight_diagnostics"]["effective_input_tokens"] == 168
    assert update["frontdoor_actual_request_history"][-1]["observed_input_truth"] == expected_truth
    assert payload["usage"] == {
        "input_tokens": 123,
        "output_tokens": 9,
        "cache_hit_tokens": 45,
    }


@pytest.mark.asyncio
async def test_ceo_frontdoor_call_model_falls_back_to_preflight_truth_when_usage_has_no_input_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_selected_tool_schemas",
        lambda names: [
            {
                "name": str((list(names or []) or ["submit_next_stage"])[0]),
                "description": "",
                "parameters": {"type": "object"},
            }
        ],
        raising=False,
    )
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "ceo_primary",
            "provider_model": "openai:gpt-5.2",
            "context_window_tokens": 32000,
        },
        raising=False,
    )
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1200, raising=False)

    async def _call_model_without_input_usage(**kwargs):
        return AIMessage(
            content="plain reply",
            response_metadata={
                "finish_reason": "stop",
                "usage": {
                    "output_tokens": 9,
                },
                "provider_request_meta": {
                    "provider": "responses",
                    "endpoint": "https://example.test/v1/responses",
                },
                "provider_request_body": {
                    "model": "gpt-5.4-mini",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
                    "prompt_cache_key": "cache-key",
                    "tool_choice": "auto",
                },
            },
        )

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_without_input_usage)

    session = SimpleNamespace(
        state=SimpleNamespace(session_key="web:shared"),
        _active_turn_id="turn-frontdoor-estimate-fallback-1",
        _frontdoor_actual_request_history=[],
        _current_turn_id=lambda prompt=None: "turn-frontdoor-estimate-fallback-1",
    )

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
            "dynamic_appendix_messages": [],
            "turn_overlay_text": "",
            "tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
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
        runtime=SimpleNamespace(
            context=CeoRuntimeContext(
                loop=None,
                session=session,
                session_key="web:shared",
                on_progress=None,
            )
        ),
    )

    request_path = Path(str(update["frontdoor_actual_request_path"]))
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    expected_truth = {
        "effective_input_tokens": 1200,
        "input_tokens": 1200,
        "cache_hit_tokens": 0,
        "provider_model": "openai:gpt-5.2",
        "actual_request_hash": str(payload["actual_request_hash"] or "").strip(),
        "source": "preflight_estimate",
    }

    assert payload["observed_input_truth"] == expected_truth
    assert payload["frontdoor_token_preflight_diagnostics"]["observed_input_truth"] == expected_truth
    assert payload["frontdoor_token_preflight_diagnostics"]["effective_input_tokens"] == 1200
    assert update["frontdoor_token_preflight_diagnostics"]["observed_input_truth"] == expected_truth
    assert update["frontdoor_token_preflight_diagnostics"]["effective_input_tokens"] == 1200
    assert update["frontdoor_actual_request_history"][-1]["observed_input_truth"] == expected_truth
    assert payload["usage"] == {
        "output_tokens": 9,
    }


@pytest.mark.asyncio
async def test_ceo_frontdoor_call_model_keeps_request_messages_append_only_inside_turn_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_id": "openai_codex",
            "provider_model": "openai_codex:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 32000,
        },
        raising=False,
    )

    async def _call_model_with_tools(**kwargs):
        _ = kwargs
        return AIMessage(content="plain reply", response_metadata={"finish_reason": "stop"})

    monkeypatch.setattr(runner, "_call_model_with_tools", _call_model_with_tools)

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

    update = await runner._graph_call_model(
        {
            "messages": [
                {"role": "system", "content": "stable system"},
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
            ],
            "stable_messages": [
                {"role": "system", "content": "stable system"},
                {"role": "user", "content": "hello"},
            ],
            "dynamic_appendix_messages": [{"role": "user", "content": contract_text}],
            "turn_overlay_text": "",
            "tool_names": ["submit_next_stage"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "hydrated_tool_names": [],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["submit_next_stage"],
            "rbac_visible_skill_ids": [],
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
            "model_refs": ["openai_codex:gpt-test"],
            "parallel_enabled": False,
            "prompt_cache_key": "cache-key",
            "iteration": 1,
            "max_iterations": 4,
            "session_key": "web:shared",
        },
        runtime=SimpleNamespace(context=CeoRuntimeContext(loop=None, session=None, session_key="web:shared", on_progress=None)),
    )

    replaced_messages = list(update["messages"] or [])
    replaced_body_messages = [dict(item) for item in replaced_messages if isinstance(item, dict)]
    expected_body_messages = [
        {"role": "system", "content": "stable system"},
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
    ]
    # The tool contract is turn-only and must never be persisted into state messages:
    # the stale contract carried inside the live body is dropped before the state update.
    assert replaced_body_messages == expected_body_messages
    assert all(not is_frontdoor_tool_contract_message(dict(item)) for item in replaced_body_messages)
    assert all(
        str(item.get("content") or "") != contract_text
        for item in replaced_body_messages
    )


@pytest.mark.asyncio
async def test_ceo_frontdoor_call_model_keeps_provider_tool_schema_set_stable_when_stage_transition_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())

    monkeypatch.setattr(runner, "_build_langchain_tools_for_state", lambda **kwargs: [])
    monkeypatch.setattr(
        runner,
        "_resolve_frontdoor_send_model_context_window",
        lambda **_: {
            "model_key": "openai_codex:gpt-test",
            "provider_id": "openai_codex",
            "provider_model": "openai_codex:gpt-test",
            "resolved_model": "gpt-test",
            "context_window_tokens": 32000,
        },
        raising=False,
    )

    selected_tool_schema_requests: list[list[str]] = []

    def _capture_selected_tool_schemas(tool_names):
        normalized = [str(name or "").strip() for name in list(tool_names or []) if str(name or "").strip()]
        selected_tool_schema_requests.append(normalized)
        return [{"name": name, "parameters": {"type": "object"}} for name in normalized]

    monkeypatch.setattr(runner, "_selected_tool_schemas", _capture_selected_tool_schemas)

    async def _call_model_with_tools(**kwargs):
        _ = kwargs
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
            "dynamic_appendix_messages": [],
            "turn_overlay_text": "",
            "tool_names": ["exec", "load_tool_context", "submit_next_stage"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "hydrated_tool_names": [],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_tool_names": ["exec", "load_tool_context", "submit_next_stage"],
            "rbac_visible_skill_ids": [],
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": True,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Wrap up the current stage before continuing",
                        "tool_round_budget": 5,
                        "tool_rounds_used": 5,
                        "status": "active",
                        "mode": "自主执行",
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "rounds": [],
                    }
                ],
            },
            "model_refs": ["openai_codex:gpt-test"],
            "parallel_enabled": False,
            "prompt_cache_key": "cache-key",
            "iteration": 0,
            "max_iterations": 4,
            "session_key": "web:shared",
        },
        runtime=SimpleNamespace(
            context=CeoRuntimeContext(loop=None, session=None, session_key="web:shared", on_progress=None)
        ),
    )

    assert selected_tool_schema_requests
    assert selected_tool_schema_requests[-1] == ["exec", "load_tool_context", "submit_next_stage"]
    replaced_messages = [dict(item) for item in list(update["messages"] or []) if isinstance(item, dict)]
    # Turn-only tool contract must not be persisted into state messages, even when a
    # stage transition is required; the latest request still exposes it at its tail.
    assert replaced_messages == [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "hello"},
    ]
    assert all(not is_frontdoor_tool_contract_message(dict(item)) for item in replaced_messages)
    assert "callable_tools:" not in str([
        item.get("content") for item in replaced_messages if isinstance(item, dict)
    ])


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
    assert config.frontdoor_interrupt_tool_names == ["create_async_task"]


def test_checkpoint_safe_model_response_payload_summarizes_provider_request_body() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    payload = runner._checkpoint_safe_model_response_payload(
        AIMessage(
            content="ok",
            response_metadata={
                "provider_request_meta": {"provider": "responses"},
                "provider_request_body": {
                    "model": "gpt-5.4-mini",
                    "input": [
                        {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/jpeg;base64," + ("A" * 2000),
                                }
                            ],
                        },
                    ],
                    "tools": [{"type": "function", "name": "exec"}],
                    "prompt_cache_key": "cache-key",
                    "tool_choice": "auto",
                },
            },
        )
    )

    body = dict(payload["provider_request_body"] or {})
    assert body["model"] == "gpt-5.4-mini"
    assert body["input_count"] == 2
    assert body["tools_count"] == 1
    assert body["contains_multimodal"] is True
    assert body["prompt_cache_key"] == "cache-key"
    assert "input" not in body
    assert "tools" not in body


def test_persist_frontdoor_actual_request_degrades_after_memory_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    original_dump = json.dump
    call_count = {"value": 0}

    def _flaky_dump(payload, handle, *args, **kwargs):
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise MemoryError()
        return original_dump(payload, handle, *args, **kwargs)

    monkeypatch.setattr(web_ceo_sessions.json, "dump", _flaky_dump)

    image_data = "data:image/png;base64," + ("A" * 4096)
    record = web_ceo_sessions.persist_frontdoor_actual_request(
        "web:shared",
        payload={
            "request_messages": [
                {"role": "system", "content": "SYSTEM"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Please inspect this image"},
                        {"type": "image_url", "image_url": {"url": image_data}},
                    ],
                },
            ],
            "provider_request_body": {
                "model": "gpt-5.4-mini",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Please inspect this image"},
                            {"type": "input_image", "image_url": image_data},
                        ],
                    }
                ],
                "tools": [{"type": "function", "name": "exec"}],
                "prompt_cache_key": "cache-key",
                "tool_choice": "auto",
            },
            "actual_request_hash": "request-hash",
            "actual_request_message_count": 2,
            "actual_tool_schema_hash": "tool-hash",
            "provider_model": "openai:gpt-5.4-mini",
        },
    )

    request_path = Path(str(record["path"]))
    serialized = request_path.read_text(encoding="utf-8")
    payload = json.loads(serialized)

    assert call_count["value"] == 2
    assert payload["artifact_persistence_mode"] == "memory_guard_degraded"
    assert payload["artifact_persistence_reason"] == "memory_error"
    assert payload["request_messages"][1]["content"] == "Please inspect this image"
    assert payload["provider_request_body"] == {}
    assert payload["provider_request_body_summary"]["contains_multimodal"] is True
    assert payload["provider_request_body_summary"]["input_count"] == 1
    assert payload["provider_request_body_summary"]["tools_count"] == 1
    assert "data:image" not in serialized
    assert "messages" not in payload


def test_checkpoint_safe_stable_messages_strip_multimodal_payloads() -> None:
    runner = CeoFrontDoorRunner(loop=SimpleNamespace())
    messages = [
        {"role": "system", "content": "SYSTEM"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + ("B" * 2000)}},
            ],
        },
    ]

    durable = runner._checkpoint_safe_stable_messages(messages)

    assert durable[0]["content"] == "SYSTEM"
    serialized = json.dumps(durable, ensure_ascii=False)
    assert "data:image" not in serialized
    assert "base64" not in serialized


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
        return {"skills": [], "tool_families": [], "tool_names": ["exec"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["exec"],
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
        dict(message)
        for message in list(state_update["dynamic_appendix_messages"] or [])
        if isinstance(message, dict) and is_frontdoor_tool_contract_message(dict(message))
    ]
    assert len(contract_payloads) == 1
    assert "callable_tools: `submit_next_stage`" in str(contract_payloads[0].get("content") or "")
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
        return {"skills": [], "tool_families": [], "tool_names": ["exec"]}

    prompt_variants = iter(
        [
            SimpleNamespace(
                tool_names=["exec"],
                model_messages=[
                    {"role": "system", "content": "SYSTEM PROMPT A"},
                    {"role": "user", "content": "same question"},
                ],
            ),
            SimpleNamespace(
                tool_names=["exec"],
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
    class _ExecTool(Tool):
        @property
        def name(self) -> str:
            return "exec"

        @property
        def description(self) -> str:
            return "run command"

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
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
        tools={"exec": _ExecTool()},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["exec"],
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
    assert str(diagnostics["actual_request_hash"] or "").strip()
    assert diagnostics["actual_request_message_count"] == (
        len(list(state_update["messages"] or []))
        + len(list(state_update["dynamic_appendix_messages"] or []))
    )
    assert str(diagnostics["actual_tool_schema_hash"] or "") == str(diagnostics["tool_signature_hash"] or "")


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
        tools={"exec": _ExecTool()},
        max_iterations=8,
        workspace=tmp_path,
        temp_dir=str(tmp_path / "tmp"),
    )
    runner = CeoFrontDoorRunner(loop=loop)

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["exec"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["exec"],
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
        dict(message)
        for message in list(result["dynamic_appendix_messages"] or [])
        if isinstance(message, dict) and is_frontdoor_tool_contract_message(dict(message))
    ]
    assert len(contract_payloads) == 1
    assert "callable_tools: `submit_next_stage`" in str(contract_payloads[0].get("content") or "")
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
    assert stage["completed_stage_summary"] == ceo_runtime_ops.STAGE_TURN_END_SUMMARY_POINTER
    assert stage["finished_at"]


def test_prune_frontdoor_request_artifacts_keeps_referenced_and_newest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)
    directory = web_ceo_sessions.actual_request_dir_for_session("web:test")
    referenced = directory / "20260101T000000_000000_referenced.json"
    stale = directory / "20260101T000001_000000_stale.json"
    newest = directory / "29991231T000000_000000_newest.json"
    for path in (referenced, stale, newest):
        path.write_text("{}", encoding="utf-8")
    web_ceo_sessions.write_completed_continuity_snapshot(
        "web:test",
        {
            "frontdoor_request_body_messages": [{"role": "user", "content": "hello"}],
            "frontdoor_actual_request_path": str(referenced.resolve()),
            "source_reason": "actual_request_sync",
            "updated_at": "2026-09-01T00:00:00",
        },
    )

    deleted = web_ceo_sessions.prune_frontdoor_actual_request_artifacts("web:test", keep=1)

    assert deleted == 1
    assert referenced.exists()
    assert newest.exists()
    assert not stale.exists()


def test_persist_frontdoor_request_survives_pruning_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)

    def _fail(*args, **kwargs):
        raise RuntimeError("pruning unavailable")

    monkeypatch.setattr(web_ceo_sessions, "prune_frontdoor_actual_request_artifacts", _fail)

    record = web_ceo_sessions.persist_frontdoor_actual_request(
        "web:test",
        payload={
            "created_at": "2026-09-01T00:00:00",
            "request_messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert Path(record["path"]).exists()


def _internal_prompt_message(role: str, content: str, kind: str, *, state: str = "completed") -> dict:
    return {
        "role": role,
        "content": content,
        "metadata": {
            "internal_prompt_kind": kind,
            "prompt_visible": True,
            "ui_visible": False,
            "source": "heartbeat",
            "_transcript_state": state,
        },
    }


def test_fold_internal_prompt_history_collapses_duplicates_keeping_last() -> None:
    rule = "This is a background heartbeat. # Heartbeat Rules"
    bundle_a = "[SESSION EVENTS] node:aaaa error A"
    bundle_b = "[SESSION EVENTS] node:bbbb error B"
    messages: list[dict] = []
    for _ in range(4):
        messages.append(_internal_prompt_message("system", rule, "heartbeat_rule"))
        messages.append(_internal_prompt_message("user", bundle_a, "heartbeat_event_bundle"))
    messages.append({"role": "assistant", "content": "我恢复了节点", "metadata": {}})
    for _ in range(5):
        messages.append(_internal_prompt_message("system", rule, "heartbeat_rule"))
        messages.append(_internal_prompt_message("user", bundle_b, "heartbeat_event_bundle"))

    folded = web_ceo_sessions.fold_internal_prompt_history(messages)

    kinds = [m["metadata"].get("internal_prompt_kind") for m in folded if m.get("metadata")]
    # 9 份相同规则折叠为 1；两种不同事件束各保留最后一份；助手消息原样保留
    assert kinds.count("heartbeat_rule") == 1
    assert kinds.count("heartbeat_event_bundle") == 2
    assert sum(1 for m in folded if m["role"] == "assistant") == 1
    # keep-last：最新事件（bundle_b）位于末尾，当前轮提示词不被前移
    assert folded[-1]["content"] == bundle_b
    # 非内部消息相对顺序不变（assistant 仍在 bundle_a 之后、bundle_b 之前）
    contents = [m["content"] for m in folded]
    assert contents.index(bundle_a) < contents.index("我恢复了节点") < contents.index(bundle_b)


def test_is_prompt_visible_message_excludes_discarded_state() -> None:
    discarded = _internal_prompt_message("user", "[SESSION EVENTS] stale", "heartbeat_event_bundle", state="discarded")
    completed = _internal_prompt_message("user", "[SESSION EVENTS] live", "heartbeat_event_bundle", state="completed")
    assert web_ceo_sessions.is_prompt_visible_message(discarded) is False
    assert web_ceo_sessions.is_prompt_visible_message(completed) is True


def test_prompt_history_messages_drops_discarded_and_folds_internal_prompts() -> None:
    rule = "This is a background heartbeat. # Heartbeat Rules"
    live_bundle = "[SESSION EVENTS] node:live error"
    stale_bundle = "[SESSION EVENTS] node:stale error"
    session = SimpleNamespace(messages=[
        # 失败回合：规则 + 事件束被翻成 discarded，应整体排除
        _internal_prompt_message("system", rule, "heartbeat_rule", state="discarded"),
        _internal_prompt_message("user", stale_bundle, "heartbeat_event_bundle", state="discarded"),
        # 成功回合：规则 + 事件束 completed，应保留并折叠
        _internal_prompt_message("system", rule, "heartbeat_rule"),
        _internal_prompt_message("user", live_bundle, "heartbeat_event_bundle"),
        {"role": "assistant", "content": "处理完成", "metadata": {}},
        # 又一次成功回合：相同规则/事件束，折叠后只留最后一份
        _internal_prompt_message("system", rule, "heartbeat_rule"),
        _internal_prompt_message("user", live_bundle, "heartbeat_event_bundle"),
    ])

    history = web_ceo_sessions.prompt_history_messages(session)

    contents = [m["content"] for m in history]
    assert stale_bundle not in contents  # discarded 被排除
    assert contents.count(rule) == 1  # 规则折叠为 1
    assert contents.count(live_bundle) == 1  # 同一事件束折叠为 1
    assert "处理完成" in contents  # 助手消息保留


def test_fold_recognizes_baseline_messages_without_metadata() -> None:
    """续跑基线只存 {role, content}（无 metadata），折叠须按内容标记识别内部提示词。

    这是 warm 路径堆积的修复点：基线每个请求都从 request_messages 提交（含失败回合），
    provider 长期不可用、同一事件每轮重投时，若不在基线折叠则 bundle 线性堆积。
    """
    rule = "This is a background heartbeat. Do not explain internal mechanics.\n# Heartbeat Rules"
    bundle = "[SESSION EVENTS]\n## EVENT BUNDLE\n- Task X (task:t) has a node paused after an error"
    msgs: list[dict] = []
    for _ in range(5):
        msgs.append({"role": "system", "content": rule})
        msgs.append({"role": "user", "content": bundle})
    msgs.append({"role": "assistant", "content": "处理完成"})

    folded = web_ceo_sessions.fold_internal_prompt_history(msgs)
    assert sum(1 for m in folded if m["role"] == "system") == 1  # 5 份相同规则 → 1
    assert sum(1 for m in folded if m["role"] == "user") == 1  # 5 份相同事件束 → 1
    assert sum(1 for m in folded if m["role"] == "assistant") == 1  # 助手消息保留


def test_fold_does_not_collapse_ordinary_user_messages() -> None:
    """普通用户消息（不以内部前缀开头）即使内容相同也不折叠，避免误删真实对话。"""
    normal = [
        {"role": "user", "content": "请帮我查一下这个文件"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "请帮我查一下这个文件"},  # 与上面相同但非内部提示词
    ]
    folded = web_ceo_sessions.fold_internal_prompt_history(normal)
    assert len(folded) == 3  # 原样保留，不折叠


def _warm_loop_bundle_messages(messages):
    return [
        m for m in messages
        if m.get("role") == "user" and str(m.get("content") or "").lstrip().startswith("[SESSION EVENTS]")
    ]


def _warm_loop_rule_messages(messages):
    return [
        m for m in messages
        if m.get("role") == "system" and str(m.get("content") or "").lstrip().startswith("This is a background heartbeat")
    ]


def test_warm_path_baseline_bundle_count_stays_bounded_across_failed_heartbeats() -> None:
    """集成：模拟 provider 永久不可用、同一节点事件连续 10 轮重投（每轮模型调用失败）。

    warm 路径的累积循环：每轮请求体 = 上一轮续跑基线（checkpoint）+ 本轮注入（heartbeat
    规则 system + 事件束 user），随后 _persist_frontdoor_actual_request 在**每个请求**
    （含失败回合）把请求体经 _durable_frontdoor_request_body_messages 提交回基线。
    折叠按内容标记识别（基线无 metadata），同一事件的 bundle 不应随轮数增长。
    """
    helpers = ceo_runtime_ops.CeoFrontDoorRuntimeOps
    rule = "This is a background heartbeat.\n# Heartbeat Rules"
    bundle = (
        "[SESSION EVENTS]\n## EVENT BUNDLE\n"
        "- Task 日报 (task:26e64d1dc8b3) has a node paused after an error\n"
        "  Node: node:85b3119ce0ac\n  Error: RateLimitError: 429"
    )

    baseline: list[dict] = []
    for round_index in range(10):
        request_body = list(baseline) + [
            {"role": "system", "content": rule},
            {"role": "user", "content": bundle},
        ]
        # 模型实际看到的请求体：事件束/规则有界（首轮 1，之后"基线折叠副本 + 当前注入"= 2，不再增长）
        assert len(_warm_loop_bundle_messages(request_body)) <= 2, (
            f"第 {round_index + 1} 轮请求体事件束数应 ≤2，实际 {len(_warm_loop_bundle_messages(request_body))}"
        )
        assert len(_warm_loop_rule_messages(request_body)) <= 2
        # 提交回基线（真实函数，chokepoint 折叠）
        baseline = helpers._durable_frontdoor_request_body_messages(request_body)
        assert len(_warm_loop_bundle_messages(baseline)) == 1, (
            f"第 {round_index + 1} 轮基线事件束数应 =1，实际 {len(_warm_loop_bundle_messages(baseline))}"
        )
    # 10 轮失败后基线仍只有 1 份事件束，而非逐轮堆积
    assert len(_warm_loop_bundle_messages(baseline)) == 1


def test_warm_path_baseline_keeps_distinct_events_but_collapses_redispatches() -> None:
    """不同事件（内容不同）各自保留最后一份；同一事件重复重投折叠到 1 份，不混叠。"""
    helpers = ceo_runtime_ops.CeoFrontDoorRuntimeOps
    rule = "This is a background heartbeat."
    bundle_a = "[SESSION EVENTS]\n## EVENT BUNDLE\n- node A error: 429 quota"
    bundle_b = "[SESSION EVENTS]\n## EVENT BUNDLE\n- node B error: 400 bad request"

    baseline: list[dict] = []
    # 事件 A 连续重投 3 次（同一内容）→ 折叠到 1
    for _ in range(3):
        request_body = list(baseline) + [{"role": "system", "content": rule}, {"role": "user", "content": bundle_a}]
        baseline = helpers._durable_frontdoor_request_body_messages(request_body)
    assert len(_warm_loop_bundle_messages(baseline)) == 1

    # 事件 B 出现 → 基线保留两个不同事件
    baseline = helpers._durable_frontdoor_request_body_messages(
        list(baseline) + [{"role": "user", "content": bundle_b}]
    )
    contents = [str(m.get("content")) for m in _warm_loop_bundle_messages(baseline)]
    assert contents.count(bundle_a) == 1 and contents.count(bundle_b) == 1 and len(contents) == 2

    # 事件 B 再次重投 → 仍不增长
    baseline = helpers._durable_frontdoor_request_body_messages(
        list(baseline) + [{"role": "user", "content": bundle_b}]
    )
    assert len(_warm_loop_bundle_messages(baseline)) == 2


