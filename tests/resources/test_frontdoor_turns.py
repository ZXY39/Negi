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


def test_build_tools_defaults_missing_timing_and_derives_elapsed() -> None:
    """账本行必须带可解析计时：前端头部秒数会回退到"本轮最早工具开始时间"，
    契约下发/reuse 车道漏带计时空串会让整轮退成 `--`（2026-11-13 实盘事故）。"""
    started = "2026-11-13T00:16:00+08:00"
    finished = "2026-11-13T00:16:02+08:00"
    tools = build_frontdoor_turn_tools(
        [
            {"id": "call-timed", "name": "exec"},
            {"id": "call-untimed", "name": "filesystem_stat"},
            {"id": "call-error", "name": "exec"},
            {"id": "call-background", "name": "exec"},
        ],
        [
            {"status": "success", "result_text": "ok", "started_at": started, "finished_at": finished},
            {"status": "success", "result_text": "reuse result"},
            {"status": "error", "result_text": "Error: refused"},
            {"status": "background", "result_text": "dispatched", "started_at": started},
        ],
    )

    timed = tools[0]
    assert timed["started_at"] == started
    assert timed["finished_at"] == finished
    assert timed["elapsed_seconds"] == 2.0

    untimed = tools[1]
    # 无计时已收尾：起点终点都兜成落账时刻（零宽），不给前端"耗时"无限增长的口子。
    assert untimed["started_at"]
    assert untimed["finished_at"] == untimed["started_at"]
    assert untimed["elapsed_seconds"] == 0.0

    errored = tools[2]
    assert errored["started_at"]
    assert errored["finished_at"] == errored["started_at"]
    assert errored["elapsed_seconds"] == 0.0

    background = tools[3]
    assert background["started_at"] == started
    assert background["finished_at"] == ""
    assert background["elapsed_seconds"] is None


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
