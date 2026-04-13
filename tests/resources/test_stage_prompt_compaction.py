from __future__ import annotations

import json

from g3ku.runtime.stage_prompt_compaction import (
    STAGE_COMPACT_PREFIX,
    STAGE_EXTERNALIZED_PREFIX,
    prepare_stage_prompt_messages,
)


def _assistant_stage_call(call_id: str) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "submit_next_stage", "arguments": "{}"},
            }
        ],
    }


def _tool_stage_result(call_id: str) -> dict[str, object]:
    return {
        "role": "tool",
        "name": "submit_next_stage",
        "tool_call_id": call_id,
        "content": '{"ok": true}',
    }


def test_prepare_stage_prompt_messages_keeps_latest_three_completed_windows_and_compacts_older_history() -> None:
    stage_state = {
        "active_stage_id": "stage-5",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "stage-1",
                "stage_index": 1,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage one",
                "completed_stage_summary": "finished stage one",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "stage-2",
                "stage_index": 2,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage two",
                "completed_stage_summary": "finished stage two",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "stage-3",
                "stage_index": 3,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage three",
                "completed_stage_summary": "finished stage three",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "stage-4",
                "stage_index": 4,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage four",
                "completed_stage_summary": "finished stage four",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "stage-5",
                "stage_index": 5,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "active",
                "stage_goal": "inspect stage five",
                "completed_stage_summary": "",
                "key_refs": [],
                "tool_round_budget": 3,
                "tool_rounds_used": 0,
            },
        ],
    }
    original = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": '{"task_id":"task-1","goal":"demo"}'},
        _assistant_stage_call("call-stage-1"),
        _tool_stage_result("call-stage-1"),
        {"role": "assistant", "content": "stage one raw detail"},
        _assistant_stage_call("call-stage-2"),
        _tool_stage_result("call-stage-2"),
        {"role": "assistant", "content": "stage two raw detail"},
        _assistant_stage_call("call-stage-3"),
        _tool_stage_result("call-stage-3"),
        {"role": "assistant", "content": "stage three raw detail"},
        _assistant_stage_call("call-stage-4"),
        _tool_stage_result("call-stage-4"),
        {"role": "assistant", "content": "stage four raw detail"},
        _assistant_stage_call("call-stage-5"),
        _tool_stage_result("call-stage-5"),
        {"role": "assistant", "content": "current stage assistant detail"},
        {"role": "tool", "name": "record_tool", "tool_call_id": "call-current", "content": "current stage tool output"},
    ]

    prepared = prepare_stage_prompt_messages(
        original,
        stage_state=stage_state,
        keep_latest_completed_stages=3,
        stage_tool_name="submit_next_stage",
    )

    rendered_contents = [str(item.get("content") or "") for item in prepared]
    assert "stage two raw detail" in rendered_contents
    assert "stage three raw detail" in rendered_contents
    assert "stage four raw detail" in rendered_contents
    assert "current stage assistant detail" in rendered_contents
    assert "current stage tool output" in rendered_contents
    assert "stage one raw detail" not in rendered_contents

    compact_blocks = [
        content
        for content in rendered_contents
        if content.startswith(STAGE_COMPACT_PREFIX)
    ]
    assert len(compact_blocks) == 1
    compact_payload = json.loads(compact_blocks[0].split("\n", 1)[1])
    assert compact_payload["stage_index"] == 1
    assert compact_payload["completed_stage_summary"] == "finished stage one"


def test_prepare_stage_prompt_messages_externalizes_compression_stages() -> None:
    stage_state = {
        "active_stage_id": "stage-3",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "stage-compression-1",
                "stage_index": 1,
                "stage_kind": "compression",
                "system_generated": True,
                "status": "completed",
                "stage_goal": "Archive completed stage history 1-10",
                "completed_stage_summary": "archived old stages",
                "archive_ref": "artifact:artifact:stage-archive-1",
                "archive_stage_index_start": 1,
                "archive_stage_index_end": 10,
                "tool_round_budget": 0,
                "tool_rounds_used": 0,
            },
            {
                "stage_id": "stage-2",
                "stage_index": 11,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "completed",
                "stage_goal": "inspect stage two",
                "completed_stage_summary": "finished stage two",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 1,
            },
            {
                "stage_id": "stage-3",
                "stage_index": 12,
                "stage_kind": "normal",
                "system_generated": False,
                "mode": "自主执行",
                "status": "active",
                "stage_goal": "inspect stage three",
                "completed_stage_summary": "",
                "key_refs": [],
                "tool_round_budget": 2,
                "tool_rounds_used": 0,
            },
        ],
    }

    prepared = prepare_stage_prompt_messages(
        [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}],
        stage_state=stage_state,
        keep_latest_completed_stages=0,
        stage_tool_name="submit_next_stage",
    )

    rendered_contents = [str(item.get("content") or "") for item in prepared]
    externalized_blocks = [
        content
        for content in rendered_contents
        if content.startswith(STAGE_EXTERNALIZED_PREFIX)
    ]
    assert len(externalized_blocks) == 1
    payload = json.loads(externalized_blocks[0].split("\n", 1)[1])
    assert payload["archive_ref"] == "artifact:artifact:stage-archive-1"
    assert payload["archive_stage_index_start"] == 1
    assert payload["archive_stage_index_end"] == 10
