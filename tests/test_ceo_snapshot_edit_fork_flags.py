"""快照消息级 can_edit_fork / can_fork / task_dispatched 标志与防御型稳定态总开关测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from g3ku.runtime import web_ceo_sessions as wcs
from g3ku.runtime.api import websocket_ceo


def _user(turn_id: str, content: str = "u") -> dict:
    return {
        "role": "user",
        "content": content,
        "timestamp": f"2026-09-14T10:00:0{turn_id}",
        "metadata": {"_transcript_turn_id": turn_id},
    }


def _assistant(turn_id: str, content: str = "a", **metadata) -> dict:
    return {
        "role": "assistant",
        "content": content,
        "timestamp": f"2026-09-14T10:01:0{turn_id}",
        "turn_id": turn_id,
        "metadata": dict(metadata),
    }


def _idle_session_stub(**overrides):
    state = SimpleNamespace(
        is_running=False,
        status="completed",
        paused=False,
        pending_interrupts=[],
        queued_follow_up_messages=[],
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    session = SimpleNamespace(state=state)
    session.has_blocking_tool_execution = lambda: False
    return session


def test_build_ceo_snapshot_emits_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    messages = [
        _user("t1", "第一条"),
        _assistant("t1", "回复一", task_ids=["task:a"]),
        _user("t2", "第二条"),
    ]
    items = websocket_ceo._build_ceo_snapshot(
        messages,
        session_id="web:ceo-x",
        edit_fork_gates={0: True, 2: False},
        fork_gates={0: True, 2: True},
    )
    users = [item for item in items if item["role"] == "user"]
    assistants = [item for item in items if item["role"] == "assistant"]
    assert users[0].get("can_edit_fork") is True
    assert "can_edit_fork" not in users[1]
    # 两个 flag 各自独立：编辑被收回的那条仍可 Fork。
    assert users[0].get("can_fork") is True
    assert users[1].get("can_fork") is True
    assert assistants[0].get("task_dispatched") is True


def test_build_ceo_snapshot_without_gates_has_no_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    items = websocket_ceo._build_ceo_snapshot([_user("t1")], session_id="web:ceo-x")
    assert "can_edit_fork" not in items[0]
    assert "can_fork" not in items[0]


def test_stability_gate_rejects_running_and_pending_lanes():
    stable = _idle_session_stub()
    assert websocket_ceo._session_fully_stable_for_history_edit(stable, {}) is True
    assert websocket_ceo._session_fully_stable_for_history_edit(
        _idle_session_stub(is_running=True), {}
    ) is False
    assert websocket_ceo._session_fully_stable_for_history_edit(
        _idle_session_stub(queued_follow_up_messages=["x"]), {}
    ) is False
    assert websocket_ceo._session_fully_stable_for_history_edit(
        _idle_session_stub(pending_interrupts=[{"id": "a"}]), {}
    ) is False
    assert websocket_ceo._session_fully_stable_for_history_edit(
        _idle_session_stub(paused=True), {}
    ) is False
    # inflight/preserved lane 存在(含 heartbeat 内部轮)即不稳定。
    assert websocket_ceo._session_fully_stable_for_history_edit(
        stable, {"inflight_turn": {"turn_id": "h1", "status": "running"}}
    ) is False
    assert websocket_ceo._session_fully_stable_for_history_edit(
        stable, {"preserved_turn": {"turn_id": "p1"}}
    ) is False

    blocking = _idle_session_stub()
    blocking.has_blocking_tool_execution = lambda: True
    assert websocket_ceo._session_fully_stable_for_history_edit(blocking, {}) is False


def test_session_edit_fork_gates_channel_and_stability_short_circuit(tmp_path, monkeypatch):
    monkeypatch.setattr(wcs, "workspace_path", lambda: tmp_path)
    messages = [_user("t1"), _assistant("t1"), _user("t2")]
    session = _idle_session_stub()
    wcs.write_turn_boundary_snapshot("web:ceo-x", "t1", {
        "frontdoor_request_body_messages": [{"role": "user", "content": "x"}],
        "source_reason": "finalize",
    })
    # 渠道会话:两类 flag 都不下发（编辑/Fork 的历史不可改语义不跟着输入闸门放宽）。
    assert websocket_ceo._session_edit_fork_gates(
        session, "ext:qq:1", messages, turn_payload={}, is_channel_session=True, agent=None
    ) == (None, None)
    # 非稳定态:只收编辑资格，Fork 资格照发（源会话零变更，回合在跑也能复制前缀）。
    for unstable in (
        _idle_session_stub(is_running=True),
        _idle_session_stub(paused=True),
        _idle_session_stub(pending_interrupts=[{"id": "a"}]),
    ):
        edit_gates, fork_gates = websocket_ceo._session_edit_fork_gates(
            unstable,
            "web:ceo-x",
            messages,
            turn_payload={},
            is_channel_session=False,
            agent=None,
        )
        assert edit_gates is None
        assert fork_gates == {0: True, 2: True}
    # 稳定 web 会话:两类资格同源。
    edit_gates, fork_gates = websocket_ceo._session_edit_fork_gates(
        session, "web:ceo-x", messages, turn_payload={}, is_channel_session=False, agent=None
    )
    assert edit_gates == {0: True, 2: True}
    assert fork_gates == edit_gates


def test_edit_fork_eligible_turn_ids_maps_gate_indices_to_turn_ids():
    messages = [
        _user("t1", "同批第一条"),
        _user("t1", "同批第二条"),
        _assistant("t1"),
        {"role": "user", "content": "旧转录无 turn_id", "metadata": {}},
    ]
    # 门槛按原始下标编码，翻成前端可匹配的 turn_id；无 turn_id 的行按 key 定位不到，跳过。
    assert websocket_ceo._edit_fork_eligible_turn_ids(messages, {0: True, 1: False}) == ["t1"]
    assert websocket_ceo._edit_fork_eligible_turn_ids(messages, {3: True}) == []
    assert websocket_ceo._edit_fork_eligible_turn_ids(messages, None) == []
    # 下标越界（门槛来自更早的一份转录）不能带崩收尾推送。
    assert websocket_ceo._edit_fork_eligible_turn_ids(messages, {99: True}) == []


def test_relay_pushes_gates_frame_when_session_becomes_stable():
    source = Path(websocket_ceo.__file__).read_text(encoding="utf-8")
    start = source.index("if event.type == 'state_snapshot':")
    relay_block = source[start:source.index("if event.type == 'message_end':")]
    assert "await _push_edit_fork_gates()" in relay_block, "稳定态回到时未补发编辑/Fork 门槛"
    assert "_session_fully_stable_for_history_edit(session, turn_payload)" in source
    # 门槛帧必须留在 paused 分支之外：暂停/等审批时输入被闸门挡住，Fork 是唯一出口。
    gate_line = next(
        line for line in relay_block.splitlines() if "await _push_edit_fork_gates()" in line
    )
    assert gate_line.startswith("            await "), "门槛帧被关进了 paused 的 if 块里"
    assert "'fork_turn_ids'" in source, "补发帧缺 Fork 资格列表"
