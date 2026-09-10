from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfoNotFoundError

import pytest
from loguru import logger

import g3ku.shells.web as web_shell
from g3ku.agent.tools.cron import CronTool
from g3ku.bus.events import OutboundMessage
import g3ku.cron.timezones as cron_timezones
from g3ku.config.loader import get_data_dir
from g3ku.core.messages import UserInputMessage
from g3ku.core.task_diagnostics import format_task_await_chain
from g3ku.cron.runtime_dispatch import dispatch_cron_job, resolve_cron_session_key
from g3ku.cron.service import CronService
from g3ku.cron.types import CronJob, CronJobState, CronPayload, CronSchedule
from g3ku.services.cron import (
    DEFAULT_DISPATCH_CANCEL_GRACE_SECONDS,
    DEFAULT_DISPATCH_TIMEOUT_SECONDS,
)
from g3ku.session.manager import SessionManager


class _BridgeRecorder:
    def __init__(self, output: str = "done") -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    async def prompt(self, message, **kwargs):
        self.calls.append({"message": message, **kwargs})
        return SimpleNamespace(output=self.output)


class _Heartbeat:
    def __init__(self) -> None:
        self._started = False
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self._started = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self._started = False


class _MainService:
    def __init__(self) -> None:
        self._started = False

    async def startup(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._started = False


class _CronRecorder:
    def __init__(self) -> None:
        self.enabled = False
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.enabled = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.enabled = False

    def status(self) -> dict[str, object]:
        return {"enabled": self.enabled}


class _CronToolService:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, object]] = []
        self.removed: list[str] = []

    def add_job(self, **kwargs):
        self.add_calls.append(dict(kwargs))
        return SimpleNamespace(name="job", id="job-1")

    def list_jobs(self):
        return []

    def remove_job(self, job_id: str):
        self.removed.append(str(job_id))
        return True


def _make_job(
    *,
    job_id: str = "job-1",
    message: str = "hello",
    channel: str | None = "web",
    to: str | None = "shared",
    session_key: str | None = None,
    max_runs: int = 1,
    delivered_runs: int = 0,
    deliver: bool = True,
) -> CronJob:
    return CronJob(
        id=job_id,
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60000),
        payload=CronPayload(
            kind="agent_turn",
            message=message,
            max_runs=max_runs,
            deliver=deliver,
            channel=channel,
            to=to,
            session_key=session_key,
        ),
        state=CronJobState(
            next_run_at_ms=int(time.time() * 1000) + 60000,
            delivered_runs=delivered_runs,
        ),
    )


def test_cron_service_clears_legacy_store_versions(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "job-1",
                        "name": "legacy",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 60000},
                        "payload": {
                            "kind": "agent_turn",
                            "message": "legacy",
                            "deliver": True,
                            "channel": "web",
                            "to": "shared",
                        },
                        "state": {"nextRunAtMs": int(time.time() * 1000) + 60000},
                        "createdAtMs": int(time.time() * 1000),
                        "updatedAtMs": int(time.time() * 1000),
                        "deleteAfterRun": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=True)
    raw = json.loads(store_path.read_text(encoding="utf-8"))

    assert jobs == []
    assert raw["version"] == 2
    assert raw["jobs"] == []


def test_cron_service_persists_session_key(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60000),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        session_key="web:demo",
        max_runs=3,
    )

    raw = json.loads(store_path.read_text(encoding="utf-8"))

    assert job.payload.session_key == "web:demo"
    assert job.payload.max_runs == 3
    assert job.state.delivered_runs == 0
    assert raw["jobs"][0]["payload"]["sessionKey"] == "web:demo"
    assert raw["jobs"][0]["payload"]["maxRuns"] == 3
    assert raw["jobs"][0]["state"]["deliveredRuns"] == 0


def test_cron_service_defaults_recurring_jobs_to_one_shot_when_max_runs_is_omitted(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60000),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
    )

    assert job.payload.max_runs == 1


def test_cron_service_forces_at_jobs_to_one_shot_even_when_max_runs_is_higher(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 60000),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        max_runs=5,
    )

    assert job.payload.max_runs == 1


def test_cron_service_rejects_non_positive_max_runs(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)

    with pytest.raises(ValueError, match="max_runs must be a positive integer"):
        service.add_job(
            name="demo",
            schedule=CronSchedule(kind="every", every_ms=60000),
            message="hello",
            deliver=True,
            channel="web",
            to="shared",
            max_runs=0,
        )


def test_cron_service_rejects_expired_at_jobs_on_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    monkeypatch.setattr("g3ku.services.cron._format_local_time_ms", lambda _now_ms: "2026-04-22 13:08:16 +08:00")

    with pytest.raises(
        ValueError,
        match="任务定时已过期，当前时间为2026-04-22 13:08:16 \\+08:00，请立即执行或视情况废弃而不要创建过期任务",
    ):
        service.add_job(
            name="demo",
            schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) - 1000),
            message="hello",
            deliver=True,
            channel="web",
            to="shared",
        )

    assert service.list_jobs(include_disabled=True) == []
    assert not store_path.exists()


def test_cron_service_rejects_duplicate_at_job_same_session_same_time(tmp_path: Path) -> None:
    """Re-registering a one-shot reminder for the same session at the exact
    same instant is a duplicate and must be rejected (different message text
    does not make it a new reminder)."""
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    at_ms = int(time.time() * 1000) + 3_600_000

    first = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        message="first wording",
        deliver=True,
        channel="web",
        to="shared",
        session_key="web:ceo-cd3aa88b1ef0",
    )

    with pytest.raises(ValueError, match="已存在一次性提醒"):
        service.add_job(
            name="demo",
            schedule=CronSchedule(kind="at", at_ms=at_ms),
            message="second, reworded message",
            deliver=True,
            channel="web",
            to="shared",
            session_key="web:ceo-cd3aa88b1ef0",
        )

    jobs = service.list_jobs(include_disabled=True)
    assert [j.id for j in jobs] == [first.id]


def test_cron_service_allows_same_time_for_different_sessions(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    at_ms = int(time.time() * 1000) + 3_600_000

    service.add_job(
        name="demo",
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        session_key="web:session-a",
    )
    second = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        session_key="web:session-b",
    )

    assert len(service.list_jobs(include_disabled=True)) == 2
    assert second.payload.session_key == "web:session-b"


def test_cron_service_allows_same_session_at_different_times(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    base_ms = int(time.time() * 1000) + 3_600_000

    service.add_job(
        name="demo",
        schedule=CronSchedule(kind="at", at_ms=base_ms),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        session_key="web:session-a",
    )
    second = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="at", at_ms=base_ms + 60_000),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        session_key="web:session-a",
    )

    assert len(service.list_jobs(include_disabled=True)) == 2
    assert second.schedule.at_ms == base_ms + 60_000


def test_cron_service_conflict_check_ignores_disabled_jobs(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    service = CronService(store_path)
    at_ms = int(time.time() * 1000) + 3_600_000

    first = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        session_key="web:session-a",
    )
    service.enable_job(first.id, enabled=False)

    # disabled job must not block a fresh registration
    second = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="at", at_ms=at_ms),
        message="hello again",
        deliver=True,
        channel="web",
        to="shared",
        session_key="web:session-a",
    )

    enabled_ids = [j.id for j in service.list_jobs(include_disabled=False)]
    assert enabled_ids == [second.id]


@pytest.mark.asyncio
async def test_cron_tool_accepts_asia_shanghai_without_zoneinfo_tzdata(monkeypatch) -> None:
    service = _CronToolService()
    tool = CronTool(service)
    tool.set_context("web", "shared")

    class _MissingZoneInfo:
        def __init__(self, key: str) -> None:
            raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(cron_timezones, "ZoneInfo", _MissingZoneInfo)

    result = await tool.execute(
        action="add",
        message="hello",
        cron_expr="0 10 * * *",
        tz="Asia/Shanghai",
        max_runs=2,
    )

    assert result == "Created job 'job' (id: job-1)"
    assert service.add_calls[-1]["schedule"].tz == "Asia/Shanghai"
    assert service.add_calls[-1]["max_runs"] == 2


@pytest.mark.asyncio
async def test_cron_tool_reports_expired_at_jobs_clearly() -> None:
    class _RejectExpiredCronService(_CronToolService):
        def add_job(self, **kwargs):
            raise ValueError(
                "任务定时已过期，当前时间为2026-04-22 13:08:16 +08:00，请立即执行或视情况废弃而不要创建过期任务"
            )

    service = _RejectExpiredCronService()
    tool = CronTool(service)
    tool.set_context("web", "shared")

    result = await tool.execute(
        action="add",
        message="hello",
        at="2026-04-22T13:07:21+08:00",
    )

    assert (
        result
        == "Error: 任务定时已过期，当前时间为2026-04-22 13:08:16 +08:00，请立即执行或视情况废弃而不要创建过期任务"
    )


def test_resolve_timezone_reports_missing_tzdata_helpfully(monkeypatch) -> None:
    class _MissingZoneInfo:
        def __init__(self, key: str) -> None:
            raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(cron_timezones, "ZoneInfo", _MissingZoneInfo)
    monkeypatch.setattr(cron_timezones, "_tzdata_available", lambda: False)

    with pytest.raises(ValueError, match="tzdata"):
        cron_timezones.resolve_timezone("America/Vancouver")

    fallback = cron_timezones.resolve_timezone("Etc/GMT-8")
    assert fallback.utcoffset(None) == timedelta(hours=8)


@pytest.mark.asyncio
async def test_cron_tool_cron_internal_rounds_allow_add_and_list_but_restrict_remove_to_self() -> None:
    service = _CronToolService()
    tool = CronTool(service)
    tool.set_context("web", "shared")

    add_result = await tool.execute(
        action="add",
        message="hello",
        every_seconds=60,
        __g3ku_runtime={"cron_internal": True, "cron_job_id": "job-1"},
    )
    list_result = await tool.execute(
        action="list",
        __g3ku_runtime={"cron_internal": True, "cron_job_id": "job-1"},
    )
    missing_job_id_remove_result = await tool.execute(
        action="remove",
        __g3ku_runtime={"cron_internal": True, "cron_job_id": "job-1"},
    )
    missing_runtime_job_id_remove_result = await tool.execute(
        action="remove",
        job_id="job-1",
        __g3ku_runtime={"cron_internal": True},
    )
    wrong_remove_result = await tool.execute(
        action="remove",
        job_id="job-2",
        __g3ku_runtime={"cron_internal": True, "cron_job_id": "job-1"},
    )
    remove_result = await tool.execute(
        action="remove",
        job_id="job-1",
        __g3ku_runtime={"cron_internal": True, "cron_job_id": "job-1"},
    )

    assert add_result == "Created job 'job' (id: job-1)"
    assert len(service.add_calls) == 1
    assert service.add_calls[0]["schedule"].kind == "every"
    assert service.add_calls[0]["message"] == "hello"
    assert list_result == "No scheduled jobs."
    assert "only remove the current job_id 'job-1'" in missing_job_id_remove_result
    assert "current job_id is missing" in missing_runtime_job_id_remove_result
    assert "only remove the current job_id 'job-1'" in wrong_remove_result
    assert remove_result == "Removed job job-1"
    assert service.removed == ["job-1"]


@pytest.mark.asyncio
async def test_dispatch_cron_job_resumes_original_session_when_it_exists(tmp_path: Path) -> None:
    session_manager = SessionManager(tmp_path)
    session = session_manager.get_or_create("web:demo")
    session.add_message("user", "hello")
    session_manager.save(session)
    bridge = _BridgeRecorder(output="scheduled")
    job = _make_job(session_key="web:demo")

    result = await dispatch_cron_job(
        job,
        runtime_bridge=bridge,
        session_manager=session_manager,
    )

    assert result == "scheduled"
    assert len(bridge.calls) == 1
    message = bridge.calls[0]["message"]
    assert isinstance(message, UserInputMessage)
    assert message.content == "hello"
    delivered_at_ms = message.metadata.pop("cron_delivered_at_ms")
    assert isinstance(delivered_at_ms, int)
    assert abs(delivered_at_ms - time.time() * 1000) < 60_000
    assert message.metadata == {
        "cron_internal": True,
        "cron_job_id": "job-1",
        "cron_max_runs": 1,
        "cron_delivery_index": 1,
        "cron_delivered_runs": 0,
        "cron_reminder_text": "hello",
        "cron_scheduled_run_at_ms": job.state.next_run_at_ms,
        "cron_last_delivered_at_ms": None,
    }
    assert bridge.calls[0]["session_key"] == "web:demo"
    assert bridge.calls[0]["channel"] == "web"
    assert bridge.calls[0]["chat_id"] == "shared"
    assert bridge.calls[0]["register_task"] is None
    assert resolve_cron_session_key(job, session_manager=session_manager) == "web:demo"


@pytest.mark.asyncio
async def test_dispatch_cron_job_falls_back_to_cron_thread_when_session_missing(tmp_path: Path) -> None:
    session_manager = SessionManager(tmp_path)
    bridge = _BridgeRecorder(output="fallback")
    job = _make_job(
        job_id="job-42",
        session_key="web:missing",
        channel=None,
        to=None,
        max_runs=3,
        delivered_runs=1,
    )

    result = await dispatch_cron_job(
        job,
        runtime_bridge=bridge,
        session_manager=session_manager,
    )

    assert result == "fallback"
    assert len(bridge.calls) == 1
    message = bridge.calls[0]["message"]
    assert isinstance(message, UserInputMessage)
    assert message.content == "hello"
    delivered_at_ms = message.metadata.pop("cron_delivered_at_ms")
    assert isinstance(delivered_at_ms, int)
    assert abs(delivered_at_ms - time.time() * 1000) < 60_000
    assert message.metadata == {
        "cron_internal": True,
        "cron_job_id": "job-42",
        "cron_max_runs": 3,
        "cron_delivery_index": 2,
        "cron_delivered_runs": 1,
        "cron_reminder_text": "hello",
        "cron_scheduled_run_at_ms": job.state.next_run_at_ms,
        "cron_last_delivered_at_ms": None,
    }
    assert bridge.calls[0]["session_key"] == "cron:job-42"
    assert bridge.calls[0]["channel"] == "cli"
    assert bridge.calls[0]["chat_id"] == "direct"
    assert bridge.calls[0]["register_task"] is None
    assert resolve_cron_session_key(job, session_manager=session_manager) == "cron:job-42"


@pytest.mark.asyncio
async def test_dispatch_cron_job_publishes_outbound_when_deliver_true() -> None:
    bridge = _BridgeRecorder(output="杭州今天晴，28℃")
    published: list[OutboundMessage] = []
    job = _make_job(channel="qqbot", to="default:dm:EB6C8D4341C1238A627FF73CBE540DAE")

    result = await dispatch_cron_job(
        job,
        runtime_bridge=bridge,
        publish_outbound=published.append,
    )

    assert result == "杭州今天晴，28℃"
    assert len(published) == 1
    msg = published[0]
    assert msg.channel == "qqbot"
    assert msg.chat_id == "default:dm:EB6C8D4341C1238A627FF73CBE540DAE"
    assert msg.content == "杭州今天晴，28℃"
    assert msg.metadata["source"] == "cron"
    assert msg.metadata["cron_job_id"] == "job-1"
    assert msg.metadata["session_key"] == "cron:job-1"


@pytest.mark.asyncio
async def test_dispatch_cron_job_awaits_async_outbound_publisher() -> None:
    bridge = _BridgeRecorder(output="async hello")
    published: list[OutboundMessage] = []

    async def _publish(msg: OutboundMessage) -> None:
        published.append(msg)

    result = await dispatch_cron_job(
        job=_make_job(channel="qqbot", to="default:dm:USER123"),
        runtime_bridge=bridge,
        publish_outbound=_publish,
    )

    assert result == "async hello"
    assert len(published) == 1
    assert published[0].chat_id == "default:dm:USER123"
    assert published[0].content == "async hello"


@pytest.mark.asyncio
async def test_dispatch_cron_job_skips_outbound_when_deliver_false() -> None:
    bridge = _BridgeRecorder(output="trace only")
    published: list[OutboundMessage] = []

    result = await dispatch_cron_job(
        job=_make_job(channel="qqbot", to="default:dm:USER123", deliver=False),
        runtime_bridge=bridge,
        publish_outbound=published.append,
    )

    assert result == "trace only"
    assert published == []


@pytest.mark.asyncio
async def test_dispatch_cron_job_skips_outbound_for_empty_output() -> None:
    bridge = _BridgeRecorder(output="")
    published: list[OutboundMessage] = []

    result = await dispatch_cron_job(
        job=_make_job(channel="qqbot", to="default:dm:USER123"),
        runtime_bridge=bridge,
        publish_outbound=published.append,
    )

    assert result == ""
    assert published == []


@pytest.mark.asyncio
async def test_dispatch_cron_job_skips_outbound_for_internal_direct_target() -> None:
    bridge = _BridgeRecorder(output="internal done")
    published: list[OutboundMessage] = []

    result = await dispatch_cron_job(
        job=_make_job(channel=None, to=None),
        runtime_bridge=bridge,
        publish_outbound=published.append,
    )

    assert result == "internal done"
    assert published == []


@pytest.mark.asyncio
async def test_dispatch_cron_job_swallows_outbound_publish_failure() -> None:
    bridge = _BridgeRecorder(output="deliver anyway")

    def _boom(_msg: OutboundMessage) -> None:
        raise RuntimeError("bus down")

    result = await dispatch_cron_job(
        job=_make_job(channel="qqbot", to="default:dm:USER123"),
        runtime_bridge=bridge,
        publish_outbound=_boom,
    )

    assert result == "deliver anyway"


@pytest.mark.asyncio
async def test_web_cron_on_job_wires_bus_outbound_publisher(monkeypatch) -> None:
    calls: dict[str, object] = {}

    async def _fake_dispatch(job, **kwargs):
        calls["job"] = job
        calls["publish_outbound"] = kwargs.get("publish_outbound")
        return "ok"

    class _Bus:
        def __init__(self) -> None:
            self.outbound: list[object] = []

        def publish_outbound(self, msg) -> str:
            self.outbound.append(msg)
            return str(msg)

    class _FakeSessionRuntimeBridge:
        def __init__(self, *args, **kwargs) -> None:
            calls["bridge"] = (args, kwargs)

    bus = _Bus()
    monkeypatch.setattr(web_shell, "dispatch_cron_job", _fake_dispatch)
    monkeypatch.setattr(web_shell, "SessionRuntimeBridge", _FakeSessionRuntimeBridge)
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda agent: ("manager", agent))
    monkeypatch.setattr(web_shell, "_global_bus", bus)

    agent = SimpleNamespace(sessions=object(), _register_active_task=lambda *a, **k: None)
    service = web_shell._build_web_cron_service({"agent": agent})
    job = _make_job(channel="qqbot", to="default:dm:USER123")

    result = await service.on_job(job)

    assert result == "ok"
    assert calls["job"] is job
    publisher = calls["publish_outbound"]
    assert callable(publisher)
    # publisher must be bound to the live bus instance, not None
    assert getattr(publisher, "__self__", None) is bus


@pytest.mark.asyncio
async def test_cron_service_start_catches_up_overdue_at_job_once(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    store_path.write_text(
        json.dumps(
            {
                "version": 2,
                "jobs": [
                    {
                        "id": "job-at",
                        "name": "at",
                        "enabled": True,
                        "schedule": {"kind": "at", "atMs": now_ms - 30_000},
                        "payload": {"kind": "agent_turn", "message": "run once", "deliver": True},
                        "state": {"nextRunAtMs": now_ms - 30_000},
                        "createdAtMs": now_ms - 60_000,
                        "updatedAtMs": now_ms - 60_000,
                        "deleteAfterRun": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fired: list[str] = []

    async def _on_job(job: CronJob) -> str | None:
        fired.append(job.id)
        return "ok"

    service = CronService(store_path, on_job=_on_job)

    await service.start()
    await _wait_until(
        lambda: fired == ["job-at"] and service.list_jobs(include_disabled=True) == []
    )
    service.stop()

    jobs = service.list_jobs(include_disabled=True)
    assert fired == ["job-at"]
    assert jobs == []


@pytest.mark.asyncio
async def test_cron_service_start_catches_up_only_latest_recurring_run(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    store_path.write_text(
        json.dumps(
            {
                "version": 2,
                "jobs": [
                    {
                        "id": "job-every",
                        "name": "every",
                        "enabled": True,
                        "schedule": {"kind": "every", "everyMs": 3_600_000},
                        "payload": {"kind": "agent_turn", "message": "run every", "deliver": True},
                        "state": {"nextRunAtMs": now_ms - 7_200_000},
                        "createdAtMs": now_ms - 7_200_000,
                        "updatedAtMs": now_ms - 7_200_000,
                        "deleteAfterRun": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fired: list[str] = []

    async def _on_job(job: CronJob) -> str | None:
        fired.append(job.id)
        return "ok"

    service = CronService(store_path, on_job=_on_job)

    await service.start()
    await _wait_until(
        lambda: fired == ["job-every"] and service.list_jobs(include_disabled=True) == []
    )
    service.stop()

    jobs = service.list_jobs(include_disabled=True)
    assert fired == ["job-every"]
    assert jobs == []


@pytest.mark.asyncio
async def test_cron_service_counts_successful_deliveries_and_deletes_at_max_runs(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    delivered: list[str] = []

    async def _on_job(job: CronJob) -> str | None:
        delivered.append(job.id)
        return "ok"

    service = CronService(store_path, on_job=_on_job)
    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        max_runs=3,
    )

    await service._execute_job(job)
    assert job.state.delivered_runs == 1
    assert service.list_jobs(include_disabled=True)[0].id == job.id

    await service._execute_job(job)
    assert job.state.delivered_runs == 2
    assert service.list_jobs(include_disabled=True)[0].id == job.id

    await service._execute_job(job)

    assert delivered == [job.id, job.id, job.id]
    assert service.list_jobs(include_disabled=True) == []


@pytest.mark.asyncio
async def test_cron_service_persists_claim_before_dispatch(tmp_path: Path) -> None:
    """The run claim (last_run_at_ms + status=running) must be on disk before
    the handler runs, so a crash mid-dispatch cannot re-arm the job."""
    store_path = tmp_path / "jobs.json"
    captured: dict[str, object] = {}

    async def _on_job(job: CronJob) -> str | None:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        captured["claim"] = raw["jobs"][0]["state"]
        return "ok"

    service = CronService(store_path, on_job=_on_job)
    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        max_runs=3,
    )

    await service._execute_job(job)

    assert captured["claim"]["lastStatus"] == "running"
    assert captured["claim"]["lastRunAtMs"] is not None
    assert captured["claim"]["deliveredRuns"] == 0
    # finalized after dispatch
    assert job.state.last_status == "ok"
    assert job.state.delivered_runs == 1


@pytest.mark.asyncio
async def test_cron_service_suppresses_interrupted_at_job_on_restart(tmp_path: Path) -> None:
    """A one-shot job whose claim was persisted but dispatch never finalized
    (process restarted mid-flight) must NOT be re-dispatched on startup."""
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    store_path.write_text(
        json.dumps(
            {
                "version": 2,
                "jobs": [
                    {
                        "id": "job-int",
                        "name": "interrupted",
                        "enabled": True,
                        "schedule": {"kind": "at", "atMs": now_ms - 30_000},
                        "payload": {"kind": "agent_turn", "message": "run once", "deliver": True},
                        "state": {
                            "nextRunAtMs": now_ms - 30_000,
                            "lastRunAtMs": now_ms - 30_000,
                            "deliveredRuns": 0,
                            "lastStatus": "running",
                        },
                        "createdAtMs": now_ms - 60_000,
                        "updatedAtMs": now_ms - 30_000,
                        "deleteAfterRun": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    fired: list[str] = []

    async def _on_job(job: CronJob) -> str | None:
        fired.append(job.id)
        return "ok"

    service = CronService(store_path, on_job=_on_job)

    await service.start()
    await asyncio.sleep(0.05)
    service.stop()

    jobs = service.list_jobs(include_disabled=True)
    assert fired == []
    assert len(jobs) == 1
    assert jobs[0].state.last_status == "interrupted"
    assert jobs[0].state.next_run_at_ms is None
    assert jobs[0].enabled is False


@pytest.mark.asyncio
async def test_cron_service_in_flight_guard_blocks_concurrent_dispatch(tmp_path: Path) -> None:
    """A second _execute_job for a job already dispatching must be skipped."""
    store_path = tmp_path / "jobs.json"
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _on_job(job: CronJob) -> str | None:
        calls.append(job.id)
        entered.set()
        await release.wait()
        return "ok"

    service = CronService(store_path, on_job=_on_job)
    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        max_runs=3,
    )

    first = asyncio.create_task(service._execute_job(job))
    await entered.wait()  # first call is now inside the handler (in-flight)
    await service._execute_job(job)  # must be rejected by the guard
    release.set()
    await first

    assert calls == [job.id]
    assert job.state.delivered_runs == 1


@pytest.mark.asyncio
async def test_cron_service_does_not_increment_failed_deliveries(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"

    async def _on_job(job: CronJob) -> str | None:
        _ = job
        raise RuntimeError("delivery failed")

    service = CronService(store_path, on_job=_on_job)
    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="hello",
        deliver=True,
        channel="web",
        to="shared",
        max_runs=2,
    )

    await service._execute_job(job)

    assert job.state.delivered_runs == 0
    assert job.state.last_status == "error"
    assert service.list_jobs(include_disabled=True)[0].id == job.id


@pytest.mark.asyncio
async def test_ensure_web_runtime_services_starts_cron_once_for_owner(monkeypatch) -> None:
    service = _MainService()
    heartbeat = _Heartbeat()
    cron_service = _CronRecorder()
    worker_waits: list[float] = []

    async def _ensure_worker(_service, *, wait_timeout_s: float = 5.0):
        _ = _service
        worker_waits.append(wait_timeout_s)
        return False

    async def _start_heartbeat(_agent, _runtime_manager, **kwargs):
        _ = _agent, _runtime_manager, kwargs
        await heartbeat.start()
        return heartbeat

    monkeypatch.setattr(web_shell, "_global_runtime_services_lock", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", None)
    monkeypatch.setattr(web_shell, "ensure_managed_task_worker", _ensure_worker)
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)
    monkeypatch.setattr(web_shell, "_should_start_web_cron", lambda _agent=None: True)

    agent = SimpleNamespace(main_task_service=service, cron_service=cron_service)

    await web_shell.ensure_web_runtime_services(agent)
    await web_shell.ensure_web_runtime_services(agent)

    assert worker_waits == [1.0]
    assert service._started is True
    assert heartbeat.start_calls == 1
    assert cron_service.start_calls == 1


@pytest.mark.asyncio
async def test_ensure_web_runtime_services_skips_cron_when_not_owner(monkeypatch) -> None:
    service = _MainService()
    heartbeat = _Heartbeat()
    cron_service = _CronRecorder()

    async def _start_heartbeat(_agent, _runtime_manager, **kwargs):
        _ = _agent, _runtime_manager, kwargs
        await heartbeat.start()
        return heartbeat

    async def _ensure_worker(_service, *, wait_timeout_s: float = 5.0):
        _ = _service, wait_timeout_s
        return False

    monkeypatch.setattr(web_shell, "_global_runtime_services_lock", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", None)
    monkeypatch.setattr(web_shell, "ensure_managed_task_worker", _ensure_worker)
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)
    monkeypatch.setattr(web_shell, "_should_start_web_cron", lambda _agent=None: False)

    agent = SimpleNamespace(main_task_service=service, cron_service=cron_service)

    await web_shell.ensure_web_runtime_services(agent)

    assert heartbeat.start_calls == 1
    assert cron_service.start_calls == 0


@pytest.mark.asyncio
async def test_shutdown_web_runtime_stops_cron(monkeypatch) -> None:
    cron_service = _CronRecorder()
    heartbeat = _Heartbeat()

    async def _noop_shutdown_worker() -> None:
        return None

    async def _cancel_session_tasks(_session_key: str) -> int:
        return 0

    agent = SimpleNamespace(
        cron_service=cron_service,
        _active_tasks={},
        main_task_service=None,
        background_pool=None,
        cancel_session_tasks=_cancel_session_tasks,
        close_mcp=lambda: asyncio.sleep(0),
    )

    monkeypatch.setattr(web_shell, "shutdown_managed_task_worker", _noop_shutdown_worker)
    monkeypatch.setattr(web_shell, "_global_agent", agent)
    monkeypatch.setattr(web_shell, "_global_bus", object())
    monkeypatch.setattr(web_shell, "_global_runtime_manager", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", heartbeat)
    monkeypatch.setattr(web_shell, "_global_outbound_drain_task", None)

    await web_shell.shutdown_web_runtime()

    assert cron_service.stop_calls == 1
    assert heartbeat.stop_calls == 1


def test_get_agent_injects_web_cron_service(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _Security:
        @staticmethod
        def is_unlocked() -> bool:
            return True

    class _FakeAgentLoop:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)
            self.__dict__.update(kwargs)
            self.main_task_service = None
            self.sessions = object()

    def _fake_make_agent_loop(_config, _bus, _provider, **kwargs):
        return _FakeAgentLoop(
            bus=_bus,
            provider=_provider,
            app_config=_config,
            **kwargs,
        )

    config = SimpleNamespace(
        workspace_path=tmp_path,
        web=SimpleNamespace(port=18790),
        resources=SimpleNamespace(),
        agents=SimpleNamespace(
            defaults=SimpleNamespace(
                temperature=0.1,
                max_tokens=1024,
                memory_window=20,
                reasoning_effort=None,
                middlewares=[],
            ),
            multi_agent=SimpleNamespace(),
        ),
        get_role_model_target=lambda _role: ("provider", "model"),
        get_role_max_iterations=lambda _role: 8,
        resolve_role_model_key=lambda _role: "ceo",
    )

    monkeypatch.setattr(web_shell, "_global_agent", None)
    monkeypatch.setattr(web_shell, "_global_bus", None)
    monkeypatch.setattr(web_shell, "_global_runtime_manager", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", None)
    monkeypatch.setattr(web_shell, "_make_provider", lambda _config, scope="ceo": (scope, _config))
    monkeypatch.setattr(web_shell, "_make_agent_loop", _fake_make_agent_loop)
    monkeypatch.setattr(web_shell, "get_bootstrap_security_service", lambda: _Security())
    monkeypatch.setattr(web_shell, "get_runtime_config", lambda force=True: (config, "rev-1", False))
    monkeypatch.setattr(web_shell, "debug_trace_enabled", lambda: False)
    monkeypatch.setenv("G3KU_INTERNAL_CALLBACK_URL", "http://127.0.0.1:18790/api/internal/task-terminal")

    agent = web_shell.get_agent()

    assert agent is web_shell._global_agent
    assert isinstance(captured["cron_service"], CronService)
    assert captured["cron_service"].store_path == get_data_dir() / "cron" / "jobs.json"


# =============================================================================
# 调度器抗挂起回归（2026-09 生产停摆事故）：
# 一次 dispatch 挂起（session.prompt 在回合 finalize 后不返回）曾把整个定时器
# 任务钉死——jobs.json 永久停在 Phase-1 claim（lastStatus="running"）、所有 job
# 停摆、零日志，直到 remove_job 取消卡死的 timer task 才复活。以下测试锁定新
# 合同：独立 dispatch 任务 + 投递级看门狗 + 任意退出路径都落盘收尾 + 定时器永
# 不静默死亡。
# =============================================================================


def _store_state_by_id(store_path: Path) -> dict[str, dict]:
    raw = json.loads(store_path.read_text(encoding="utf-8"))
    return {str(j["id"]): j for j in raw["jobs"]}


async def _wait_until(
    predicate, *, timeout: float = 3.0, interval: float = 0.01
) -> None:
    """有界条件等待：dispatch 是独立任务，固定短 sleep 会被首次 live-config
    解析等一次性开销打穿，收敛断言必须等条件而不是等时长。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.mark.asyncio
async def test_cron_watchdog_timeout_finalizes_store_and_other_jobs_keep_running(
    tmp_path: Path,
) -> None:
    """A wedged dispatch must not pin the tick: the watchdog cancels it, the
    claim is finalized as "timeout" with the schedule advanced, an unrelated
    due job still completes in the same tick, and the timer stays armed."""
    store_path = tmp_path / "jobs.json"
    hang_entered = asyncio.Event()
    hang_cancelled = asyncio.Event()
    ok_calls: list[str] = []

    async def _on_job(job: CronJob) -> str | None:
        if job.name == "hang":
            hang_entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                hang_cancelled.set()
                raise
            return "never"
        ok_calls.append(job.id)
        return "ok"

    service = CronService(
        store_path,
        on_job=_on_job,
        dispatch_timeout_s=0.05,
        dispatch_cancel_grace_s=0.5,
    )
    job_hang = service.add_job(
        name="hang",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="h",
        max_runs=10,
    )
    job_ok = service.add_job(
        name="ok",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="o",
        max_runs=10,
    )
    now_ms = int(time.time() * 1000)
    job_hang.state.next_run_at_ms = now_ms - 1
    job_ok.state.next_run_at_ms = now_ms - 1
    service._running = True

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        await service._on_timer()
        elapsed = loop.time() - started
        # 旧实现会在这里 await 挂死的 dispatch（30s+）；新实现立即返回。
        assert elapsed < 1.0

        await asyncio.wait_for(hang_entered.wait(), timeout=2)
        await asyncio.sleep(0.01)
        # 无关 job 不受牵连，同一 tick 内正常完成
        assert ok_calls == [job_ok.id]

        await asyncio.wait_for(hang_cancelled.wait(), timeout=2)
        await asyncio.sleep(0.05)
        # 调度器仍然存活：finalize 已重新武装定时器（stop 会清空，先取证）
        timer_alive = service._timer_task is not None and not service._timer_task.done()
        hang_released = job_hang.id not in service._in_flight
    finally:
        logger.remove(sink_id)
        service.stop()

    jobs = _store_state_by_id(store_path)
    hang_state = jobs[job_hang.id]["state"]
    assert hang_state["lastStatus"] == "timeout"
    assert "watchdog timeout" in str(hang_state["lastError"])
    assert hang_state["deliveredRuns"] == 0
    # 周期任务的调度必须推进，而不是冻结在 claim 时刻
    assert hang_state["nextRunAtMs"] is not None
    assert int(hang_state["nextRunAtMs"]) > now_ms
    ok_state = jobs[job_ok.id]["state"]
    assert ok_state["lastStatus"] == "ok"
    assert ok_state["deliveredRuns"] == 1
    # store 里绝不允许残留 running
    assert all(j["state"]["lastStatus"] != "running" for j in jobs.values())
    # 看门狗必须留下可 grep 的取证日志（含挂起任务的 await 链）
    text = "\n".join(str(item) for item in messages)
    assert "dispatch watchdog timeout" in text
    assert "await chain" in text
    assert "_on_job" in text
    assert timer_alive
    assert hang_released


@pytest.mark.asyncio
async def test_cron_watchdog_abandons_cancel_resistant_dispatch(tmp_path: Path) -> None:
    """A dispatch that ignores cancellation is finalized as timeout (with an
    "abandoned" marker), stays in-flight so later ticks cannot stack a second
    dispatch on the wedged session, and is released when it finally ends."""
    store_path = tmp_path / "jobs.json"
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def _resist(job: CronJob) -> str | None:
        calls.append(job.id)
        entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # 抗取消：吞掉第一次取消，直到测试放行才真正退出
            await release.wait()
            raise
        return "never"

    service = CronService(
        store_path,
        on_job=_resist,
        dispatch_timeout_s=0.05,
        dispatch_cancel_grace_s=0.05,
    )
    job = service.add_job(
        name="resist",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="r",
        max_runs=10,
    )
    job.state.next_run_at_ms = int(time.time() * 1000) - 1
    service._running = True

    try:
        await service._on_timer()
        await asyncio.wait_for(entered.wait(), timeout=2)
        # 看门狗 0.05s 触发 + 0.05s 宽限 → 0.3s 内必然已放弃并收尾
        await asyncio.sleep(0.3)

        state = _store_state_by_id(store_path)[job.id]["state"]
        assert state["lastStatus"] == "timeout"
        assert "abandoned" in str(state["lastError"])
        # 僵尸任务存活期间 job 保持 in-flight
        assert job.id in service._in_flight

        # 后续 tick 不得在同一 job 上叠加第二次 dispatch
        job.state.next_run_at_ms = int(time.time() * 1000) - 1
        await service._on_timer()
        await asyncio.sleep(0.05)
        assert calls == [job.id]
    finally:
        # 先 stop（置 _running=False，阻止释放回调再武装 0 延迟 tick），再放行僵尸任务
        service.stop()
        release.set()
        await asyncio.sleep(0.05)

    assert job.id not in service._in_flight
    assert calls == [job.id]


@pytest.mark.asyncio
async def test_cron_dispatch_cancellation_persists_interrupted(tmp_path: Path) -> None:
    """Cancelling a claimed dispatch (CancelledError is a BaseException that
    used to slip past `except Exception` straight into a bare in-flight
    discard) must still persist an identifiable finalize and cancel the inner
    handler task."""
    store_path = tmp_path / "jobs.json"
    entered = asyncio.Event()
    inner_cancelled = asyncio.Event()

    async def _slow(job: CronJob) -> str | None:
        entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            inner_cancelled.set()
            raise
        return "ok"

    service = CronService(store_path, on_job=_slow, dispatch_timeout_s=30)
    job = service.add_job(
        name="slow",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="s",
        max_runs=10,
    )

    task = asyncio.create_task(service._execute_job(job))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(inner_cancelled.wait(), timeout=2)
    await asyncio.sleep(0.01)

    state = _store_state_by_id(store_path)[job.id]["state"]
    assert state["lastStatus"] == "interrupted"
    assert str(state["lastError"]).strip() != ""
    assert state["deliveredRuns"] == 0
    assert int(state["nextRunAtMs"]) > int(time.time() * 1000)
    assert job.id not in service._in_flight


@pytest.mark.asyncio
async def test_cron_stop_cancels_inflight_dispatch_and_persists_interrupted(
    tmp_path: Path,
) -> None:
    """Service stop must cancel independent dispatch tasks spawned by the
    timer, and their finalize must persist "interrupted" on the way out."""
    store_path = tmp_path / "jobs.json"
    entered = asyncio.Event()

    async def _hang(job: CronJob) -> str | None:
        entered.set()
        await asyncio.sleep(30)
        return "never"

    service = CronService(store_path, on_job=_hang, dispatch_timeout_s=60)
    job = service.add_job(
        name="hang",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="h",
        max_runs=10,
    )
    job.state.next_run_at_ms = int(time.time() * 1000) - 1
    service._running = True

    await service._on_timer()
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert len(service._dispatch_tasks) == 1

    service.stop()
    await asyncio.sleep(0.05)

    state = _store_state_by_id(store_path)[job.id]["state"]
    assert state["lastStatus"] == "interrupted"
    assert int(state["nextRunAtMs"]) > int(time.time() * 1000)
    assert job.id not in service._in_flight
    assert all(task.cancelled() or task.done() for task in service._dispatch_tasks)


@pytest.mark.asyncio
async def test_cron_watchdog_suppresses_one_shot_at_job(tmp_path: Path) -> None:
    """At-most-once also applies to watchdog kills: an unfinished one-shot
    dispatch may already have reached the downstream handler, so the job is
    suppressed (next_run cleared + disabled) instead of retried."""
    store_path = tmp_path / "jobs.json"

    async def _hang(job: CronJob) -> str | None:
        await asyncio.sleep(30)
        return "never"

    service = CronService(
        store_path,
        on_job=_hang,
        dispatch_timeout_s=0.05,
        dispatch_cancel_grace_s=0.2,
    )
    job = service.add_job(
        name="once",
        schedule=CronSchedule(kind="at", at_ms=int(time.time() * 1000) + 3_600_000),
        message="m",
        deliver=True,
        channel="web",
        to="shared",
    )
    job.state.next_run_at_ms = int(time.time() * 1000) - 1

    await service._execute_job(job)

    entry = _store_state_by_id(store_path)[job.id]
    assert entry["state"]["lastStatus"] == "timeout"
    assert entry["state"]["nextRunAtMs"] is None
    assert entry["state"]["deliveredRuns"] == 0
    assert entry["enabled"] is False


@pytest.mark.asyncio
async def test_cron_finalize_reattaches_after_external_store_reload(tmp_path: Path) -> None:
    """外部进程（如 CLI）在派发期间改写 jobs.json 会触发本进程 store 重载，
    原 job 对象脱钩；finalize 必须写回当前 store 里的同 id 副本，否则收尾
    状态只落在临时对象上，磁盘永久停在 running。"""
    store_path = tmp_path / "jobs.json"
    service_ref: dict[str, CronService] = {}

    async def _on_job(job: CronJob) -> str | None:
        raw = json.loads(store_path.read_text(encoding="utf-8"))
        raw["jobs"][0]["name"] = "renamed-externally"
        store_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        # _load_store 侦测外部 mtime 变化 → 重载，store 内 job 对象被替换
        service_ref["service"].list_jobs()
        return "ok"

    service = CronService(store_path, on_job=_on_job)
    service_ref["service"] = service
    job = service.add_job(
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="m",
        max_runs=5,
    )

    await service._execute_job(job)

    entry = _store_state_by_id(store_path)[job.id]
    assert entry["name"] == "renamed-externally"
    assert entry["state"]["lastStatus"] == "ok"
    assert entry["state"]["deliveredRuns"] == 1
    # 脱钩的旧对象不再承载权威状态
    assert job.state.delivered_runs == 0


def test_cron_dispatch_timeout_resolves_from_live_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constructor arg wins; otherwise the live runtime config `cron` section
    is consulted; a broken/missing config falls back to module defaults."""
    import g3ku.config.live_runtime as live_runtime

    fake_config = SimpleNamespace(
        cron=SimpleNamespace(
            dispatch_timeout_seconds=42.0,
            dispatch_cancel_grace_seconds=1.5,
        )
    )
    monkeypatch.setattr(
        live_runtime, "get_runtime_config", lambda force=False: (fake_config, "rev", False)
    )

    service = CronService(tmp_path / "jobs.json")
    assert service._resolve_dispatch_timeout_s() == 42.0
    assert service._resolve_dispatch_cancel_grace_s() == 1.5

    explicit = CronService(
        tmp_path / "jobs.json", dispatch_timeout_s=7.0, dispatch_cancel_grace_s=2.0
    )
    assert explicit._resolve_dispatch_timeout_s() == 7.0
    assert explicit._resolve_dispatch_cancel_grace_s() == 2.0

    def _boom(force: bool = False):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(live_runtime, "get_runtime_config", _boom)
    fallback = CronService(tmp_path / "jobs.json")
    assert fallback._resolve_dispatch_timeout_s() == DEFAULT_DISPATCH_TIMEOUT_SECONDS
    assert fallback._resolve_dispatch_cancel_grace_s() == DEFAULT_DISPATCH_CANCEL_GRACE_SECONDS
    assert DEFAULT_DISPATCH_TIMEOUT_SECONDS > 0


@pytest.mark.asyncio
async def test_cron_timer_task_death_triggers_self_heal_rearm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scheduler heartbeat must never die silently: an unexpected tick
    task exception logs an error and re-arms after a short heal delay. A
    cancelled tick (normal re-arm churn) must not trigger the heal."""
    store_path = tmp_path / "jobs.json"

    async def _ok(job: CronJob) -> str | None:
        return "ok"

    service = CronService(store_path, on_job=_ok)
    service.add_job(
        name="j",
        schedule=CronSchedule(kind="every", every_ms=60_000),
        message="m",
    )
    service._running = True
    service._load_store()
    service._arm_timer()
    original = service._timer_task
    assert original is not None
    monkeypatch.setattr("g3ku.services.cron._TIMER_SELF_HEAL_DELAY_SECONDS", 0.01)

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="ERROR")
    try:
        class _CancelledTick:
            def cancelled(self) -> bool:
                return True

            def exception(self):  # pragma: no cover - must not be called
                raise AssertionError("cancelled ticks must not be inspected")

        service._on_timer_task_done(_CancelledTick())
        assert service._heal_task is None

        class _DeadTick:
            def cancelled(self) -> bool:
                return False

            def exception(self):
                return OSError("tick body exploded past its finally")

        service._on_timer_task_done(_DeadTick())
        await asyncio.sleep(0.1)
        # stop() 会清空 _timer_task，先取证自愈结果
        healed = service._timer_task
        heal_spawned = service._heal_task is not None
    finally:
        logger.remove(sink_id)
        service.stop()

    assert healed is not None
    assert healed is not original
    assert original.done()
    assert heal_spawned
    text = "\n".join(str(item) for item in messages)
    assert "timer task died unexpectedly" in text


@pytest.mark.asyncio
async def test_bridge_slow_prompt_watchdog_dumps_await_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bridge.prompt's watchdog must warn with the LIVE await chain of the
    still-suspended session task — the forensic evidence that was missing in
    the 2026-09 incident."""
    from g3ku.runtime import bridge as bridge_mod

    monkeypatch.setenv("G3KU_SLOW_PROMPT_WATCHDOG_SECONDS", "0.05")
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    victim: asyncio.Task | None = None
    watchdog: asyncio.Task | None = None
    try:
        async def _hang_forever() -> None:
            await asyncio.sleep(30)

        victim = asyncio.create_task(_hang_forever())
        watchdog = bridge_mod._start_slow_prompt_watchdog(
            victim, label="bridge.prompt session_key=ext:test:1"
        )
        await asyncio.sleep(0.2)
    finally:
        logger.remove(sink_id)
        for task in (victim, watchdog):
            if task is not None:
                task.cancel()
        await asyncio.gather(victim, watchdog, return_exceptions=True)

    text = "\n".join(str(item) for item in messages)
    assert "session prompt still awaiting" in text
    assert "bridge.prompt session_key=ext:test:1" in text
    assert "_hang_forever" in text  # 挂起帧出现在 dump 里


def test_bridge_slow_prompt_watchdog_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("G3KU_SLOW_PROMPT_WATCHDOG_SECONDS", "0")
    from g3ku.runtime import bridge as bridge_mod

    assert bridge_mod._slow_prompt_watchdog_seconds() == 0.0
    monkeypatch.setenv("G3KU_SLOW_PROMPT_WATCHDOG_SECONDS", "not-a-number")
    assert bridge_mod._slow_prompt_watchdog_seconds() == bridge_mod.DEFAULT_SLOW_PROMPT_WATCHDOG_SECONDS


def test_turn_tail_profiler_warns_with_stage_breakdown() -> None:
    """The finalize-tail profiler names the slow stage (transcript / memory
    review / terminal emits) so a slow-but-completed tail is attributable."""
    from g3ku.runtime.session_agent import _TurnTailProfiler

    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING")
    try:
        profiler = _TurnTailProfiler(session_key="web:test", threshold_s=0.0)
        profiler.mark("persist_transcript")
        profiler.mark("record_turn_for_review")
        profiler.mark("emit_message_end")
        profiler.warn_if_slow()

        quiet = _TurnTailProfiler(session_key="web:test", threshold_s=60.0)
        quiet.mark("persist_transcript")
        quiet.warn_if_slow()
    finally:
        logger.remove(sink_id)

    text = "\n".join(str(item) for item in messages)
    assert "turn finalize tail slow for session web:test" in text
    assert "persist_transcript=" in text
    assert "record_turn_for_review=" in text
    assert "emit_message_end=" in text
    # 未超阈值时不得告警（只出现一条 warning）
    assert text.count("turn finalize tail slow") == 1


@pytest.mark.asyncio
async def test_format_task_await_chain_renders_stuck_frames() -> None:
    entered = asyncio.Event()

    async def _inner() -> None:
        await asyncio.sleep(30)

    async def _outer() -> None:
        entered.set()
        await _inner()

    task = asyncio.create_task(_outer())
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        await asyncio.sleep(0)
        chain = format_task_await_chain(task)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert "_outer" in chain
    assert "_inner" in chain
    assert "task=" in chain


@pytest.mark.asyncio
async def test_format_task_await_chain_crosses_awaited_task_boundary() -> None:
    """bridge.prompt awaits the session-prompt task it created; ``await
    some_task`` parks on an opaque FutureIter, so the walker must cross via
    the suspended frame's locals to reach the truly stuck frame."""
    session_started = asyncio.Event()
    inner_ref: list[asyncio.Task] = []

    async def _session_prompt() -> None:
        session_started.set()
        await asyncio.sleep(30)

    async def _bridge_prompt() -> None:
        task = asyncio.create_task(_session_prompt(), name="session-prompt")
        inner_ref.append(task)
        await task

    outer = asyncio.create_task(_bridge_prompt(), name="bridge-prompt")
    try:
        await asyncio.wait_for(session_started.wait(), timeout=2)
        await asyncio.sleep(0)
        chain = format_task_await_chain(outer)
    finally:
        outer.cancel()
        for task in inner_ref:
            task.cancel()
        await asyncio.gather(outer, *inner_ref, return_exceptions=True)

    assert "_bridge_prompt" in chain
    assert "session-prompt" in chain  # 跨界进入了被 await 的任务
    assert "_session_prompt" in chain  # 最深挂起帧可见
