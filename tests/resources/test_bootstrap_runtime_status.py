from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import g3ku.shells.web as web_shell
import main.api.bootstrap_rest as bootstrap_rest


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
