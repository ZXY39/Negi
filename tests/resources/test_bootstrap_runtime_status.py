from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from types import SimpleNamespace

import g3ku.shells.web as web_shell
import main.api.bootstrap_rest as bootstrap_rest
from g3ku.config.schema import MultiAgentConfig
from g3ku.runtime.bootstrap_bridge import RuntimeBootstrapBridge
from g3ku.runtime.frontdoor.ceo_runner import CeoFrontDoorRunner
from g3ku.runtime.langgraph_ceo import LangGraphCeoRunner


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
    monkeypatch.setattr(web_shell, "_ensure_china_bridge_services", _noop)

    class _Agent:
        main_task_service = service

    await web_shell.ensure_web_runtime_services(_Agent())

    assert waits == [1.0]
    assert service._started is True
    assert heartbeat._started is True


@pytest.mark.asyncio
async def test_refresh_web_agent_runtime_restarts_china_bridge_when_config_changes(monkeypatch):
    class _BridgeConfig:
        def __init__(self, token: str) -> None:
            self.enabled = True
            self.auto_start = True
            self.control_token = token

        def model_dump(self, **_kwargs):
            return {
                'enabled': self.enabled,
                'autoStart': self.auto_start,
                'controlToken': self.control_token,
            }

    loop = SimpleNamespace(app_config=SimpleNamespace(china_bridge=_BridgeConfig('next')))
    started_with: list[str] = []
    stop_calls: list[str] = []

    class _Supervisor:
        def __init__(self) -> None:
            self._app_config = SimpleNamespace(china_bridge=_BridgeConfig('current'))

        async def stop(self) -> None:
            stop_calls.append('stopped')

    async def _start_china(_agent, config) -> None:
        started_with.append(config.china_bridge.control_token)

    monkeypatch.setattr(web_shell, 'get_agent', lambda: loop)
    monkeypatch.setattr(web_shell, 'refresh_loop_runtime_config', lambda _loop, **_kwargs: True)
    monkeypatch.setattr(web_shell, '_ensure_china_bridge_services', _noop)
    monkeypatch.setattr(web_shell, '_global_china_supervisor', _Supervisor())
    monkeypatch.setattr(web_shell, '_global_china_outbound_task', None)
    monkeypatch.setattr(web_shell, '_global_china_start_task', None)
    monkeypatch.setattr(web_shell, '_start_china_bridge_services_now', _start_china)

    changed = await web_shell.refresh_web_agent_runtime(force=True, reason='test')

    assert changed is True
    assert stop_calls == ['stopped']
    assert started_with == ['next']


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


def test_bootstrap_bridge_uses_langgraph_ceo_runner_by_default():
    loop = SimpleNamespace(
        multi_agent_config=MultiAgentConfig(),
        app_config=SimpleNamespace(get_ceo_frontdoor_implementation=lambda: "langgraph"),
    )

    RuntimeBootstrapBridge(loop).init_multi_agent_runtime()

    assert isinstance(loop.multi_agent_runner, LangGraphCeoRunner)


def test_bootstrap_bridge_can_switch_back_to_legacy_ceo_runner():
    loop = SimpleNamespace(
        multi_agent_config=MultiAgentConfig(),
        app_config=SimpleNamespace(get_ceo_frontdoor_implementation=lambda: "legacy"),
    )

    RuntimeBootstrapBridge(loop).init_multi_agent_runtime()

    assert isinstance(loop.multi_agent_runner, CeoFrontDoorRunner)
