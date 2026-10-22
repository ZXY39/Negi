from __future__ import annotations

from g3ku.runtime.frontdoor.turns import (
    append_frontdoor_turn,
    build_frontdoor_turn_tools,
    find_frontdoor_turn_tool,
    frontdoor_turns_for_turn,
    normalize_frontdoor_turns,
)


def test_normalize_keeps_turn_text_and_tools() -> None:
    turns = normalize_frontdoor_turns(
        [
            {
                "seq": 2,
                "turn_id": "turn-1",
                "text": "progress",
                "created_at": "2026-10-22T10:00:00+08:00",
                "tools": [
                    {
                        "tool_call_id": "call-1",
                        "tool_name": "exec",
                        "status": "success",
                        "arguments_text": "{}",
                    }
                ],
            }
        ]
    )

    assert len(turns) == 1
    assert turns[0]["seq"] == 2
    assert turns[0]["text"] == "progress"
    assert turns[0]["tools"][0]["tool_call_id"] == "call-1"


def test_append_merges_same_tool_cycle_and_assigns_seq() -> None:
    turns = append_frontdoor_turn(
        [],
        turn_id="turn-1",
        text="first",
        tools=[{"tool_call_id": "call-1", "tool_name": "exec"}],
    )
    turns = append_frontdoor_turn(
        turns,
        turn_id="turn-1",
        text="first updated",
        tools=[{"tool_call_id": "call-1", "tool_name": "exec", "status": "success"}],
    )
    turns = append_frontdoor_turn(
        turns,
        turn_id="turn-1",
        text="second",
        tools=[{"tool_call_id": "call-2", "tool_name": "exec"}],
    )

    assert [turn["seq"] for turn in turns] == [1, 2]
    assert turns[0]["text"] == "first updated"
    assert turns[0]["tools"][0]["status"] == "success"
    assert turns[1]["text"] == "second"


def test_build_tools_keeps_full_arguments_and_result_text() -> None:
    tools = build_frontdoor_turn_tools(
        [
            {
                "id": "call-1",
                "name": "exec",
                "arguments": {"command": "Get-ChildItem"},
            }
        ],
        [{"status": "success", "result_text": "file.txt"}],
    )

    assert tools[0]["tool_call_id"] == "call-1"
    assert "Get-ChildItem" in tools[0]["arguments_text"]
    assert tools[0]["output_text"] == "file.txt"


def test_find_tool_and_turn_filter() -> None:
    turns = append_frontdoor_turn(
        [],
        turn_id="turn-1",
        text="progress",
        tools=[{"tool_call_id": "call-1", "tool_name": "exec", "arguments_text": "{\"a\":1}"}],
    )
    turns = append_frontdoor_turn(turns, turn_id="turn-2", text="later")

    assert find_frontdoor_turn_tool(turns, "call-1")["tool_name"] == "exec"
    assert find_frontdoor_turn_tool(turns, "missing") is None
    assert [turn["text"] for turn in frontdoor_turns_for_turn(turns, "turn-2")] == ["later"]
