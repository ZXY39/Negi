from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.core.messages import UserInputMessage
from g3ku.heartbeat.session_service import HEARTBEAT_OK, WebSessionHeartbeatService
from g3ku.session.manager import SessionManager
from main.protocol import now_iso
from main.service.runtime_service import MainRuntimeService
from main.service.task_stall_callback import (
    TASK_STALL_REASON_SUSPECTED_STALL,
    TASK_STALL_REASON_USER_PAUSED,
    TASK_STALL_REASON_WORKER_UNAVAILABLE,
)
from main.service.task_stall_notifier import _next_bucket_minutes, stall_bucket_minutes


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


class _TaskStallRecorder:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def enqueue_task_stall_payload(self, payload: dict[str, object] | None) -> bool:
        self.payloads.append(dict(payload or {}))
        return True


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


class _RuntimeManager:
    def __init__(self, session) -> None:
        self._session = session

    def get_or_create(self, **kwargs):
        _ = kwargs
        return self._session


class _FakeHeartbeatSession:
    def __init__(self, *, output: str = HEARTBEAT_OK) -> None:
        self.state = SimpleNamespace(status="idle", is_running=False)
        self.prompts: list[UserInputMessage] = []
        self._listeners = set()
        self._output = output

    def subscribe(self, listener):
        self._listeners.add(listener)

        def _unsubscribe() -> None:
            self._listeners.discard(listener)

        return _unsubscribe

    async def prompt(self, user_message, persist_transcript: bool = False) -> SimpleNamespace:
        _ = persist_transcript
        self.prompts.append(user_message)
        return SimpleNamespace(output=self._output)


class _Registry:
    def __init__(self) -> None:
        self._seq: dict[str, int] = {}
        self.published: list[tuple[str, dict[str, object]]] = []

    def next_ceo_seq(self, session_id: str) -> int:
        key = str(session_id or "")
        self._seq[key] = self._seq.get(key, 0) + 1
        return self._seq[key]

    def publish_ceo(self, session_id: str, envelope: dict[str, object]) -> None:
        self.published.append((str(session_id or ""), dict(envelope)))


def test_task_stall_bucket_schedule_starts_at_twenty_minutes() -> None:
    assert stall_bucket_minutes("2026-03-24T00:00:00+00:00", now=datetime.fromisoformat("2026-03-24T00:19:59+00:00")) == 0
    assert stall_bucket_minutes("2026-03-24T00:00:00+00:00", now=datetime.fromisoformat("2026-03-24T00:20:00+00:00")) == 20
    assert stall_bucket_minutes("2026-03-24T00:00:00+00:00", now=datetime.fromisoformat("2026-03-24T00:29:59+00:00")) == 20
    assert stall_bucket_minutes("2026-03-24T00:00:00+00:00", now=datetime.fromisoformat("2026-03-24T00:30:00+00:00")) == 30
    assert stall_bucket_minutes("2026-03-24T00:00:00+00:00", now=datetime.fromisoformat("2026-03-24T00:40:00+00:00")) == 40
    assert _next_bucket_minutes(0) == 20
    assert _next_bucket_minutes(20) == 30
    assert _next_bucket_minutes(30) == 40


@pytest.mark.asyncio
async def test_task_stall_notifier_emits_and_resets_after_visible_output(tmp_path: Path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    heartbeat = _TaskStallRecorder()
    service.bind_runtime_loop(SimpleNamespace(web_session_heartbeat=heartbeat))
    service.task_stall_notifier.minute_seconds = 0.01
    started = asyncio.Event()
    blocker = asyncio.Event()

    async def _blocking_run_node(task_id: str, node_id: str):
        _ = task_id, node_id
        started.set()
        await blocker.wait()
        raise asyncio.CancelledError()

    service.node_runner.run_node = _blocking_run_node

    try:
        record = await service.create_task("stall me", session_id="web:stall-demo")
        service.task_stall_notifier.reset_visible_output(
            record.task_id,
            occurred_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.sleep(0.21)
        await asyncio.sleep(0.11)

        assert [payload["bucket_minutes"] for payload in heartbeat.payloads[:2]] == [20, 30]

        before_count = len(heartbeat.payloads)
        service.log_service.append_node_output(record.task_id, record.root_node_id, content="partial progress")
        for _ in range(40):
            if any(
                int(payload.get("bucket_minutes") or 0) == 20
                for payload in heartbeat.payloads[before_count:]
            ):
                break
            await asyncio.sleep(0.01)

        assert len(heartbeat.payloads) > before_count
        new_payloads = heartbeat.payloads[before_count:]
        assert any(int(payload.get("bucket_minutes") or 0) == 20 for payload in new_payloads)
        assert any(str(payload.get("task_id") or "") == record.task_id for payload in new_payloads)
    finally:
        blocker.set()
        await service.close()


@pytest.mark.asyncio
async def test_task_stall_heartbeat_prompt_includes_diagnostics_and_actions(tmp_path: Path) -> None:
    session_id = "web:ceo-stall"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession()
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    task = await service.create_task("stall heartbeat", session_id=session_id)
    service.log_service.update_task_runtime_meta(
        task.task_id,
        last_visible_output_at="2026-03-24T00:00:00+00:00",
        last_stall_notice_bucket_minutes=10,
    )
    heartbeat = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=service,
        session_manager=session_manager,
    )
    heartbeat.enqueue_task_stall_payload(
        service.build_task_stall_payload(
            task.task_id,
            bucket_minutes=10,
            last_visible_output_at="2026-03-24T00:00:00+00:00",
        )
    )
    heartbeat._started = True

    next_delay = await heartbeat._run_session(session_id)

    assert next_delay is None
    assert len(live_session.prompts) == 1
    prompt = str(live_session.prompts[0].content)
    assert "suspected_stall" in prompt
    assert "task_progress(task_id)" in prompt
    assert "stop_tool_execution(task_id)" in prompt
    assert task.task_id in prompt
    assert "may be stalled" in prompt
    await service.close()


@pytest.mark.asyncio
async def test_task_stall_heartbeat_discards_stale_event_after_new_output(tmp_path: Path) -> None:
    session_id = "web:ceo-stall-stale"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession()
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    task = await service.create_task("stale stall heartbeat", session_id=session_id)
    heartbeat = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(tool_execution_manager=None),
        runtime_manager=_RuntimeManager(live_session),
        main_task_service=service,
        session_manager=session_manager,
    )
    payload = service.build_task_stall_payload(
        task.task_id,
        bucket_minutes=10,
        last_visible_output_at="2026-03-24T00:00:00+00:00",
    )
    heartbeat.enqueue_task_stall_payload(payload)
    service.log_service.update_task_runtime_meta(
        task.task_id,
        last_visible_output_at=now_iso(),
        last_stall_notice_bucket_minutes=0,
    )
    heartbeat._started = True

    next_delay = await heartbeat._run_session(session_id)

    assert next_delay is None
    assert live_session.prompts == []
    assert heartbeat._events.peek(session_id) == []
    await service.close()


@pytest.mark.asyncio
async def test_web_mode_build_task_stall_payload_skips_when_worker_offline(tmp_path: Path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None

    try:
        task = await service.create_task("worker offline should not look stalled", session_id="web:stall-worker-offline")
        stale_at = "2000-01-01T00:00:00+00:00"
        service.log_service.update_task_runtime_meta(
            task.task_id,
            last_visible_output_at=stale_at,
            last_stall_notice_bucket_minutes=0,
        )

        payload = service.build_task_stall_payload(
            task.task_id,
            bucket_minutes=10,
            last_visible_output_at=stale_at,
        )

        assert payload == {}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_stall_reason_classification_distinguishes_pause_worker_and_real_stall(tmp_path: Path) -> None:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None

    try:
        paused_task = await service.create_task("paused task", session_id="web:stall-reason-paused")
        service.log_service.set_pause_state(paused_task.task_id, pause_requested=True, is_paused=True)
        assert service.classify_task_stall_reason(paused_task.task_id) == TASK_STALL_REASON_USER_PAUSED

        offline_task = await service.create_task("offline task", session_id="web:stall-reason-offline")
        assert service.classify_task_stall_reason(offline_task.task_id) == TASK_STALL_REASON_WORKER_UNAVAILABLE

        service.store.upsert_worker_status(
            worker_id="worker:test",
            role="task_worker",
            status="running",
            updated_at=now_iso(),
            payload={"active_task_count": 1, "execution_mode": "worker"},
        )
        stalled_task = await service.create_task("real stall task", session_id="web:stall-reason-stalled")
        assert service.classify_task_stall_reason(stalled_task.task_id) == TASK_STALL_REASON_SUSPECTED_STALL
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_web_session_heartbeat_drops_task_stall_outbox_when_worker_offline(tmp_path: Path) -> None:
    session_id = "web:ceo-stall-worker-offline"
    session_manager = SessionManager(tmp_path)
    persisted = session_manager.get_or_create(session_id)
    session_manager.save(persisted)
    live_session = _FakeHeartbeatSession()
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None

    try:
        task = await service.create_task("offline replay should be ignored", session_id=session_id)
        stale_at = "2000-01-01T00:00:00+00:00"
        payload = {
            "task_id": task.task_id,
            "session_id": session_id,
            "title": task.title,
            "bucket_minutes": 10,
            "stalled_minutes": 14,
            "last_visible_output_at": stale_at,
            "brief_text": "stalled while worker was restarting",
            "latest_node_summary": "root node",
            "runtime_summary_excerpt": "root phase=before_model",
        }
        normalized = service.build_task_stall_payload(
            task.task_id,
            bucket_minutes=10,
            last_visible_output_at=stale_at,
        )
        assert normalized == {}
        dedupe_key = "task-stall-offline-replay"
        service.store.put_task_stall_outbox(
            dedupe_key=dedupe_key,
            task_id=task.task_id,
            session_id=session_id,
            created_at=stale_at,
            payload={**payload, "dedupe_key": dedupe_key},
        )
        heartbeat = WebSessionHeartbeatService(
            workspace=tmp_path,
            agent=SimpleNamespace(tool_execution_manager=None),
            runtime_manager=_RuntimeManager(live_session),
            main_task_service=service,
            session_manager=session_manager,
        )

        accepted = heartbeat.enqueue_task_stall_payload({**payload, "dedupe_key": dedupe_key})

        assert accepted is False
        assert heartbeat._events.peek(session_id) == []
        entry = service.store.get_task_stall_outbox(dedupe_key)
        assert entry is not None
        assert entry["delivery_state"] == "delivered"
    finally:
        await service.close()
