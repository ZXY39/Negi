from __future__ import annotations

from main.models import NodeOutputEntry, NodeRecord
from main.monitoring.execution_trace import build_execution_trace
from main.monitoring.models import TaskProjectionToolResultRecord
from main.monitoring.query_service import TaskQueryService
from main.protocol import now_iso
from main.runtime.execution_trace_compaction import build_execution_trace_summary

TASK_ID = "task:turns"
NODE_ID = "node:turns"


def _call(call_id: str, name: str = "exec") -> dict[str, object]:
    return {"id": call_id, "name": name, "arguments": {"command": call_id}}


def _record(call_id: str, *, output: str = "ok") -> TaskProjectionToolResultRecord:
    return TaskProjectionToolResultRecord(
        task_id=TASK_ID,
        node_id=NODE_ID,
        tool_call_id=call_id,
        tool_name="exec",
        status="success",
        payload={
            "parsed_payload": {},
            "result_text": output,
        },
    )


def _node() -> NodeRecord:
    return NodeRecord(
        node_id=NODE_ID,
        task_id=TASK_ID,
        root_node_id=NODE_ID,
        goal="goal",
        prompt="prompt",
        status="success",
        output=[
            NodeOutputEntry(
                seq=1,
                content="first progress",
                tool_calls=[_call("call-1"), _call("call-2")],
                created_at=now_iso(),
            ),
            NodeOutputEntry(
                seq=2,
                content="",
                tool_calls=[_call("call-3")],
                created_at=now_iso(),
            ),
            NodeOutputEntry(
                seq=3,
                content="second progress",
                tool_calls=[_call("call-4")],
                created_at=now_iso(),
            ),
            NodeOutputEntry(
                seq=4,
                content="plain text turn",
                created_at=now_iso(),
            ),
        ],
        created_at=now_iso(),
        updated_at=now_iso(),
    )


def test_execution_trace_turns_keep_text_and_following_tools() -> None:
    trace = build_execution_trace(
        _node(),
        tool_results=[_record("call-1"), _record("call-2"), _record("call-3"), _record("call-4")],
        live_tool_calls=[],
    )

    turns = trace["turns"]
    assert [turn["seq"] for turn in turns] == [1, 2, 3, 4]
    assert [turn["text"] for turn in turns] == [
        "first progress",
        "",
        "second progress",
        "plain text turn",
    ]
    assert [tool["tool_call_id"] for tool in turns[0]["tools"]] == ["call-1", "call-2"]
    assert [tool["tool_call_id"] for tool in turns[1]["tools"]] == ["call-3"]
    assert [tool["tool_call_id"] for tool in turns[2]["tools"]] == ["call-4"]
    assert turns[3]["tools"] == []
    assert turns[0]["tools"][0]["arguments_text"]


def test_execution_trace_turn_keeps_stored_summary_when_text_is_externalized() -> None:
    node = _node()
    node.output[0].content = "stored summary"
    node.output[0].content_ref = "artifact:node-output-full"

    trace = build_execution_trace(node, tool_results=[], live_tool_calls=[])

    turn = trace["turns"][0]
    assert turn["text"] == "stored summary"
    assert turn["text_ref"] == "artifact:node-output-full"


def test_execution_trace_summary_and_projection_keep_text_only_turns() -> None:
    trace = build_execution_trace(_node(), tool_results=[], live_tool_calls=[])
    summary = build_execution_trace_summary(trace)
    projected = TaskQueryService._sanitize_execution_trace_summary(summary)

    for payload in (summary, projected):
        turns = payload["turns"]
        assert [turn["seq"] for turn in turns] == [1, 2, 3, 4]
        assert turns[3]["text"] == "plain text turn"
        assert turns[3]["tools"] == []


def test_turn_thinking_reading_survives_both_summary_builders() -> None:
    """行上的思考读数要活过两条读路（现算轨迹、存量摘要），断在哪一条界面就少一半轮次。"""
    node = _node()
    node.output[0].thinking = {
        "seconds": 12.3,
        "chars": 880,
        "est_tokens": 220,
        "tokens": 259,
        "reasoning_content": "思考全文不许跟着读数走到节点详情",
    }

    trace = build_execution_trace(node, tool_results=[], live_tool_calls=[])
    reading = {"seconds": 12.3, "chars": 880, "est_tokens": 220, "tokens": 259}
    assert trace["turns"][0]["thinking"] == reading
    # 没上报的那一轮留空字典，前端据此整行不画。
    assert trace["turns"][1]["thinking"] == {}

    for payload in (
        build_execution_trace_summary(trace),
        TaskQueryService._sanitize_execution_trace_summary(build_execution_trace_summary(trace)),
    ):
        assert payload["turns"][0]["thinking"] == reading
