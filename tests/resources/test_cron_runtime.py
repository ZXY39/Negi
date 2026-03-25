from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import g3ku.shells.web as web_shell
from g3ku.config.loader import get_data_dir
from g3ku.cron.runtime_dispatch import dispatch_cron_job, resolve_cron_session_key
from g3ku.cron.service import CronService
from g3ku.cron.types import CronJob, CronJobState, CronPayload, CronSchedule
from g3ku.session.manager import SessionManager


class _BridgeRecorder:
    def __init__(self, output: str = "done") -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    async def prompt(self, message: str, **kwargs):
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


def _make_job(
    *,
    job_id: str = "job-1",
    message: str = "hello",
    channel: str | None = "web",
    to: str | None = "shared",
    session_key: str | None = None,
) -> CronJob:
    return CronJob(
        id=job_id,
        name="demo",
        schedule=CronSchedule(kind="every", every_ms=60000),
        payload=CronPayload(
            kind="agent_turn",
            message=message,
            deliver=True,
            channel=channel,
            to=to,
            session_key=session_key,
        ),
        state=CronJobState(next_run_at_ms=int(time.time() * 1000) + 60000),
    )


def test_cron_service_loads_legacy_jobs_without_session_key(tmp_path: Path) -> None:
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

    assert len(jobs) == 1
    assert jobs[0].payload.session_key is None


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
    )

    raw = json.loads(store_path.read_text(encoding="utf-8"))

    assert job.payload.session_key == "web:demo"
    assert raw["jobs"][0]["payload"]["sessionKey"] == "web:demo"


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
    assert bridge.calls == [
        {
            "message": "hello",
            "session_key": "web:demo",
            "channel": "web",
            "chat_id": "shared",
            "register_task": None,
        }
    ]
    assert resolve_cron_session_key(job, session_manager=session_manager) == "web:demo"


@pytest.mark.asyncio
async def test_dispatch_cron_job_falls_back_to_cron_thread_when_session_missing(tmp_path: Path) -> None:
    session_manager = SessionManager(tmp_path)
    bridge = _BridgeRecorder(output="fallback")
    job = _make_job(job_id="job-42", session_key="web:missing", channel=None, to=None)

    result = await dispatch_cron_job(
        job,
        runtime_bridge=bridge,
        session_manager=session_manager,
    )

    assert result == "fallback"
    assert bridge.calls == [
        {
            "message": "hello",
            "session_key": "cron:job-42",
            "channel": "cli",
            "chat_id": "direct",
            "register_task": None,
        }
    ]
    assert resolve_cron_session_key(job, session_manager=session_manager) == "cron:job-42"


@pytest.mark.asyncio
async def test_cron_service_start_catches_up_overdue_at_job_once(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
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
    await asyncio.sleep(0.05)
    service.stop()

    jobs = service.list_jobs(include_disabled=True)
    assert fired == ["job-at"]
    assert jobs[0].enabled is False
    assert jobs[0].state.last_status == "ok"
    assert jobs[0].state.next_run_at_ms is None


@pytest.mark.asyncio
async def test_cron_service_start_catches_up_only_latest_recurring_run(tmp_path: Path) -> None:
    store_path = tmp_path / "jobs.json"
    now_ms = int(time.time() * 1000)
    store_path.write_text(
        json.dumps(
            {
                "version": 1,
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
    await asyncio.sleep(0.05)
    service.stop()

    jobs = service.list_jobs(include_disabled=True)
    assert fired == ["job-every"]
    assert jobs[0].enabled is True
    assert jobs[0].state.last_status == "ok"
    assert (jobs[0].state.next_run_at_ms or 0) > int(time.time() * 1000)


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

    async def _skip_china(_agent=None) -> None:
        return None

    monkeypatch.setattr(web_shell, "_global_runtime_services_lock", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", None)
    monkeypatch.setattr(web_shell, "ensure_managed_task_worker", _ensure_worker)
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)
    monkeypatch.setattr(web_shell, "_ensure_china_bridge_services", _skip_china)
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

    async def _skip_china(_agent=None) -> None:
        return None

    async def _ensure_worker(_service, *, wait_timeout_s: float = 5.0):
        _ = _service, wait_timeout_s
        return False

    monkeypatch.setattr(web_shell, "_global_runtime_services_lock", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", None)
    monkeypatch.setattr(web_shell, "ensure_managed_task_worker", _ensure_worker)
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)
    monkeypatch.setattr(web_shell, "_ensure_china_bridge_services", _skip_china)
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
    monkeypatch.setattr(web_shell, "_global_china_transport", None)
    monkeypatch.setattr(web_shell, "_global_china_supervisor", None)
    monkeypatch.setattr(web_shell, "_global_china_outbound_task", None)
    monkeypatch.setattr(web_shell, "_global_china_start_task", None)

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
        china_bridge=SimpleNamespace(),
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
