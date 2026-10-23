from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from main.monitoring.query_service import TaskQueryService  # noqa: E402
from main.runtime.execution_trace_compaction import build_execution_trace_summary  # noqa: E402

# 写侧存进 `task_node_details.payload.execution_trace_summary` 的那份，与读侧在存量摘要
# 缺失/没有 turns 时现算的那份，必须是同一个函数产出的同一个文档——否则「存不存这一份」
# 就成了显示口径问题。两条读路的等价性由最后两个用例钉住。

_TOOL_RUNNING = {
    "tool_call_id": "call-running",
    "tool_name": "spawn_child_nodes",
    "arguments_text": '{"children": 3}',
    "output_text": "",
    "output_ref": "",
    "status": "running",
    "started_at": "2026-04-04T19:37:42+08:00",
    "finished_at": "",
    "elapsed_seconds": None,
}

_TRACE = {
    "turns": [
        {
            "seq": 1,
            "text": "spawn child researchers",
            "text_ref": "",
            "created_at": "2026-04-04T19:37:42+08:00",
            "status": "running",
            "tools": [dict(_TOOL_RUNNING)],
        },
        {
            "seq": 2,
            "text": "",
            "text_ref": "",
            "created_at": "2026-04-04T19:37:52+08:00",
            "status": "completed",
            "tools": [],
        },
    ],
    "tool_steps": [dict(_TOOL_RUNNING)],
}


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def test_execution_trace_summary_preserves_turn_and_tool_runtime_fields() -> None:
    summary = TaskQueryService._sanitize_execution_trace_summary(
        build_execution_trace_summary(_TRACE)
    )

    turn = summary["turns"][0]
    assert turn["seq"] == 1
    assert turn["text"] == "spawn child researchers"
    assert turn["status"] == "running"
    assert turn["created_at"] == "2026-04-04T19:37:42+08:00"
    assert turn["tools"][0]["tool_call_id"] == "call-running"
    assert turn["tools"][0]["status"] == "running"
    assert turn["tools"][0]["started_at"] == "2026-04-04T19:37:42+08:00"
    assert turn["tools"][0]["finished_at"] == ""
    # 轮次的形状就是这一组键；多出来的键都来自已被移除的台账形状。
    assert set(turn) == {"seq", "text", "text_ref", "created_at", "status", "tools"}


def test_execution_trace_summary_keeps_multiple_turn_boundaries() -> None:
    trace = {
        "turns": [
            {
                "seq": 1,
                "text": "",
                "created_at": "2026-04-04T19:37:42+08:00",
                "status": "completed",
                "tools": [
                    {
                        "tool_call_id": "call-1",
                        "tool_name": "filesystem",
                        "arguments_text": '{"path": "."}',
                        "output_text": "repo listing",
                        "status": "success",
                    }
                ],
            },
            {
                "seq": 2,
                "text": "",
                "created_at": "2026-04-04T19:38:12+08:00",
                "status": "completed",
                "tools": [
                    {
                        "tool_call_id": "call-2",
                        "tool_name": "content",
                        "arguments_text": '{"ref": "artifact:1"}',
                        "output_text": "file contents",
                        "status": "success",
                    }
                ],
            },
        ]
    }

    summary = TaskQueryService._sanitize_execution_trace_summary(
        build_execution_trace_summary(trace)
    )

    assert len(summary["turns"]) == 2
    assert [item["seq"] for item in summary["turns"]] == [1, 2]
    assert summary["turns"][0]["tools"][0]["tool_name"] == "filesystem"
    assert summary["turns"][1]["tools"][0]["tool_name"] == "content"


def test_stored_arguments_preview_survive_the_read_projection() -> None:
    """实盘回归：面板历史轮次的「参数」整列是空的。

    写侧存的是 `arguments_preview`（160 字档），读侧的显示档 compactor 过去只认
    `arguments_text`，于是存量摘要过一遍 sanitize 就把参数丢掉——`node:8e2382a036e3`
    实测 425 条工具行 0 条保住参数。
    """
    stored = build_execution_trace_summary(_TRACE)
    stored_tool = stored["turns"][0]["tools"][0]
    assert stored_tool["arguments_preview"] == '{"children": 3}'

    projected = TaskQueryService._sanitize_execution_trace_summary(stored)
    tool = projected["turns"][0]["tools"][0]

    assert tool["arguments_text"] == '{"children": 3}'
    assert tool["output_text"] == ""


def test_stored_summary_and_rebuilt_summary_are_the_same_document() -> None:
    """两条读路必须逐字节相同：行内存量摘要 / 从外置轨迹现算。

    这条等价是「明细行不再内联整份摘要」的前提——否则删掉存量那一份会改显示。
    """
    from_storage = TaskQueryService._sanitize_execution_trace_summary(
        build_execution_trace_summary(_TRACE)
    )
    from_rebuild = TaskQueryService._sanitize_execution_trace_summary(
        build_execution_trace_summary(json.loads(json.dumps(_TRACE)))
    )

    assert _dump(from_storage) == _dump(from_rebuild)
    assert [turn["seq"] for turn in from_storage["turns"]] == [1, 2]
    # 读投影再跑一遍必须还是同一份文档：旧形状在这里丢的是轮次计数（实测 1 -> 0），
    # 现在丢的会是参数或工具本身。
    second_pass = TaskQueryService._sanitize_execution_trace_summary(from_storage)
    assert _dump(second_pass) == _dump(from_storage)
    assert second_pass["turns"][0]["tools"][0]["arguments_text"] == '{"children": 3}'


def test_evicted_context_mark_does_not_leak_into_the_summary() -> None:
    """按阶段裁撤的标记随机制一起没了：摘要文档不得带它，两条读路也不得各自发明一个。

    节点详情只按轮次自身的 `text` / `text_ref` 判断正文在不在场；存量行里带着旧标记也
    不能被搬进文档。
    """
    evicted_trace = json.loads(json.dumps(_TRACE))
    for turn in evicted_trace["turns"]:
        turn["context_evicted"] = True

    written = build_execution_trace_summary(evicted_trace)
    read_back = TaskQueryService._sanitize_execution_trace_summary(written)
    baseline = TaskQueryService._sanitize_execution_trace_summary(
        build_execution_trace_summary(_TRACE)
    )

    assert "context_evicted" not in written["turns"][0]
    assert "context_evicted" not in read_back["turns"][0]
    assert _dump(read_back) == _dump(baseline)


def test_contract_delivery_marker_survives_both_summary_builders() -> None:
    """只交契约的那一行必须带着标记穿过摘要与读投影：节点详情靠它画"加载 tool"卡片。

    完整轨迹在场时读 `execution_trace`（那条由 `build_execution_trace` 带标记），缺席时
    读这份摘要；两条道都要带，且未标记的行不涨体积。
    """
    delivered_trace = json.loads(json.dumps(_TRACE))
    delivered_trace["turns"][0]["tools"][0]["contract_delivery"] = "staged"

    written = build_execution_trace_summary(delivered_trace)
    read_back = TaskQueryService._sanitize_execution_trace_summary(written)
    untouched = build_execution_trace_summary(_TRACE)

    assert written["turns"][0]["tools"][0]["contract_delivery"] == "staged"
    assert read_back["turns"][0]["tools"][0]["contract_delivery"] == "staged"
    assert "contract_delivery" not in untouched["turns"][0]["tools"][0]
