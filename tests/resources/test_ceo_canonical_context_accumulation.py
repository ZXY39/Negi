from __future__ import annotations

from g3ku.runtime.frontdoor.canonical_context import (
    TRANSCRIPT_PROJECTION_MODE,
    apply_cc_upsert,
    canonical_context_delta,
    encode_cc_upsert,
    merge_turn_stage_state_into_canonical_context,
    normalize_frontdoor_canonical_context,
    project_canonical_context_for_transcript,
    project_canonical_context_for_ui_payload,
    ui_canonical_context_delta,
)


def _stage(
    stage_id: str,
    index: int,
    *,
    status: str = "completed",
    goal: str = "",
    summary: str = "",
    rounds: list[dict[str, object]] | None = None,
    representation: str | None = None,
    evicted: bool = False,
) -> dict[str, object]:
    stage: dict[str, object] = {
        "stage_id": stage_id,
        "stage_index": index,
        "stage_goal": goal or f"goal {index}",
        "completed_stage_summary": summary or f"summary {index}",
        "status": status,
        "stage_kind": "normal",
        "representation": representation,
        "created_at": f"2026-09-01T00:0{index}:00+08:00",
        "finished_at": f"2026-09-01T00:1{index}:00+08:00",
        "rounds": list(rounds or []),
    }
    if evicted:
        stage["context_evicted"] = True
    return stage


def _tool(name: str, output_text: str = "", **overrides: object) -> dict[str, object]:
    return {
        "tool_call_id": f"{name}:1",
        "tool_name": name,
        "status": "success",
        "arguments": {"path": "a"},
        "arguments_text": "read a",
        "output_text": output_text,
        "output_preview_text": output_text[:80],
        "output_ref": f"artifact://{name}",
        **overrides,
    }


def test_normalize_keeps_latest_copy_for_repeated_stage_id() -> None:
    normalized = normalize_frontdoor_canonical_context(
        {
            "stages": [
                _stage("frontdoor-stage-1", 1, summary="old"),
                _stage("frontdoor-stage-1", 1, summary="new"),
            ]
        }
    )

    assert len(normalized["stages"]) == 1
    assert normalized["stages"][0]["completed_stage_summary"] == "new"


def test_normalize_collapses_rebased_copies_of_the_same_completed_stage() -> None:
    original = _stage("frontdoor-stage-1", 1, summary="same")
    rebased = dict(original)
    rebased["stage_id"] = "frontdoor-stage-98"
    rebased["stage_index"] = 98

    normalized = normalize_frontdoor_canonical_context({"stages": [original, rebased]})

    assert [stage["stage_id"] for stage in normalized["stages"]] == ["frontdoor-stage-98"]


def test_repeated_finalization_does_not_reappend_the_carried_workset() -> None:
    durable = normalize_frontdoor_canonical_context(
        {"stages": [_stage(f"frontdoor-stage-{index}", index) for index in range(1, 96)]}
    )
    turn_state = {
        "active_stage_id": "frontdoor-stage-97",
        "stages": [
            *[dict(stage) for stage in durable["stages"]],
            _stage("frontdoor-stage-96", 96, goal="new work", summary="new result"),
            _stage("frontdoor-stage-97", 97, status="active", goal="current", summary=""),
        ],
    }

    first = merge_turn_stage_state_into_canonical_context(durable, turn_state)
    second = merge_turn_stage_state_into_canonical_context(first, turn_state)

    stage_ids = [str(stage.get("stage_id")) for stage in first["stages"]]
    assert len(stage_ids) == len(set(stage_ids))
    assert len(first["stages"]) < 100
    assert len(second["stages"]) <= len(first["stages"]) + 1


def test_transcript_projection_compacts_marked_stages_and_caps_tool_bodies() -> None:
    # 表示形式只由标记决定：点名裁撤的那条降为 compact 并清 rounds，其余一律 raw，
    # 体积靠转录侧的字符上限约束，不再靠"只留最近 3 条"的位置切片。
    long_output = "x" * 2500
    long_arguments = {"payload": "y" * 3000}
    context = {
        "active_stage_id": "",
        "stages": [
            _stage(
                "frontdoor-stage-1",
                1,
                evicted=True,
                rounds=[
                    {
                        "round_index": 1,
                        "text": "old round",
                        "tools": [_tool("old", output_text=long_output, arguments=long_arguments)],
                    }
                ],
            ),
            *[
                _stage(
                    f"frontdoor-stage-{index}",
                    index,
                    rounds=[
                        {
                            "round_index": 1,
                            "text": "kept round",
                            "tools": [_tool(f"kept-{index}", output_text=long_output)],
                        }
                    ],
                )
                for index in range(2, 6)
            ],
        ]
    }

    projected = project_canonical_context_for_transcript(context)
    old_stage = projected["stages"][0]
    kept_stage = projected["stages"][-1]
    kept_tool = kept_stage["rounds"][0]["tools"][0]

    assert old_stage["representation"] == "compact"
    assert old_stage["rounds"] == []
    assert kept_stage["representation"] == "raw"
    assert len(kept_tool["output_text"]) == 0
    assert kept_tool["output_preview_text"] == long_output[:80]
    assert kept_tool["output_ref"] == "artifact://kept-5"


def test_transcript_projection_caps_oversized_tool_arguments() -> None:
    projected = project_canonical_context_for_transcript(
        {
            "stages": [
                _stage(
                    "frontdoor-stage-9",
                    9,
                    rounds=[
                        {
                            "round_index": 1,
                            "tools": [
                                _tool("large", arguments={"payload": "y" * 3000}),
                                _tool("small", arguments={"payload": "z"}),
                            ],
                        }
                    ],
                )
            ]
        }
    )
    tools = projected["stages"][0]["rounds"][0]["tools"]

    assert tools[0]["arguments"] == {}
    assert len(str(tools[0]["arguments_text"])) <= 4000
    # 清空要说得出口：前端靠这颗标记去账本按需回取全量入参。
    assert tools[0]["arguments_truncated"] is True
    # 没被清空的行不带标记——每帧都多一个键就是白付的字节。
    assert tools[1]["arguments"] == {"payload": "z"}
    assert "arguments_truncated" not in tools[1]


def test_arguments_truncated_marker_survives_re_normalization() -> None:
    capped = {
        "stages": [
            _stage(
                "frontdoor-stage-3",
                3,
                rounds=[{"round_index": 1, "tools": [_tool("large", arguments={"payload": "y" * 3000})]}],
            )
        ]
    }
    projected = project_canonical_context_for_transcript(capped)
    again = project_canonical_context_for_transcript(projected)
    tool = again["stages"][0]["rounds"][0]["tools"][0]

    assert tool["arguments_truncated"] is True


def test_ui_delta_ignores_projection_representation_flips() -> None:
    rounds = [
        {
            "round_index": 1,
            "text": f"same round {index}",
            "tools": [_tool(f"read-{index}", output_text="same output")],
        }
        for index in range(1, 6)
    ]
    persisted = normalize_frontdoor_canonical_context(
        {
            "stages": [
                _stage(f"frontdoor-stage-{index}", index, rounds=[rounds[index - 1]])
                for index in range(1, 6)
            ]
        }
    )
    persisted_projected = project_canonical_context_for_transcript(persisted)
    live = {
        "active_stage_id": "",
        "stages": [
            _stage(
                f"frontdoor-stage-{index}",
                index,
                representation="raw",
                rounds=[rounds[index - 1]],
            )
            for index in range(1, 6)
        ],
    }

    raw_delta = canonical_context_delta(persisted_projected, live)
    ui_delta = ui_canonical_context_delta(persisted_projected, live)

    assert len(raw_delta.get("stages") or []) >= 2
    assert ui_delta == {}


def test_ui_delta_ignores_bulk_closure_marks() -> None:
    """压缩收口一次性给历史阶段盖 context_visible=False 时，UI delta 必须为空。

    收口标记只决定阶段正文进不进模型上下文，前端不读它；把它算成展示变化，收口
    那一轮的气泡就会堆满整本历史账本（QQ 渠道会话实测一行 delta 429 条阶段 / 302KB）。
    存储侧仍必须带这个标记，所以这里同时断言 upsert 编码没被顺手削掉。
    """
    rounds = [
        {
            "round_index": 1,
            "text": f"round {index}",
            "tools": [_tool(f"read-{index}", output_text="out")],
        }
        for index in range(1, 7)
    ]
    before = normalize_frontdoor_canonical_context(
        {"stages": [_stage(f"frontdoor-stage-{index}", index, rounds=[rounds[index - 1]]) for index in range(1, 7)]}
    )
    after = normalize_frontdoor_canonical_context(
        {
            "stages": [
                {
                    **_stage(f"frontdoor-stage-{index}", index, rounds=[rounds[index - 1]]),
                    "context_visible": False,
                }
                for index in range(1, 7)
            ]
        }
    )

    assert ui_canonical_context_delta(before, after) == {}
    assert canonical_context_delta(before, after) == {}

    upsert = encode_cc_upsert(before, after)
    assert upsert is not None
    assert any(stage.get("context_visible") is False for stage in (upsert.get("upsert") or []))
    assert apply_cc_upsert(before, upsert) == after


def test_ui_delta_reports_a_closed_stage_only_for_its_visible_change() -> None:
    """同一个阶段既被收口又真的改了展示内容：delta 带上它，但不带簿记位。"""
    before = normalize_frontdoor_canonical_context({"stages": [_stage("frontdoor-stage-1", 1)]})
    after = normalize_frontdoor_canonical_context(
        {
            "stages": [
                {
                    **_stage("frontdoor-stage-1", 1, summary="收口之后又改了摘要"),
                    "context_visible": False,
                }
            ]
        }
    )

    stages = list((ui_canonical_context_delta(before, after).get("stages") or []))

    assert len(stages) == 1
    assert stages[0]["completed_stage_summary"] == "收口之后又改了摘要"
    assert "context_visible" not in stages[0]


def test_ui_delta_reports_the_eviction_mark_the_stage_card_badges() -> None:
    """裁撤标记必须穿过 UI delta：阶段卡靠它把"完成"换成"已移出上下文"。

    与收口不同，`submit_next_stage` 每回合只给正在收口的那一条阶段盖章，不会把整本历史
    一次性扫进同一帧，所以把它算进展示变化是安全的；把它当簿记位抹掉等于前端永远看不到
    模型点名移出这件事（live 轨道只读 delta，全量 canonical 只在刷新那一次才到）。
    """
    rounds = [{"round_index": 1, "text": "round 1", "tools": [_tool("read-1", output_text="out")]}]
    before = normalize_frontdoor_canonical_context(
        {"stages": [_stage("frontdoor-stage-1", 1, rounds=rounds)]}
    )
    after = normalize_frontdoor_canonical_context(
        {"stages": [_stage("frontdoor-stage-1", 1, rounds=rounds, evicted=True)]}
    )

    stages = list((ui_canonical_context_delta(before, after).get("stages") or []))

    assert len(stages) == 1
    assert stages[0]["context_evicted"] is True


def test_ui_delta_keeps_only_new_stages_and_backfills_live_bodies() -> None:
    old_rounds = [
        {
            "round_index": 1,
            "text": f"old round {index}",
            "tools": [
                _tool(
                    f"old-{index}",
                    output_text="x" * 3000 if index >= 4 else "old output",
                    arguments_text="q" * 6000 if index == 5 else "arg",
                )
            ],
        }
        for index in range(1, 6)
    ]
    new_round = {
        "round_index": 1,
        "text": "y" * 6000,
        "tools": [
            _tool(
                "new",
                output_text="z" * 3000,
                arguments_text="q" * 6000,
            )
        ],
    }
    persisted_stages = [
        _stage(f"frontdoor-stage-{index}", index, rounds=[old_rounds[index - 1]])
        for index in range(1, 6)
    ]
    persisted_projected = project_canonical_context_for_transcript(
        {
            "stages": [dict(stage) for stage in persisted_stages]
        }
    )
    live = {
        "active_stage_id": "",
        "stages": [
            _stage(
                f"frontdoor-stage-{index}",
                index,
                representation="raw",
                rounds=[old_rounds[index - 1]],
            )
            for index in range(1, 6)
        ]
        + [_stage("frontdoor-stage-6", 6, representation="raw", rounds=[new_round])],
    }

    ui_delta = ui_canonical_context_delta(persisted_projected, live)
    delta_stages = list(ui_delta.get("stages") or [])

    assert [stage["stage_id"] for stage in delta_stages] == ["frontdoor-stage-6"]
    rendered_round = delta_stages[0]["rounds"][0]
    rendered_tool = rendered_round["tools"][0]
    assert rendered_round["text"] == "y" * 6000
    assert rendered_tool["output_text"] == "z" * 3000
    assert rendered_tool["arguments_text"] == "q" * 6000


def test_ui_delta_does_not_reopen_settled_evicted_stages() -> None:
    """裁撤过的历史阶段在 live 帧里必须继续算"没变"。

    轨道要替被裁撤的阶段回填工具行，但回填不能发生在比对之前：transcript 投影把
    compact 阶段的 rounds 抹成 []，未投影的 live 阶段态带着正身，于是每一帧都会把
    这些早已收口的历史阶段重新算成新增。渠道会话实盘：573 条阶段里 545 条 / 2.85MB
    挤进最新气泡（正确答案是 0 条），且只在 live 侧错——刷新走的是投影对投影的快照
    路径，所以"刷新完就正常了"。
    """
    settled_round = {
        "round_index": 1,
        "text": "old round",
        "tools": [_tool("old-1", output_text="x" * 3000)],
    }
    settled = _stage("frontdoor-stage-1", 1, rounds=[settled_round], evicted=True)
    persisted_projected = project_canonical_context_for_transcript({"stages": [dict(settled)]})
    live_settled = _stage("frontdoor-stage-1", 1, rounds=[settled_round], evicted=True)

    assert ui_canonical_context_delta(persisted_projected, {"stages": [live_settled]}) == {}

    new_round = {
        "round_index": 1,
        "text": "new round",
        "tools": [_tool("new-1", output_text="fresh")],
    }
    live = {"stages": [live_settled, _stage("frontdoor-stage-2", 2, rounds=[new_round])]}

    delta_stages = list((ui_canonical_context_delta(persisted_projected, live).get("stages") or []))

    assert [stage["stage_id"] for stage in delta_stages] == ["frontdoor-stage-2"]


def test_ui_delta_backfills_rows_for_a_changed_evicted_stage() -> None:
    """裁撤阶段真的改了展示内容时，delta 仍要带上它的调用记录（回填发生在比对之后）。"""
    settled_round = {
        "round_index": 1,
        "text": "old round",
        "tools": [_tool("old-1", output_text="x" * 3000)],
    }
    persisted_projected = project_canonical_context_for_transcript(
        {"stages": [_stage("frontdoor-stage-1", 1, rounds=[settled_round], evicted=True)]}
    )
    live_stage = _stage(
        "frontdoor-stage-1",
        1,
        rounds=[settled_round],
        summary="收口之后又改了摘要",
        evicted=True,
    )

    stages = list((ui_canonical_context_delta(persisted_projected, {"stages": [live_stage]}).get("stages") or []))

    assert [stage["stage_id"] for stage in stages] == ["frontdoor-stage-1"]
    assert stages[0]["completed_stage_summary"] == "收口之后又改了摘要"
    assert stages[0]["representation"] == "compact"
    tool = stages[0]["rounds"][0]["tools"][0]
    assert tool["tool_call_id"] == "old-1:1"
    assert tool["output_text"] == ""


def test_ui_payload_projection_keeps_rows_for_evicted_stages() -> None:
    """Web 轨道永远带工具行：阶段被裁撤只该影响发送体，不该让界面显示"暂无工具轮次"。

    与 transcript 投影的分工是这条断言的意义所在：同一份账本，持久化投影把裁撤
    阶段的 rounds 清空（那是发送体与存储的口径），UI 投影必须把行补回来，并按
    transcript 上限收掉超长正文，避免整份轨道灌进每一帧。
    """
    context = {
        "stages": [
            _stage("frontdoor-stage-1", 1, evicted=True, rounds=[{"round_index": 1, "tools": [_tool("old")]}]),
            _stage("frontdoor-stage-2", 2, rounds=[{"round_index": 1, "tools": [_tool("old-2")]}]),
            _stage("frontdoor-stage-3", 3, rounds=[{"round_index": 1, "tools": [_tool("old-3")]}]),
            _stage(
                "frontdoor-stage-4",
                4,
                rounds=[
                    {
                        "round_index": 1,
                        "tools": [_tool("kept", output_text="x" * 3000)],
                    }
                ],
            ),
            _stage(
                "frontdoor-stage-5",
                5,
                rounds=[
                    {
                        "round_index": 1,
                        "tools": [_tool("kept-5", output_text="x" * 3000)],
                    }
                ],
            ),
        ]
    }

    projected = project_canonical_context_for_ui_payload(context)

    assert projected["stages"][0]["representation"] == "compact"
    assert projected["stages"][0]["rounds"][0]["tools"][0]["tool_call_id"] == "old:1"
    assert projected["stages"][1]["representation"] == "raw"
    assert projected["stages"][1]["rounds"][0]["tools"][0]["tool_call_id"] == "old-2:1"
    assert projected["stages"][-1]["representation"] == "raw"
    assert projected["stages"][-1]["rounds"][0]["tools"][0]["output_text"] == "x" * 3000
    # 同一行在持久化投影里确实是被清空的——两份投影的分工是刻意的
    from g3ku.runtime.frontdoor.canonical_context import project_canonical_context_for_transcript

    assert project_canonical_context_for_transcript(context)["stages"][0]["rounds"] == []


def test_ui_payload_projection_caps_evicted_stage_bodies() -> None:
    """裁撤阶段的正文按 transcript 上限收掉：行必须在，整段原文不灌进轨道。"""
    context = {
        "stages": [
            _stage(
                "frontdoor-stage-1",
                1,
                evicted=True,
                rounds=[{"round_index": 1, "tools": [_tool("big", output_text="x" * 3000)]}],
            )
        ]
    }

    row = project_canonical_context_for_ui_payload(context)["stages"][0]["rounds"][0]["tools"][0]
    assert row["tool_call_id"] == "big:1"
    assert row["output_text"] == ""


def test_transcript_projection_returns_empty_for_missing_stage_state() -> None:
    assert project_canonical_context_for_transcript({}) == {}
    assert project_canonical_context_for_transcript({"stages": []}) == {}


def _old_loop_deltas(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """改动前 `_build_ceo_snapshot` 的逐行 delta（滚动 UI 投影链），用于 parity 对比。"""
    previous: dict[str, object] = {}
    deltas = []
    for row in rows:
        canonical_context = row["canonical_context"]
        projected = project_canonical_context_for_ui_payload(canonical_context)
        deltas.append(ui_canonical_context_delta(previous, canonical_context))
        previous = projected or canonical_context
    return deltas


def test_snapshot_delta_contract_and_projection_parity() -> None:
    from g3ku.runtime.api import websocket_ceo

    def _cc(stage_count: int, revise_last: bool) -> dict[str, object]:
        stages = [
            _stage(
                f"frontdoor-stage-{index}",
                index,
                summary=f"summary {index} revised" if (revise_last and index == stage_count) else "",
                rounds=[
                    {
                        "round_index": 1,
                        "tools": [_tool(f"t{index}", output_text="out " * 500)],
                    }
                ],
            )
            for index in range(1, stage_count + 1)
        ]
        return {"active_stage_id": "", "stages": stages}

    raw_rows = [
        {"role": "assistant", "content": f"reply {i}", "canonical_context": _cc(4 + i, revise_last=(i == 2))}
        for i in range(3)
    ]
    marked_rows = [
        {
            "role": "assistant",
            "content": f"reply {i}",
            "canonical_context": project_canonical_context_for_transcript(row["canonical_context"]),
            "canonical_context_projection": TRANSCRIPT_PROJECTION_MODE,
        }
        for i, row in enumerate(raw_rows)
    ]

    for rows in (raw_rows, marked_rows):
        items = websocket_ceo._build_ceo_snapshot(rows)
        assert all("canonical_context" not in item for item in items)
        assert all("canonical_context_delta" in item for item in items)
        assert [item["canonical_context_delta"] for item in items] == _old_loop_deltas(rows)
