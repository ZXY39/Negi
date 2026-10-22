from __future__ import annotations

import json

from g3ku.agent.memory_agent_runtime import MemoryManager


def _turn(seq: int = 1) -> dict:
    return {
        "seq": seq,
        "turn_id": "turn-1",
        "text": "检查第一批文件",
        "tools": [
            {
                "tool_call_id": "call-1",
                "tool_name": "exec",
                "status": "error",
                "arguments_text": "{\"command\": \"dir\"}",
                "output_text": "permission denied",
            },
            {
                "tool_call_id": "call-2",
                "tool_name": "filesystem_read",
                "status": "success",
                "arguments_text": "{\"path\": \"a.txt\"}",
                "output_text": "ok",
            },
        ],
    }


def test_review_turn_payload_uses_flat_tool_records() -> None:
    payload = MemoryManager._review_turn_payload(
        turn_id="turn-1",
        user_messages=["检查文件"],
        assistant_text="已完成",
        compression_summary=None,
        canonical_summary=None,
        turns=[_turn()],
    )

    assert "tool_records:" in payload
    assert "stages:" not in payload
    records = json.loads(payload.split("tool_records:\n", 1)[1].split("\nassistant_text:", 1)[0])
    assert [record["tool_call_id"] for record in records] == ["call-1", "call-2"]
    assert records[0]["mid_text"] == "检查第一批文件"
    assert records[0]["is_error"] is True
    assert records[1]["is_error"] is False


def test_review_window_splits_flat_tool_records_without_losing_header() -> None:
    section = MemoryManager._review_turn_payload(
        turn_id="turn-1",
        user_messages=["检查文件"],
        assistant_text="已完成",
        compression_summary=None,
        canonical_summary=None,
        turns=[_turn()],
    )

    pieces = MemoryManager._window_turn_sections(section, max_tokens=40)

    assert len(pieces) >= 2
    assert all("[turn_id] turn-1" in piece for piece in pieces)
    assert all("assistant_text:" in piece for piece in pieces)
    joined = "\n".join(pieces)
    assert joined.count('"tool_call_id": "call-1"') == 1
    assert joined.count('"tool_call_id": "call-2"') == 1


def test_turn_tool_record_key_falls_back_to_fingerprint() -> None:
    records = MemoryManager._collect_turn_tool_records(turns=[_turn()])

    assert records[0]["key"] == "call-1"
    assert records[1]["key"] == "call-2"
