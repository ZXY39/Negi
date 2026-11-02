from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from g3ku.runtime.tool_jobs import (
    TOOL_JOB_STATUS_INTERRUPTED,
    TOOL_JOB_STATUS_RUNNING,
    TOOL_JOB_STATUS_STOPPED_BY_USER,
    TOOL_JOB_STATUS_STOPPING,
    TOOL_JOB_STATUS_SUCCESS,
    prune_tool_job_rows,
    recover_tool_jobs,
    tool_job_dedupe_key,
)
from main.protocol import now_iso
from main.storage.sqlite_store import SQLiteTaskStore


@pytest.fixture()
def store(tmp_path) -> SQLiteTaskStore:
    return SQLiteTaskStore(tmp_path / "runtime.sqlite3")


def _put_running(store: SQLiteTaskStore, *, job_id: str = "tooljob:crash", status: str = TOOL_JOB_STATUS_RUNNING, stop_requested: bool = False):
    store.put_tool_job(
        job_id=job_id,
        session_id="web:demo",
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="hash",
        hint="sleep 300",
        status=status,
        started_at=now_iso(),
        updated_at=now_iso(),
        timeout_seconds=1800,
        owner_pid=1,
        recovery_policy="model_decide",
    )
    if stop_requested:
        store.update_tool_job(job_id, status=status, stop_requested_by_user=True, updated_at=now_iso())


def test_running_row_becomes_interrupted_with_one_event(store):
    _put_running(store)

    result = recover_tool_jobs(store)

    assert result["recovered"] == 1
    assert result["events"] == 1
    row = store.get_tool_job("tooljob:crash")
    assert row is not None and row["status"] == TOOL_JOB_STATUS_INTERRUPTED
    assert row["finished_at"]
    pending = store.list_pending_tool_job_outbox()
    assert len(pending) == 1
    assert pending[0]["dedupe_key"] == tool_job_dedupe_key("tooljob:crash", TOOL_JOB_STATUS_INTERRUPTED)
    assert pending[0]["payload"]["status"] == TOOL_JOB_STATUS_INTERRUPTED
    assert "restarted" in pending[0]["payload"]["result_preview"].lower()

    assert recover_tool_jobs(store) == {"recovered": 0, "events": 0}


def test_stopping_row_uses_the_already_emitted_stop_event(store):
    _put_running(store, status=TOOL_JOB_STATUS_STOPPING, stop_requested=True)
    dedupe = tool_job_dedupe_key("tooljob:crash", TOOL_JOB_STATUS_STOPPED_BY_USER)
    store.put_tool_job_outbox(
        dedupe_key=dedupe,
        job_id="tooljob:crash",
        session_id="web:demo",
        created_at=now_iso(),
        payload={"session_id": "web:demo", "job_id": "tooljob:crash", "status": TOOL_JOB_STATUS_STOPPED_BY_USER},
    )
    store.mark_tool_job_outbox_delivered(dedupe, delivered_at=now_iso())

    result = recover_tool_jobs(store)

    assert result == {"recovered": 1, "events": 0}
    row = store.get_tool_job("tooljob:crash")
    assert row is not None and row["status"] == TOOL_JOB_STATUS_STOPPED_BY_USER
    assert store.list_pending_tool_job_outbox() == []


def test_existing_terminal_event_wins_over_restart_guess(store):
    _put_running(store, status=TOOL_JOB_STATUS_RUNNING)
    dedupe = tool_job_dedupe_key("tooljob:crash", TOOL_JOB_STATUS_SUCCESS)
    store.put_tool_job_outbox(
        dedupe_key=dedupe,
        job_id="tooljob:crash",
        session_id="web:demo",
        created_at=now_iso(),
        payload={
            "session_id": "web:demo",
            "job_id": "tooljob:crash",
            "status": TOOL_JOB_STATUS_SUCCESS,
            "result_preview": "finished before the crash",
        },
    )

    result = recover_tool_jobs(store)

    assert result == {"recovered": 1, "events": 0}
    row = store.get_tool_job("tooljob:crash")
    assert row is not None and row["status"] == TOOL_JOB_STATUS_SUCCESS
    assert row["result_preview"] == "finished before the crash"
    assert [item["dedupe_key"] for item in store.list_pending_tool_job_outbox()] == [dedupe]


def test_terminal_row_without_outbox_gets_one_event(store):
    _put_running(store, job_id="tooljob:lost")
    store.update_tool_job(
        "tooljob:lost",
        status=TOOL_JOB_STATUS_SUCCESS,
        finished_at=now_iso(),
        updated_at=now_iso(),
        result_preview="done before the crash",
    )

    result = recover_tool_jobs(store)

    assert result["events"] == 1
    pending = store.list_pending_tool_job_outbox()
    assert len(pending) == 1
    assert pending[0]["dedupe_key"] == tool_job_dedupe_key("tooljob:lost", TOOL_JOB_STATUS_SUCCESS)
    assert pending[0]["payload"]["result_preview"] == "done before the crash"
    assert recover_tool_jobs(store) == {"recovered": 0, "events": 0}


def test_prune_keeps_running_rows_and_drops_old_terminal_rows(store):
    _put_running(store, job_id="tooljob:live")
    _put_running(store, job_id="tooljob:done")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    store.update_tool_job(
        "tooljob:done",
        status=TOOL_JOB_STATUS_SUCCESS,
        finished_at=old,
        updated_at=old,
    )
    dedupe = tool_job_dedupe_key("tooljob:done", TOOL_JOB_STATUS_SUCCESS)
    store.put_tool_job_outbox(
        dedupe_key=dedupe,
        job_id="tooljob:done",
        session_id="web:demo",
        created_at=old,
        payload={"session_id": "web:demo", "job_id": "tooljob:done", "status": TOOL_JOB_STATUS_SUCCESS},
    )
    store.mark_tool_job_outbox_delivered(dedupe, delivered_at=old)

    prune_tool_job_rows(store)

    assert store.get_tool_job("tooljob:live") is not None
    assert store.get_tool_job("tooljob:done") is None
    assert store.get_tool_job_outbox(dedupe) is None
