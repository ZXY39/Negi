from __future__ import annotations

import asyncio

import g3ku.web.worker_control as worker_control


class _FakeStore:
    def __init__(self, lease=None):
        self.lease = lease
        self.released: list[tuple[str, str]] = []

    def get_worker_lease(self, role):
        return self.lease

    def release_worker_lease(self, *, role, worker_id):
        self.released.append((role, worker_id))


class _FakeService:
    def __init__(self, store):
        self.store = store


def test_pid_alive_zero_or_negative_is_dead() -> None:
    assert worker_control._pid_alive(0) is False
    assert worker_control._pid_alive(-1) is False


def test_settle_worker_lease_no_service_returns_true() -> None:
    assert worker_control._settle_worker_lease_for_restart(None) is True


def test_settle_worker_lease_no_lease_returns_true() -> None:
    assert worker_control._settle_worker_lease_for_restart(_FakeService(_FakeStore(None))) is True


def test_settle_worker_lease_live_holder_skips_respawn(monkeypatch) -> None:
    monkeypatch.setattr(worker_control, "_pid_alive", lambda pid: True)
    store = _FakeStore({"worker_id": "worker:live", "holder_pid": 1234})
    assert worker_control._settle_worker_lease_for_restart(_FakeService(store)) is False
    assert store.released == []


def test_settle_worker_lease_dead_holder_releases_stale_lease(monkeypatch) -> None:
    monkeypatch.setattr(worker_control, "_pid_alive", lambda pid: False)
    store = _FakeStore({"worker_id": "worker:dead", "holder_pid": 1234})
    assert worker_control._settle_worker_lease_for_restart(_FakeService(store)) is True
    assert store.released == [("task_worker", "worker:dead")]


def test_settle_worker_lease_unknown_liveness_leaves_lease_untouched(monkeypatch) -> None:
    monkeypatch.setattr(worker_control, "_pid_alive", lambda pid: None)
    store = _FakeStore({"worker_id": "worker:unknown", "holder_pid": 1234})
    assert worker_control._settle_worker_lease_for_restart(_FakeService(store)) is True
    assert store.released == []


def test_watchdog_returns_immediately_when_auto_worker_disabled(monkeypatch) -> None:
    monkeypatch.setattr(worker_control, "auto_worker_enabled", lambda: False)
    asyncio.run(worker_control.run_managed_task_worker_watchdog(None))