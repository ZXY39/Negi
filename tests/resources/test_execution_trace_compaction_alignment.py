from __future__ import annotations


def test_compact_tool_step_for_summary_uses_preview_and_ref_without_full_payload() -> None:
    from main.runtime.execution_trace_compaction import compact_tool_step_for_summary

    result = compact_tool_step_for_summary(
        {
            "tool_call_id": "call-1",
            "tool_name": "filesystem",
            "arguments_text": '{"path": "."}',
            "output_text": "very long inline output that should not survive in summary mode",
            "output_ref": "artifact:artifact:tool-output",
            "output_preview_text": "repo listing preview",
            "status": "success",
            "started_at": "2026-04-08T12:00:00+08:00",
            "finished_at": "2026-04-08T12:00:01+08:00",
        }
    )

    assert result == {
        "tool_call_id": "call-1",
        "tool_name": "filesystem",
        "arguments_preview": '{"path": "."}',
        "output_preview": "repo listing preview",
        "output_ref": "artifact:artifact:tool-output",
        "status": "success",
        "started_at": "2026-04-08T12:00:00+08:00",
        "finished_at": "2026-04-08T12:00:01+08:00",
    }
