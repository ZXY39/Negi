from __future__ import annotations

import asyncio
from typing import Any

from main.runtime.model_load_balancer import ModelLoadBalancer
from main.service.runtime_service import _WORKER_STATE_ONLINE, MainRuntimeService
from tests.resources.test_model_load_balancer import _balancer, _Clock, _group, _select


class _FakeStore:
    def __init__(self) -> None:
        self.finished: list[dict[str, Any]] = []
        self.enqueued: list[dict[str, Any]] = []

    def finish_task_command(self, command_id: str, **kwargs: Any) -> None:
        self.finished.append({'command_id': command_id, **kwargs})

    def enqueue_task_command(self, **kwargs: Any) -> None:
        self.enqueued.append(dict(kwargs))


def _worker_service(balancer: ModelLoadBalancer) -> MainRuntimeService:
    service = object.__new__(MainRuntimeService)
    service.execution_mode = 'worker'
    service.worker_id = 'w1'
    service.model_load_balancer = balancer
    service.store = _FakeStore()
    service.normalize_task_id = lambda value: str(value or '').strip()
    return service


def test_worker_command_ingests_observation_events() -> None:
    clock = _Clock()
    worker = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    web = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    web.set_share_observation(True)
    lease = _select(web, "node:1")
    web.record_request_start(lease)
    web.record_outcome(lease, status_code=429, error_text="429 rpm limit")
    events = web.drain_outbound_events()

    service = _worker_service(worker)
    command = {
        'command_id': 'cmd1',
        'command_type': 'model_route_observation',
        'task_id': '',
        'payload': {'events': events},
    }
    asyncio.run(service._process_worker_command(command))

    assert worker.upstream_view()["m_a"]["rolling_rpm_60s"] == 1
    assert worker.upstream_view()["m_a"]["penalty_429"] > 0.5
    assert service.store.finished[0]['success'] is True
    assert service.store.finished[0]['result']['ingested'] == 2


def test_web_sync_reports_events_and_consumes_view() -> None:
    clock = _Clock()
    web = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    web.set_share_observation(True)
    lease = _select(web, "node:1")
    web.record_request_start(lease)
    events_before = web.drain_outbound_events()
    # 重新攒一批供同步上报。
    lease2 = _select(web, "node:2")
    web.record_request_start(lease2)

    service = object.__new__(MainRuntimeService)
    service.execution_mode = 'web'
    service.model_load_balancer = web
    service.store = _FakeStore()
    service.latest_worker_status = lambda: {
        'payload': {'model_route_member_upstream': {'m_a': {'rolling_rpm_60s': 7, 'penalty_429': 1.5}}}
    }
    service.worker_state = lambda **kwargs: _WORKER_STATE_ONLINE

    service._sync_model_route_observation()

    # 上报：本地事件批投成 model_route_observation 命令。
    assert len(service.store.enqueued) == 1
    assert service.store.enqueued[0]['command_type'] == 'model_route_observation'
    assert len(service.store.enqueued[0]['payload']['events']) == 1
    # 消费：合并视图喂进本地 balancer，打分读它。
    consumed = _select(web, "node:3")
    assert consumed.rolling_rpm == 7
    assert consumed.penalty_before == 1.5
    assert events_before  # sanity


def test_web_sync_noop_without_balancer() -> None:
    service = object.__new__(MainRuntimeService)
    service.execution_mode = 'web'
    service.model_load_balancer = None
    service.store = _FakeStore()
    service._sync_model_route_observation()
    assert service.store.enqueued == []


def test_web_sync_skips_consume_when_worker_offline() -> None:
    clock = _Clock()
    web = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    service = object.__new__(MainRuntimeService)
    service.execution_mode = 'web'
    service.model_load_balancer = web
    service.store = _FakeStore()
    service.latest_worker_status = lambda: {
        'payload': {'model_route_member_upstream': {'m_a': {'rolling_rpm_60s': 9, 'penalty_429': 3.0}}}
    }
    service.worker_state = lambda **kwargs: 'offline'

    service._sync_model_route_observation()

    # worker 离线：不刷新视图，本地零事件 => 打分仍 0（退本地真源）。
    lease = _select(web, "node:1")
    assert lease.rolling_rpm == 0
    assert lease.penalty_before == 0.0
