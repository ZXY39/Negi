from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.runtime.web_ceo_sessions import (
    clear_inflight_turn_snapshot,
    write_inflight_turn_snapshot,
    write_paused_execution_context,
)
from g3ku.session.manager import SessionManager


def _manager(tmp_path) -> SessionManager:
    return SessionManager(tmp_path / "workspace")


def _user_row(turn_id: str, text: str, *, state: str = "pending") -> dict:
    return {
        "content": text,
        "metadata": {
            "_transcript_turn_id": turn_id,
            "_transcript_state": state,
        },
    }


def _session(manager: SessionManager, key: str = "web:crash") -> RuntimeAgentSession:
    loop = SimpleNamespace(model="test-model", reasoning_effort=None, sessions=manager)
    return RuntimeAgentSession(loop, session_key=key, channel="web", chat_id="crash")


def _seed(manager: SessionManager, key: str, rows: list[dict], *, save: bool = True):
    persisted = manager.get_or_create(key)
    for row in rows:
        persisted.add_message("user", row["content"], metadata=row["metadata"])
    if save:
        manager.save(persisted)
    return persisted


def test_pending_primary_turn_becomes_interrupted_and_stays_out_of_queue(tmp_path):
    manager = _manager(tmp_path)
    _seed(manager, "web:crash", [_user_row("turn-1", "continue the deployment")])
    write_inflight_turn_snapshot(
        "web:crash",
        {"status": "running", "turn_id": "turn-1", "frontdoor_turns": []},
    )

    session = _session(manager)

    recovery = session.interrupted_turn_recovery_state()
    assert recovery is not None
    assert recovery["turn_id"] == "turn-1"
    assert recovery["last_activity_at"]
    assert session.state.queued_follow_up_messages == []
    persisted = manager.get_or_create("web:crash")
    assert persisted.messages[0]["metadata"]["_transcript_state"] == "interrupted"
    assert manager.keys_with_pending_or_interrupted_user_rows() == ["web:crash"]
    assert manager.keys_with_pending_user_rows() == []


def test_follow_up_with_another_turn_stays_pending(tmp_path):
    manager = _manager(tmp_path)
    _seed(
        manager,
        "web:crash",
        [
            _user_row("turn-1", "primary request"),
            _user_row("turn-2", "also check the logs"),
        ],
    )
    write_inflight_turn_snapshot(
        "web:crash",
        {"status": "running", "turn_id": "turn-1", "frontdoor_turns": []},
    )

    session = _session(manager)

    queued = list(session.state.queued_follow_up_messages)
    assert [item.content for item in queued] == ["also check the logs"]
    persisted = manager.get_or_create("web:crash")
    states = {row["content"]: row["metadata"]["_transcript_state"] for row in persisted.messages}
    assert states["primary request"] == "interrupted"
    assert states["also check the logs"] == "pending"


def test_no_inflight_snapshot_keeps_legacy_pending_replay(tmp_path):
    manager = _manager(tmp_path)
    _seed(manager, "web:crash", [_user_row("turn-1", "legacy queued input")])
    clear_inflight_turn_snapshot("web:crash")

    session = _session(manager)

    assert session.interrupted_turn_recovery_state() is None
    assert [item.content for item in session.state.queued_follow_up_messages] == [
        "legacy queued input"
    ]
    persisted = manager.get_or_create("web:crash")
    assert persisted.messages[0]["metadata"]["_transcript_state"] == "pending"


def test_assistant_reply_finishes_the_row_without_recovery(tmp_path):
    manager = _manager(tmp_path)
    persisted = _seed(manager, "web:crash", [_user_row("turn-1", "answered request")])
    persisted.add_message(
        "assistant",
        "already answered",
        turn_id="turn-1",
        metadata={"source": "heartbeat"},
    )
    manager.save(persisted)
    write_inflight_turn_snapshot(
        "web:crash",
        {"status": "running", "turn_id": "turn-1", "frontdoor_turns": []},
    )

    session = _session(manager)

    assert session.interrupted_turn_recovery_state() is None
    persisted = manager.get_or_create("web:crash")
    assert persisted.messages[0]["metadata"]["_transcript_state"] == "completed"


def test_manual_pause_context_is_not_treated_as_crash(tmp_path):
    manager = _manager(tmp_path)
    _seed(manager, "web:crash", [_user_row("turn-1", "paused request")])
    write_paused_execution_context(
        "web:crash",
        {"status": "paused", "turn_id": "turn-1", "frontdoor_turns": []},
    )
    write_inflight_turn_snapshot(
        "web:crash",
        {"status": "running", "turn_id": "turn-1", "frontdoor_turns": []},
    )

    session = _session(manager)

    assert session.interrupted_turn_recovery_state() is None
    persisted = manager.get_or_create("web:crash")
    assert persisted.messages[0]["metadata"]["_transcript_state"] == "pending"


def test_interrupted_turn_id_is_adopted_only_for_single_reason_bundle(tmp_path):
    manager = _manager(tmp_path)
    _seed(manager, "web:crash", [_user_row("turn-1", "primary request", state="interrupted")])

    session = _session(manager)
    # 内部提示词持久化先给本条心跳输入生成了一个内部 turn_id；恢复车道可以覆盖它。
    session._active_turn_id = "generated-internal"
    single = UserInputMessage(
        content="[SESSION EVENTS]",
        metadata={
            "heartbeat_internal": True,
            "heartbeat_reason": "turn_interrupted",
            "turn_interrupted_turn_id": "turn-1",
            "_transcript_turn_id": "generated-internal",
        },
    )
    assert session._adopt_interrupted_turn_id_for_resume(single) == "turn-1"
    assert session._active_turn_id == "turn-1"
    assert single.metadata["_transcript_turn_id"] == "turn-1"

    session._active_turn_id = "generated-internal"
    mixed = UserInputMessage(
        content="[SESSION EVENTS]",
        metadata={
            "heartbeat_internal": True,
            "heartbeat_reason": "mixed",
            "turn_interrupted_turn_id": "turn-1",
            "_transcript_turn_id": "generated-internal",
        },
    )
    assert session._adopt_interrupted_turn_id_for_resume(mixed) == ""

    session._active_turn_id = "real-running-turn"
    blocked = UserInputMessage(
        content="[SESSION EVENTS]",
        metadata={
            "heartbeat_internal": True,
            "heartbeat_reason": "turn_interrupted",
            "turn_interrupted_turn_id": "turn-1",
            "_transcript_turn_id": "generated-internal",
        },
    )
    assert session._adopt_interrupted_turn_id_for_resume(blocked) == ""


def test_completion_marks_interrupted_row_completed(tmp_path):
    manager = _manager(tmp_path)
    persisted = _seed(
        manager,
        "web:crash",
        [_user_row("turn-1", "primary request", state="interrupted")],
    )
    write_inflight_turn_snapshot(
        "web:crash",
        {"status": "running", "turn_id": "turn-1", "frontdoor_turns": []},
    )
    session = _session(manager)
    assert session.interrupted_turn_recovery_state() is not None

    completed = session._complete_interrupted_user_messages(
        persisted,
        turn_id="turn-1",
        persist=True,
    )

    assert completed == 1
    assert session.interrupted_turn_recovery_state() is None
    persisted = manager.get_or_create("web:crash")
    assert persisted.messages[0]["metadata"]["_transcript_state"] == "completed"


@pytest.mark.asyncio
async def test_recovery_turn_transcript_completes_interrupted_row(tmp_path):
    manager = _manager(tmp_path)
    _seed(
        manager,
        "web:crash",
        [_user_row("turn-1", "primary request", state="interrupted")],
    )
    write_inflight_turn_snapshot(
        "web:crash",
        {"status": "running", "turn_id": "turn-1", "frontdoor_turns": []},
    )
    session = _session(manager)
    session._active_turn_id = "turn-1"

    await session._persist_turn_transcript(
        user_input=UserInputMessage(content="primary request"),
        user_text="primary request",
        assistant_text="continued and finished",
        interaction_flow=[],
        internal_source="heartbeat",
        route_kind="",
        assistant_metadata={
            "source": "heartbeat",
            "turn_interrupted_turn_id": "turn-1",
        },
    )

    persisted = manager.get_or_create("web:crash")
    assert persisted.messages[0]["metadata"]["_transcript_state"] == "completed"
    assistant_rows = [row for row in persisted.messages if row.get("role") == "assistant"]
    assert assistant_rows and assistant_rows[-1]["turn_id"] == "turn-1"
    assert session.interrupted_turn_recovery_state() is None
