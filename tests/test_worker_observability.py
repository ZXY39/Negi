"""Observability regression tests for worker liveness and event-write failures.

Covers two runtime observability contracts introduced with the worker
liveness canary work:

- task_events write failures are counted in-process, surfaced via a
  rate-limited WARNING (300 s interval by default), and readable through
  `TaskLogService.event_write_failure_count()`.
- the managed task worker emits a coarse-grained "worker heartbeat alive"
  INFO line at a configurable interval (600 s default) so that
  managed-worker.log doubles as a process-liveness canary; the web process
  does not emit it.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import MagicMock

import main.monitoring.log_service as log_service_module
import main.service.worker_heartbeat_service_v2 as heartbeat_module
from main.models import TaskRecord
from main.monitoring.log_service import TaskLogService
from main.service.worker_heartbeat_service_v2 import WorkerHeartbeatServiceV2


class _RaisingEventStore:
    def append_task_event(self, **_kwargs):
        raise sqlite3.OperationalError('database is locked')


class _FakeClock:
    """Controllable monotonic clock for the failure-warning rate limiter."""

    def __init__(self) -> None:
        self._now = 1000.0

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeHeartbeatStore:
    def __init__(self) -> None:
        self.statuses: list[dict] = []

    def upsert_worker_status(self, **_kwargs) -> None:
        self.statuses.append(dict(_kwargs))

    def write_failure_counts(self) -> dict[str, int]:
        return {'disk_full': 1, 'error': 2}


class _FakeScheduler:
    def active_task_count(self) -> int:
        return 0

    def queued_task_count(self) -> int:
        return 0


def _make_log_service(monkeypatch, clock: _FakeClock):
    monkeypatch.setattr(log_service_module, 'time', clock)
    fake_logger = MagicMock()
    monkeypatch.setattr(log_service_module, 'logger', fake_logger)
    service = TaskLogService(store=_RaisingEventStore(), file_store=MagicMock())
    return service, fake_logger


def test_event_write_failure_counter_and_rate_limited_warning(monkeypatch):
    clock = _FakeClock()
    service, fake_logger = _make_log_service(monkeypatch, clock)

    assert service.event_write_failure_count() == 0

    for advance in (10.0, 10.0, 310.0):
        clock.advance(advance)
        result = service.append_task_event(
            task_id='task:demo',
            session_id='web:shared',
            event_type='task.terminal',
            data={},
        )
        assert result == 0

    # Three failures all count; WARNING is rate-limited to one per 300 s window.
    assert service.event_write_failure_count() == 3
    assert fake_logger.warning.call_count == 2
    first_args = fake_logger.warning.call_args_list[0].args
    assert first_args[0] == 'task_events write failure (rate-limited): total={} latest={!r}'
    assert first_args[1] == 1
    last_args = fake_logger.warning.call_args_list[-1].args
    assert last_args[1] == 3


def test_event_write_failure_counter_covers_live_snapshot_flush(monkeypatch):
    clock = _FakeClock()
    fake_logger = MagicMock()
    monkeypatch.setattr(log_service_module, 'time', clock)
    monkeypatch.setattr(log_service_module, 'logger', fake_logger)

    class _FlushRaisingStore:
        def write_task_live_snapshot(self, *_args, **_kwargs):
            raise sqlite3.OperationalError('disk I/O error')

    service = TaskLogService(store=_FlushRaisingStore(), file_store=MagicMock())
    task = TaskRecord(
        task_id='task:demo',
        title='demo',
        user_request='demo',
        root_node_id='node:demo',
        created_at='2026-09-15T12:00:00+08:00',
        updated_at='2026-09-15T12:00:00+08:00',
        status='in_progress',
        is_paused=True,
    )

    # The single-file live.patch snapshot path shares the failure counter:
    # a buffered patch flushed against a failing store counts and warns once.
    service._buffer_task_live_patch_locked(task=task, payload={'frame': {'status': 'paused'}})
    service.flush_live_patch_history('task:demo')

    assert service.event_write_failure_count() == 1
    assert fake_logger.warning.call_count == 1
    warn_args = fake_logger.warning.call_args.args
    assert warn_args[0] == 'task_events write failure (rate-limited): total={} latest={!r}'
    assert warn_args[1] == 1


def test_worker_mode_emits_alive_log_line(monkeypatch):
    fake_logger = MagicMock()
    monkeypatch.setattr(heartbeat_module, 'logger', fake_logger)
    store = _FakeHeartbeatStore()
    service = WorkerHeartbeatServiceV2(
        store=store,
        scheduler=_FakeScheduler(),
        execution_mode='worker',
        worker_id='worker:test-alive',
        publish_status=lambda item: None,
        alive_log_interval_seconds=0.05,
    )

    service.start_background()
    try:
        deadline = time.monotonic() + 2.0
        while fake_logger.info.call_count < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        asyncio.run(service.close())

    assert fake_logger.info.call_count == 1
    args = fake_logger.info.call_args.args
    assert args[0].startswith('worker heartbeat alive: ')
    assert args[1] == 'worker:test-alive'
    assert args[5] == {'disk_full': 1, 'error': 2}
    assert store.statuses, 'worker_status heartbeat rows must still be written'


def test_web_mode_does_not_emit_alive_log_line(monkeypatch):
    fake_logger = MagicMock()
    monkeypatch.setattr(heartbeat_module, 'logger', fake_logger)
    service = WorkerHeartbeatServiceV2(
        store=_FakeHeartbeatStore(),
        scheduler=_FakeScheduler(),
        execution_mode='web',
        worker_id='worker:web-side',
        publish_status=lambda item: None,
        alive_log_interval_seconds=0.05,
    )

    service.start_background()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        asyncio.run(service.close())

    assert fake_logger.info.call_count == 0
