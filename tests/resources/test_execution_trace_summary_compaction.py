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
# 缺失/没有 rounds 时现算的那份，必须是同一个函数产出的同一个文档——否则「存不存这一份」
# 就成了显示口径问题。两条读路的等价性由最后两个用例钉住。

_TRACE = {
    "stages": [
        {
            "stage_id": "stage-1",
            "stage_index": 1,
            "mode": "dispatch-with-children",
            "status": "进行中",
            "stage_goal": "spawn child researchers",
            "tool_round_budget": 10,
            "tool_rounds_used": 2,
            "created_at": "2026-04-04T19:36:36+08:00",
            "finished_at": "",
            "rounds": [
                {
                    "round_id": "round-1",
                    "round_index": 1,
                    "created_at": "2026-04-04T19:37:42+08:00",
                    "budget_counted": False,
                    "tools": [
                        {
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
                    ],
                },
                {
                    "round_id": "round-empty",
                    "round_index": 2,
                    "created_at": "2026-04-04T19:37:52+08:00",
                    "budget_counted": True,
                    "tools": [],
                },
            ],
        }
    ]
}


def _dump(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def test_execution_trace_summary_preserves_stage_and_tool_runtime_fields() -> None:
    summary = TaskQueryService._sanitize_execution_trace_summary(
        build_execution_trace_summary(_TRACE)
    )

    stage = summary["stages"][0]
    assert stage["stage_id"] == "stage-1"
    assert stage["status"] == "进行中"
    assert stage["mode"] == "dispatch-with-children"
    assert stage["created_at"] == "2026-04-04T19:36:36+08:00"
    assert stage["finished_at"] == ""
    assert stage["tool_calls"][0]["tool_call_id"] == "call-running"
    assert stage["tool_calls"][0]["status"] == "running"
    assert stage["tool_calls"][0]["started_at"] == "2026-04-04T19:37:42+08:00"
    assert stage["tool_calls"][0]["finished_at"] == ""
    assert [item["round_id"] for item in stage["rounds"]] == ["round-1"]


def test_execution_trace_summary_keeps_multiple_round_boundaries() -> None:
    trace = {
        "stages": [
            {
                "stage_id": "stage-1",
                "stage_index": 1,
                "mode": "自主执行",
                "status": "完成",
                "stage_goal": "inspect repository",
                "tool_round_budget": 4,
                "tool_rounds_used": 2,
                "rounds": [
                    {
                        "round_id": "round-1",
                        "round_index": 1,
                        "created_at": "2026-04-04T19:37:42+08:00",
                        "budget_counted": True,
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
                        "round_id": "round-2",
                        "round_index": 2,
                        "created_at": "2026-04-04T19:38:12+08:00",
                        "budget_counted": True,
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
                ],
            }
        ]
    }

    summary = TaskQueryService._sanitize_execution_trace_summary(
        build_execution_trace_summary(trace)
    )

    assert len(summary["stages"][0]["rounds"]) == 2
    assert [item["round_index"] for item in summary["stages"][0]["rounds"]] == [1, 2]
    assert summary["stages"][0]["rounds"][0]["tools"][0]["tool_name"] == "filesystem"
    assert summary["stages"][0]["rounds"][1]["tools"][0]["tool_name"] == "content"


def test_stored_arguments_preview_survive_the_read_projection() -> None:
    """实盘回归：面板历史轮次的「参数」整列是空的。

    写侧存的是 `arguments_preview`（160 字档），读侧的显示档 compactor 过去只认
    `arguments_text`，于是存量摘要过一遍 sanitize 就把参数丢掉——`task:1d9cddf9858e`
    的 `node:8e2382a036e3` 实测 425 条工具行 0 条保住参数。
    """
    stored = build_execution_trace_summary(_TRACE)
    stored_tool = stored["stages"][0]["rounds"][0]["tools"][0]
    assert stored_tool["arguments_preview"] == '{"children": 3}'

    projected = TaskQueryService._sanitize_execution_trace_summary(stored)
    tool = projected["stages"][0]["rounds"][0]["tools"][0]

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
    stage = from_storage["stages"][0]
    # 明细行存的是**写侧文档**（保留 budget_counted 但没有工具的轮次）。读投影只做一次：
    # 把读投影再存回去会让那一轮的计数丢掉（tool_rounds_used 实测 1 → 0），显示就变了。
    assert [round_item["round_id"] for round_item in stage["rounds"]] == ["round-1"]
    assert stage["tool_rounds_used"] == 1
    second_pass = TaskQueryService._sanitize_execution_trace_summary(from_storage)
    assert second_pass["stages"][0]["tool_rounds_used"] == 0
