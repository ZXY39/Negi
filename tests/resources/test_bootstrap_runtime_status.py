from __future__ import annotations

import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from types import SimpleNamespace

import g3ku.shells.web as web_shell
import main.api.bootstrap_rest as bootstrap_rest
from g3ku.bus.queue import MessageBus
from g3ku.config.schema import MultiAgentConfig
from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.session.manager import SessionManager


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(bootstrap_rest.router)
    return app


def test_bootstrap_status_reports_runtime_state_when_unlocked(monkeypatch):
    class _Security:
        def status(self):
            return {"mode": "unlocked"}

    monkeypatch.setattr(bootstrap_rest, "_service", lambda: _Security())
    monkeypatch.setattr(
        bootstrap_rest,
        "describe_web_runtime_services",
        lambda: {
            "agent_ready": True,
            "main_runtime_ready": True,
            "heartbeat_ready": True,
            "bootstrapping": False,
            "ready": True,
        },
    )

    client = TestClient(_build_app())
    response = client.get("/bootstrap/status")

    assert response.status_code == 200
    payload = response.json()["item"]
    assert payload["runtime_ready"] is True
    assert payload["runtime_bootstrapping"] is False
    assert payload["runtime"]["main_runtime_ready"] is True


def test_bootstrap_status_hides_stale_runtime_state_when_locked(monkeypatch):
    class _Security:
        def status(self):
            return {"mode": "locked"}

    monkeypatch.setattr(bootstrap_rest, "_service", lambda: _Security())
    monkeypatch.setattr(
        bootstrap_rest,
        "describe_web_runtime_services",
        lambda: {
            "agent_ready": True,
            "main_runtime_ready": True,
            "heartbeat_ready": True,
            "bootstrapping": True,
            "ready": True,
        },
    )

    client = TestClient(_build_app())
    response = client.get("/bootstrap/status")

    assert response.status_code == 200
    payload = response.json()["item"]
    assert payload["runtime_ready"] is False
    assert payload["runtime_bootstrapping"] is False
    assert payload["runtime"]["agent_ready"] is False


def test_bootstrap_bridge_logs_runtime_reset_diagnostics_when_active_sessions_exist(monkeypatch) -> None:
    logs: list[tuple[str, str]] = []

    def _record(level: str):
        def _inner(template, *args, **kwargs):
            _ = kwargs
            try:
                rendered = str(template).format(*args)
            except Exception:
                rendered = str(template)
            logs.append((level, rendered))
        return _inner

    monkeypatch.setattr(
        "g3ku.runtime.bootstrap_bridge.logger",
        SimpleNamespace(
            info=_record("info"),
            warning=_record("warning"),
            debug=_record("debug"),
        ),
    )

    loop = SimpleNamespace(
        commit_service=None,
        _memory_runtime_settings=SimpleNamespace(),
        memory_manager=None,
        _active_tasks={"web:ceo-demo": {object()}},
    )

    RuntimeBootstrapBridge(loop)._reset_memory_runtime(reason="resource_snapshot")

    warning_logs = [message for level, message in logs if level == "warning"]
    assert any("Resetting memory runtime while active sessions exist" in message for message in warning_logs)
    assert any("reason=resource_snapshot" in message for message in warning_logs)
    assert any("active_task_sessions=web:ceo-demo" in message for message in warning_logs)
    assert loop._memory_runtime_settings is None


def test_sync_memory_runtime_resets_and_rebuilds_when_fingerprint_changes(monkeypatch) -> None:
    monkeypatch.setattr(
        "g3ku.runtime.bootstrap_bridge.logger",
        SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None),
    )

    descriptor = SimpleNamespace(fingerprint="fp-1", metadata={"settings": {}})
    loop = SimpleNamespace(
        resource_manager=SimpleNamespace(
            get_tool_descriptor=lambda name: descriptor if name == "memory_runtime" else None
        ),
        _internal_tool_settings_fingerprints={},
        _memory_runtime_settings=SimpleNamespace(),
        memory_manager=None,
        commit_service=None,
        _active_tasks={},
    )

    bridge = RuntimeBootstrapBridge(loop)
    init_calls: list[object] = []

    def _fake_init(cfg):
        init_calls.append(cfg)
        loop._memory_runtime_settings = cfg

    monkeypatch.setattr(bridge, "init_memory_runtime", _fake_init)

    assert bridge.sync_internal_tool_runtimes(force=True, reason="reload") is True
    assert loop._internal_tool_settings_fingerprints["memory_runtime"] == "fp-1"
    assert len(init_calls) == 1
    assert loop._memory_runtime_settings is not None

    # An unchanged fingerprint with an existing runtime is a no-op.
    assert bridge.sync_internal_tool_runtimes(force=False, reason="recheck") is False
    assert len(init_calls) == 1


def test_sync_memory_runtime_fingerprint_gate_controls_reset(monkeypatch) -> None:
    descriptor = SimpleNamespace(fingerprint="fp-stable", metadata={"settings": {}})
    loop = SimpleNamespace(
        resource_manager=SimpleNamespace(
            get_tool_descriptor=lambda name: descriptor if name == "memory_runtime" else None
        ),
        _internal_tool_settings_fingerprints={"memory_runtime": "fp-stable"},
        _memory_runtime_settings=SimpleNamespace(),
        memory_manager=object(),
    )

    bridge = RuntimeBootstrapBridge(loop)
    reset_calls: list[str] = []
    init_calls: list[object] = []
    monkeypatch.setattr(
        bridge, "_reset_memory_runtime", lambda reason="runtime": reset_calls.append(reason)
    )
    monkeypatch.setattr(bridge, "init_memory_runtime", lambda cfg: init_calls.append(cfg))

    # Unchanged fingerprint + settings already initialized: the gate skips the
    # reset entirely, so the active checkpointer is left alone.
    assert bridge.sync_internal_tool_runtimes(force=False, reason="model_config_tool") is False
    assert reset_calls == []
    assert init_calls == []

    # force=True bypasses the gate and resets + reinitializes.
    assert bridge.sync_internal_tool_runtimes(force=True, reason="manual") is True
    assert reset_calls == ["manual"]
    assert len(init_calls) == 1

    # A changed fingerprint triggers reset + reinit even without force, and the
    # stored fingerprint is updated to the new value.
    reset_calls.clear()
    init_calls.clear()
    loop._internal_tool_settings_fingerprints["memory_runtime"] = "fp-old"
    assert bridge.sync_internal_tool_runtimes(force=False, reason="resource_snapshot") is True
    assert reset_calls == ["resource_snapshot"]
    assert len(init_calls) == 1
    assert loop._internal_tool_settings_fingerprints["memory_runtime"] == "fp-stable"


async def _noop(*_args, **_kwargs):
    return None


class _Store:
    @staticmethod
    def list_pending_task_terminal_outbox(limit: int = 500):
        _ = limit
        return []

    @staticmethod
    def list_pending_task_stall_outbox(limit: int = 500):
        _ = limit
        return []


class _Service:
    def __init__(self) -> None:
        self._started = False
        self.store = _Store()

    async def startup(self) -> None:
        self._started = True


class _Heartbeat:
    def __init__(self) -> None:
        self._started = False

    async def start(self) -> None:
        self._started = True

    def enqueue_task_terminal_payload(self, payload):
        _ = payload

    def enqueue_task_stall_payload(self, payload):
        _ = payload


@pytest.mark.asyncio
async def test_ensure_web_runtime_services_limits_worker_wait(monkeypatch):
    service = _Service()
    heartbeat = _Heartbeat()
    waits: list[float] = []

    async def _ensure_worker(_service, *, wait_timeout_s: float = 5.0):
        _ = _service
        waits.append(wait_timeout_s)
        return False

    async def _start_heartbeat(_agent, _runtime_manager, **kwargs):
        _ = _agent, _runtime_manager, kwargs
        await heartbeat.start()
        return heartbeat

    monkeypatch.setattr(web_shell, "_global_runtime_services_lock", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", heartbeat)
    monkeypatch.setattr(web_shell, "ensure_managed_task_worker", _ensure_worker)
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)

    class _Agent:
        main_task_service = service

    await web_shell.ensure_web_runtime_services(_Agent())

    assert waits == [1.0]
    assert service._started is True
    assert heartbeat._started is True


def test_bootstrap_exit_stops_runtime_before_requesting_server_shutdown(monkeypatch):
    calls: list[str] = []

    class _Security:
        def is_unlocked(self) -> bool:
            return True

    async def _snapshot() -> dict[str, object]:
        return {
            "has_running_work": False,
            "running_sessions": [],
            "running_tasks": [],
            "summary_text": "idle",
        }

    async def _shutdown_runtime() -> None:
        calls.append("shutdown_runtime")

    monkeypatch.setattr(bootstrap_rest, "_service", lambda: _Security())
    monkeypatch.setattr(bootstrap_rest, "_running_work_snapshot", _snapshot)
    monkeypatch.setattr(bootstrap_rest, "shutdown_web_runtime", _shutdown_runtime)
    monkeypatch.setattr(
        bootstrap_rest,
        "request_server_shutdown",
        lambda: calls.append("request_server_shutdown") or True,
    )

    client = TestClient(_build_app())
    response = client.post("/bootstrap/exit", json={})

    assert response.status_code == 200


def test_bootstrap_exit_pauses_running_work_before_shutdown(monkeypatch):
    calls: list[str] = []

    class _Security:
        def is_unlocked(self) -> bool:
            return True

    class _RuntimeManager:
        def list_sessions(self) -> list[str]:
            return ["web:shared"]

        def get(self, session_id: str):
            if session_id != "web:shared":
                return None
            return SimpleNamespace(
                state=SimpleNamespace(is_running=True, status="running"),
            )

        async def pause(self, session_id: str, *, manual: bool = False) -> int:
            calls.append(f"pause_session:{session_id}:{manual}")
            return 1

    class _TaskService:
        def __init__(self) -> None:
            self.store = SimpleNamespace(
                list_tasks=lambda: [
                    SimpleNamespace(task_id="task:1", status="in_progress", is_paused=False),
                ]
            )

        async def startup(self) -> None:
            calls.append("startup")

        async def pause_task(self, task_id: str):
            calls.append(f"pause_task:{task_id}")
            return None

    agent = SimpleNamespace(
        sessions=None,
        main_task_service=_TaskService(),
    )
    runtime_manager = _RuntimeManager()
    snapshots = iter(
        [
            {
                "has_running_work": True,
                "running_sessions": [{"session_id": "web:shared", "title": "web:shared"}],
                "running_tasks": [{"task_id": "task:1", "title": "", "session_id": ""}],
                "summary_text": "1 running session. 1 running task.",
            },
            {
                "has_running_work": False,
                "running_sessions": [],
                "running_tasks": [],
                "summary_text": "idle",
            },
        ]
    )

    async def _snapshot() -> dict[str, object]:
        return next(snapshots)

    async def _shutdown_runtime() -> None:
        calls.append("shutdown_runtime")

    monkeypatch.setattr(bootstrap_rest, "_service", lambda: _Security())
    monkeypatch.setattr(bootstrap_rest, "get_agent", lambda: agent)
    monkeypatch.setattr(bootstrap_rest, "get_runtime_manager", lambda _agent=None: runtime_manager)
    monkeypatch.setattr(bootstrap_rest, "_running_work_snapshot", _snapshot)
    monkeypatch.setattr(bootstrap_rest, "shutdown_web_runtime", _shutdown_runtime)
    monkeypatch.setattr(
        bootstrap_rest,
        "request_server_shutdown",
        lambda: calls.append("request_server_shutdown") or True,
    )

    client = TestClient(_build_app())
    response = client.post("/bootstrap/exit", json={"stop_running_work": True})

    assert response.status_code == 200
    assert "pause_session:web:shared:True" in calls
    assert "pause_task:task:1" in calls
    assert "shutdown_runtime" in calls
    assert response.json()["item"]["paused_sessions"] == 1
    assert response.json()["item"]["paused_tasks"] == 1


def test_bootstrap_exit_requires_pause_confirmation(monkeypatch):
    class _Security:
        def is_unlocked(self) -> bool:
            return True

    async def _snapshot() -> dict[str, object]:
        return {
            "has_running_work": True,
            "running_sessions": [{"session_id": "web:shared", "title": "Demo"}],
            "running_tasks": [],
            "summary_text": "1 running session.",
        }

    monkeypatch.setattr(bootstrap_rest, "_service", lambda: _Security())
    monkeypatch.setattr(bootstrap_rest, "_running_work_snapshot", _snapshot)

    client = TestClient(_build_app())
    response = client.post("/bootstrap/exit", json={})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "running_work_requires_confirmation"
    assert "暂停" in detail["message"]


def test_bootstrap_unlock_succeeds_when_runtime_start_is_deferred(monkeypatch):
    calls: list[str] = []

    class _Security:
        def unlock(self, *, password: str) -> None:
            calls.append(f"unlock:{password}")

        def status(self):
            return {"mode": "unlocked"}

    async def _deferred_runtime_start() -> None:
        raise RuntimeError("No model configured for role 'ceo'.")

    monkeypatch.setattr(bootstrap_rest, "_service", lambda: _Security())
    monkeypatch.setattr(bootstrap_rest, "_start_runtime_after_unlock", _deferred_runtime_start)
    monkeypatch.setattr(
        bootstrap_rest,
        "_status_payload",
        lambda include_preview=True: {
            "mode": "unlocked",
            "runtime_ready": False,
            "runtime_bootstrapping": False,
            "runtime": {
                "agent_ready": False,
                "main_runtime_ready": False,
                "heartbeat_ready": False,
                "bootstrapping": False,
                "ready": False,
            },
        },
    )

    client = TestClient(_build_app())
    response = client.post("/bootstrap/unlock", json={"password": "demo"})

    assert response.status_code == 200
    assert calls == ["unlock:demo"]
    assert response.json()["item"]["runtime_ready"] is False


def test_bootstrap_bridge_uses_canonical_ceo_runner():
    loop = SimpleNamespace(
        multi_agent_config=MultiAgentConfig(),
        app_config=SimpleNamespace(),
    )

    RuntimeBootstrapBridge(loop).init_multi_agent_runtime()

    assert isinstance(loop.multi_agent_runner, CeoFrontDoorRunner)


@pytest.mark.asyncio
async def test_heartbeat_reply_notifier_skips_legacy_china_sessions(tmp_path):
    """Legacy ``china:`` sessions keep their transcripts, but the channel
    subsystem is gone: proactive replies must be skipped silently without
    publishing outbound or raising."""
    bus = MessageBus()
    session_manager = SessionManager(tmp_path)
    session_id = "china:qqbot:default:dm"
    session_manager.get_or_create(session_id)

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(web_shell, "_global_bus", bus)
        await web_shell._notify_heartbeat_channel_reply(session_id, "reminder text")
        await asyncio.sleep(0.05)
    finally:
        monkeypatch.undo()

    assert bus.outbound.empty()
