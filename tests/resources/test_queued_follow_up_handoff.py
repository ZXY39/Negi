"""排队补充的接棒合同：批次归属、连续接批、失败/取消结算与单实例任务。

钉住的形状（docs/architecture/runtime-overview.md「压缩窗口：入站闸门与基线写入仲裁」、
web-and-admin.md「Queued Follow-Ups」）：

- 活动批次在回合锁内配置：等锁的第二批不能覆盖第一批的 `_active_user_batch_inputs`。
- 一次空闲排水会连续接走多批；中途失败/暂停/审批/压缩立即停手。
- 失败批次只恢复转录里仍是 pending 的条目；已落 failed 的不重投。
- 取消给每个已发 started 的批次补一个终态，整批不重投。
- 手动暂停后的空闲排水不自动起回合。
- 接棒任务在会话内单实例，并登记到 `_active_tasks` 的会话键上（pause/cancel/shutdown 可停）。
- 外部会话的排队批次进 `ExternalTurnService` 登记，状态/暂停接口按同一 turn_id 找得到。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.cron.runtime_dispatch import request_queued_follow_up_handoff
from g3ku.heartbeat.session_service import WebSessionHeartbeatService
from g3ku.runtime import web_ceo_sessions
from g3ku.runtime.api.external_turns import ExternalTurnService
from g3ku.runtime.external_events import get_session_event_hub, reset_session_event_hubs
from g3ku.runtime.external_sessions import ExternalSessionEntry
from g3ku.runtime.session_agent import (
    _TRANSCRIPT_STATE_FAILED,
    _TRANSCRIPT_STATE_PENDING,
    RuntimeAgentSession,
)
from g3ku.session.manager import SessionManager


class _CancelToken:
    def cancel(self, *, reason: str = "") -> None:
        _ = reason


class _HeartbeatController:
    def clear_session(self, session_id: str) -> None:
        _ = session_id


class _RaisingRunner:
    async def run_turn(self, *, user_input, session, on_progress):
        _ = user_input, session, on_progress
        raise RuntimeError("Error code: 400 - provider rejected the request")


@pytest.fixture(autouse=True)
def _clean_hubs():
    reset_session_event_hubs()
    yield
    reset_session_event_hubs()


@pytest.fixture(autouse=True)
def _patch_web_runtime_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _noop_refresh(*, force: bool = False, reason: str = "") -> None:
        _ = force, reason

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _noop_refresh)
    monkeypatch.setattr(web_ceo_sessions, "workspace_path", lambda: tmp_path)


def _build_loop(
    tmp_path: Path,
    *,
    runner: object = None,
    active_tasks: dict[str, set[asyncio.Task]] | None = None,
) -> SimpleNamespace:
    registered = active_tasks if active_tasks is not None else {}

    def _register_active_task(session_key: str, task: asyncio.Task) -> None:
        bucket = registered.setdefault(str(session_key or "").strip(), set())
        bucket.add(task)

        def _cleanup(done: asyncio.Task) -> None:
            tasks = registered.get(str(session_key or "").strip())
            if tasks is None:
                return
            tasks.discard(done)
            if not tasks:
                registered.pop(str(session_key or "").strip(), None)

        task.add_done_callback(_cleanup)

    async def _cancel_session_tasks(session_key: str) -> int:
        tasks = list(registered.pop(str(session_key or "").strip(), set()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    return SimpleNamespace(
        model="gpt-test",
        reasoning_effort=None,
        sessions=SessionManager(tmp_path),
        multi_agent_runner=runner,
        memory_manager=None,
        commit_service=None,
        prompt_trace=False,
        web_session_heartbeat=_HeartbeatController(),
        create_session_cancellation_token=lambda _key: _CancelToken(),
        release_session_cancellation_token=lambda _key, _token: None,
        cancel_session_tasks=_cancel_session_tasks,
        _register_active_task=_register_active_task,
        _use_rag_memory=lambda: False,
    )


def _session(
    tmp_path: Path,
    key: str = "ext:test-bridge:handoff",
    *,
    runner: object = None,
    active_tasks: dict[str, set[asyncio.Task]] | None = None,
) -> RuntimeAgentSession:
    return RuntimeAgentSession(
        _build_loop(tmp_path, runner=runner, active_tasks=active_tasks),
        session_key=key,
        channel="ext" if key.startswith("ext:") else "web",
        chat_id=key.rsplit(":", 1)[-1],
    )


def _transcript_rows(tmp_path: Path, key: str) -> list[dict]:
    return list(SessionManager(tmp_path).get_or_create(key).messages or [])


def _user_row(tmp_path: Path, key: str, turn_id: str) -> dict:
    for row in _transcript_rows(tmp_path, key):
        if str(row.get("role") or "") != "user":
            continue
        if str((row.get("metadata") or {}).get("_transcript_turn_id") or "") == turn_id:
            return row
    raise AssertionError(f"user row not found: {turn_id}")


# -- a) 批次上下文归回合锁 -----------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_batch_activates_its_batch_inside_the_turn_lock(tmp_path: Path) -> None:
    session = _session(tmp_path, "ext:test-bridge:batch-lock")
    entered = asyncio.Event()
    second_started = asyncio.Event()
    observed: list[tuple[str, ...]] = []

    def _active_contents() -> tuple[str, ...]:
        return tuple(
            str(getattr(item, "content", "") or "")
            for item in list(session._active_user_batch_inputs or [])
        )

    async def _fake_prompt_locked(user_input, *, persist_transcript=True, live_context=None):
        _ = user_input, persist_transcript, live_context
        observed.append(_active_contents())
        if len(observed) == 1:
            entered.set()
            # 等第二批已经等在锁外再读一次：锁外配置会把这里读成 "B"。
            await second_started.wait()
            observed.append(_active_contents())
        return SimpleNamespace(output="")

    session._prompt_locked = _fake_prompt_locked  # type: ignore[method-assign]

    first = asyncio.create_task(session.prompt_batch(["A"]))
    await entered.wait()
    second = asyncio.create_task(session.prompt_batch(["B"]))
    await asyncio.sleep(0.05)
    second_started.set()
    await asyncio.gather(first, second)

    assert observed == [("A",), ("A",), ("B",)]


# -- b) 一次排水连续接走多批 ---------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_continues_with_messages_queued_during_the_first_batch(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, "ext:test-bridge:batch-chain")
    calls: list[list[str]] = []

    async def _prompt_batch(messages, **_kwargs):
        texts = [str(getattr(item, "content", item)) for item in messages]
        calls.append(texts)
        if len(calls) == 1:
            await session.queue_follow_up_batch(["后到的"], persist_transcript=False)
        return SimpleNamespace(output="ok")

    session.prompt_batch = _prompt_batch  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["先到的"], persist_transcript=False)

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    assert calls == [["先到的"], ["后到的"]]
    assert result["dispatched"] == 2
    assert result["started"] == 2
    assert result["batches"] == 2
    assert result["reason"] == ""
    assert list(session._state.queued_follow_up_messages or []) == []


# -- c) 失败结算读转录真实状态 -------------------------------------------------


@pytest.mark.asyncio
async def test_real_failure_settles_rows_and_does_not_requeue_the_batch(
    tmp_path: Path,
) -> None:
    key = "web:ceo-handoff-failure"
    session = _session(tmp_path, key, runner=_RaisingRunner())
    queued = await session.queue_follow_up_batch(
        ["这条会在失败请求里"],
        persist_transcript=True,
    )
    turn_id = str((queued[0].metadata or {}).get("_transcript_turn_id") or "").strip()

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    assert result["reason"] == "dispatch_failed"
    assert result["failed"] == 1
    assert result["restored"] == 0
    assert list(session._state.queued_follow_up_messages or []) == []
    assert _user_row(tmp_path, key, turn_id)["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_FAILED
    # 失败批次没有被热重试。
    assert await session.dispatch_queued_follow_ups_if_idle(source="test") == {
        "dispatched": 0,
        "reason": "empty",
        "source": "test",
        "turn_id": "",
        "turn_ids": [],
        "output": "",
        "queued": 0,
        "started": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "restored": 0,
        "batches": 0,
    }


@pytest.mark.asyncio
async def test_unclaimed_rows_are_restored_front_when_prompt_fails_before_claim(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, "ext:test-bridge:restore-pending")
    queued = await session.queue_follow_up_batch(["还没被认领"], persist_transcript=True)
    turn_id = str((queued[0].metadata or {}).get("_transcript_turn_id") or "").strip()
    assert _user_row(tmp_path, "ext:test-bridge:restore-pending", turn_id)[
        "metadata"
    ]["_transcript_state"] == _TRANSCRIPT_STATE_PENDING

    async def _raise_before_persisting(messages, **_kwargs):
        _ = messages
        raise RuntimeError("transcript persistence unavailable")

    session.prompt_batch = _raise_before_persisting  # type: ignore[method-assign]

    result = await session.dispatch_queued_follow_ups_if_idle(source="test")

    assert result["reason"] == "dispatch_failed"
    assert result["restored"] == 1
    assert [str(item.content) for item in session._state.queued_follow_up_messages] == [
        "还没被认领"
    ]


# -- d) 取消的终态与不重投 -----------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_dispatch_emits_one_terminal_and_leaves_the_claimed_row_pending(
    tmp_path: Path,
) -> None:
    key = "ext:test-bridge:dispatch-cancel"
    session = _session(tmp_path, key)
    entered = asyncio.Event()

    async def _blocking(messages, **_kwargs):
        _ = messages
        entered.set()
        await asyncio.Event().wait()

    session.prompt_batch = _blocking  # type: ignore[method-assign]
    queued = await session.queue_follow_up_batch(["取消时在跑"], persist_transcript=True)
    turn_id = str((queued[0].metadata or {}).get("_transcript_turn_id") or "").strip()

    task = asyncio.create_task(session.dispatch_queued_follow_ups_if_idle(source="test"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    events = get_session_event_hub(key).replay(0)
    assert [str(event["type"]) for event in events] == ["turn.started", "turn.completed"]
    assert events[-1].get("cancelled") is True
    assert list(session._state.queued_follow_up_messages or []) == []
    assert len(session._listeners) == 0
    # durable 那一半没有被吞：仍是 pending，交给既有恢复路径。
    assert _user_row(tmp_path, key, turn_id)["metadata"]["_transcript_state"] == _TRANSCRIPT_STATE_PENDING


@pytest.mark.asyncio
async def test_cancel_before_started_returns_the_batch_to_the_queue(tmp_path: Path) -> None:
    key = "ext:test-bridge:dispatch-cancel-before-start"
    session = _session(tmp_path, key)
    entered = asyncio.Event()

    async def _blocking_state_snapshot() -> None:
        entered.set()
        await asyncio.Event().wait()

    session._emit_state_snapshot = _blocking_state_snapshot  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["还没起回合"], persist_transcript=False)

    task = asyncio.create_task(session.dispatch_queued_follow_ups_if_idle(source="test"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [str(item.content) for item in session._state.queued_follow_up_messages] == [
        "还没起回合"
    ]
    assert get_session_event_hub(key).replay(0) == []


@pytest.mark.asyncio
async def test_pause_guard_refuses_to_auto_start_after_a_manual_pause(tmp_path: Path) -> None:
    session = _session(tmp_path, "ext:test-bridge:pause-guard")
    await session.queue_follow_up_batch(["暂停期间排队的"], persist_transcript=False)
    session._last_stop_reason = "user_pause"

    result = await session.dispatch_queued_follow_ups_if_idle(source="ws_reconnect")

    assert result["reason"] == "paused"
    assert result["started"] == 0
    assert len(session._state.queued_follow_up_messages) == 1

    session._last_stop_reason = ""

    async def _prompt_batch(messages, **_kwargs):
        _ = messages
        return SimpleNamespace(output="ok")

    session.prompt_batch = _prompt_batch  # type: ignore[method-assign]
    result = await session.dispatch_queued_follow_ups_if_idle(source="ws_reconnect")
    assert result["dispatched"] == 1


# -- e) 接棒任务单实例、可被会话停止 -------------------------------------------


@pytest.mark.asyncio
async def test_request_queued_follow_up_dispatch_merges_triggers_into_one_task(
    tmp_path: Path,
) -> None:
    active: dict[str, set[asyncio.Task]] = {}
    session = _session(tmp_path, "ext:test-bridge:handoff-single", active_tasks=active)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _waiting(*, source: str = "") -> dict:
        calls.append(source)
        entered.set()
        await release.wait()
        return {"dispatched": 0}

    session.dispatch_queued_follow_ups_if_idle = _waiting  # type: ignore[method-assign]

    first = session.request_queued_follow_up_dispatch(source="heartbeat_wake")
    await entered.wait()
    second = session.request_queued_follow_up_dispatch(source="cron_wake")

    assert first is second
    assert calls == ["heartbeat_wake"]
    assert session._queued_follow_up_dispatch_sources == {"heartbeat_wake", "cron_wake"}
    assert session._state.session_key in active

    release.set()
    await first
    await asyncio.sleep(0)
    assert session.queued_follow_up_dispatch_pending() is False


@pytest.mark.asyncio
async def test_cancel_session_tasks_stops_the_handoff_task_and_closes_its_turn(
    tmp_path: Path,
) -> None:
    key = "ext:test-bridge:handoff-cancel"
    session = _session(tmp_path, key)
    entered = asyncio.Event()

    async def _blocking(messages, **_kwargs):
        _ = messages
        entered.set()
        await asyncio.Event().wait()

    session.prompt_batch = _blocking  # type: ignore[method-assign]
    await session.queue_follow_up_batch(["被关机取消"], persist_transcript=False)
    task = session.request_queued_follow_up_dispatch(source="heartbeat_wake")
    assert task is not None
    await entered.wait()

    cancelled = await session._loop.cancel_session_tasks(key)

    assert cancelled == 1
    assert task.cancelled()
    events = get_session_event_hub(key).replay(0)
    assert [str(event["type"]) for event in events] == ["turn.started", "turn.completed"]
    assert events[-1].get("cancelled") is True
    await asyncio.sleep(0)
    assert session.queued_follow_up_dispatch_pending() is False


# -- f) 唤醒与 cron 只请求接棒，不内联等用户回合 -------------------------------


@pytest.mark.asyncio
async def test_heartbeat_requests_the_handoff_instead_of_awaiting_the_user_turn(
    tmp_path: Path,
) -> None:
    class _HandoffSession:
        def __init__(self) -> None:
            self.requests: list[str] = []
            self.dispatches: list[str] = []

        def request_queued_follow_up_dispatch(self, *, source: str = ""):
            self.requests.append(source)
            return None

        async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict:
            self.dispatches.append(source)
            return {"dispatched": 1}

    live_session = _HandoffSession()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(frontdoor_message_scanner=None),
        runtime_manager=SimpleNamespace(get=lambda _key: live_session),
        main_task_service=SimpleNamespace(),
        session_manager=SessionManager(tmp_path),
    )

    await service._dispatch_queued_follow_ups_after_wake("ext:test-bridge:heartbeat")

    assert live_session.requests == ["heartbeat_wake"]
    assert live_session.dispatches == []


@pytest.mark.asyncio
async def test_heartbeat_falls_back_to_awaiting_dispatch_for_duck_sessions(
    tmp_path: Path,
) -> None:
    class _DispatchOnlySession:
        def __init__(self) -> None:
            self.dispatches: list[str] = []

        async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict:
            self.dispatches.append(source)
            return {"dispatched": 1}

    live_session = _DispatchOnlySession()
    service = WebSessionHeartbeatService(
        workspace=tmp_path,
        agent=SimpleNamespace(frontdoor_message_scanner=None),
        runtime_manager=SimpleNamespace(get=lambda _key: live_session),
        main_task_service=SimpleNamespace(),
        session_manager=SessionManager(tmp_path),
    )

    await service._dispatch_queued_follow_ups_after_wake("ext:test-bridge:heartbeat")

    assert live_session.dispatches == ["heartbeat_wake"]


@pytest.mark.asyncio
async def test_cron_requests_the_session_handoff_and_reports_whether_it_was_owned() -> None:
    class _HandoffSession:
        def __init__(self) -> None:
            self.requests: list[str] = []
            self.dispatches: list[str] = []

        def request_queued_follow_up_dispatch(self, *, source: str = ""):
            self.requests.append(source)
            return None

        async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict:
            self.dispatches.append(source)
            return {"dispatched": 1}

    session = _HandoffSession()
    bridge = SimpleNamespace(get_existing_session=lambda _key: session)

    handed = await request_queued_follow_up_handoff(bridge, "ext:test-bridge:cron", source="cron_wake")

    assert handed is True
    assert session.requests == ["cron_wake"]
    assert session.dispatches == []


@pytest.mark.asyncio
async def test_cron_falls_back_to_dispatch_when_the_session_has_no_handoff_api() -> None:
    class _DispatchOnlySession:
        def __init__(self) -> None:
            self.dispatches: list[str] = []

        async def dispatch_queued_follow_ups_if_idle(self, *, source: str = "") -> dict:
            self.dispatches.append(source)
            return {"dispatched": 1}

    session = _DispatchOnlySession()
    bridge = SimpleNamespace(get_existing_session=lambda _key: session)

    handed = await request_queued_follow_up_handoff(bridge, "ext:test-bridge:cron", source="cron_wake")

    assert handed is False
    assert session.dispatches == ["cron_wake"]


# -- g) 外部会话的排队批次进回合登记 -------------------------------------------


def test_external_queued_batch_turn_is_visible_to_status_and_pause_lookups() -> None:
    service = ExternalTurnService(runtime_bridge=SimpleNamespace(), register_task=None)
    record = service.register_queued_batch_turn(
        turn_id="turn-queued-1",
        session_key="ext:bridge:chat",
        bridge_id="bridge",
        external_key="chat",
        source="heartbeat_wake",
    )

    assert record is not None
    assert record.status == "running"
    assert service.get_turn("turn-queued-1") is record
    assert service.inflight_turn_id_for("ext:bridge:chat") == "turn-queued-1"
    # 同一批重复登记不造第二条记录。
    assert (
        service.register_queued_batch_turn(
            turn_id="turn-queued-1",
            session_key="ext:bridge:chat",
            bridge_id="bridge",
            external_key="chat",
        )
        is record
    )

    service.settle_queued_batch_turn("turn-queued-1", status="completed")

    assert record.status == "completed"
    assert service.inflight_turn_id_for("ext:bridge:chat") is None


@pytest.mark.asyncio
async def test_dispatch_registers_and_settles_the_external_batch_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "ext:test-bridge:external-register"
    session = _session(tmp_path, key)

    class _FakeService:
        def __init__(self) -> None:
            self.registered: list[dict] = []
            self.settled: list[tuple[str, str]] = []

        def register_queued_batch_turn(self, **kwargs):
            self.registered.append(kwargs)
            return SimpleNamespace(turn_id=kwargs.get("turn_id"))

        def settle_queued_batch_turn(self, turn_id: str, *, status: str) -> None:
            self.settled.append((turn_id, status))

    service = _FakeService()
    entry = ExternalSessionEntry(
        bridge_id="test-bridge",
        external_key="chat",
        session_key=key,
        created_at="2026-11-06T00:00:00",
    )
    monkeypatch.setattr(
        "g3ku.runtime.api.external_turns.get_external_turn_service",
        lambda: service,
    )
    monkeypatch.setattr(
        "g3ku.runtime.external_sessions.get_external_session_registry",
        lambda: SimpleNamespace(get_by_session_key=lambda _key: entry),
    )

    async def _prompt_batch(messages, **_kwargs):
        _ = messages
        return SimpleNamespace(output="ok")

    session.prompt_batch = _prompt_batch  # type: ignore[method-assign]
    queued = await session.queue_follow_up_batch(["外部唤醒补发"], persist_transcript=False)
    turn_id = str((queued[0].metadata or {}).get("_transcript_turn_id") or "").strip()

    result = await session.dispatch_queued_follow_ups_if_idle(source="heartbeat_wake")

    assert result["dispatched"] == 1
    assert service.registered == [
        {
            "turn_id": turn_id,
            "session_key": key,
            "bridge_id": "test-bridge",
            "external_key": "chat",
            "source": "heartbeat_wake",
        }
    ]
    assert service.settled == [(turn_id, "completed")]
    assert result["turn_id"] == turn_id
