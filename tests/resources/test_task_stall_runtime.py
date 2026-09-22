from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
    build_task_stall_dedupe_key,
)
from main.service.task_stall_notifier import (
    TaskStallNotifier,
    _next_bucket_minutes,
    effective_silence_start,
    running_tool_deadline,
    stall_bucket_minutes,
)


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
        workspace_root=tmp_path,
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
        workspace_root=tmp_path,
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
    assert "Perf in stall window" in prompt
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
        workspace_root=tmp_path,
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
        workspace_root=tmp_path,
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
        workspace_root=tmp_path,
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
        workspace_root=tmp_path,
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
        # The endpoint/heartbeat now recompute the canonical dedupe key, so the
        # outbox row must be written under that same canonical key for the
        # offline-drop ack to land on it.
        dedupe_key = build_task_stall_dedupe_key(
            task_id=task.task_id,
            bucket_minutes=10,
            last_visible_output_at=stale_at,
        )
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


def _runtime_state_with_tool(tool_call: dict[str, object]) -> dict[str, object]:
    return {"frames": [{"node_id": "node:a", "tool_calls": [tool_call]}]}


def test_running_tool_deadline_uses_started_at_plus_timeout() -> None:
    state = _runtime_state_with_tool(
        {
            "tool_call_id": "call_1",
            "tool_name": "exec",
            "status": "running",
            "started_at": "2026-03-24T00:00:00+00:00",
            "timeout_seconds": 1800.0,
        }
    )
    assert running_tool_deadline(state) == datetime.fromisoformat("2026-03-24T00:30:00+00:00")


def test_running_tool_deadline_ignores_finished_exempt_and_invalid() -> None:
    # 已完成（非 running）的工具不再约束失速判定。
    assert (
        running_tool_deadline(
            _runtime_state_with_tool(
                {
                    "status": "success",
                    "started_at": "2026-03-24T00:00:00+00:00",
                    "timeout_seconds": 1800.0,
                }
            )
        )
        is None
    )
    # 豁免工具没有记录 timeout_seconds，不产生截止时间。
    assert (
        running_tool_deadline(
            _runtime_state_with_tool({"status": "running", "started_at": "2026-03-24T00:00:00+00:00"})
        )
        is None
    )
    # 缺少 started_at 的 running 工具无法推导截止时间。
    assert (
        running_tool_deadline(_runtime_state_with_tool({"status": "running", "timeout_seconds": 600.0}))
        is None
    )
    # 空状态。
    assert running_tool_deadline({"frames": []}) is None
    assert running_tool_deadline(None) is None


def test_running_tool_deadline_takes_latest_of_multiple_running_tools() -> None:
    state = {
        "frames": [
            {
                "node_id": "node:a",
                "tool_calls": [
                    {"status": "running", "started_at": "2026-03-24T00:00:00+00:00", "timeout_seconds": 600.0},
                    {"status": "running", "started_at": "2026-03-24T00:05:00+00:00", "timeout_seconds": 3600.0},
                ],
            }
        ]
    }
    # 第二个调用：00:05 + 60min = 01:05，晚于第一个的 00:10。
    assert running_tool_deadline(state) == datetime.fromisoformat("2026-03-24T01:05:00+00:00")


def test_effective_silence_start_prefers_running_tool_deadline() -> None:
    state = _runtime_state_with_tool(
        {
            "status": "running",
            "started_at": "2026-03-24T00:00:00+00:00",
            "timeout_seconds": 1800.0,
        }
    )
    # 最近可见输出早于工具截止时间：以截止时间作为静默起点。
    assert effective_silence_start(state, "2026-03-24T00:00:00+00:00") == datetime.fromisoformat(
        "2026-03-24T00:30:00+00:00"
    )
    # 最近可见输出晚于工具截止时间：保留可见输出时间。
    assert effective_silence_start(state, "2026-03-24T01:00:00+00:00") == datetime.fromisoformat(
        "2026-03-24T01:00:00+00:00"
    )
    # 无运行中工具：保留可见输出时间。
    assert effective_silence_start({"frames": []}, "2026-03-24T00:00:00+00:00") == datetime.fromisoformat(
        "2026-03-24T00:00:00+00:00"
    )


def test_running_tool_within_deadline_does_not_reach_stall_bucket() -> None:
    # 复刻事故场景：节点按要求长时间运行一个带 30 分钟超时的工具（如 exec 内睡眠等待），
    # 在工具截止时间之前静默 25 分钟，不应落入任何失速桶。
    tool_started = "2026-03-24T00:00:00+00:00"
    state = _runtime_state_with_tool(
        {"status": "running", "started_at": tool_started, "timeout_seconds": 1800.0}
    )
    silence_start = effective_silence_start(state, tool_started)
    assert silence_start is not None
    # 截止时间为 00:30；在 00:25（静默 25 分钟）时，相对静默起点仍为负/零 → 无桶。
    at_25min = datetime.fromisoformat("2026-03-24T00:25:00+00:00")
    assert stall_bucket_minutes(silence_start.isoformat(), now=at_25min) == 0
    assert int(max(0.0, (at_25min - silence_start).total_seconds()) // 60) == 0
    # 截止时间过后 20 分钟（00:50）才进入首个失速桶。
    at_50min = datetime.fromisoformat("2026-03-24T00:50:00+00:00")
    assert stall_bucket_minutes(silence_start.isoformat(), now=at_50min) == 20


class _FakeStallLogService:
    def __init__(self, state_getter):
        self._state_getter = state_getter

    def read_runtime_state(self, task_id: str):
        return self._state_getter()

    def read_task_runtime_meta(self, task_id: str):
        state = self._state_getter() or {}
        return {
            "last_visible_output_at": state.get("last_visible_output_at"),
            "last_stall_notice_bucket_minutes": state.get("last_stall_notice_bucket_minutes", 0),
        }

    def update_task_runtime_meta(self, task_id: str, **kwargs):
        state = self._state_getter()
        if isinstance(state, dict):
            state.update(kwargs)


class _FakeStallService:
    def __init__(self, state_getter):
        self.log_service = _FakeStallLogService(state_getter)
        self.emitted: list[dict[str, object]] = []
        self.task = SimpleNamespace(
            task_id="task:fake",
            status="in_progress",
            is_paused=False,
            pause_requested=False,
            cancel_requested=False,
            created_at="2026-03-24T00:00:00+00:00",
        )

    def get_task(self, task_id: str):
        return self.task

    def _task_origin_session_id(self, task):
        return "web:fake"

    def is_task_stall_actionable(self, task_id: str, runtime_state=None) -> bool:
        return True

    def build_task_stall_payload(self, task_id: str, *, bucket_minutes, last_visible_output_at=None):
        return {
            "task_id": task_id,
            "bucket_minutes": bucket_minutes,
            "last_visible_output_at": last_visible_output_at,
        }

    def emit_task_stall(self, payload) -> bool:
        self.emitted.append(dict(payload or {}))
        return True

    def _stall_now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@pytest.mark.asyncio
async def test_notifier_suppresses_then_emits_around_tool_deadline() -> None:
    now = datetime.now(timezone.utc)
    holder: dict[str, object] = {
        "state": {
            # 最近可见输出就在当下，且有一个截止时间仍在未来的运行中工具。
            "last_visible_output_at": now.isoformat(),
            "last_stall_notice_bucket_minutes": 0,
            "frames": [
                {
                    "node_id": "node:a",
                    "tool_calls": [
                        {"status": "running", "started_at": now.isoformat(), "timeout_seconds": 3600.0}
                    ],
                }
            ],
        }
    }
    service = _FakeStallService(lambda: holder["state"])
    notifier = TaskStallNotifier(service=service)
    try:
        # 工具仍在截止时间内：不产生失速心跳。
        await notifier._emit_if_still_due("task:fake")
        assert service.emitted == []

        # 切到「截止时间已过但工具仍 running、且持续无可见输出」的状态：应判定失速。
        past = now - timedelta(minutes=60)
        holder["state"] = {
            "last_visible_output_at": past.isoformat(),
            "last_stall_notice_bucket_minutes": 0,
            "frames": [
                {
                    "node_id": "node:a",
                    "tool_calls": [
                        {"status": "running", "started_at": past.isoformat(), "timeout_seconds": 1800.0}
                    ],
                }
            ],
        }
        await notifier._emit_if_still_due("task:fake")
        assert service.emitted, "expected a stall emission after the tool deadline passed"
        # 静默起点被锚定到工具截止时间（60 分钟前 + 30 分钟 = 30 分钟前）→ 30 分钟桶。
        assert service.emitted[0]["bucket_minutes"] == 30
    finally:
        await notifier.close()
