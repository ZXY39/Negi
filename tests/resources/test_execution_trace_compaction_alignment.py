from __future__ import annotations


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
    assert summary_step["output_preview"]
    assert "arguments_text" not in summary_step
    assert "output_text" not in summary_step
