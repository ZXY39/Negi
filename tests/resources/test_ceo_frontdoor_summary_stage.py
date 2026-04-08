from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner


@pytest.mark.asyncio
async def test_frontdoor_postprocess_tool_cycle_drops_summary_fields(monkeypatch) -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))

    async def _fake_summarize_messages(*, messages, state):
        _ = state
        return {"messages": list(messages)}

    monkeypatch.setattr(runner, "_summarize_messages", _fake_summarize_messages)

    result = await runner._postprocess_completed_tool_cycle(
        state={
            "tool_call_payloads": [{"id": "call-1", "name": "message", "arguments": {"text": "hello"}}],
            "messages": [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1", "name": "message"}]},
                {"role": "tool", "tool_call_id": "call-1", "name": "message", "content": "sent", "status": "success"},
            ],
            "used_tools": [],
            "route_kind": "direct_reply",
            "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        }
    )

    assert result is not None
    assert "summary_text" not in result
    assert "summary_payload" not in result
    assert "summary_model_key" not in result


def test_frontdoor_build_prompt_context_uses_stage_overlay_only() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    context = runner.build_prompt_context(
        state={
            "summary_text": "legacy summary should be ignored",
            "frontdoor_stage_state": {
                "active_stage_id": "frontdoor-stage-1",
                "transition_required": True,
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_index": 1,
                        "stage_goal": "Inspect the request",
                        "tool_round_budget": 1,
                        "tool_rounds_used": 1,
                        "status": "active",
                        "mode": "自主执行",
                        "completed_stage_summary": "",
                        "key_refs": [],
                        "rounds": [],
                    }
                ],
            },
        },
        runtime=None,
        tools=[],
    )

    assert "legacy summary should be ignored" not in context["system_overlay"]
    assert "Inspect the request" in context["system_overlay"]
