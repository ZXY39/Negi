from __future__ import annotations

import json

from g3ku.runtime.stage_prompt_compaction import (
    STAGE_COMPACT_PREFIX,
    STAGE_EXTERNALIZED_PREFIX,
    compact_stage_prompt_messages_in_place,
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


def test_in_place_compaction_removes_internal_events_only_after_structural_change_point() -> None:
    # 缓存中性：内部事件束只顺路清理"不早于本次压缩既有最早结构变化点"的条目；
    # 结构变化点之前的内部事件保留到后续压缩，避免把前缀断裂点提前。
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "你好"},
    ]
    messages.append({"role": "system", "content": "你接收到了之前你定时的任务，如下：\n当前定时任务 ID：pair-a"})
    messages.append({"role": "system", "content": "[CRON INTERNAL EVENT] pair-a"})
    messages.extend(_stage_window(1))
    messages.append({"role": "system", "content": "你接收到了之前你定时的任务，如下：\n当前定时任务 ID：pair-b"})
    messages.append({"role": "system", "content": "[CRON INTERNAL EVENT] pair-b"})
    messages.extend(_stage_window(2))
    stage_state = {
        "active_stage_id": "",
        "transition_required": False,
        "stages": [_stage_record(1, rounds=[_round(1, ["call-work-1"])]), _stage_record(2, rounds=[_round(2, ["call-work-2"])])],
    }

    gated = compact_stage_prompt_messages_in_place(messages, stage_state=stage_state, keep_latest_completed_stages=1)
    baseline = compact_stage_prompt_messages_in_place(
        messages, stage_state=stage_state, keep_latest_completed_stages=1, internal_event_markers=()
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
    assert len(gated_contents) == len(baseline_contents) - 2

