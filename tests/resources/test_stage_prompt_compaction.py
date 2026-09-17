from __future__ import annotations

import json

from g3ku.runtime.stage_prompt_compaction import (
    STAGE_COMPACT_PREFIX,
    STAGE_EXTERNALIZED_PREFIX,
    STAGE_RAW_PREFIX,
    compact_stage_prompt_messages_in_place,
    is_stage_block_echo_text,
    is_stage_context_message,
    keep_stage_blocks_off_continuation_tail,
    prepare_stage_prompt_messages,
    stage_prompt_prefix,
    strip_stage_block_echo,
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
                "rounds": [
                    {
                        "round_id": "stage-1:round-1",
                        "round_index": 1,
                        "tool_call_ids": ["call-stage-1-work"],
                        "tools": [{"tool_call_id": "call-stage-1-work", "tool_name": "record_tool"}],
                    }
                ],
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
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-stage-1-work",
                    "type": "function",
                    "function": {"name": "record_tool", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "record_tool", "tool_call_id": "call-stage-1-work", "content": "stage one tool output"},
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
        {
            "role": "assistant",
            "content": "current stage assistant detail",
            "tool_calls": [
                {
                    "id": "call-current",
                    "type": "function",
                    "function": {"name": "record_tool", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "record_tool", "tool_call_id": "call-current", "content": "current stage tool output"},
    ]

    prepared = prepare_stage_prompt_messages(
        original,
        stage_state=stage_state,
        keep_latest_completed_stages=3,
        stage_tool_name="submit_next_stage",
    )

    rendered_contents = [str(item.get("content") or "") for item in prepared]
    # 最近 3 个完成阶段与活动阶段的工具调用原位保留
    assert "stage two raw detail" in rendered_contents
    assert "stage three raw detail" in rendered_contents
    assert "stage four raw detail" in rendered_contents
    assert "current stage assistant detail" in rendered_contents
    assert "current stage tool output" in rendered_contents
    # 过期阶段的工具肉身被移除，但其文本汇报作为对话保留
    assert "stage one tool output" not in rendered_contents
    assert "stage one raw detail" in rendered_contents

    compact_blocks = [
        content
        for content in rendered_contents
        if content.startswith(STAGE_COMPACT_PREFIX)
    ]
    assert len(compact_blocks) == 1
    # 阶段块以 system 角色落地（压缩元数据、非对话内容）
    compact_messages = [
        item
        for item in prepared
        if str(item.get("content") or "").startswith(STAGE_COMPACT_PREFIX)
    ]
    assert [str(item.get("role")) for item in compact_messages] == ["system"]
    compact_payload = json.loads(compact_blocks[0].split("\n", 1)[1])
    assert compact_payload["stage_index"] == 1
    assert compact_payload["completed_stage_summary"] == "finished stage one"
    # 压缩块原位放置：落在阶段 1 被移除工具消息的位置，而不是整体置顶
    block_index = rendered_contents.index(compact_blocks[0])
    assert rendered_contents.index("stage one raw detail") == block_index + 1


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
    # 外置归档块同样以 system 角色落地
    externalized_messages = [
        item
        for item in prepared
        if str(item.get("content") or "").startswith(STAGE_EXTERNALIZED_PREFIX)
    ]
    assert [str(item.get("role")) for item in externalized_messages] == ["system"]
    payload = json.loads(externalized_blocks[0].split("\n", 1)[1])
    assert payload["archive_ref"] == "artifact:artifact:stage-archive-1"
    assert payload["archive_stage_index_start"] == 1
    assert payload["archive_stage_index_end"] == 10


def _stage_record(index: int, *, status: str = "completed", rounds: list | None = None) -> dict[str, object]:
    return {
        "stage_id": f"frontdoor-stage-{index}",
        "stage_index": index,
        "stage_kind": "normal",
        "system_generated": False,
        "mode": "自主执行",
        "status": status,
        "stage_goal": f"goal {index}",
        "completed_stage_summary": "" if status == "active" else f"finished {index}",
        "key_refs": [],
        "tool_round_budget": 3,
        "tool_rounds_used": len(rounds or []),
        "rounds": rounds or [],
    }


def _round(index: int, call_ids: list[str]) -> dict[str, object]:
    return {
        "round_id": f"frontdoor-stage-{index}:round-1",
        "round_index": 1,
        "tool_call_ids": list(call_ids),
        "tools": [{"tool_call_id": call_id, "tool_name": "exec"} for call_id in call_ids],
    }


def _stage_window(index: int) -> list[dict[str, object]]:
    submit_call_id = f"call-submit-{index}"
    work_call_id = f"call-work-{index}"
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": submit_call_id, "type": "function", "function": {"name": "submit_next_stage", "arguments": "{}"}}
            ],
        },
        {
            "role": "tool",
            "name": "submit_next_stage",
            "tool_call_id": submit_call_id,
            "content": json.dumps(
                {"stage_id": f"frontdoor-stage-{index}", "stage_index": index}, ensure_ascii=False
            ),
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": work_call_id, "type": "function", "function": {"name": "exec", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "name": "exec", "tool_call_id": work_call_id, "content": f"output-{index}"},
        {"role": "assistant", "content": f"report-{index}"},
    ]


def _five_completed_stage_state() -> dict[str, object]:
    return {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [_stage_record(index, rounds=[_round(index, [f"call-work-{index}"])]) for index in range(1, 6)],
    }


def test_in_place_compaction_without_active_stage_keeps_latest_three_raw() -> None:
    # 回归：无活动阶段（纯对话回合）不再触发"全压缩 + 全保留"的退化分支
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
    ]
    for index in range(1, 6):
        messages.extend(_stage_window(index))
    messages.append({"role": "user", "content": "现在能看见哪些阶段的工具调用？"})

    result = compact_stage_prompt_messages_in_place(
        messages, stage_state=_five_completed_stage_state(), keep_latest_completed_stages=3
    )

    assert result["stage_compaction_applied"] is True
    assert result["retained_completed_stage_ids"] == {
        "frontdoor-stage-3",
        "frontdoor-stage-4",
        "frontdoor-stage-5",
    }
    contents = [str(item.get("content") or "") for item in result["rewritten"]]
    # 阶段 1/2 的工具肉身被移除，3/4/5 完整保留
    assert "output-1" not in contents
    assert "output-2" not in contents
    assert "output-3" in contents
    assert "output-4" in contents
    assert "output-5" in contents
    # 压缩块只有 2 个，且原位放置（紧邻各自阶段的文本汇报之前）
    compact_blocks = [content for content in contents if content.startswith(STAGE_COMPACT_PREFIX)]
    assert len(compact_blocks) == 2
    assert contents.index(compact_blocks[0]) + 1 == contents.index("report-1")
    assert contents.index(compact_blocks[1]) + 1 == contents.index("report-2")
    # 对话与阶段文本汇报原位保留
    assert "现在能看见哪些阶段的工具调用？" in contents
    assert "report-1" in contents


def test_in_place_compaction_removes_internal_event_bundles_but_keeps_dialogue() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    messages.extend(_stage_window(1))
    messages.append({"role": "user", "content": "This is a background heartbeat. Do not explain internal mechanics."})
    messages.append({"role": "assistant", "content": "心跳可见播报"})
    messages.extend(_stage_window(2))
    stage_state = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [_stage_record(1, rounds=[_round(1, ["call-work-1"])]), _stage_record(2, rounds=[_round(2, ["call-work-2"])])],
    }

    result = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=1
    )

    contents = [str(item.get("content") or "") for item in result["rewritten"]]
    assert all("This is a background heartbeat." not in content for content in contents)
    assert "心跳可见播报" in contents  # 用户可见回复保留
    assert "你好" in contents
    assert "output-2" in contents  # 保留阶段
    assert "output-1" not in contents  # 过期阶段被压


def test_in_place_compaction_is_idempotent_and_dedupes_stale_blocks() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
    ]
    for index in range(1, 6):
        messages.extend(_stage_window(index))
    stage_state = _five_completed_stage_state()

    first = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=3
    )
    first_output = [*first["prefix"], *first["rewritten"]]
    second = compact_stage_prompt_messages_in_place(
        first_output, stage_state=stage_state, keep_latest_completed_stages=3
    )
    second_output = [*second["prefix"], *second["rewritten"]]
    assert first_output == second_output
    assert second["removed_message_count"] == 0
    assert second["stage_compaction_applied"] is False

    # 闸门 bug 遗留的"保留阶段也有块"布局：残留块被去重丢弃，raw 不受影响
    stale_layout = list(first_output)
    stale_block = {
        "role": "assistant",
        "content": (
            f"{STAGE_COMPACT_PREFIX}\n"
            + json.dumps(
                {"stage_index": 5, "stage_kind": "normal", "completed_stage_summary": "finished 5"},
                ensure_ascii=False,
                sort_keys=True,
            )
        ),
    }
    stale_layout.insert(3, stale_block)
    cleaned = compact_stage_prompt_messages_in_place(
        stale_layout, stage_state=stage_state, keep_latest_completed_stages=3
    )
    cleaned_contents = [str(item.get("content") or "") for item in cleaned["rewritten"]]
    block_count = sum(1 for content in cleaned_contents if content.startswith(STAGE_COMPACT_PREFIX))
    assert block_count == 2  # 阶段 5 的残留块被去重，仅阶段 1/2 的块存在
    assert "output-5" in cleaned_contents


def _block_positions_by_stage(rendered: list[dict[str, object]]) -> dict[int, int]:
    positions: dict[int, int] = {}
    for position, item in enumerate(rendered):
        content = str(item.get("content") or "")
        if not content.startswith(STAGE_COMPACT_PREFIX):
            continue
        payload = json.loads(content.split("\n", 1)[1])
        positions[int(payload["stage_index"])] = position
    return positions


def _stage_state_for_indexes(indexes: list[int]) -> dict[str, object]:
    return {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            _stage_record(index, rounds=[_round(index, [f"call-work-{index}"])]) for index in indexes
        ],
    }


def test_compaction_keeps_user_block_reply_order() -> None:
    # 保序契约：压缩后仍是「用户消息 → 该阶段压缩块 → 该阶段最终回复」，且块不落在
    # 最后一条 user 之后（末位保持当前用户回合，不占模型的续写位）。
    messages: list[dict[str, object]] = [{"role": "system", "content": "system"}]
    for index in range(1, 6):
        messages.append({"role": "user", "content": f"ask-{index}"})
        messages.extend(_stage_window(index))
    messages.append({"role": "user", "content": "current turn"})

    result = compact_stage_prompt_messages_in_place(
        messages, stage_state=_five_completed_stage_state(), keep_latest_completed_stages=3
    )
    rendered = [*result["prefix"], *result["rewritten"]]
    contents = [str(item.get("content") or "") for item in rendered]
    positions = _block_positions_by_stage(rendered)

    for index in (1, 2):  # 过期阶段：块夹在自己的用户消息与最终回复之间
        assert contents.index(f"ask-{index}") < positions[index] < contents.index(f"report-{index}")
    # 阶段顺序不变量：块位置相对 stage_index 单调不减
    assert [positions[index] for index in sorted(positions)] == sorted(positions.values())
    last_user = max(
        position for position, item in enumerate(rendered) if str(item.get("role")) == "user"
    )
    assert all(position < last_user for position in positions.values())


def test_compaction_never_parks_frameless_blocks_at_request_head() -> None:
    # 回归：阶段帧与旧块双双缺失（请求体被重建过）时，兜底锚点不得是 0——实测
    # ext:qq-official:f8a8001865631301 有 6 个块被永久钉在请求最前面，脱离自己的
    # 对话。改为邻居夹逼后，这类块落在前一个已确定锚点之后。
    messages: list[dict[str, object]] = [{"role": "system", "content": "system"}]
    for index in (1, 2):
        messages.append({"role": "user", "content": f"ask-{index}"})
        messages.extend(_stage_window(index))
    for index in (3, 4, 5):  # 无帧阶段：只剩对话，没有任何可锚定的工具帧
        messages.append({"role": "user", "content": f"ask-{index}"})
        messages.append({"role": "assistant", "content": f"report-{index}"})
    messages.append({"role": "user", "content": "current turn"})

    result = compact_stage_prompt_messages_in_place(
        messages,
        stage_state=_stage_state_for_indexes([1, 2, 3, 4, 5, 6]),
        keep_latest_completed_stages=1,
    )
    rendered = [*result["prefix"], *result["rewritten"]]
    contents = [str(item.get("content") or "") for item in rendered]
    positions = _block_positions_by_stage(rendered)

    # 阶段 6 保留为 raw；1/2 有帧可锚定，3/4/5 无帧
    assert sorted(positions) == [1, 2, 3, 4, 5]
    assert min(positions.values()) > 0  # 不再被甩到请求最前面
    assert [positions[index] for index in sorted(positions)] == sorted(positions.values())
    for index in (3, 4, 5):
        assert positions[index] >= positions[2]
        assert positions[index] < contents.index(f"report-{index}")


def test_compaction_places_roundless_stage_block_in_neighbor_window() -> None:
    # 0 轮阶段没有 call_id 可锚定（真实会话里 279 个阶段中有 1 例），必须按邻居落位：
    # 排在前一个已确定锚点之后，且仍在它自己那段回复之前。
    stage_state = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            _stage_record(1, rounds=[_round(1, ["call-work-1"])]),
            {**_stage_record(2), "rounds": []},
            _stage_record(3, rounds=[_round(3, ["call-work-3"])]),
        ],
    }
    messages: list[dict[str, object]] = [{"role": "system", "content": "system"}]
    messages.append({"role": "user", "content": "ask-1"})
    messages.extend(_stage_window(1))
    messages.append({"role": "user", "content": "ask-2"})
    messages.append({"role": "assistant", "content": "report-2"})
    messages.append({"role": "user", "content": "ask-3"})
    messages.extend(_stage_window(3))
    messages.append({"role": "user", "content": "current turn"})

    result = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=1
    )
    rendered = [*result["prefix"], *result["rewritten"]]
    contents = [str(item.get("content") or "") for item in rendered]
    positions = _block_positions_by_stage(rendered)

    assert sorted(positions) == [1, 2]
    assert positions[1] < positions[2]
    assert positions[2] < contents.index("report-2")
    assert positions[2] < contents.index("current turn")


def test_compact_block_payload_omits_constant_and_empty_fields() -> None:
    # 骨架精简：常量字段（stage_kind / status / 默认 mode）与空字段不再逐块重发。
    # stage_index 必须保留——_stage_block_stage_index 依赖它做块位置记忆与存量去重。
    messages: list[dict[str, object]] = [{"role": "system", "content": "system"}, {"role": "user", "content": "hi"}]
    for index in range(1, 6):
        messages.extend(_stage_window(index))

    result = compact_stage_prompt_messages_in_place(
        messages, stage_state=_five_completed_stage_state(), keep_latest_completed_stages=3
    )
    payloads = [
        json.loads(str(item.get("content") or "").split("\n", 1)[1])
        for item in result["rewritten"]
        if str(item.get("content") or "").startswith(STAGE_COMPACT_PREFIX)
    ]
    assert len(payloads) == 2
    for payload in payloads:
        assert payload["stage_index"] in (1, 2)
        assert payload["stage_goal"]
        assert payload["completed_stage_summary"]
        assert payload["tool_rounds"] == "1/3"
        assert "stage_kind" not in payload
        assert "status" not in payload
        assert "mode" not in payload  # 默认值"自主执行"不写
        assert "system_generated" not in payload
        assert "key_refs" not in payload  # 空引用省略


def test_compact_block_payload_keeps_non_default_mode_and_generated_flag() -> None:
    # 非默认值必须保住，否则精简会丢信息。
    stage_state = _stage_state_for_indexes([1, 2])
    stage_state["stages"][0]["mode"] = "包含派生"
    stage_state["stages"][0]["system_generated"] = True
    stage_state["stages"][0]["key_refs"] = [{"ref": "artifact:demo", "note": "证据"}]
    messages: list[dict[str, object]] = [{"role": "system", "content": "system"}, {"role": "user", "content": "hi"}]
    for index in range(1, 4):
        messages.extend(_stage_window(index))

    result = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=0
    )
    payloads = {
        json.loads(str(item.get("content") or "").split("\n", 1)[1])["stage_index"]: json.loads(
            str(item.get("content") or "").split("\n", 1)[1]
        )
        for item in result["rewritten"]
        if str(item.get("content") or "").startswith(STAGE_COMPACT_PREFIX)
    }
    assert payloads[1]["mode"] == "包含派生"
    assert payloads[1]["system_generated"] is True
    assert payloads[1]["key_refs"] == [{"ref": "artifact:demo", "note": "证据"}]
    assert "mode" not in payloads[2]


def test_stage_blocks_render_with_system_role() -> None:
    # 角色对齐（事故 ext:qq-official:f8a8001865631301）：阶段块是运行时标注的
    # 已完成阶段摘要（压缩元数据、非对话内容），必须以 system 角色落地；
    # assistant 角色会向模型示范"你的回复长这样"，诱导其在续写位置仿造/回显
    # 整块 JSON。[G3KU_TOKEN_COMPACT_V2] 例外保持 assistant（自然语言会话摘要，
    # 渲染在 _ceo_runtime_ops，不在本模块）。
    stage_state = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-compression-1-10",
                "stage_index": 10,
                "stage_kind": "compression",
                "system_generated": True,
                "status": "completed",
                "stage_goal": "Archive completed stage history 1-10",
                "completed_stage_summary": "archived",
                "archive_ref": "artifact:artifact:legacy-archive",
                "archive_stage_index_start": 1,
                "archive_stage_index_end": 10,
                "tool_round_budget": 0,
                "tool_rounds_used": 0,
            },
            *[
                _stage_record(index, rounds=[_round(index, [f"call-work-{index}"])])
                for index in range(11, 16)
            ],
        ],
    }
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
    ]
    for index in range(11, 16):
        messages.extend(_stage_window(index))

    result = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=3
    )

    blocks = [
        item
        for item in result["rewritten"]
        if str(item.get("content") or "").startswith(
            (STAGE_COMPACT_PREFIX, STAGE_EXTERNALIZED_PREFIX, STAGE_RAW_PREFIX)
        )
    ]
    assert blocks
    assert all(str(item.get("role") or "") == "system" for item in blocks)
    rendered_prefixes = {str(item.get("content") or "").split("\n", 1)[0] for item in blocks}
    assert STAGE_COMPACT_PREFIX in rendered_prefixes
    assert STAGE_EXTERNALIZED_PREFIX in rendered_prefixes


def test_in_place_compaction_accepts_mixed_legacy_assistant_and_system_blocks() -> None:
    # 迁移期双角色识别：存量 durable baseline / continuity sidecar / 续跑 seed /
    # actual-request scaffold 里可能仍有 assistant 角色旧块，与新渲染的 system 块
    # 混存。压缩必须两类都剥离并去重回插——不得因识别不到旧块而重复或丢失，
    # 且重复执行收敛（幂等），旧块随下一次渲染自然收敛为 system 角色。
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
    ]
    for index in range(1, 6):
        messages.extend(_stage_window(index))
    stage_state = _five_completed_stage_state()

    first = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=3
    )
    first_output = [*first["prefix"], *first["rewritten"]]
    stage_one_block = next(
        item
        for item in first_output
        if str(item.get("content") or "").startswith(STAGE_COMPACT_PREFIX)
        and int(json.loads(str(item.get("content")).split("\n", 1)[1])["stage_index"]) == 1
    )
    assert str(stage_one_block.get("role")) == "system"

    # 模拟存量旧块：同内容、assistant 角色，混入历史另一位置
    legacy_block = {"role": "assistant", "content": stage_one_block["content"]}
    mixed = list(first_output)
    mixed.insert(2, legacy_block)

    second = compact_stage_prompt_messages_in_place(
        mixed, stage_state=stage_state, keep_latest_completed_stages=3
    )
    second_output = [*second["prefix"], *second["rewritten"]]
    # 旧块被剥离并去重：收敛回与无旧块时完全相同的布局
    assert second_output == first_output
    assert sum(
        1 for item in second_output if str(item.get("content") or "").startswith(STAGE_COMPACT_PREFIX)
    ) == 2

    third = compact_stage_prompt_messages_in_place(
        second_output, stage_state=stage_state, keep_latest_completed_stages=3
    )
    assert [*third["prefix"], *third["rewritten"]] == second_output


def test_keep_stage_blocks_off_continuation_tail_moves_trailing_blocks_before_last_user() -> None:
    # 块不占续写位：落在最后一条 user 之后的阶段块（新 system 块与存量旧
    # assistant 块都算）整体前移到该 user 之前，保持块间相对顺序。
    block = {"role": "system", "content": f'{STAGE_COMPACT_PREFIX}\n{{"stage_index":1}}'}
    legacy_block = {"role": "assistant", "content": f'{STAGE_RAW_PREFIX}\n{{"stage_index":2}}'}
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "bootstrap"},
        {"role": "user", "content": "current turn"},
        block,
        legacy_block,
    ]

    guarded = keep_stage_blocks_off_continuation_tail(messages)

    assert [str(item.get("content") or "") for item in guarded] == [
        "system",
        "bootstrap",
        block["content"],
        legacy_block["content"],
        "current turn",
    ]

    # 无越界块时原样返回（缓存中性：不做任何重排）
    already_ok = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "bootstrap"},
        block,
        {"role": "user", "content": "current turn"},
    ]
    assert keep_stage_blocks_off_continuation_tail(already_ok) == already_ok

    # 无 user 消息时原样返回（后续组装会补当前用户回合到末位）
    assert keep_stage_blocks_off_continuation_tail([block]) == [block]
    assert keep_stage_blocks_off_continuation_tail([]) == []


def test_in_place_compaction_renders_legacy_compression_stage_blocks() -> None:
    stage_state = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            {
                "stage_id": "frontdoor-compression-1-10",
                "stage_index": 10,
                "stage_kind": "compression",
                "system_generated": True,
                "status": "completed",
                "stage_goal": "Archive completed stage history 1-10",
                "completed_stage_summary": "archived",
                "archive_ref": "artifact:artifact:legacy-archive",
                "archive_stage_index_start": 1,
                "archive_stage_index_end": 10,
                "tool_round_budget": 0,
                "tool_rounds_used": 0,
            },
            _stage_record(11, rounds=[_round(11, ["call-work-11"])]),
        ],
    }
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
    ]
    messages.extend(_stage_window(11))

    result = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=3
    )
    contents = [str(item.get("content") or "") for item in result["rewritten"]]
    externalized = [content for content in contents if content.startswith(STAGE_EXTERNALIZED_PREFIX)]
    assert len(externalized) == 1
    assert "artifact:artifact:legacy-archive" in externalized[0]
    # 遗留压缩阶段无窗口痕迹，块落在重写区开头
    assert contents[0].startswith(STAGE_EXTERNALIZED_PREFIX)
    assert "output-11" in contents  # 最近 3 保留

def test_in_place_compaction_keeps_internal_events_when_no_structural_change() -> None:
    # 缓存中性：本次压缩没有任何阶段结构变化时，内部事件束一律保留，
    # 不允许为清理历史定时任务凭空打断 provider 前缀缓存。
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "你接收到了之前你定时的任务，如下：\n当前定时任务 ID：abc"},
        {"role": "system", "content": "[CRON INTERNAL EVENT]\n{}"},
        {"role": "user", "content": "This is a background heartbeat. stay calm"},
        {"role": "assistant", "content": "ok"},
    ]
    stage_state = {"active_stage_id": "", "transition_required": False, "stages": []}

    result = compact_stage_prompt_messages_in_place(messages, stage_state=stage_state)

    contents = [str(item.get("content") or "") for item in result["rewritten"]]
    assert result["removed_message_count"] == 0
    assert result["stage_compaction_applied"] is False
    assert any("[CRON INTERNAL EVENT]" in content for content in contents)
    assert any("This is a background heartbeat." in content for content in contents)


def test_in_place_compaction_keeps_event_bodies_and_removes_rule_text_after_change_point() -> None:
    # 内部事件束拆两类：事件体（心跳事件束/定时种子）是因果载荷，压缩后必须保留；
    # 规则文本（每次心跳重复注入的框架规则）才随过期内容顺路清理。
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    # 结构变化点之前：规则与事件体都保留
    messages.append({"role": "system", "content": "This is a background heartbeat.\n# Heartbeat Rules\n.rule-a."})
    messages.append({"role": "user", "content": "## EVENT BUNDLE\n- Task paused (.event-a.)"})
    messages.append({"role": "system", "content": "你接收到了之前你定时的任务，如下：\n当前定时任务 ID：cron-a"})
    messages.append({"role": "system", "content": "[CRON INTERNAL EVENT] cron-a"})
    messages.extend(_stage_window(1))
    # 结构变化点之后：规则文本顺路清理，事件体保留
    messages.append({"role": "system", "content": "This is a background heartbeat.\n# Heartbeat Rules\n.rule-b."})
    messages.append({"role": "user", "content": "## EVENT BUNDLE\n- Task paused (.event-b.)"})
    messages.append({"role": "system", "content": "你接收到了之前你定时的任务，如下：\n当前定时任务 ID：cron-b"})
    messages.append({"role": "system", "content": "[CRON INTERNAL EVENT] cron-b"})
    messages.extend(_stage_window(2))
    stage_state = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [
            _stage_record(1, rounds=[_round(1, ["call-work-1"])]),
            _stage_record(2, rounds=[_round(2, ["call-work-2"])]),
        ],
    }

    result = compact_stage_prompt_messages_in_place(messages, stage_state=stage_state, keep_latest_completed_stages=1)
    contents = [str(item.get("content") or "") for item in result["rewritten"]]

    # 规则文本：变化点前保留、之后清理
    assert any(".rule-a." in content for content in contents)
    assert all(".rule-b." not in content for content in contents)
    # 事件体（心跳事件束 + 定时种子）：前后都保留，保证后续回合能够追溯因果
    assert any(".event-a." in content for content in contents)
    assert any(".event-b." in content for content in contents)
    assert any("cron-a" in content for content in contents)
    assert any("cron-b" in content for content in contents)
    # 过期阶段仍被压缩，保留阶段不动
    assert "output-1" not in contents
    assert "output-2" in contents


def test_in_place_compaction_removes_rule_text_only_after_structural_change_point() -> None:
    # 缓存中性：内部规则文本只顺路清理"不早于本次压缩既有最早结构变化点"的条目；
    # 结构变化点之前的规则保留，避免把前缀断裂点提前。
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    messages.append({"role": "system", "content": "This is a background heartbeat.\n# Heartbeat Rules\npair-a"})
    messages.extend(_stage_window(1))
    messages.append({"role": "system", "content": "This is a background heartbeat.\n# Heartbeat Rules\npair-b"})
    messages.extend(_stage_window(2))
    stage_state = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [_stage_record(1, rounds=[_round(1, ["call-work-1"])]), _stage_record(2, rounds=[_round(2, ["call-work-2"])])],
    }

    gated = compact_stage_prompt_messages_in_place(messages, stage_state=stage_state, keep_latest_completed_stages=1)
    baseline = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=1, internal_rule_markers=()
    )

    gated_contents = [str(item.get("content") or "") for item in gated["rewritten"]]
    baseline_contents = [str(item.get("content") or "") for item in baseline["rewritten"]]
    # 结构变化点之前的 pair-a 保留；之后的 pair-b 顺路清理
    assert any("pair-a" in content for content in gated_contents)
    assert all("pair-b" not in content for content in gated_contents)
    assert "output-1" not in gated_contents  # 过期阶段仍被压缩
    assert "output-2" in gated_contents  # 保留阶段不动

    # 缓存中性：与无标记基线相比，首次分叉不早于基线自身的结构变化区域，
    # 即 gated 输出在基线首个结构变化点之前与基线逐条相同。
    common_length = min(len(baseline_contents), len(gated_contents))
    first_diff = next(
        (index for index in range(common_length) if baseline_contents[index] != gated_contents[index]),
        common_length,
    )
    structural_onset = next(
        index for index, content in enumerate(baseline_contents) if content.startswith(STAGE_COMPACT_PREFIX)
    )
    # gated 只比基线多删了 pair-b：分叉点不早于基线自身的压缩块回插位置
    assert first_diff >= structural_onset
    assert len(gated_contents) == len(baseline_contents) - 1


_STAGE_ECHO_PREFIX_PAYLOADS = [
    (STAGE_COMPACT_PREFIX, '{"stage_index": 48, "status": "completed"}'),
    (STAGE_EXTERNALIZED_PREFIX, '{"stage_index": 48, "stage_kind": "compression"}'),
    (STAGE_RAW_PREFIX, '{"stage_index": 48}'),
]


def test_is_stage_block_echo_text_matches_all_three_prefixes() -> None:
    for prefix, payload in _STAGE_ECHO_PREFIX_PAYLOADS:
        block = f"{prefix}\n{payload}"
        assert is_stage_block_echo_text(block) is True
        # 前后空白不影响判定
        assert is_stage_block_echo_text(f"  \n{block}\n") is True
    # 正常回复与内部提及（非块首）不算回显
    assert is_stage_block_echo_text("") is False
    assert is_stage_block_echo_text("这是给用户的可见回复。") is False
    assert is_stage_block_echo_text(f"可见回复，引用过 {STAGE_COMPACT_PREFIX} 标记。") is False


def test_strip_stage_block_echo_removes_standalone_and_tail_blocks() -> None:
    for prefix, payload in _STAGE_ECHO_PREFIX_PAYLOADS:
        block = f"{prefix}\n{payload}"
        assert strip_stage_block_echo(block) == ""
        assert strip_stage_block_echo(f"可见答案。\n\n{block}") == "可见答案。"
    # 无块的文本原样保留
    assert strip_stage_block_echo("可见答案。") == "可见答案。"



def test_stage_echo_with_tool_calls_is_not_treated_as_stage_block() -> None:
    # 携带 tool_calls 的 assistant 消息即使以阶段块前缀开头，也不是阶段块：
    # 整块丢弃会连带丢掉工具调用声明，使其配对 role=tool 结果成为孤儿工具
    # 结果（生产事故：ext 会话连续 5 天每轮携带同一对孤儿结果发给供应商）。
    echo_block = f"{STAGE_COMPACT_PREFIX}\n" + json.dumps(
        {"stage_index": 30, "status": "completed"}, ensure_ascii=False
    )
    echo_with_calls = {
        "role": "assistant",
        "content": echo_block,
        "tool_calls": [
            {
                "id": "call_echo_1",
                "type": "function",
                "function": {"name": "load_tool_context", "arguments": "{}"},
            },
        ],
    }
    echo_without_calls = {"role": "assistant", "content": echo_block}
    system_block = {"role": "system", "content": echo_block}

    assert is_stage_context_message(echo_without_calls) is True
    assert is_stage_context_message(system_block) is True
    # 携带 tool_calls 的回显回合不识别为阶段块（节点/会话通道共享此谓词）。
    assert is_stage_context_message(echo_with_calls) is False

    messages = [
        {"role": "system", "content": "sys"},
        echo_with_calls,
        {"role": "tool", "content": '{"ok": true}', "tool_call_id": "call_echo_1"},
        {"role": "user", "content": "继续"},
    ]
    prefix, remainder = stage_prompt_prefix(messages)
    kept = prefix + remainder
    # 回显回合与其 tool 结果都保留，不产生孤儿。
    assert any(list(item.get("tool_calls") or []) for item in kept if item.get("role") == "assistant")
    assert any(str(item.get("tool_call_id") or "") == "call_echo_1" for item in kept)

    # 尾部守卫同样不得把携带 tool_calls 的回显当作阶段块前移重排。
    tail_messages = [
        {"role": "user", "content": "current turn"},
        echo_with_calls,
        {"role": "tool", "content": '{"ok": true}', "tool_call_id": "call_echo_1"},
    ]
    reordered = keep_stage_blocks_off_continuation_tail(tail_messages)
    assert [str(item.get("role")) for item in reordered] == ["user", "assistant", "tool"]
