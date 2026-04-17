from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor import prompt_cache_contract
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.frontdoor.state_models import CeoRuntimeContext, initial_persistent_state


def _loop_with_session(session_key: str):
    runtime_session = SimpleNamespace(session_key=session_key, messages=[])
    return SimpleNamespace(
        sessions=SimpleNamespace(get_or_create=lambda key: runtime_session),
        main_task_service=None,
        tools={},
        max_iterations=8,
        workspace=None,
        temp_dir="",
    )


@pytest.mark.asyncio
async def test_prepare_turn_reuses_session_context_window_for_new_user_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    loop = _loop_with_session(session_key)
    runner = CeoFrontDoorRunner(loop=loop)
    captured: dict[str, object] = {}

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["message", "submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        captured.update(kwargs)
        seed_messages = list(kwargs.get("request_body_seed_messages") or kwargs.get("checkpoint_messages") or [])
        model_messages = list(seed_messages)
        if str(kwargs.get("user_content") or "").strip():
            model_messages.append({"role": "user", "content": kwargs["user_content"]})
        return SimpleNamespace(
            tool_names=["message", "submit_next_stage"],
            model_messages=model_messages,
            stable_messages=list(seed_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["message", "submit_next_stage"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

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
        ],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={
            "active_stage_id": "frontdoor-stage-1",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_index": 1,
                    "stage_goal": "Keep working",
                    "tool_round_budget": 8,
                    "tool_rounds_used": 2,
                    "status": "active",
                    "rounds": [],
                }
            ],
        },
        _frontdoor_canonical_context={
            "active_stage_id": "",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-0",
                    "stage_index": 0,
                    "stage_goal": "Older context",
                    "tool_round_budget": 8,
                    "tool_rounds_used": 2,
                    "status": "completed",
                    "rounds": [],
                }
            ],
        },
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "new question", "metadata": {}}),
        runtime=runtime,
    )

    assert captured["checkpoint_messages"] == []
    assert captured["request_body_seed_messages"] == session._frontdoor_request_body_messages


@pytest.mark.asyncio
async def test_prepare_turn_passes_session_request_body_as_direct_continuation_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    loop = _loop_with_session(session_key)
    runner = CeoFrontDoorRunner(loop=loop)
    captured: dict[str, object] = {}

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["message", "submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        captured.update(kwargs)
        seed_messages = list(kwargs.get("request_body_seed_messages") or [])
        checkpoint_messages = list(kwargs.get("checkpoint_messages") or [])
        model_messages = [*seed_messages, *checkpoint_messages]
        if str(kwargs.get("user_content") or "").strip():
            model_messages.append({"role": "user", "content": kwargs["user_content"]})
        return SimpleNamespace(
            tool_names=["message", "submit_next_stage"],
            model_messages=model_messages,
            stable_messages=list(seed_messages),
            dynamic_appendix_messages=[],
            candidate_tool_names=[],
            candidate_tool_items=[],
            trace={
                "selected_skills": [],
                "semantic_frontdoor": {},
                "tool_selection": {},
                "capability_snapshot": {
                    "visible_tool_ids": ["message", "submit_next_stage"],
                    "visible_skill_ids": [],
                },
            },
            cache_family_revision="frontdoor:v1",
            turn_overlay_text="",
        )

    monkeypatch.setattr(runner._resolver, "resolve_for_actor", _resolve_for_actor)
    monkeypatch.setattr(runner._builder, "build_for_ceo", _build_for_ceo)
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

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
        ],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={},
        _compression_state={"status": "", "text": "", "source": "", "needs_recheck": False},
        _semantic_context_state={"summary_text": "", "needs_refresh": False},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    await runner._graph_prepare_turn(
        initial_persistent_state(user_input={"content": "new question", "metadata": {}}),
        runtime=runtime,
    )

    assert captured["checkpoint_messages"] == []
    assert captured["request_body_seed_messages"] == session._frontdoor_request_body_messages


@pytest.mark.asyncio
async def test_finalize_turn_preserves_authoritative_request_body_baseline() -> None:
    runner = CeoFrontDoorRunner(loop=_loop_with_session("web:shared"))
    state = {
        "query_text": "continue",
        "route_kind": "direct_reply",
        "final_output": "final answer",
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "latest user"},
        ],
        "frontdoor_request_body_messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "older user"},
            {"role": "assistant", "content": "older answer"},
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
        ],
        "frontdoor_history_shrink_reason": "stage_compaction",
        "frontdoor_stage_state": {},
        "frontdoor_canonical_context": {"active_stage_id": "", "transition_required": False, "stages": []},
    }

    finalized = await runner._graph_finalize_turn(state)

    assert finalized["frontdoor_request_body_messages"] == [
        *state["frontdoor_request_body_messages"],
        {"role": "assistant", "content": "final answer"},
    ]
    assert finalized["frontdoor_history_shrink_reason"] == "stage_compaction"


@pytest.mark.asyncio
async def test_prepare_turn_rejects_unexpected_context_shrink_without_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "web:shared"
    loop = _loop_with_session(session_key)
    runner = CeoFrontDoorRunner(loop=loop)

    monkeypatch.setattr(ceo_runtime_ops, "current_project_environment", lambda workspace_root=None: {})
    monkeypatch.setattr(prompt_cache_contract, "build_session_prompt_cache_key", lambda **kwargs: "cache-key")

    async def _resolve_for_actor(*, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return {"skills": [], "tool_families": [], "tool_names": ["submit_next_stage"]}

    async def _build_for_ceo(**kwargs):
        _ = kwargs
        return SimpleNamespace(
            tool_names=["submit_next_stage"],
            model_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "new question"},
            ],
            stable_messages=[
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "new question"},
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
    monkeypatch.setattr(runner, "_resolve_ceo_model_refs", lambda: ["openai_codex:gpt-test"])

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
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "tool", "name": "exec", "tool_call_id": "call-1", "content": '{"status":"success"}'},
        ],
        _frontdoor_history_shrink_reason="",
        _frontdoor_stage_state={},
        _frontdoor_canonical_context={"active_stage_id": "", "transition_required": False, "stages": []},
        _compression_state={},
        _semantic_context_state={},
        _frontdoor_hydrated_tool_names=[],
        _frontdoor_selection_debug={},
    )
    runtime = SimpleNamespace(
        context=CeoRuntimeContext(loop=loop, session=session, session_key=session_key, on_progress=None)
    )

    with pytest.raises(RuntimeError, match="frontdoor context shrank without an allowed reason"):
        await runner._graph_prepare_turn(
            initial_persistent_state(user_input={"content": "new question", "metadata": {}}),
            runtime=runtime,
        )
