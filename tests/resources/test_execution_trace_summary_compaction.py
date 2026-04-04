from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from main.monitoring.log_service import TaskLogService
from main.monitoring.query_service import TaskQueryService


@pytest.mark.parametrize(
    ("service_name", "summary_builder"),
    [
        ("query", TaskQueryService._execution_trace_summary),
        ("log", TaskLogService._execution_trace_summary),
    ],
)
def test_execution_trace_summary_preserves_stage_and_tool_runtime_fields(service_name, summary_builder) -> None:
    trace = {
        "stages": [
            {
                "stage_id": "stage-1",
                "stage_index": 1,
                "mode": "dispatch-with-children",
                "status": "\u8fdb\u884c\u4e2d",
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
                    }
                ],
            }
        ]
    }

    summary = summary_builder(trace)

    assert summary["stages"][0]["stage_id"] == "stage-1", service_name
    assert summary["stages"][0]["status"] == "\u8fdb\u884c\u4e2d", service_name
    assert summary["stages"][0]["mode"] == "dispatch-with-children", service_name
    assert summary["stages"][0]["created_at"] == "2026-04-04T19:36:36+08:00", service_name
    assert summary["stages"][0]["finished_at"] == "", service_name
    assert summary["stages"][0]["tool_calls"][0]["tool_call_id"] == "call-running", service_name
    assert summary["stages"][0]["tool_calls"][0]["status"] == "running", service_name
    assert summary["stages"][0]["tool_calls"][0]["started_at"] == "2026-04-04T19:37:42+08:00", service_name
    assert summary["stages"][0]["tool_calls"][0]["finished_at"] == "", service_name
