"""编辑重发/Fork 的截断边界定位与异步任务门槛（纯函数）测试。"""

from __future__ import annotations

from g3ku.runtime.web_ceo_history_edit import (
    compute_edit_fork_gates,
    message_turn_id,
    resolve_truncation_boundary,
    transcript_has_task_ids_field,
    visible_user_run_first_indices,
)


def _user(turn_id: str, content: str = "u", **metadata) -> dict:
    return {
        "role": "user",
        "content": content,
        "timestamp": f"2026-09-14T10:00:{turn_id}",
        "metadata": {"_transcript_turn_id": turn_id, **metadata},
    }


def _assistant(turn_id: str, content: str = "a", **metadata) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "timestamp": f"2026-09-14T10:01:{turn_id}",
        "turn_id": turn_id,
        "metadata": dict(metadata),
    }


def _internal_user(turn_id: str) -> dict:
    return _user(turn_id, "internal", heartbeat_internal=True, ui_visible=False)


# ---------- resolve_truncation_boundary ----------


def test_boundary_resolves_single_user_message():
    messages = [_user("t1"), _assistant("t1"), _user("t2"), _assistant("t2")]
    resolution = resolve_truncation_boundary(messages, "t2", available_boundary_turn_ids={"t1"})
    assert resolution is not None
    assert resolution.boundary_index == 2
    assert resolution.eligible is True
    assert resolution.reason == ""
    assert resolution.prev_turn_id == "t1"
    assert resolution.removed_turn_ids == ["t2"]


def test_boundary_expands_user_run_and_requires_run_first():
    # 批次兄弟:u1/u2/u3 连续 user 段共享一个执行轮,只有 run 首条可点。
    messages = [_user("t1"), _assistant("t1"), _user("t2"), _user("t3"), _assistant("t3")]
    first = resolve_truncation_boundary(messages, "t2", available_boundary_turn_ids={"t1"})
    assert first.eligible is True
    assert first.boundary_index == 2
    # 点 run 首条:整段(含全部兄弟与宿主轮回复)一并进入 removed。
    assert first.removed_turn_ids == ["t2", "t3"]
    middle = resolve_truncation_boundary(messages, "t3", available_boundary_turn_ids={"t1"})
    assert middle is not None
    assert middle.eligible is False
    assert middle.reason == "turn_not_run_first"
    assert middle.boundary_index == 2


def test_boundary_crosses_internal_rows_to_last_user_turn():
    # 内部轮不写边界快照：锚点跨过心跳的内部 user 行与可见回复，回到 t1。
    messages = [_user("t1"), _internal_user("h1"), _assistant("h1", source="heartbeat"), _user("t2")]
    resolution = resolve_truncation_boundary(messages, "t2", available_boundary_turn_ids={"t1"})
    assert resolution.boundary_index == 3
    assert resolution.eligible is True
    assert resolution.prev_turn_id == "t1"


def test_boundary_first_message_needs_no_snapshot():
    messages = [_user("t1"), _assistant("t1")]
    resolution = resolve_truncation_boundary(messages, "t1", available_boundary_turn_ids=set())
    assert resolution.boundary_index == 0
    assert resolution.eligible is True
    assert resolution.prev_turn_id == ""


def test_boundary_missing_snapshot_is_ineligible():
    messages = [_user("t1"), _assistant("t1"), _user("t2")]
    resolution = resolve_truncation_boundary(messages, "t2", available_boundary_turn_ids=set())
    assert resolution.eligible is False
    assert resolution.reason == "boundary_unavailable"


def test_boundary_followup_archive_resolves_archived_turn():
    # 归档行自己的复合 turn id 从不落快照：按 archived_from_turn_id 解包到被归档的轮。
    archive = _assistant("t1:followup:9", source="follow_up_archive", archived_from_turn_id="t1")
    messages = [_user("t1"), archive, _user("t2")]
    hit = resolve_truncation_boundary(messages, "t2", available_boundary_turn_ids={"t1"})
    assert hit.eligible is True
    assert hit.prev_turn_id == "t1"
    # 解包指向的轮次出窗时依旧不合格，不退回复合 id 去猜。
    stale = resolve_truncation_boundary(messages, "t2", available_boundary_turn_ids=set())
    assert stale.eligible is False
    assert stale.reason == "boundary_unavailable"


def test_boundary_unknown_turn_returns_none():
    messages = [_user("t1")]
    assert resolve_truncation_boundary(messages, "nope", available_boundary_turn_ids={"t1"}) is None
    # assistant 的 turn_id 不能作为编辑目标。
    messages2 = [_user("t1"), _assistant("t1")]
    assert resolve_truncation_boundary(messages2, "t1x", available_boundary_turn_ids=None) is None


def test_message_turn_id_prefers_top_level():
    assert message_turn_id({"turn_id": "top", "metadata": {"_transcript_turn_id": "meta"}}) == "top"
    assert message_turn_id({"metadata": {"_transcript_turn_id": "meta"}}) == "meta"


def test_run_first_indices():
    messages = [_user("t1"), _user("t2"), _assistant("t2"), _user("t3")]
    assert visible_user_run_first_indices(messages) == {0, 3}


# ---------- compute_edit_fork_gates ----------


def test_gates_disabled_returns_empty():
    messages = [_user("t1")]
    assert compute_edit_fork_gates(messages, enabled=False) == {}


def test_gates_dispatch_in_own_region_blocks_only_while_unfinished():
    # 截断删掉 [该消息, 末尾]：只有区间内的首次派发、且任务仍未跑完才关门。
    messages = [
        _user("t1"),
        _assistant("t1"),
        _user("t2"),
        _assistant("t2", task_ids=["task:abc"]),
        _user("t3"),
    ]
    available = {"t1", "t2"}
    live = compute_edit_fork_gates(
        messages,
        enabled=True,
        available_boundary_turn_ids=available,
        unfinished_task_ids={"task:abc"},
    )
    assert live[0] is False
    assert live[2] is False
    assert live[4] is True
    settled = compute_edit_fork_gates(
        messages,
        enabled=True,
        available_boundary_turn_ids=available,
        unfinished_task_ids=set(),
    )
    assert settled == {0: True, 2: True, 4: True}
    # 读不到任务服务时按仍未跑完保守处理。
    unknown = compute_edit_fork_gates(messages, enabled=True, available_boundary_turn_ids=available)
    assert unknown[2] is False


def test_gates_echo_rows_are_not_dispatch_points():
    # 心跳回复顺口提到的任务号（已在 t1 的回复里出现过）不建立新派发。
    messages = [
        _user("t1"),
        _assistant("t1", task_ids=["task:x"]),
        _assistant("h1", source="heartbeat", task_ids=["task:x"]),
        _user("t2"),
        _assistant("t2"),
        _user("t3"),
    ]
    gates = compute_edit_fork_gates(
        messages,
        enabled=True,
        available_boundary_turn_ids={"t1", "t2"},
        unfinished_task_ids=set(),
    )
    assert gates == {0: True, 3: True, 5: True}


def test_gates_dispatch_recorded_after_internal_reply_still_counts():
    # 派发记录可以在心跳回复之后才落进转录：它仍属被点击消息自己的区间。
    messages = [
        _user("t1"),
        _assistant("t1"),
        _user("t2"),
        _assistant("h1", source="heartbeat"),
        _assistant("t2", task_ids=["task:x"]),
    ]
    gates = compute_edit_fork_gates(
        messages,
        enabled=True,
        available_boundary_turn_ids={"t1"},
        unfinished_task_ids={"task:x"},
    )
    assert gates[0] is False
    assert gates[2] is False


def test_gates_internal_dispatch_before_the_message_does_not_block():
    # cron 内部轮派发的任务：在其之后的用户消息看来是边界之前建立的，记录留在前缀里。
    messages = [
        _user("t1"),
        _assistant("t1"),
        _internal_user("c1"),
        _assistant("c1", source="cron", task_ids=["task:y"]),
        _user("t2"),
    ]
    gates = compute_edit_fork_gates(
        messages,
        enabled=True,
        available_boundary_turn_ids={"t1"},
        unfinished_task_ids={"task:y"},
    )
    assert gates[0] is False
    assert gates[4] is True


def test_gates_run_first_and_boundary_availability():
    messages = [_user("t1"), _assistant("t1"), _user("t2"), _user("t3"), _assistant("t3")]
    gates = compute_edit_fork_gates(messages, enabled=True, available_boundary_turn_ids={"t1"})
    assert gates[0] is True
    assert gates[2] is True
    assert gates[3] is False  # run 中段不可点
    # 边界快照缺失 → run 首条也不显示。
    gates_missing = compute_edit_fork_gates(messages, enabled=True, available_boundary_turn_ids=set())
    assert gates_missing[0] is True  # 会话开头截断无需快照
    assert gates_missing[2] is False


def test_gates_legacy_timestamp_fallback():
    # 整份转录无 task_ids 字段:用任务 created_at 与回复时间戳比较兜底。
    messages = [
        _user("t1"),
        _assistant("t1"),
        _user("t2"),
        _assistant("t2"),
    ]
    assert transcript_has_task_ids_field(messages) is False
    # 仍未跑完的任务在 t1 发出之后、t2 发出之前创建 → 落在 t1 的区间里,t2 不受影响。
    gates = compute_edit_fork_gates(
        messages,
        enabled=True,
        task_created_ats=["2026-09-14T10:00:t15"],
        available_boundary_turn_ids={"t1"},
    )
    assert gates[0] is False
    assert gates[2] is True
    # 有 task_ids 字段时兜底不启用(即使传入 created_ats)。
    messages_with_field = messages + [_assistant("t3", task_ids=[])]
    assert transcript_has_task_ids_field(messages_with_field) is True
    gates2 = compute_edit_fork_gates(
        messages_with_field,
        enabled=True,
        task_created_ats=["2026-09-14T10:01:t15"],
        available_boundary_turn_ids={"t1"},
    )
    assert gates2[0] is True
    assert gates2[2] is True
