from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from g3ku.deployment import data_root as data_root_module
from g3ku.runtime import owned_processes
from g3ku.runtime.api import ceo_sessions
from g3ku.runtime.tool_jobs import ToolJobOutcome, ToolJobRunner, owned_process_items
from main.storage.sqlite_store import SQLiteTaskStore


class _FakeHeartbeat:
    def enqueue_tool_job(self, _payload: dict) -> bool:
        return True


class _FakeRuntimeSession:
    def __init__(self, store: SQLiteTaskStore, session_key: str = "web:demo") -> None:
        self.session_key = session_key
        self.store = store
        self.runner = ToolJobRunner(
            session_key=session_key,
            store_getter=lambda: store,
            heartbeat_getter=lambda: _FakeHeartbeat(),
            stop_abandon_grace_seconds=0.05,
        )

    def _tool_job_store(self):
        return self.store

    def list_tool_jobs(self):
        return [
            *self.runner.list_jobs(self.session_key),
            *owned_process_items(self.session_key),
        ]

    async def stop_tool_job(self, job_id: str):
        return await self.runner.stop_job(job_id)


class _FakeRuntimeManager:
    def __init__(self, sessions: dict[str, _FakeRuntimeSession]) -> None:
        self._sessions = sessions

    def get(self, session_id: str):
        return self._sessions.get(str(session_id or "").strip())


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    # GET 在会话不驻留时会直接读盘上的 owned-processes；不隔离会扫到真实数据根。
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    try:
        yield
    finally:
        data_root_module.reset_cache()


@pytest.fixture()
def store(tmp_path) -> SQLiteTaskStore:
    return SQLiteTaskStore(tmp_path / "runtime.sqlite3")


def _spawn_sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _wait_gone(process: subprocess.Popen, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        # poll() 顺手回收子进程；POSIX 僵尸在 is_alive 里仍算活着。
        process.poll()
        if not owned_processes.is_alive(process.pid):
            return True
        time.sleep(0.1)
    return False


def _cleanup_process(process: subprocess.Popen, scope: str) -> None:
    if process.poll() is None:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=5)
    except Exception:
        pass
    owned_processes.drop(scope, process.pid)


async def _background_job(session: _FakeRuntimeSession, *, seconds: float = 10.0, tool_hash: str = "h"):
    async def run() -> ToolJobOutcome:
        await asyncio.sleep(seconds)
        return ToolJobOutcome(status="success", result_text="done", result_preview="done")

    job, _reused = session.runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash=tool_hash,
        hint="sleep 10",
        run=run,
        timeout_seconds=1800,
    )
    done, _outcome = await session.runner.wait_first(job, 0.01)
    assert done is False
    return job


def _patch_sessions(monkeypatch, runtime_manager):
    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (None, SimpleNamespace(), runtime_manager, SimpleNamespace()),
    )
    monkeypatch.setattr(
        ceo_sessions,
        "_resolve_bulk_session_key",
        lambda _manager, _runtime_manager, _state_store, session_id: (str(session_id), False),
    )


@pytest.mark.asyncio
async def test_get_returns_running_jobs_and_empty_for_missing_runtime(store, monkeypatch):
    session = _FakeRuntimeSession(store)
    manager = _FakeRuntimeManager({"web:demo": session})
    _patch_sessions(monkeypatch, manager)
    job = await _background_job(session)

    payload = await ceo_sessions.get_ceo_session_tool_jobs("web:demo")
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["items"][0]["job_id"] == job.job_id
    assert payload["items"][0]["tool_name"] == "exec"
    assert payload["items"][0]["status"] == "running"
    assert payload["items"][0]["elapsed_seconds"] >= 0

    empty = await ceo_sessions.get_ceo_session_tool_jobs("web:none")
    assert empty == {"ok": True, "session_id": "web:none", "count": 0, "items": [], "finished": []}

    await session.runner.cancel_all(reason="test_cleanup", suppress_event=True)


@pytest.mark.asyncio
async def test_get_returns_recent_terminal_jobs_for_reload(store, monkeypatch):
    session = _FakeRuntimeSession(store)
    manager = _FakeRuntimeManager({"web:demo": session})
    _patch_sessions(monkeypatch, manager)
    job = await _background_job(session, seconds=0.05)
    await asyncio.sleep(0.15)

    payload = await ceo_sessions.get_ceo_session_tool_jobs("web:demo")
    assert payload["items"] == []
    assert len(payload["finished"]) == 1
    row = payload["finished"][0]
    assert row["job_id"] == job.job_id
    assert row["tool_call_id"] == "call-1"
    assert row["status"] == "success"
    assert row["finished_at"]


@pytest.mark.asyncio
async def test_stop_returns_stopping_and_is_idempotent(store, monkeypatch):
    session = _FakeRuntimeSession(store)
    manager = _FakeRuntimeManager({"web:demo": session})
    _patch_sessions(monkeypatch, manager)
    job = await _background_job(session)

    first = await ceo_sessions.stop_ceo_session_tool_job("web:demo", job.job_id)
    assert first == {"ok": True, "job_id": job.job_id, "status": "stopping"}
    second = await ceo_sessions.stop_ceo_session_tool_job("web:demo", job.job_id)
    assert second == {"ok": True, "job_id": job.job_id, "status": "stopping"}

    await asyncio.sleep(0.1)
    row = store.get_tool_job(job.job_id)
    assert row is not None and row["status"] == "stopped_by_user"


@pytest.mark.asyncio
async def test_stop_rejects_unknown_and_finished_jobs(store, monkeypatch):
    session = _FakeRuntimeSession(store)
    manager = _FakeRuntimeManager({"web:demo": session})
    _patch_sessions(monkeypatch, manager)

    with pytest.raises(HTTPException) as missing:
        await ceo_sessions.stop_ceo_session_tool_job("web:demo", "tooljob:missing")
    assert missing.value.status_code == 404
    assert missing.value.detail == "tool_job_not_found"

    async def fast() -> ToolJobOutcome:
        await asyncio.sleep(0.05)
        return ToolJobOutcome(status="success", result_text="done")

    job, _ = session.runner.start_job(
        turn_id="turn-1",
        tool_call_id="call-1",
        tool_name="exec",
        args_hash="fast",
        hint="",
        run=fast,
        timeout_seconds=60,
    )
    await session.runner.wait_first(job, 0.01)
    await asyncio.sleep(0.1)

    with pytest.raises(HTTPException) as finished:
        await ceo_sessions.stop_ceo_session_tool_job("web:demo", job.job_id)
    assert finished.value.status_code == 409
    assert finished.value.detail == "tool_job_finished"


@pytest.mark.asyncio
async def test_jobs_are_isolated_by_session(store, monkeypatch):
    demo = _FakeRuntimeSession(store, "web:demo")
    other = _FakeRuntimeSession(store, "web:other")
    manager = _FakeRuntimeManager({"web:demo": demo, "web:other": other})
    _patch_sessions(monkeypatch, manager)
    await _background_job(other, tool_hash="other")

    payload = await ceo_sessions.get_ceo_session_tool_jobs("web:demo")
    assert payload["count"] == 0

    await other.runner.cancel_all(reason="test_cleanup", suppress_event=True)


@pytest.mark.asyncio
async def test_owned_process_listed_and_stopped_without_resident_session(monkeypatch):
    manager = _FakeRuntimeManager({})
    _patch_sessions(monkeypatch, manager)
    session_key = "web:proc"
    scope = owned_processes.scope_key(session_key=session_key)
    process = _spawn_sleeper()
    try:
        owned_processes.register(
            scope=scope,
            pid=process.pid,
            cmd="sleep 60",
            log_path="",
            session_key=session_key,
        )

        payload = await ceo_sessions.get_ceo_session_tool_jobs(session_key)
        assert payload["count"] == 1
        item = payload["items"][0]
        assert item["kind"] == "process"
        assert item["job_id"] == f"proc:{process.pid}"
        assert item["tool_name"] == "exec"
        assert item["hint"] == "sleep 60"

        stopped = await ceo_sessions.stop_ceo_session_tool_job(session_key, item["job_id"])
        assert stopped == {"ok": True, "job_id": item["job_id"], "status": "stopped"}
        assert _wait_gone(process)
        assert owned_processes.list_owned(scope) == []

        after = await ceo_sessions.get_ceo_session_tool_jobs(session_key)
        assert after["items"] == []
    finally:
        _cleanup_process(process, scope)


@pytest.mark.asyncio
async def test_owned_process_stop_rejects_unowned_pid(monkeypatch):
    manager = _FakeRuntimeManager({})
    _patch_sessions(monkeypatch, manager)

    with pytest.raises(HTTPException) as missing:
        await ceo_sessions.stop_ceo_session_tool_job("web:proc", "proc:99999999")
    assert missing.value.status_code == 404
    assert missing.value.detail == "tool_job_not_found"


@pytest.mark.asyncio
async def test_owned_process_stop_enqueues_model_event(monkeypatch):
    manager = _FakeRuntimeManager({})
    monkeypatch.setattr(
        ceo_sessions,
        "_sessions",
        lambda: (object(), SimpleNamespace(), manager, SimpleNamespace()),
    )
    monkeypatch.setattr(
        ceo_sessions,
        "_resolve_bulk_session_key",
        lambda _manager, _runtime_manager, _state_store, session_id: (str(session_id), False),
    )
    events: list[dict] = []
    monkeypatch.setattr(
        ceo_sessions,
        "get_web_heartbeat_service",
        lambda _agent: SimpleNamespace(enqueue_tool_job=events.append),
    )
    session_key = "web:proc"
    scope = owned_processes.scope_key(session_key=session_key)
    process = _spawn_sleeper()
    try:
        owned_processes.register(
            scope=scope,
            pid=process.pid,
            cmd="sleep 60",
            log_path="",
            session_key=session_key,
        )

        await ceo_sessions.stop_ceo_session_tool_job(session_key, f"proc:{process.pid}")

        assert len(events) == 1
        event = events[0]
        assert event["kind"] == "process"
        assert event["reason"] == "tool_job"
        assert event["status"] == "stopped_by_user"
        assert event["job_id"] == f"proc:{process.pid}"
        assert event["session_id"] == session_key
        assert event["pid"] == process.pid
        assert event["cmd"] == "sleep 60"
    finally:
        _cleanup_process(process, scope)
