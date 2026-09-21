from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import main.api.bootstrap_rest as bootstrap_rest
from g3ku.deployment.runtime_startup import BOOTSTRAP_PASSWORD_ENV, auto_unlock_from_env
from g3ku.security import BOOTSTRAP_MASTER_KEY_ENV
from g3ku.security.bootstrap import BootstrapSecurityService

AUTO_UNLOCK_PATH = (".g3ku", "llm-config", "auto-unlock.key")


def _auto_unlock_file(workspace):
    path = workspace
    for part in AUTO_UNLOCK_PATH:
        path = path / part
    return path


def _setup(workspace, *, password="alpha-pass"):
    service = BootstrapSecurityService(workspace=workspace)
    service.setup_initial_realm(password=password, confirm_legacy_reset=True)
    return service


def test_change_password_rewraps_same_master_key_and_old_password_stops_working(tmp_path, monkeypatch):
    monkeypatch.delenv(BOOTSTRAP_MASTER_KEY_ENV, raising=False)
    service = _setup(tmp_path)
    key_before = service.active_master_key()

    service.change_password(current_password="alpha-pass", new_password="beta-pass")

    assert service.active_master_key() == key_before
    service.lock()
    with pytest.raises(ValueError):
        service.unlock(password="alpha-pass")
    service.unlock(password="beta-pass")
    assert service.active_master_key() == key_before


def test_change_password_requires_unlocked_service_and_valid_current(tmp_path):
    service = _setup(tmp_path)
    service.lock()

    with pytest.raises(ValueError, match="project is locked"):
        service.change_password(current_password="alpha-pass", new_password="beta-pass")

    service.unlock(password="alpha-pass")
    with pytest.raises(ValueError, match="invalid password"):
        service.change_password(current_password="nope", new_password="beta-pass")
    with pytest.raises(ValueError, match="password is required"):
        service.change_password(current_password="alpha-pass", new_password="")


def test_auto_unlock_enable_writes_key_file_and_env_and_disable_clears_both(tmp_path, monkeypatch):
    monkeypatch.delenv(BOOTSTRAP_MASTER_KEY_ENV, raising=False)
    service = _setup(tmp_path)
    key = service.active_master_key()

    status = service.set_auto_unlock(enabled=True)

    assert status["auto_unlock"] is True
    assert _auto_unlock_file(tmp_path).read_text(encoding="utf-8").strip() == key
    assert os.environ.get(BOOTSTRAP_MASTER_KEY_ENV) == key

    status = service.set_auto_unlock(enabled=False)

    assert status["auto_unlock"] is False
    assert not _auto_unlock_file(tmp_path).exists()
    assert BOOTSTRAP_MASTER_KEY_ENV not in os.environ


def test_auto_unlock_enable_requires_unlocked_service(tmp_path, monkeypatch):
    monkeypatch.delenv(BOOTSTRAP_MASTER_KEY_ENV, raising=False)
    service = _setup(tmp_path)
    service.lock()

    with pytest.raises(ValueError, match="project is locked"):
        service.set_auto_unlock(enabled=True)

    # 关闭不需要解锁状态：删掉凭据不能要求先证明身份。
    assert service.set_auto_unlock(enabled=False)["auto_unlock"] is False


def test_auto_unlock_from_env_falls_back_to_local_key_file(tmp_path, monkeypatch):
    monkeypatch.delenv(BOOTSTRAP_MASTER_KEY_ENV, raising=False)
    monkeypatch.delenv(BOOTSTRAP_PASSWORD_ENV, raising=False)
    service = _setup(tmp_path)
    service.set_auto_unlock(enabled=True)
    service.lock()
    # 直接 pop 而不是再 monkeypatch.delenv：后者会把这份泄漏值在 teardown 时塞回去。
    os.environ.pop(BOOTSTRAP_MASTER_KEY_ENV, None)

    try:
        assert auto_unlock_from_env(security_service=service) == "master_key"
        assert service.is_unlocked()
    finally:
        service.set_auto_unlock(enabled=False)


def _client_for(stub, monkeypatch) -> TestClient:
    monkeypatch.setattr(bootstrap_rest, "_service", lambda: stub)
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
    app = FastAPI()
    app.include_router(bootstrap_rest.router)
    return TestClient(app)


class _Stub:
    def __init__(self, *, unlocked=True) -> None:
        self.calls: list[tuple[str, object]] = []
        self._unlocked = unlocked

    def is_unlocked(self) -> bool:
        return self._unlocked

    def status(self) -> dict[str, object]:
        return {"mode": "unlocked" if self._unlocked else "locked", "auto_unlock": False}

    def unlock(self, *, password: str) -> dict[str, object]:
        self.calls.append(("unlock", password))
        self._unlocked = True
        return self.status()

    def change_password(self, *, current_password: str, new_password: str) -> dict[str, object]:
        self.calls.append(("change", current_password, new_password))
        return self.status()

    def set_auto_unlock(self, *, enabled: bool) -> dict[str, object]:
        self.calls.append(("auto-unlock", enabled))
        return {"mode": "unlocked", "auto_unlock": bool(enabled)}

    def lock(self) -> dict[str, object]:
        self.calls.append(("lock",))
        self._unlocked = False
        return self.status()


def test_unlock_route_passes_remember_flag_to_auto_unlock(monkeypatch):
    stub = _Stub(unlocked=False)
    client = _client_for(stub, monkeypatch)

    response = client.post("/bootstrap/unlock", json={"password": "alpha-pass", "remember": True})

    assert response.status_code == 200
    assert stub.calls == [("unlock", "alpha-pass"), ("auto-unlock", True)]


def test_change_password_route_validates_confirmation(monkeypatch):
    stub = _Stub()
    client = _client_for(stub, monkeypatch)

    mismatch = client.post(
        "/bootstrap/change-password",
        json={"current_password": "a", "new_password": "b", "password_confirm": "c"},
    )
    ok = client.post(
        "/bootstrap/change-password",
        json={"current_password": "a", "new_password": "b", "password_confirm": "b"},
    )

    assert mismatch.status_code == 400
    assert mismatch.json()["detail"] == "password_confirmation_mismatch"
    assert ok.status_code == 200
    assert stub.calls == [("change", "a", "b")]


def test_auto_unlock_route_requires_explicit_enabled_flag(monkeypatch):
    stub = _Stub()
    client = _client_for(stub, monkeypatch)

    missing = client.post("/bootstrap/auto-unlock", json={})

    assert missing.status_code == 400
    assert missing.json()["detail"] == "enabled_required"
    assert stub.calls == []

    disabled = client.post("/bootstrap/auto-unlock", json={"enabled": False})

    assert disabled.status_code == 200
    assert disabled.json()["item"]["auto_unlock"] is False
    assert stub.calls == [("auto-unlock", False)]


def test_lock_route_returns_423_when_already_locked(monkeypatch):
    client = _client_for(_Stub(unlocked=False), monkeypatch)

    response = client.post("/bootstrap/lock")

    assert response.status_code == 423
    assert response.json()["detail"] == "project_locked"


def test_lock_route_locks_without_touching_runtime(monkeypatch):
    stub = _Stub()
    client = _client_for(stub, monkeypatch)
    shutdown_calls: list[str] = []
    monkeypatch.setattr(
        bootstrap_rest,
        "shutdown_web_runtime",
        lambda: shutdown_calls.append("runtime"),
    )
    monkeypatch.setattr(
        bootstrap_rest,
        "request_server_shutdown",
        lambda: shutdown_calls.append("server"),
    )

    response = client.post("/bootstrap/lock")

    assert response.status_code == 200
    assert response.json()["item"]["mode"] == "locked"
    assert shutdown_calls == []


def test_status_payload_exposes_auto_unlock(monkeypatch):
    stub = SimpleNamespace(
        status=lambda: {"mode": "locked", "unlock_scope": "global", "legacy_detected": False, "auto_unlock": True},
        legacy_detected=lambda: False,
    )
    client = _client_for(stub, monkeypatch)

    response = client.get("/bootstrap/status")

    assert response.status_code == 200
    assert response.json()["item"]["auto_unlock"] is True
