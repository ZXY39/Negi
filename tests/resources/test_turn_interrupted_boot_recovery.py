from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.runtime.web_ceo_sessions import write_inflight_turn_snapshot
from g3ku.session.manager import SessionManager


@pytest.mark.asyncio
async def test_boot_recovery_enqueues_turn_interrupted_heartbeat() -> None:
    from g3ku.shells import web

    enqueued: list[dict] = []

    class _Sessions:
        def keys_with_pending_or_interrupted_user_rows(self) -> list[str]:
            return ["web:crash"]

    class _Session:
        def interrupted_turn_recovery_state(self) -> dict:
            return {
                "turn_id": "turn-1",
                "last_activity_at": "2026-11-02T10:00:00+08:00",
            }

    class _Manager:
        def get_or_create(self, **kwargs):
            assert kwargs["session_key"] == "web:crash"
            return _Session()

    class _Heartbeat:
        def enqueue_turn_interrupted(self, payload: dict) -> bool:
            enqueued.append(dict(payload))
            return True

    recovered = await web.recover_interrupted_turns(
        SimpleNamespace(sessions=_Sessions()),
        _Manager(),
        _Heartbeat(),
    )

    assert recovered == 1
    assert enqueued[0]["session_id"] == "web:crash"
    assert enqueued[0]["turn_id"] == "turn-1"
    assert enqueued[0]["last_activity_at"] == "2026-11-02T10:00:00+08:00"
    assert enqueued[0]["dedupe_key"] == "turn-interrupted:web:crash:turn-1"


@pytest.mark.asyncio
async def test_boot_recovery_ignores_sessions_without_interrupted_turn() -> None:
    from g3ku.shells import web

    class _Sessions:
        def keys_with_pending_or_interrupted_user_rows(self) -> list[str]:
            return ["web:queued"]

    class _Session:
        def interrupted_turn_recovery_state(self) -> None:
            return None

    class _Manager:
        def get_or_create(self, **kwargs):
            return _Session()

    class _Heartbeat:
        def enqueue_turn_interrupted(self, payload: dict) -> bool:
            raise AssertionError("must not enqueue")

    recovered = await web.recover_interrupted_turns(
        SimpleNamespace(sessions=_Sessions()),
        _Manager(),
        _Heartbeat(),
    )

    assert recovered == 0


@pytest.mark.asyncio
async def test_boot_recovery_uses_the_real_session_constructor(tmp_path) -> None:
    from g3ku.shells import web

    manager = SessionManager(tmp_path / "workspace")
    persisted = manager.get_or_create("web:crash")
    persisted.add_message(
        "user",
        "primary request",
        metadata={
            "_transcript_turn_id": "turn-1",
            "_transcript_state": "pending",
        },
    )
    manager.save(persisted)
    write_inflight_turn_snapshot(
        "web:crash",
        {"status": "running", "turn_id": "turn-1", "frontdoor_turns": []},
    )
    session = RuntimeAgentSession(
        SimpleNamespace(model="test-model", reasoning_effort=None, sessions=manager),
        session_key="web:crash",
        channel="web",
        chat_id="crash",
    )
    enqueued: list[dict] = []

    class _Manager:
        def get_or_create(self, **kwargs):
            return session

    class _Heartbeat:
        def enqueue_turn_interrupted(self, payload: dict) -> bool:
            enqueued.append(dict(payload))
            return True

    recovered = await web.recover_interrupted_turns(
        SimpleNamespace(sessions=manager),
        _Manager(),
        _Heartbeat(),
    )

    assert recovered == 1
    assert enqueued[0]["turn_id"] == "turn-1"
    assert manager.get_or_create("web:crash").messages[0]["metadata"]["_transcript_state"] == "interrupted"


@pytest.mark.asyncio
async def test_queued_boot_replay_skips_interrupted_turn_session() -> None:
    from g3ku.shells import web

    dispatched: list[str] = []

    class _Sessions:
        def keys_with_pending_user_rows(self) -> list[str]:
            return ["web:crash"]

    class _Session:
        def interrupted_turn_recovery_state(self) -> dict:
            return {"turn_id": "turn-1", "last_activity_at": ""}

        async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict:
            dispatched.append(source)
            return {"dispatched": 1, "reason": ""}

    class _Manager:
        def get_or_create(self, **kwargs):
            return _Session()

    replayed = await web.replay_queued_follow_ups(SimpleNamespace(sessions=_Sessions()), _Manager())

    assert replayed == 0
    assert dispatched == []
