"""CEO 前门：阶段调用被拒时同批普通调用的作废判据（与节点道同一条）。

节点道的说明见 `test_stage_batch_invalidation.py`；这里只证前门用的是同一事实判据：
`mutable_stage_state` 一字未动 ⇒ 本批普通调用照跑；动过 ⇒ 照旧作废。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from main.runtime.internal_tools import STAGE_TOOL_NAME


class _ProbeTool(Tool):
    @property
    def name(self) -> str:
        return "probe_tool"

    @property
    def description(self) -> str:
        return "probe"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        _ = kwargs
        return "PROBE-RAN"


def _stage_state() -> dict:
    return {
        "active_stage_id": "frontdoor-stage-1",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "mode": "自主执行",
                "status": "active",
                "stage_goal": "跑实验",
                "completed_stage_summary": "",
                "tool_round_budget": 6,
                "tool_rounds_used": 1,
                "key_refs": [],
                "rounds": [],
                "created_at": "2026-10-09T06:36:27+08:00",
                "finished_at": "",
            }
        ],
    }


async def _run_batch(monkeypatch, *, mutate_before_failing: bool) -> list[str]:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"probe_tool": _ProbeTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    def _fake_submit_next_stage(stage_state, *args, **kwargs):
        _ = args, kwargs
        if mutate_before_failing:
            stages = stage_state.setdefault("stages", [])
            stages.append(
                {
                    "stage_id": "frontdoor-stage-2",
                    "stage_index": len(stages) + 1,
                    "stage_kind": "normal",
                    "mode": "自主执行",
                    "status": "active",
                    "stage_goal": "新阶段",
                    "completed_stage_summary": "",
                    "tool_round_budget": 4,
                    "tool_rounds_used": 0,
                    "key_refs": [],
                    "rounds": [],
                    "created_at": "2026-10-09T06:37:00+08:00",
                    "finished_at": "",
                }
            )
            stage_state["active_stage_id"] = "frontdoor-stage-2"
        # 两种情形都在提交处失败：区别只在于账本是否已被改过
        raise ValueError("drop_completed_stage_tool_detail requires a non-empty completed_stage_summary")

    monkeypatch.setattr(runner, "_frontdoor_submit_next_stage", _fake_submit_next_stage)

    async def _on_progress(content: str, *, event_kind=None, event_data=None, **kwargs):
        _ = content, event_kind, event_data, kwargs

    async def _fake_execute(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = runtime_context, on_progress, tool_call_id
        try:
            raw = await tool.execute(**dict(arguments or {}))
        except Exception as exc:
            # 真执行器把异常折成 error 回执，这里同形：两种情形都以 error 结束本条调用
            text = f"Error executing {tool_name}: {type(exc).__name__}: {exc}"
            return (None, text, "error", "", "", 0.1)
        import json as _json

        text = raw if isinstance(raw, str) else _json.dumps(raw, ensure_ascii=False)
        return (raw, text, "success", "", "", 0.1)

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute)

    result = await runner._graph_execute_tools(
        {
            "session_key": "web:shared",
            "messages": [
                {"role": "system", "content": "SYSTEM"},
                {"role": "user", "content": "本轮问题"},
            ],
            "tool_names": ["exec", STAGE_TOOL_NAME, "probe_tool", "load_tool_context"],
            "candidate_tool_names": [],
            "candidate_tool_items": [],
            "hydrated_tool_names": [],
            "rbac_visible_tool_names": ["exec", STAGE_TOOL_NAME, "probe_tool"],
            "visible_skill_ids": [],
            "candidate_skill_ids": [],
            "rbac_visible_skill_ids": [],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": False,
            "max_parallel_tool_calls": 1,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": "", "tool_calls": []},
            "frontdoor_request_body_messages": [],
            "frontdoor_history_shrink_reason": "",
            "frontdoor_stage_state": _stage_state(),
            "tool_call_payloads": [
                {"id": "call-stage", "name": STAGE_TOOL_NAME, "arguments": {"stage_goal": "g", "tool_round_budget": 4}},
                {"id": "call-probe", "name": "probe_tool", "arguments": {}},
            ],
        },
        runtime=SimpleNamespace(context=SimpleNamespace(session=SimpleNamespace(
            state=SimpleNamespace(session_key="web:shared"), _frontdoor_provider_tool_schema_names=[],
        ))),
    )
    contents = [str(item.get("content") or "") for item in list(result.get("messages") or []) if item.get("role") == "tool"]
    return contents


@pytest.mark.asyncio
async def test_frontdoor_rejected_stage_transition_keeps_the_batch(monkeypatch) -> None:
    contents = await _run_batch(monkeypatch, mutate_before_failing=False)
    stage_row = next((c for c in contents if "submit_next_stage" in c or c.startswith("Error")), "")
    assert stage_row.startswith("Error")
    assert any("PROBE-RAN" in c for c in contents), contents
    assert not any("failed earlier in this batch" in c for c in contents)


@pytest.mark.asyncio
async def test_frontdoor_voids_batch_only_after_the_ledger_moved(monkeypatch) -> None:
    contents = await _run_batch(monkeypatch, mutate_before_failing=True)
    assert any("failed earlier in this batch" in c for c in contents), contents
    assert not any("PROBE-RAN" in c for c in contents)
