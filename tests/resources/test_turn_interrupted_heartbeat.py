from __future__ import annotations

import re
from pathlib import Path

from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane, turn_interrupted_lines
from g3ku.heartbeat.session_events import SessionHeartbeatEventQueue
from g3ku.heartbeat.session_service import WebSessionHeartbeatService


def _event() -> object:
    return SessionHeartbeatEventQueue().enqueue(
        session_id="web:crash",
        source="frontdoor",
        reason="turn_interrupted",
        dedupe_key="turn-interrupted:web:crash:turn-1",
        payload={
            "session_id": "web:crash",
            "turn_id": "turn-1",
            "last_activity_at": "2026-11-02T10:00:00+08:00",
            "event_reason": "turn_interrupted",
            "reason": "turn_interrupted",
        },
        delay_seconds=0.0,
    )


def _service(tmp_path: Path) -> WebSessionHeartbeatService:
    return WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=None,
        runtime_manager=None,
        main_task_service=None,
        session_manager=None,
        reply_notifier=None,
    )


def test_turn_interrupted_lines_include_last_activity_now_and_stopped_duration() -> None:
    text = "\n".join(
        turn_interrupted_lines(
            {
                "turn_id": "turn-1",
                "last_activity_at": "2026-11-02T10:00:00+08:00",
            }
        )
    )

    assert "Previous turn: turn-1" in text
    assert "Last activity: 2026-11-02 10:00:00 +08:00" in text
    assert "Current time: " in text
    assert re.search(r"Stopped for: .+ \(\d+s\)", text)
    assert "The user did not send it again" in text


def test_turn_interrupted_bundle_uses_the_same_lines() -> None:
    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[
            {
                "reason": "turn_interrupted",
                "turn_id": "turn-1",
                "last_activity_at": "2026-11-02T10:00:00+08:00",
            }
        ],
    )

    text = str(lane.event_bundle_text)
    assert "Previous turn: turn-1" in text
    assert "Stopped for:" in text


def test_heartbeat_stable_rules_for_turn_interrupted_force_visible_reply(tmp_path: Path) -> None:
    service = _service(tmp_path)
    events = [_event()]
    prompt = service._build_heartbeat_stable_rules(events)

    assert "turn_interrupted" in prompt
    assert "always end with a user-visible reply" in prompt
    assert "the user did not send it again" in prompt
    assert service._events_require_visible_reply(events) is True


def test_mixed_task_terminal_and_turn_interrupted_disallows_silent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    terminal = SessionHeartbeatEventQueue().enqueue(
        session_id="web:crash",
        source="main_runtime",
        reason="task_terminal",
        dedupe_key="task-terminal:task-1:failed",
        payload={
            "session_id": "web:crash",
            "task_id": "task-1",
            "status": "failed",
            "event_reason": "task_terminal",
        },
        delay_seconds=0.0,
    )
    events = [terminal, _event()]

    prompt = service._build_heartbeat_stable_rules(events)

    assert "This mixed bundle also contains a restart-resume event" in prompt
    assert "do not use the silent tool" in prompt
    assert service._events_require_visible_reply(events) is True


def test_heartbeat_user_input_carries_the_interrupted_turn_id(tmp_path: Path) -> None:
    service = _service(tmp_path)
    user_input = service._build_heartbeat_user_input(
        [_event()],
        heartbeat_reason="turn_interrupted",
        normalized_metadata={},
    )

    assert user_input.metadata["turn_interrupted_turn_id"] == "turn-1"
    assert user_input.metadata["heartbeat_reason"] == "turn_interrupted"


def test_enqueue_turn_interrupted_dedupes_and_requests_wake(monkeypatch, tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._started = True
    requests: list[tuple[str, float]] = []
    monkeypatch.setattr(
        service._wake,
        "request",
        lambda session_id, *, delay_s: requests.append((str(session_id), float(delay_s))),
    )
    payload = {
        "session_id": "web:crash",
        "turn_id": "turn-1",
        "last_activity_at": "2026-11-02T10:00:00+08:00",
    }

    assert service.enqueue_turn_interrupted(payload) is True
    assert service.enqueue_turn_interrupted(payload) is False
    assert requests == [("web:crash", 0.25)]
