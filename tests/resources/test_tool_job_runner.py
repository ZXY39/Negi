from __future__ import annotations

import asyncio

import pytest

from g3ku.runtime.tool_jobs import (
    TOOL_JOB_STATUS_STOPPED_BY_USER,
    TOOL_JOB_STATUS_STOPPING,
    TOOL_JOB_STATUS_SUCCESS,
    ToolJobOutcome,
    ToolJobRunner,
    tool_job_purpose_from_arguments,
)
from main.protocol import now_iso
from main.storage.sqlite_store import SQLiteTaskStore


class _FakeHeartbeat:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def enqueue_tool_job(self, payload: dict) -> bool:
        self.events.append(dict(payload))
        return True


@pytest.fixture()
def store(tmp_path) -> SQLiteTaskStore:
    return SQLiteTaskStore(tmp_path / "runtime.sqlite3")


def _runner(
    store: SQLiteTaskStore,
    heartbeat: _FakeHeartbeat | None = None,
    changes: list | None = None,
    finished: list | None = None,
):
    heartbeat = heartbeat or _FakeHeartbeat()
    changes = changes if changes is not None else []
    finished = finished if finished is not None else []
    runner = ToolJobRunner(
        session_key="web:demo",
        store_getter=lambda: store,
        heartbeat_getter=lambda: heartbeat,
        changed_callback=lambda items: changes.append(list(items)),
        finished_callback=lambda item: finished.append(dict(item)),
        stop_abandon_grace_seconds=0.05,
    )
    return runner, heartbeat, changes, finished


async def _run_outcome(status: str = TOOL_JOB_STATUS_SUCCESS, text: str = "ok") -> ToolJobOutcome:
    return ToolJobOutcome(status=status, result_text=text, result_preview=text, result_ref="artifact:demo")


async def test_fast_job_returns_in_foreground_without_persisting(store):
    runner, heartbeat, _changes, finished = _runner(store)

    job, reused = runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="hash-fast",
        hint="echo ok",
        run=_run_outcome,
        timeout_seconds=60,
    )
    done, outcome = await runner.wait_first(job, 0.5)

    assert reused is False
    assert done is True
    assert outcome is not None and outcome.result_text == "ok"
    assert store.get_tool_job(job.job_id) is None
    assert heartbeat.events == []
    assert finished == []
    assert runner.list_jobs("web:demo") == []


async def test_slow_job_backgrounds_then_emits_one_terminal_event(store):
    runner, heartbeat, changes, finished = _runner(store)

    async def slow() -> ToolJobOutcome:
        await asyncio.sleep(0.15)
        return await _run_outcome("success", "slow-result")

    job, reused = runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="hash-slow",
        hint="sleep 5",
        run=slow,
        timeout_seconds=1800,
    )
    done, _outcome = await runner.wait_first(job, 0.02)

    assert reused is False
    assert done is False
    row = store.get_tool_job(job.job_id)
    assert row is not None and row["status"] == "running"
    listed = runner.list_jobs("web:demo")
    assert [item["job_id"] for item in listed] == [job.job_id]
    assert changes and changes[-1]

    await asyncio.sleep(0.25)
    row = store.get_tool_job(job.job_id)
    assert row is not None and row["status"] == "success"
    assert row["finished_at"]
    assert len(heartbeat.events) == 1
    event = heartbeat.events[0]
    assert event["job_id"] == job.job_id
    assert event["status"] == "success"
    assert event["result_preview"] == "slow-result"
    pending = store.list_pending_tool_job_outbox()
    assert len(pending) == 1 and pending[0]["job_id"] == job.job_id
    assert runner.list_jobs("web:demo") == []
    assert len(finished) == 1
    assert finished[0]["job_id"] == job.job_id
    assert finished[0]["tool_call_id"] == "call-1"
    assert finished[0]["status"] == TOOL_JOB_STATUS_SUCCESS
    assert finished[0]["finished_at"]


def test_purpose_extraction_reads_exec_arguments_only():
    assert tool_job_purpose_from_arguments({"command": "x", "purpose": " rebuild gate "}) == "rebuild gate"
    assert tool_job_purpose_from_arguments({"command": "x"}) == ""
    assert tool_job_purpose_from_arguments(None) == ""
    assert tool_job_purpose_from_arguments("oops") == ""


async def test_backgrounded_job_persists_and_lists_purpose(store):
    runner, _heartbeat, _changes, _finished = _runner(store)

    async def slow() -> ToolJobOutcome:
        await asyncio.sleep(0.15)
        return await _run_outcome()

    job, _ = runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="hash-purpose",
        hint="exec (command=$ErrorActionPreference='Continue'\\n& 'C:\\Users\\A...', purpose=rebuild gate, timeout_seconds=900)",
        purpose="rebuild and re-run check gate",
        run=slow,
        timeout_seconds=1800,
    )
    await runner.wait_first(job, 0.02)

    listed = runner.list_jobs("web:demo")
    assert listed and listed[0]["purpose"] == "rebuild and re-run check gate"
    assert listed[0]["hint"].startswith("exec (command=")
    row = store.get_tool_job(job.job_id)
    assert row is not None and row["purpose"] == "rebuild and re-run check gate"
    await asyncio.sleep(0.25)


async def test_same_tool_and_args_reuses_the_inflight_job(store):
    runner, _heartbeat, _changes, _finished = _runner(store)

    async def slow() -> ToolJobOutcome:
        await asyncio.sleep(0.2)
        return await _run_outcome()

    first, _ = runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="same-hash",
        hint="sleep",
        run=slow,
        timeout_seconds=60,
    )
    await runner.wait_first(first, 0.01)
    second, reused = runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-2",
        tool_name="exec",
        args_hash="same-hash",
        hint="sleep",
        run=slow,
        timeout_seconds=60,
    )
    assert reused is True
    assert second.job_id == first.job_id
    assert len(runner.list_jobs("web:demo")) == 1
    await asyncio.sleep(0.3)


async def test_user_stop_emits_immediately_and_only_once(store):
    runner, heartbeat, _changes, finished = _runner(store)

    async def slow() -> ToolJobOutcome:
        await asyncio.sleep(10)
        return await _run_outcome()

    job, _ = runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="stop-hash",
        hint="sleep 10",
        run=slow,
        timeout_seconds=1800,
    )
    await runner.wait_first(job, 0.01)
    result = await runner.stop_job(job.job_id)

    assert result["status"] == TOOL_JOB_STATUS_STOPPING
    assert len(heartbeat.events) == 1
    assert heartbeat.events[0]["status"] == TOOL_JOB_STATUS_STOPPED_BY_USER
    assert heartbeat.events[0]["stopped_by"] == "user"

    await asyncio.sleep(0.2)
    row = store.get_tool_job(job.job_id)
    assert row is not None and row["status"] == TOOL_JOB_STATUS_STOPPED_BY_USER
    assert len(heartbeat.events) == 1
    assert runner.list_jobs("web:demo") == []
    assert len(finished) == 1
    assert finished[0]["status"] == TOOL_JOB_STATUS_STOPPED_BY_USER


async def test_cancel_resistant_job_is_abandoned_after_stop_grace(store):
    runner, heartbeat, _changes, finished = _runner(store)

    async def stubborn() -> ToolJobOutcome:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.5)
        return await _run_outcome()

    job, _ = runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="stubborn-hash",
        hint="sleep 10",
        run=stubborn,
        timeout_seconds=1800,
    )
    await runner.wait_first(job, 0.01)
    await runner.stop_job(job.job_id)
    await asyncio.sleep(0.2)

    row = store.get_tool_job(job.job_id)
    assert row is not None and row["status"] == TOOL_JOB_STATUS_STOPPED_BY_USER
    assert len(heartbeat.events) == 1
    assert runner.list_jobs("web:demo") == []
    assert len(finished) == 1
    assert finished[0]["status"] == TOOL_JOB_STATUS_STOPPED_BY_USER


async def test_old_pending_outbox_is_replayed_by_dedupe_key(store):
    created_at = now_iso()
    store.put_tool_job_outbox(
        dedupe_key="tool-job:tooljob:demo:success",
        job_id="tooljob:demo",
        session_id="web:demo",
        created_at=created_at,
        payload={"session_id": "web:demo", "job_id": "tooljob:demo", "status": "success"},
    )
    pending = store.list_pending_tool_job_outbox()
    assert [row["dedupe_key"] for row in pending] == ["tool-job:tooljob:demo:success"]

    store.mark_tool_job_outbox_delivered("tool-job:tooljob:demo:success", delivered_at=now_iso())
    assert store.list_pending_tool_job_outbox() == []
    row = store.get_tool_job_outbox("tool-job:tooljob:demo:success")
    assert row is not None and row["delivery_state"] == "delivered"
