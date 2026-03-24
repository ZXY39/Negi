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


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


class _TaskStallRecorder:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    def enqueue_task_stall_payload(self, payload: dict[str, object] | None) -> bool:
        self.payloads.append(dict(payload or {}))
        return True


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
        await asyncio.sleep(0.06)
        await asyncio.sleep(0.06)

        assert [payload["bucket_minutes"] for payload in heartbeat.payloads[:2]] == [5, 10]

        before_count = len(heartbeat.payloads)
        service.log_service.append_node_output(record.task_id, record.root_node_id, content="partial progress")
        for _ in range(20):
            if any(
                int(payload.get("bucket_minutes") or 0) == 5
                for payload in heartbeat.payloads[before_count:]
            ):
                break
            await asyncio.sleep(0.01)

        assert len(heartbeat.payloads) > before_count
        new_payloads = heartbeat.payloads[before_count:]
        assert any(int(payload.get("bucket_minutes") or 0) == 5 for payload in new_payloads)
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
    service.task_runner.start_background = lambda task_id: None
    task = await service.create_task("stall heartbeat", session_id=session_id)
    service.log_service.update_runtime_state(
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
    service.task_runner.start_background = lambda task_id: None
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
    service.log_service.update_runtime_state(
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
