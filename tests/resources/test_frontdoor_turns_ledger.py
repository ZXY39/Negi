from __future__ import annotations

import json

from g3ku.runtime.api.ceo_sessions import _tool_arguments_from_turns
from g3ku.runtime.frontdoor.turns import (
    FRONTDOOR_TURNS_MAX,
    append_frontdoor_turn,
    find_frontdoor_turn_tool,
    normalize_frontdoor_turns,
)


def test_append_frontdoor_turn_assigns_seq_and_merges_the_same_tool_cycle() -> None:
    turns = append_frontdoor_turn([], turn_id="turn-1", text="first reply", tools=[])

    tools = [
        {
            "tool_call_id": "call:exec",
            "tool_name": "exec",
            "status": "success",
            "arguments_text": '{"command": "git status"}',
        }
    ]
    turns = append_frontdoor_turn(turns, turn_id="turn-2", text="", tools=tools)
    assert [turn["seq"] for turn in turns] == [1, 2]

    # 同一 turn、同一批工具调用再写一次是补正文，不是新回合。
    turns = append_frontdoor_turn(turns, turn_id="turn-2", text="second reply", tools=tools)
    assert len(turns) == 2
    assert turns[-1]["text"] == "second reply"
    assert [tool["tool_call_id"] for tool in turns[-1]["tools"]] == ["call:exec"]

    # 同一 turn 里再并列一颗新工具调用是新一轮工具循环，不是重复写同一行。
    turns = append_frontdoor_turn(
        turns,
        turn_id="turn-2",
        text="",
        tools=[*tools, {"tool_call_id": "call:fs", "tool_name": "filesystem", "status": "success"}],
    )
    assert [turn["seq"] for turn in turns] == [1, 2, 3]
    assert [tool["tool_name"] for tool in turns[-1]["tools"]] == ["exec", "filesystem"]


def test_normalize_restores_order_keeps_the_newest_window_and_drops_invalid_rows() -> None:
    raw = [
        {"seq": 3, "turn_id": "c", "text": "third"},
        {"seq": 0, "turn_id": "zero", "text": "no seq"},
        {"seq": 1, "turn_id": "a", "text": "first"},
        {"seq": 2, "turn_id": "b", "text": "second"},
        "not-a-turn",
    ]

    restored = normalize_frontdoor_turns(raw)

    assert [turn["turn_id"] for turn in restored] == ["a", "b", "c"]
    assert [turn["seq"] for turn in restored] == [1, 2, 3]

    oversized = [{"seq": index + 1, "turn_id": f"t{index}", "text": ""} for index in range(FRONTDOOR_TURNS_MAX + 12)]
    capped = normalize_frontdoor_turns(oversized)
    assert len(capped) == FRONTDOOR_TURNS_MAX
    assert capped[0]["seq"] == 13
    assert capped[-1]["seq"] == FRONTDOOR_TURNS_MAX + 12


def test_tool_arguments_stay_untruncated_through_restore() -> None:
    long_arguments = {"command": "x" * 4000, "working_dir": "/repo"}
    raw_arguments = json.dumps(long_arguments, ensure_ascii=False, indent=2)
    turns = append_frontdoor_turn(
        [],
        turn_id="turn-long",
        text="ran a long command",
        tools=[
            {
                "tool_call_id": "call:long",
                "tool_name": "exec",
                "status": "success",
                "arguments_text": raw_arguments,
                "contract_delivery": "staged_replay",
            }
        ],
    )
    restored = normalize_frontdoor_turns(turns)
    tool = find_frontdoor_turn_tool(restored, "call:long")

    assert tool is not None
    assert tool["arguments_text"] == raw_arguments
    assert tool["contract_delivery"] == "staged_replay"


def test_compression_boundary_splits_turns_and_survives_restart() -> None:
    turns = append_frontdoor_turn([], turn_id="turn-1", text="before compaction", tools=[])
    turns = append_frontdoor_turn(turns, turn_id="turn-2", text="before compaction too", tools=[])

    # 压缩那一刻把"已存在的 turn 数"记成边界；边界之后追加的回合才是线上方的新内容。
    boundary_turn_seq = len(turns)
    turns = append_frontdoor_turn(turns, turn_id="turn-3", text="after compaction", tools=[])

    def split(window):
        return (
            [turn["turn_id"] for turn in window if int(turn["seq"]) <= boundary_turn_seq],
            [turn["turn_id"] for turn in window if int(turn["seq"]) > boundary_turn_seq],
        )

    below, above = split(turns)
    assert below == ["turn-1", "turn-2"]
    assert above == ["turn-3"]

    restarted = normalize_frontdoor_turns(json.loads(json.dumps(turns, ensure_ascii=False)))
    below_after_restart, above_after_restart = split(restarted)
    assert below_after_restart == below
    assert above_after_restart == above


def test_tool_arguments_lookup_reads_the_completed_continuity_payload() -> None:
    turns = append_frontdoor_turn(
        [],
        turn_id="turn-1",
        text="",
        tools=[
            {
                "tool_call_id": "call:search",
                "tool_name": "web_search",
                "status": "success",
                "arguments_text": json.dumps({"query": "neg i", "limit": 3}, ensure_ascii=False),
            }
        ],
    )
    # 进程重启后会话没驻留：读完成态 sidecar 里的 frontdoor_turns。
    continuity = {"frontdoor_turns": json.loads(json.dumps(turns, ensure_ascii=False))}

    assert _tool_arguments_from_turns(continuity.get("frontdoor_turns"), "call:search") == {
        "query": "neg i",
        "limit": 3,
    }
    assert _tool_arguments_from_turns(continuity.get("frontdoor_turns"), "call:missing") is None
