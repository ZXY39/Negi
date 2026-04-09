from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_compact_tool_step_for_summary_returns_preview_fields_and_drops_full_payload() -> None:
    from main.runtime.execution_trace_compaction import compact_tool_step_for_summary

    tool_step = {
        "tool_call_id": "collate:1",
        "tool_name": "collate",
        "arguments_text": " ".join(f"arg-{index}" for index in range(50)),
        "output_text": " ".join(f"output-{index}" for index in range(50)),
        "output_ref": "artifact:collate-result",
        "status": "success",
        "started_at": "2026-04-05T01:00:00.000000Z",
        "finished_at": "2026-04-05T01:00:01.000000Z",
    }

    summary_step = compact_tool_step_for_summary(tool_step)

    assert summary_step["tool_call_id"] == tool_step["tool_call_id"]
    assert summary_step["tool_name"] == tool_step["tool_name"]
    assert summary_step["output_ref"] == tool_step["output_ref"]
    assert summary_step["status"] == tool_step["status"]
    assert summary_step["started_at"] == tool_step["started_at"]
    assert summary_step["finished_at"] == tool_step["finished_at"]
    assert summary_step["arguments_preview"]
    assert summary_step["arguments_preview"] != tool_step["arguments_text"]
    assert len(summary_step["arguments_preview"]) < len(tool_step["arguments_text"])
    assert summary_step["output_preview"]
    assert summary_step["output_preview"] != tool_step["output_text"]
    assert len(summary_step["output_preview"]) < len(tool_step["output_text"])
    assert "arguments_text" not in summary_step
    assert "output_text" not in summary_step


def test_compact_tool_step_for_summary_preserves_falsy_scalars_without_ref() -> None:
    from main.runtime.execution_trace_compaction import compact_tool_step_for_summary

    summary_step = compact_tool_step_for_summary(
        {
            "tool_call_id": "calc:1",
            "tool_name": "calculator",
            "arguments_text": 0,
            "output_text": False,
            "status": "success",
        }
    )

    assert summary_step is not None
    assert summary_step["arguments_preview"] == "0"
    assert summary_step["output_preview"] == "False"


def test_compact_tool_step_for_summary_falls_back_to_text_when_output_text_is_blank() -> None:
    from main.runtime.execution_trace_compaction import compact_tool_step_for_summary

    raw_text = '{"result_text":"loaded full skill body","status":"success"}'

    summary_step = compact_tool_step_for_summary(
        {
            "tool_call_id": "load-skill:1",
            "tool_name": "load_skill_context",
            "arguments_text": "load_skill_context (skill_id=find-skills)",
            "output_text": "",
            "text": raw_text,
            "status": "success",
        }
    )

    assert summary_step is not None
    assert summary_step["arguments_preview"] == "load_skill_context (skill_id=find-skills)"
    assert "output_preview" in summary_step, summary_step
    assert summary_step["output_preview"] == '{"result_text":"loade...'
