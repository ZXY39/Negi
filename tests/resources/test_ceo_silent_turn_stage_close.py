"""静默轮收尾时收口自己的活动阶段，但把总结槽留给下一次 `submit_next_stage`。

三条设计约束在此钉住：

1. **静默轮不再把活动阶段留给下一轮。** 阶段收尾原先只挂在"本轮有可见正文"上
   （`_ceo_runtime_ops.py` 里三处 `visible_output` 分支），静默轮一条都不走，未收口的
   阶段照样被写进 durable 账本，下一轮继续往同一个 `stage_id` 里长轮 —— 网页上就是
   "静默消息中创建的阶段叠到后面正常响应的阶段"。
2. **收口不写总结。** `completed_stage_summary` 由下一次 `submit_next_stage` 书写，
   静默时先把 `reason` 填进去等于用"为什么这轮不出声"永久顶掉"这条阶段做成了什么"。
3. **理由不丢。** 完整 `reason` 落在 silent 那一轮的 `tools[].arguments.reason`，模型
   未给正文时静默行的可见正文也退回 `reason`，所以不需要第二个真相源。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from main.runtime.internal_tools import SilentTool
from main.runtime.stage_budget import SILENT_TOOL_NAME

_REASON = "17:49 已汇报过同一份 CSV，本轮无新增结论"

_EMPTY_STAGE_STATE = {
    "active_stage_id": "",
    "transition_required": False,
    "stages": [],
}

_WORKING_STAGE_STATE = {
    "active_stage_id": "frontdoor-stage-1",
    "transition_required": False,
    "stages": [
        {
            "stage_id": "frontdoor-stage-1",
            "stage_index": 1,
            "stage_goal": "核对桌面文件数并等待用户追问",
            "tool_round_budget": 10,
            "tool_rounds_used": 1,
            "status": "active",
            "mode": "自主执行",
            "stage_kind": "normal",
            "completed_stage_summary": "",
            "key_refs": [],
            "rounds": [
                {
                    "round_id": "frontdoor-stage-1:round-1",
                    "round_index": 1,
                    "budget_counted": True,
                    "tool_names": ["exec"],
                    "tools": [
                        {
                            "tool_call_id": "call-exec-1",
                            "tool_name": "exec",
                            "status": "success",
                            "arguments": {"command": "ls ~/Desktop | wc -l"},
                            "output_text": "42",
                        }
                    ],
                }
            ],
        }
    ],
}


class _DemoTool(Tool):
    @property
    def name(self) -> str:
        return "demo_tool"

    @property
    def description(self) -> str:
        return "demo tool"

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}

    async def execute(self, **kwargs):
        return kwargs


def _runner() -> create_agent_impl.CreateAgentCeoFrontDoorRunner:
    return create_agent_impl.CreateAgentCeoFrontDoorRunner(
        loop=SimpleNamespace(
            tools=SimpleNamespace(
                push_runtime_context=lambda context: object(),
                pop_runtime_context=lambda token: None,
            )
        )
    )


def _silent_payload() -> dict:
    return {
        "id": "call-silent-1",
        "name": SILENT_TOOL_NAME,
        "arguments": {"reason": _REASON, "subject": "task:543e0f15d798", "superseded_by": "task:9771d6c5469d"},
    }


async def _finalize(stage_state: dict, *, silent: bool, output: str = "痕迹正文") -> dict:
    runner = _runner()
    state = dict(initial_persistent_state(user_input={"content": "x", "metadata": {}}))
    state.update(
        {
            "final_output": output,
            "silent_reply": silent,
            "silent_reason": _REASON if silent else "",
            "route_kind": "direct_reply",
            "messages": [{"role": "user", "content": "x"}],
            "frontdoor_request_body_messages": [],
            "frontdoor_stage_state": json.loads(json.dumps(stage_state)),
            "heartbeat_internal": True,
        }
    )
    return await runner._graph_finalize_turn(state)


def _stage(result: dict, stage_id: str = "frontdoor-stage-1") -> dict:
    for stage in list(result.get("frontdoor_stage_state", {}).get("stages") or []):
        if str(stage.get("stage_id") or "") == stage_id:
            return stage
    raise AssertionError(f"stage {stage_id} missing in {result.get('frontdoor_stage_state')}")


@pytest.mark.asyncio
async def test_silent_turn_closes_its_active_stage() -> None:
    """静默轮把当时的活动阶段收口，后面的回合不能再往这条卡里长轮。"""
    result = await _finalize(_WORKING_STAGE_STATE, silent=True)

    assert result["silent_reply"] is True
    stage_state = result["frontdoor_stage_state"]
    assert str(stage_state.get("active_stage_id") or "") == ""
    stage = _stage(result)
    assert str(stage.get("status")) == "completed"
    assert str(stage.get("finished_at") or "") != ""
    # 合并进 durable 的那份同样是终态：静默行的轨道卡自带"完成"，
    # 不再依赖前端逐行副本的状态回填。
    durable = [
        item
        for item in list(result["frontdoor_canonical_context"].get("stages") or [])
        if str(item.get("stage_goal") or "") == str(stage.get("stage_goal") or "")
    ]
    assert durable and all(str(item.get("status")) == "completed" for item in durable)


@pytest.mark.asyncio
async def test_silent_closure_leaves_the_summary_slot_empty() -> None:
    """否决过的形态，重提时必须先过这条：`reason` 不进 `completed_stage_summary`。"""
    result = await _finalize(_WORKING_STAGE_STATE, silent=True)

    assert str(_stage(result).get("completed_stage_summary") or "") == ""


@pytest.mark.asyncio
async def test_silent_turn_without_an_active_stage_creates_no_phantom_stage() -> None:
    """`silent` 不需要活动阶段（tool_contract 的常驻控制工具），无阶段时收口必须 no-op。"""
    result = await _finalize(_EMPTY_STAGE_STATE, silent=True)

    assert result["silent_reply"] is True
    assert list(result["frontdoor_stage_state"].get("stages") or []) == []
    assert list(result["frontdoor_canonical_context"].get("stages") or []) == []


@pytest.mark.asyncio
async def test_visible_turn_closure_is_unchanged() -> None:
    """负向：不碰静默标志时收尾路径与改前逐字一致，同样不写指针摘要。"""
    result = await _finalize(_WORKING_STAGE_STATE, silent=False)

    assert result["silent_reply"] is False
    assert str(result["frontdoor_stage_state"].get("active_stage_id") or "") == ""
    stage = _stage(result)
    assert str(stage.get("status")) == "completed"
    assert str(stage.get("completed_stage_summary") or "") == ""


@pytest.mark.asyncio
async def test_reason_survives_on_the_silent_round(monkeypatch) -> None:
    """理由的去处：silent 那一轮的 `tools[].arguments.reason` 是全文。

    收口不再写总结槽，所以这条必须成立，否则静默轮的"为什么"就只剩前端折叠行里
    那句退回正文。
    """
    runner = _runner()

    async def _fake_execute(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, runtime_context, tool_call_id, on_progress
        return ({"silenced": True}, json.dumps({"silenced": True}), "success", "", "", 0.1)

    monkeypatch.setattr(
        runner, "_registered_tools_for_state", lambda state: {"demo_tool": _DemoTool(), SILENT_TOOL_NAME: SilentTool()}
    )
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {})
    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute)

    async def _emit(*args, **kwargs):
        return None

    monkeypatch.setattr(runner, "_emit_progress", _emit)

    state = dict(initial_persistent_state(user_input={"content": "x", "metadata": {}}))
    state.update(
        {
            "tool_call_payloads": [_silent_payload()],
            "messages": [],
            "used_tools": [],
            "route_kind": "direct_reply",
            "parallel_enabled": True,
            "max_parallel_tool_calls": 4,
            "synthetic_tool_calls_used": False,
            "response_payload": {"content": _REASON, "tool_calls": []},
            "analysis_text": _REASON,
            "heartbeat_internal": True,
            "frontdoor_stage_state": json.loads(json.dumps(_WORKING_STAGE_STATE)),
        }
    )
    executed = await runner._graph_execute_tools(state, runtime=SimpleNamespace(context=SimpleNamespace()))
    assert executed["silent_reply"] is True

    closed = await _finalize(executed["frontdoor_stage_state"], silent=True)
    stage = _stage(closed)
    round_tools = [
        tool
        for round_item in list(stage.get("rounds") or [])
        for tool in list(round_item.get("tools") or [])
        if isinstance(tool, dict) and str(tool.get("tool_name") or "") == SILENT_TOOL_NAME
    ]
    assert round_tools, "silent 调用没有进阶段轮次"
    assert str(round_tools[-1]["arguments"]["reason"]) == _REASON
    assert str(stage.get("completed_stage_summary") or "") == ""
