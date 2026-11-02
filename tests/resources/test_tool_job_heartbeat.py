from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane
from g3ku.heartbeat.session_service import WebSessionHeartbeatService
from g3ku.runtime.tool_jobs import tool_job_dedupe_key
from main.protocol import now_iso
from main.storage.sqlite_store import SQLiteTaskStore


@pytest.fixture()
def store(tmp_path) -> SQLiteTaskStore:
    return SQLiteTaskStore(tmp_path / "runtime.sqlite3")


def _payload(status: str = "success", **overrides):
    payload = {
        "event_reason": "tool_job",
        "session_id": "web:demo",
        "job_id": "tooljob:demo",
        "tool_name": "exec",
        "status": status,
        "started_at": now_iso(),
        "finished_at": now_iso(),
        "elapsed_seconds": 125.0,
        "timeout_seconds": 1800.0,
        "turn_id": "turn-1",
        "tool_call_id": "call-1",
        "result_preview": "partial output",
        "result_ref": "artifact:demo",
        "recovery_policy": "model_decide",
        "stopped_by": "user" if status == "stopped_by_user" else "",
    }
    payload.update(overrides)
    return payload


def _service(store: SQLiteTaskStore) -> WebSessionHeartbeatService:
    return WebSessionHeartbeatService(
        workspace=".",
        agent=SimpleNamespace(),
        runtime_manager=SimpleNamespace(),
        main_task_service=SimpleNamespace(store=store),
        session_manager=SimpleNamespace(),
    )


def test_enqueue_tool_job_dedupes_by_job_and_status(store):
    service = _service(store)
    payload = _payload("success")

    assert service.enqueue_tool_job(payload) is True
    assert service.enqueue_tool_job(payload) is False
    events = service._events.peek("web:demo")
    assert len(events) == 1
    assert events[0].reason == "tool_job"
    assert events[0].source == "frontdoor"

    assert service.enqueue_tool_job(_payload("interrupted")) is True
    assert service.enqueue_tool_job({"session_id": "web:demo"}) is False


def test_tool_job_prompt_renders_each_terminal_status(store):
    service = _service(store)
    for status in ("success", "error", "timeout", "interrupted", "stopped_by_user"):
        service._events.clear_all()
        assert service.enqueue_tool_job(_payload(status))
        events = service._events.peek("web:demo")
        # 事件正文的唯一载体是 prompt_lane（`_build_heartbeat_stable_rules` 只出规则段），
        # 所以这里按模型真正收到的那份束体验证。
        prompt = build_heartbeat_prompt_lane(
            provider_model="test",
            stable_rules_text="",
            events=[dict(event.payload or {}) for event in events],
        ).event_bundle_text
        assert "Background tool job tooljob:demo (exec)" in prompt
        if status in {"success", "error", "timeout"}:
            assert "Do not call the tool again" in prompt
        if status == "stopped_by_user":
            assert "用户手动停止" in prompt
        if status == "interrupted":
            assert "interrupted by the project process restart" in prompt

    bundle = build_heartbeat_prompt_lane(
        provider_model="test",
        stable_rules_text="",
        events=[_payload("stopped_by_user")],
    ).event_bundle_text
    assert "用户手动停止" in bundle


def test_replay_and_ack_mark_the_outbox_delivered(store):
    service = _service(store)
    payload = _payload("success")
    dedupe_key = tool_job_dedupe_key("tooljob:demo", "success")
    store.put_tool_job_outbox(
        dedupe_key=dedupe_key,
        job_id="tooljob:demo",
        session_id="web:demo",
        created_at=now_iso(),
        payload=payload,
    )

    counts = service.replay_pending_outbox()
    assert counts["tool_job"] == 1
    events = service._events.peek("web:demo")
    assert len(events) == 1
    entry = store.get_tool_job_outbox(dedupe_key)
    assert entry is not None and entry["accepted"] is True

    service._ack_tool_job_events(events)
    entry = store.get_tool_job_outbox(dedupe_key)
    assert entry is not None and entry["delivery_state"] == "delivered"
    assert store.list_pending_tool_job_outbox() == []
