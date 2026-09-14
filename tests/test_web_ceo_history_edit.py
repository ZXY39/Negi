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


def test_boundary_internal_messages_break_run():
    # 心跳内部 user 消息不属于 run,也不让两侧可见 user 合并。
    messages = [_user("t1"), _internal_user("h1"), _assistant("h1", source="heartbeat"), _user("t2")]
    resolution = resolve_truncation_boundary(messages, "t2", available_boundary_turn_ids={"h1"})
    assert resolution.boundary_index == 3
    assert resolution.eligible is True
    assert resolution.prev_turn_id == "h1"


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


def test_boundary_followup_archive_prev_is_not_clean():
    # prev 消息是 follow-up 归档(派生 turn id 含 :followup:),不在边界快照集合中 → 不合格。
    archive = _assistant("t1:followup:9", source="follow_up_archive")
    messages = [_user("t1"), archive, _user("t2")]
    resolution = resolve_truncation_boundary(messages, "t2", available_boundary_turn_ids={"t1"})
    assert resolution.eligible is False
    assert resolution.reason == "boundary_unavailable"


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


def test_gates_strict_task_rule_closes_own_reply_turn():
    # t2 的回复轮创建了任务 → t2 自己与其后所有消息全部关门(严格判定)。
    messages = [
        _user("t1"),
        _assistant("t1"),
        _user("t2"),
        _assistant("t2", task_ids=["task:abc"]),
        _user("t3"),
    ]
    gates = compute_edit_fork_gates(messages, enabled=True, available_boundary_turn_ids={"t1", "t2"})
    assert gates[0] is True
    assert gates[2] is False
    assert gates[4] is False


def test_gates_internal_reply_does_not_clear_pending_group():
    # 心跳可见回复不清组:t2 的用户轮回复创建任务时,回溯仍要关掉 t2。
    messages = [
        _user("t1"),
        _assistant("t1"),
        _user("t2"),
        _assistant("h1", source="heartbeat"),
        _assistant("t2", task_ids=["task:x"]),
    ]
    gates = compute_edit_fork_gates(messages, enabled=True, available_boundary_turn_ids={"t1"})
    assert gates[0] is True
    assert gates[2] is False


def test_gates_internal_dispatch_blocks_later_messages():
    # cron 内部轮派发任务:其后的用户消息按前缀规则关门。
    messages = [
        _user("t1"),
        _assistant("t1"),
        _internal_user("c1"),
        _assistant("c1", source="cron", task_ids=["task:y"]),
        _user("t2"),
    ]
    gates = compute_edit_fork_gates(messages, enabled=True, available_boundary_turn_ids={"c1"})
    assert gates[0] is True
    assert gates[4] is False


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
    # 任务在 t1 回复之后、t2 回复之前创建 → t2 关门,t1 保留。
    gates = compute_edit_fork_gates(
        messages,
        enabled=True,
        task_created_ats=["2026-09-14T10:01:t15"],
        available_boundary_turn_ids={"t1"},
    )
    assert gates[0] is True
    assert gates[2] is False
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
