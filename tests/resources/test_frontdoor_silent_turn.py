"""P2：`silent` 工具真正生效 —— 批次跑完后以静默收尾，判据进转录供后续轮次读取。

三条设计约束在此钉住：

1. **工具即终态。** 前门此前没有先例（`submit_final_result` 只存在于节点侧），
   `_graph_execute_tools` 原本唯一的出口是 `call_model`。若沿用旧路，模型调完 silent
   还得再说一句话才能收尾 —— 要求模型"说话才能不说"正是这次要换掉的东西。
2. **痕迹必须回得到模型。** 工具静默时那行 assistant 记录 `prompt_visible=True`，
   否则下一轮的模型看不见"我上次对哪个任务选了静默"，也就无从反悔；而取消机器闸门
   之后这是唯一的兜底。
3. **正文不能被写成两份。** execute_tools 已把随工具一起给出的正文放进带 tool_calls
   的 assistant 行进了基线，finalize 再 append 一次就是同文两份。

旧的文案哨兵在本阶段**仍然并行有效**（P4 才删），所以这里也钉一条"哨兵照旧被认出"
的回归，防止两个 commit 之间出现两条出口都不认的窗口。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor import _ceo_runtime_ops as ceo_runtime_ops
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.runtime.web_ceo_sessions import is_prompt_visible_message, is_ui_visible_message
from main.runtime.internal_tools import SilentTool
from main.runtime.stage_budget import SILENT_TOOL_NAME

_STAGE_STATE = {
    "active_stage_id": "frontdoor-stage-1",
    "transition_required": False,
    "stages": [
        {
            "stage_id": "frontdoor-stage-1",
            "stage_index": 1,
            "stage_goal": "verify",
            "tool_round_budget": 3,
            "tool_rounds_used": 0,
            "status": "active",
            "mode": "自主执行",
            "stage_kind": "normal",
            "completed_stage_summary": "",
            "key_refs": [],
            "rounds": [],
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


def _silent_payload(reason: str = "17:49 已汇报过同一份 CSV", subject: str = "task:543e0f15d798") -> dict:
    return {
        "id": "call-silent-1",
        "name": SILENT_TOOL_NAME,
        "arguments": {"reason": reason, "subject": subject, "superseded_by": "task:9771d6c5469d"},
    }


async def _execute_tools(monkeypatch, *, payloads, response_content=""):
    runner = _runner()
    executed: list[str] = []

    async def _fake_execute(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool, runtime_context, tool_call_id
        executed.append(tool_name)
        return ({"ok": True}, json.dumps({"ok": True}), "success", "", "", 0.1)

    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {"demo_tool": _DemoTool(), SILENT_TOOL_NAME: SilentTool()})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {})
    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute)
    monkeypatch.setattr(runner, "_emit_progress", lambda *args, **kwargs: None)

    async def _emit_progress(*args, **kwargs):
        return None

    monkeypatch.setattr(runner, "_emit_progress", _emit_progress)

    state = {
        "tool_call_payloads": payloads,
        "messages": [],
        "used_tools": [],
        "route_kind": "direct_reply",
        "parallel_enabled": True,
        "max_parallel_tool_calls": 4,
        "synthetic_tool_calls_used": False,
        "response_payload": {"content": response_content, "tool_calls": []},
        "frontdoor_stage_state": json.loads(json.dumps(_STAGE_STATE)),
    }
    result = await runner._graph_execute_tools(state, runtime=SimpleNamespace(context=SimpleNamespace()))
    return result, executed


@pytest.mark.asyncio
async def test_batch_with_silent_ends_the_turn_after_running_the_rest(monkeypatch) -> None:
    """口径：同批其余工具照常跑完，然后本轮以静默收尾，不回模型。"""
    result, executed = await _execute_tools(
        monkeypatch,
        payloads=[{"id": "call-a", "name": "demo_tool", "arguments": {"value": "alpha"}}, _silent_payload()],
        response_content="这份 CSV 的链接已在 17:49 那轮汇报过了。",
    )
    assert executed == ["demo_tool", SILENT_TOOL_NAME]
    assert result["next_step"] == "finalize"
    assert result["silent_reply"] is True
    assert result["final_output"] == "这份 CSV 的链接已在 17:49 那轮汇报过了。"
    assert result["silent_superseded_by"] == "task:9771d6c5469d"


@pytest.mark.asyncio
async def test_silent_alone_still_terminates_and_falls_back_to_reason(monkeypatch) -> None:
    result, executed = await _execute_tools(monkeypatch, payloads=[_silent_payload()], response_content="")
    assert executed == [SILENT_TOOL_NAME]
    assert result["next_step"] == "finalize"
    assert result["final_output"] == "17:49 已汇报过同一份 CSV"


@pytest.mark.asyncio
async def test_batch_without_silent_still_loops_back_to_the_model(monkeypatch) -> None:
    """负向：不碰没有 silent 的批次，收尾路径与改前逐字一致。"""
    result, _executed = await _execute_tools(
        monkeypatch, payloads=[{"id": "call-a", "name": "demo_tool", "arguments": {"value": "alpha"}}]
    )
    assert result["next_step"] == "call_model"
    assert "silent_reply" not in result


@pytest.mark.asyncio
async def test_finalize_carries_the_tool_signal_and_does_not_duplicate_the_text(monkeypatch) -> None:
    """基线里只能有一份正文：带 tool_calls 的那行已经带了，finalize 不能再 append。"""
    runner = _runner()
    accompanying = "结论同上，无需再报。"
    state = dict(initial_persistent_state(user_input={"content": "x", "metadata": {}}))
    state.update(
        {
            "final_output": accompanying,
            "silent_reply": True,
            "silent_reason": "已被 17:49 覆盖",
            "route_kind": "direct_reply",
            "messages": [
                {"role": "user", "content": "x"},
                {
                    "role": "assistant",
                    "content": accompanying,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": SILENT_TOOL_NAME, "arguments": "{}"}}],
                },
                {"role": "tool", "tool_call_id": "c1", "content": '{"silenced": true}'},
            ],
            "frontdoor_request_body_messages": [],
            "frontdoor_stage_state": json.loads(json.dumps(_STAGE_STATE)),
            "heartbeat_internal": True,
        }
    )
    result = await runner._graph_finalize_turn(state)

    assert result["silent_reply"] is True
    assert result["silent_reason"] == "已被 17:49 覆盖"
    assert result["final_output"] == accompanying
    body = result.get("frontdoor_request_body_messages") or []
    duplicated = [item for item in body if str(item.get("content") or "") == accompanying and not item.get("tool_calls")]
    assert duplicated == []


@pytest.mark.asyncio
async def test_legacy_text_sentinel_is_still_recognised_until_p4() -> None:
    """P2/P4 之间两条出口并存 —— 这条回归在 P4 删除哨兵时一并移除。"""
    runner = _runner()
    state = dict(initial_persistent_state(user_input={"content": "x", "metadata": {}}))
    state.update(
        {
            "final_output": "[G3KU_SILENT]",
            "route_kind": "direct_reply",
            "messages": [{"role": "user", "content": "x"}],
            "frontdoor_request_body_messages": [],
            "frontdoor_stage_state": json.loads(json.dumps(_STAGE_STATE)),
            "heartbeat_internal": True,
        }
    )
    result = await runner._graph_finalize_turn(state)
    assert result["silent_reply"] is True


def test_session_resolves_silent_from_tool_signal_or_legacy_token() -> None:
    agent = RuntimeAgentSession.__new__(RuntimeAgentSession)
    assert agent._resolve_silent_reply("普通回复") is False
    assert agent._resolve_silent_reply("[G3KU_SILENT]") is True
    setattr(agent, "_last_silent_reply", True)
    assert agent._resolve_silent_reply("随工具一起给出的正文") is True


def test_tool_silent_trace_row_is_visible_to_model_and_hidden_from_delivery() -> None:
    """痕迹行的两个 flag 就是本阶段的全部合同。"""
    tool_trace = {
        "role": "assistant",
        "content": "这份结果已被更晚的一轮覆盖，本轮不外发。",
        "metadata": {"prompt_visible": True, "ui_visible": True, "silent_reply": True, "silent_reason": "已被 17:49 覆盖"},
    }
    legacy = {"role": "assistant", "content": "", "metadata": {"prompt_visible": False, "ui_visible": True, "silent_reply": True}}
    assert is_prompt_visible_message(tool_trace) is True
    assert is_prompt_visible_message(legacy) is False
    # 投递侧不看转录，只看 message_end 上的 silent_reply flag（见 heartbeat :1783 与
    # external_events），所以 ui_visible=True 不等于会外发。
    assert is_ui_visible_message(tool_trace) is True


# ---------- P3：痕迹必须活过两条压缩车道（实盘裸 tool_call 行存活率仅 6%） ----------


def _assistant_with_calls(call_id: str, name: str, *, content: str = "") -> dict:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}],
    }


def _stage(index: int, *, tool_call_ids: list[str] | None = None) -> dict:
    stage = {
        "stage_id": f"frontdoor-stage-{index}",
        "stage_index": index,
        "stage_goal": f"目标 {index}",
        "status": "completed",
        "stage_kind": "normal",
        "mode": "自主执行",
        "completed_stage_summary": f"结论 {index}",
        "created_at": f"2026-09-20T0{index}:00:00+08:00",
        "finished_at": f"2026-09-20T0{index}:00:30+08:00",
        "key_refs": [],
        "rounds": [],
    }
    if tool_call_ids:
        stage["rounds"] = [{"round_index": 1, "tool_call_ids": list(tool_call_ids), "tools": []}]
    return stage


def test_stage_compaction_keeps_the_silent_row_it_would_otherwise_expire() -> None:
    """同一个已过期阶段里的两行：普通工具行照删，silent 行必须留下。

    只留 assistant 行就够 —— 配对的 tool 结果行按 remove_flags 成对处理，父行不删
    则结果行也不会被单独删，不产生 provider 孤儿。
    """
    from g3ku.runtime.stage_prompt_compaction import compact_stage_prompt_messages_in_place

    ledger = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            _stage(1, tool_call_ids=["call-exec", "call-silent"]),
            _stage(2),
            _stage(3),
            _stage(4),
        ],
    }
    messages = [
        {"role": "system", "content": "基础提示"},
        {"role": "user", "content": "最早的问题"},
        _assistant_with_calls("call-exec", "exec", content="我看一下"),
        {"role": "tool", "tool_call_id": "call-exec", "content": "ok"},
        _assistant_with_calls("call-silent", SILENT_TOOL_NAME, content="这份结果已被 17:49 那轮覆盖。"),
        {"role": "tool", "tool_call_id": "call-silent", "content": '{"silenced": true}'},
        {"role": "user", "content": "最新问题"},
        {"role": "assistant", "content": "最新回答"},
    ]
    parts = compact_stage_prompt_messages_in_place(messages, stage_state=ledger, keep_latest_completed_stages=3)
    body = [*parts["prefix"], *parts["rewritten"]]
    surviving = [str((item.get("tool_calls") or [{}])[0].get("id")) for item in body if item.get("tool_calls")]

    assert "call-exec" not in surviving, "普通工具行本该随过期阶段一起删"
    assert "call-silent" in surviving, "静默痕迹被阶段压缩裁掉了"
    assert [item for item in body if item.get("role") == "tool" and item.get("tool_call_id") == "call-exec"] == []
    assert [item for item in body if item.get("tool_call_id") == "call-silent"] != []


def test_token_lane_lift_moves_the_group_whole_and_leaves_no_orphans() -> None:
    remaining, preserved = ceo_runtime_ops.CeoFrontDoorRuntimeOps._lift_silent_trace_groups(
        [
            _assistant_with_calls("call-a", "exec"),
            {"role": "tool", "tool_call_id": "call-a", "content": "ok"},
            _assistant_with_calls("call-s", SILENT_TOOL_NAME, content="痕迹正文"),
            {"role": "tool", "tool_call_id": "call-s", "content": '{"silenced": true}'},
            {"role": "user", "content": "下一条"},
        ]
    )
    assert [item.get("role") for item in preserved] == ["assistant", "tool"]
    assert [item.get("role") for item in remaining] == ["assistant", "tool", "user"]
    declared = {call.get("id") for item in remaining if item.get("tool_calls") for call in item["tool_calls"]}
    answered = {item.get("tool_call_id") for item in remaining if item.get("role") == "tool"}
    assert declared == answered == {"call-a"}, "摘组必须整组搬，两侧都不能留孤儿"


def test_token_lane_lift_is_a_noop_without_silent() -> None:
    messages = [
        _assistant_with_calls("call-a", "exec"),
        {"role": "tool", "tool_call_id": "call-a", "content": "ok"},
    ]
    remaining, preserved = ceo_runtime_ops.CeoFrontDoorRuntimeOps._lift_silent_trace_groups(messages)
    assert preserved == []
    assert [item.get("role") for item in remaining] == ["assistant", "tool"]


def test_token_lane_lift_tolerates_composite_call_ids() -> None:
    """id 可能是 `call_x|resp_y` 形态（responses 车道），两侧归一化才配得上对。"""
    messages = [
        _assistant_with_calls("call-s|fc_resp_1", SILENT_TOOL_NAME, content="痕迹正文"),
        {"role": "tool", "tool_call_id": "call-s", "content": '{"silenced": true}'},
    ]
    remaining, preserved = ceo_runtime_ops.CeoFrontDoorRuntimeOps._lift_silent_trace_groups(messages)
    assert len(preserved) == 2
    assert remaining == []
