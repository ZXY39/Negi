from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.heartbeat.session_service import WebSessionHeartbeatService
from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore

from g3ku import shells
from g3ku.shells import web as web_shell
import main.api.bootstrap_rest as bootstrap_rest


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _task_record(task_id: str, root_node_id: str, *, is_paused: bool = False, pause_requested: bool = False) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id="web:shared",
        title="demo",
        user_request="demo",
        status="in_progress",
        root_node_id=root_node_id,
        max_depth=1,
        created_at="2026-03-29T00:00:00+08:00",
        updated_at="2026-03-29T00:00:00+08:00",
        is_paused=is_paused,
        pause_requested=pause_requested,
        token_usage=TokenUsageSummary(tracked=True),
        metadata={},
    )


def _node_record(task_id: str, node_id: str) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=task_id,
        parent_node_id=None,
        root_node_id=node_id,
        depth=0,
        node_kind="execution",
        status="in_progress",
        goal="demo",
        prompt="demo",
        input="demo",
        output=[],
        check_result="",
        final_output="",
        can_spawn_children=False,
        created_at="2026-03-29T00:00:00+08:00",
        updated_at="2026-03-29T00:00:00+08:00",
        token_usage=TokenUsageSummary(tracked=True),
        token_usage_by_model=[],
        metadata={},
    )


class _FakeScheduler:
    def __init__(self) -> None:
        self.enqueued: list[str] = []
        self.cancelled: list[str] = []

    async def enqueue_task(self, task_id: str) -> None:
        self.enqueued.append(str(task_id))

    async def cancel_task(self, task_id: str) -> None:
        self.cancelled.append(str(task_id))

    def is_active(self, task_id: str) -> bool:
        return False

    def is_queued(self, task_id: str) -> bool:
        return str(task_id) in self.enqueued

    async def close(self) -> None:
        pass


def _make_worker_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="worker",
    )
    service._start_worker_loops = lambda: None
    service.global_scheduler = _FakeScheduler()
    return service


def _seed_task(service: MainRuntimeService, task_id: str, root_node_id: str, *, is_paused: bool = False) -> None:
    task_record = _task_record(task_id, root_node_id, is_paused=is_paused, pause_requested=is_paused)
    service.store.upsert_task(task_record)
    service.store.upsert_node(_node_record(task_id, root_node_id))


# ---------------------------------------------------------------------------
# shutdown_pause_registry ledger
# ---------------------------------------------------------------------------

def test_shutdown_pause_registry_roundtrip(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        assert store.list_shutdown_pause_entries() == []

        key = store.record_shutdown_pause_entry(kind="task", ref_id="task:1")
        assert key == "task:task:1"
        store.record_shutdown_pause_entry(kind="session", ref_id="web:shared", channel="web", chat_id="shared")
        # Upsert must not duplicate rows.
        store.record_shutdown_pause_entry(kind="session", ref_id="web:shared", channel="web", chat_id="shared")

        tasks = store.list_shutdown_pause_entries(kind="task")
        sessions = store.list_shutdown_pause_entries(kind="session")
        assert len(tasks) == 1
        assert tasks[0]["entry_key"] == "task:task:1"
        assert len(sessions) == 1
        assert sessions[0]["channel"] == "web"
        assert sessions[0]["chat_id"] == "shared"
        assert len(store.list_shutdown_pause_entries()) == 2

        assert store.mark_shutdown_pause_entry_consumed(kind="session", ref_id="web:shared") is True
        assert store.list_shutdown_pause_entries() == tasks
        assert store.mark_shutdown_pause_entry_consumed(kind="session", ref_id="web:shared") is False
    finally:
        store.close()


def test_shutdown_pause_registry_rejects_unknown_kind(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    try:
        with pytest.raises(ValueError, match="invalid_shutdown_pause_entry"):
            store.record_shutdown_pause_entry(kind="node", ref_id="node:1")
    finally:
        store.close()


# ---------------------------------------------------------------------------
# worker startup auto-resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_startup_auto_resumes_shutdown_paused_task(tmp_path: Path) -> None:
    service = _make_worker_service(tmp_path)
    _seed_task(service, "task:graceful", "node:graceful", is_paused=True)
    service.store.record_shutdown_pause_entry(kind="task", ref_id="task:graceful")
    # A ledger row whose task disappeared must be retired, not crash startup.
    service.store.record_shutdown_pause_entry(kind="task", ref_id="task:gone")

    await service.startup()

    task = service.store.get_task("task:graceful")
    assert task is not None
    assert bool(task.is_paused) is False
    assert bool(task.pause_requested) is False
    assert "task:graceful" in service.global_scheduler.enqueued
    assert (task.metadata or {}).get("recovery_notice", "") == ""
    assert service.store.list_shutdown_pause_entries(kind="task") == []
    await service.close()


@pytest.mark.asyncio
async def test_worker_startup_keeps_user_paused_task_paused(tmp_path: Path) -> None:
    service = _make_worker_service(tmp_path)
    _seed_task(service, "task:user-pause", "node:user-pause", is_paused=True)

    await service.startup()

    task = service.store.get_task("task:user-pause")
    assert task is not None
    assert bool(task.is_paused) is True
    assert "task:user-pause" not in service.global_scheduler.enqueued
    await service.close()


@pytest.mark.asyncio
async def test_worker_startup_abnormal_interruption_still_sets_recovery_notice(tmp_path: Path) -> None:
    service = _make_worker_service(tmp_path)
    _seed_task(service, "task:abnormal", "node:abnormal", is_paused=False)

    await service.startup()

    task = service.store.get_task("task:abnormal")
    assert task is not None
    assert str((task.metadata or {}).get("recovery_notice") or "").strip() == "本任务遇到异常停止，已回退到稳定步骤继续。"
    assert "task:abnormal" in service.global_scheduler.enqueued
    await service.close()


@pytest.mark.asyncio
async def test_force_pause_task_durably_works_without_live_worker(tmp_path: Path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    _seed_task(service, "task:web-pause", "node:web-pause", is_paused=False)
    service.worker_state = lambda: "offline"

    result = await service.force_pause_task_durably("task:web-pause")

    assert result is not None
    assert bool(result.is_paused) is True
    assert bool(result.pause_requested) is True
    await service.close()


def test_release_task_worker_lease_durably_removes_stale_lease_row(tmp_path: Path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service.store.acquire_worker_lease(
        role="task_worker",
        worker_id="worker:old",
        holder_pid=99999,
        acquired_at="2026-09-06T10:00:00+08:00",
        heartbeat_at="2026-09-06T10:00:00+08:00",
        expires_at="2026-09-06T10:00:20+08:00",
        payload={},
    )

    assert service.release_task_worker_lease_durably() is True
    assert service.store.get_worker_lease("task_worker") is None
    assert service.release_task_worker_lease_durably() is False
    service.close()


# ---------------------------------------------------------------------------
# web-side graceful pause helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_running_work_for_shutdown_records_sessions_and_tasks() -> None:
    calls: list[str] = []
    ledger: list[dict[str, str]] = []

    class _Store:
        def __init__(self) -> None:
            self.tasks = [
                SimpleNamespace(task_id="task:running", status="in_progress", is_paused=False),
                SimpleNamespace(task_id="task:user-paused", status="in_progress", is_paused=True),
                SimpleNamespace(task_id="task:done", status="success", is_paused=False),
            ]

        def list_tasks(self):
            return list(self.tasks)

        def record_shutdown_pause_entry(self, **kwargs) -> None:
            ledger.append({str(key): str(value) for key, value in kwargs.items()})

    class _Service:
        def __init__(self) -> None:
            self.store = _Store()

        async def startup(self) -> None:
            calls.append("startup")

        async def pause_task(self, task_id: str) -> None:
            calls.append(f"pause_task:{task_id}")

    class _Session:
        def __init__(self, key: str, running: bool) -> None:
            self.key = key
            self.state = SimpleNamespace(is_running=running, status="running" if running else "idle")

        async def pause(self, *, manual: bool = False) -> None:
            calls.append(f"pause_session:{self.key}:{manual}")

    class _Manager:
        def __init__(self) -> None:
            self.sessions = {
                "web:shared": _Session("web:shared", running=True),
                "web:idle": _Session("web:idle", running=False),
            }

        def list_sessions(self) -> list[str]:
            return ["web:idle", "web:shared"]

        def get(self, session_key: str):
            return self.sessions.get(session_key)

        def session_meta(self, session_key: str):
            return ("web", "shared")

    agent = SimpleNamespace(main_task_service=_Service())
    result = await web_shell.pause_running_work_for_shutdown(agent=agent, runtime_manager=_Manager())

    assert result == {"paused_sessions": 1, "paused_tasks": 1}
    assert "startup" in calls
    assert "pause_session:web:shared:True" in calls
    assert "pause_task:task:running" in calls
    assert "pause_task:task:user-paused" not in calls
    assert {"kind": "session", "ref_id": "web:shared", "channel": "web", "chat_id": "shared"} in ledger
    assert {"kind": "task", "ref_id": "task:running"} in ledger


async def test_pause_running_work_for_shutdown_no_agent_is_noop() -> None:
    result = await web_shell.pause_running_work_for_shutdown(agent=None, runtime_manager=None)
    assert result == {"paused_sessions": 0, "paused_tasks": 0}


# ---------------------------------------------------------------------------
# web-side startup session resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_shutdown_paused_sessions_enqueues_and_consumes(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    store.record_shutdown_pause_entry(kind="session", ref_id="web:resume", channel="web", chat_id="resume")
    store.record_shutdown_pause_entry(kind="session", ref_id="web:missing", channel="web", chat_id="missing")
    try:
        woken: list[str] = []

        class _Path:
            def __init__(self, exists: bool) -> None:
                self._exists = exists

            def exists(self) -> bool:
                return self._exists

        class _SessionManager:
            def get_path(self, session_key: str):
                return _Path(session_key == "web:resume")

        class _Manager:
            def __init__(self) -> None:
                self.created: list[str] = []

            def get_or_create(self, *, session_key: str, channel: str, chat_id: str):
                self.created.append(f"{session_key}|{channel}|{chat_id}")
                return SimpleNamespace()

        class _Heartbeat:
            def enqueue_shutdown_resume(self, session_id: str) -> bool:
                woken.append(str(session_id))
                return True

        agent = SimpleNamespace(main_task_service=SimpleNamespace(store=store), sessions=_SessionManager())
        manager = _Manager()
        resumed = await web_shell.resume_shutdown_paused_sessions(agent=agent, runtime_manager=manager, heartbeat=_Heartbeat())

        assert resumed == 1
        assert woken == ["web:resume"]
        assert manager.created == ["web:resume|web|resume"]
        assert store.list_shutdown_pause_entries(kind="session") == []
    finally:
        store.close()


# ---------------------------------------------------------------------------
# heartbeat shutdown_resume event lane
# ---------------------------------------------------------------------------

def _resume_event() -> object:
    from g3ku.heartbeat.session_events import SessionHeartbeatEventQueue

    return SessionHeartbeatEventQueue().enqueue(
        session_id="web:shared",
        source="main_runtime",
        reason="shutdown_resume",
        dedupe_key="shutdown-resume:web:shared",
        payload={"session_id": "web:shared", "event_reason": "shutdown_resume"},
        delay_seconds=0.0,
    )


def test_heartbeat_build_prompt_instructs_resume_not_heartbeat_ok(tmp_path: Path) -> None:
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=None,
        runtime_manager=None,
        main_task_service=None,
        session_manager=None,
        reply_notifier=None,
    )
    events = [_resume_event()]
    prompt = service._build_prompt(events)

    assert "shutdown_resume" in prompt
    assert "must not reply with HEARTBEAT_OK" in prompt
    assert "Complete the user's previously interrupted request" in prompt
    assert service._events_require_visible_reply(events) is True
    assert service._visible_reply_requires_repair("HEARTBEAT_OK") is True
    assert service._visible_reply_requires_repair("   ") is True


def test_heartbeat_enqueue_shutdown_resume_requests_wake_when_started(monkeypatch, tmp_path: Path) -> None:
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=None,
        runtime_manager=None,
        main_task_service=None,
        session_manager=None,
        reply_notifier=None,
    )
    requests: list[tuple[str, float]] = []
    monkeypatch.setattr(service._wake, "request", lambda session_id, *, delay_s: requests.append((str(session_id), float(delay_s))))
    service._started = True

    assert service.enqueue_shutdown_resume("web:shared") is True
    assert requests == [("web:shared", 0.25)]
    # Duplicate within the same queue lifetime is rejected.
    assert service.enqueue_shutdown_resume("web:shared") is False


# ---------------------------------------------------------------------------
# bootstrap exit endpoint ledger recording
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bootstrap_exit_records_shutdown_pause_ledger(monkeypatch) -> None:
    calls: list[str] = []
    ledger: list[tuple[str, str]] = []

    class _Security:
        def is_unlocked(self) -> bool:
            return True

    class _RuntimeManager:
        def list_sessions(self) -> list[str]:
            return ["web:shared"]

        def get(self, session_id: str):
            if session_id != "web:shared":
                return None
            return SimpleNamespace(state=SimpleNamespace(is_running=True, status="running"))

        async def pause(self, session_id: str, *, manual: bool = False) -> int:
            calls.append(f"pause_session:{session_id}:{manual}")
            return 1

        def session_meta(self, session_id: str):
            return ("web", "shared")

    class _Store:
        def list_tasks(self):
            return [SimpleNamespace(task_id="task:1", status="in_progress", is_paused=False)]

        def record_shutdown_pause_entry(self, **kwargs) -> None:
            ledger.append((str(kwargs.get("kind") or ""), str(kwargs.get("ref_id") or "")))

    class _TaskService:
        def __init__(self) -> None:
            self.store = _Store()

        async def startup(self) -> None:
            calls.append("startup")

        async def pause_task(self, task_id: str):
            calls.append(f"pause_task:{task_id}")
            return None

    agent = SimpleNamespace(sessions=None, main_task_service=_TaskService())
    runtime_manager = _RuntimeManager()

    async def _snapshot() -> dict[str, object]:
        return {"has_running_work": False, "running_sessions": [], "running_tasks": [], "summary_text": "idle"}

    async def _snapshot_idle() -> dict[str, object]:
        return {"has_running_work": False, "running_sessions": [], "running_tasks": [], "summary_text": "idle"}

    monkeypatch.setattr(bootstrap_rest, "_service", lambda: _Security())
    monkeypatch.setattr(bootstrap_rest, "get_agent", lambda: agent)
    monkeypatch.setattr(bootstrap_rest, "get_runtime_manager", lambda _agent=None: runtime_manager)
    monkeypatch.setattr(bootstrap_rest, "_running_work_snapshot", _snapshot_idle)

    paused = await bootstrap_rest._pause_running_work()

    assert paused == {"paused_sessions": 1, "paused_tasks": 1}
    assert ledger == [("session", "web:shared"), ("task", "task:1")]