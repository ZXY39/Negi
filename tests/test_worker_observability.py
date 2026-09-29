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
import json
import os
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
        self.perf_samples: list[dict] = []

    def upsert_worker_status(self, **_kwargs) -> None:
        self.statuses.append(dict(_kwargs))

    def record_perf_sample(self, **_kwargs) -> None:
        self.perf_samples.append(dict(_kwargs))

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


def test_worker_status_bridge_uses_event_loop_after_agent_loop_binding() -> None:
    """`_runtime_loop` 装 AgentLoop 时，worker 状态桥接不得静默失效。

    `bind_runtime_loop` 绑的是 AgentLoop（frontdoor 要读它的 sessions/heartbeat），
    而跨线程投递要的是 asyncio 事件循环——两者曾共用一个字段，导致实盘每拍
    `AttributeError: 'AgentLoop' object has no attribute 'is_running'`，WS 侧
    状态推送长期不动。
    """
    from types import MethodType, SimpleNamespace

    from main.service.runtime_service import MainRuntimeService

    published: list[dict] = []
    harness = SimpleNamespace(
        _runtime_loop=SimpleNamespace(tool_execution_manager=None, sessions={}),
        _event_loop=None,
        publish_worker_status_event=lambda **kwargs: published.append(kwargs),
    )
    publish = MethodType(MainRuntimeService._publish_worker_status_from_any_thread, harness)
    schedule = MethodType(MainRuntimeService._schedule_loop_task, harness)

    async def scenario() -> None:
        harness._event_loop = asyncio.get_running_loop()
        publish({'worker_id': 'worker:1'})
        scheduled: list[str] = []
        schedule(lambda: _noop(scheduled))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def _noop(sink: list[str]):
        sink.append('ok')

    asyncio.run(scenario())
    assert published == [{'item': {'worker_id': 'worker:1'}, 'bridge': False}]


def test_mem_probe_requires_marker_and_writes_site_rows(tmp_path, monkeypatch) -> None:
    """分配探针只认数据根里的标记文件，落的一行要能指回申请分配的站点。

    实盘 2.7GB 的恢复峰用 py-spy 归不了因（它给的是 CPU 热点），进程外读数只有总量；
    能答"那一刻同时驻留的是谁"的只有 tracemalloc 按站点统计。
    """
    from main.monitoring.mem_probe import start_mem_probe

    monkeypatch.setenv('G3KU_MEM_PROBE_INTERVAL_SECONDS', '1')
    assert start_mem_probe(runtime_dir=tmp_path) is None

    (tmp_path / 'mem-probe.on').write_text('', encoding='utf-8')
    probe = start_mem_probe(runtime_dir=tmp_path)
    assert probe is not None
    try:
        time.sleep(1.2)
        kept = [f'{index:08d}' * 512 for index in range(2000)]
        time.sleep(2.2)
    finally:
        probe.stop()

    files = list((tmp_path / 'mem-probe').glob('mem-probe-*.jsonl'))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding='utf-8').splitlines() if line.strip()]
    assert len(rows) >= 2, rows
    row = rows[-1]
    assert row['pid'] == os.getpid()
    assert row['traced_mb'] >= 1
    assert row['traced_peak_mb'] >= row['traced_mb']
    assert any('test_worker_observability.py' in str(item['site']) for item in row['top']), row['top']
    # growth 榜只记相邻两拍的增量：落在申请之后的那一拍，具体是哪一拍取决于线程调度。
    assert any(
        any('test_worker_observability.py' in str(item['site']) for item in earlier['growth'])
        for earlier in rows
    ), rows
    assert all({'site', 'mb', 'blocks'} <= set(item) for item in row['top'])
    assert kept[0][:8] == '00000000'


def test_mem_probe_reads_settings_from_marker_content(tmp_path, monkeypatch) -> None:
    """调参写进标记正文也要生效：托管 worker 的环境来自 web，改不动。"""
    from main.monitoring.mem_probe import mem_probe_marker, probe_settings

    marker = mem_probe_marker(tmp_path)
    assert probe_settings(marker) == {
        'interval_seconds': 10.0,
        'top_limit': 12,
        'frames': 1,
        'dump_mb': 0,
    }

    marker.write_text('interval=3, frames=8, dump_mb=250, top=5', encoding='utf-8')
    assert probe_settings(marker) == {
        'interval_seconds': 3.0,
        'top_limit': 5,
        'frames': 8,
        'dump_mb': 250.0,
    }

    monkeypatch.setenv('G3KU_MEM_PROBE_FRAMES', '2')
    assert probe_settings(marker)['frames'] == 2
    marker.write_text('interval=0', encoding='utf-8')
    assert probe_settings(marker)['interval_seconds'] == 10.0


def test_mem_probe_dump_uses_the_sampling_instant_snapshot(tmp_path) -> None:
    """峰是瞬时的：闸门必须用触发那一拍的快照，重新采一次往往已经掉回去。

    实盘漏过一次——行里记到 374MB，闸门复查 get_traced_memory() 时只剩几十 MB，deep dump
    始终不落盘。
    """
    import tracemalloc

    from main.monitoring.mem_probe import MemProbe

    probe = MemProbe(
        output_path=tmp_path / 'p.jsonl',
        interval_seconds=999,
        top_limit=8,
        frames=8,
        dump_threshold_bytes=0,
    )
    probe.start()
    try:
        kept = [f'{index:08d}' * 1024 for index in range(3000)]
        row = probe.sample_once()
        snapshot_at_peak = probe._last_snapshot
        assert float(row['traced_mb']) >= 1
        del kept
        fresh = tracemalloc.take_snapshot()
        probe._dump_deep_traces(snapshot_at_peak, int(float(row['traced_mb']) * 1048576))
        dumped = probe.dump_path().read_text(encoding='utf-8')
        assert 'test_worker_observability.py' in dumped
        assert any(
            'test_worker_observability.py' in line.split('blk', 1)[-1]
            for line in dumped.splitlines()[1:]
        )
        # 反向证据：同一时刻重采的快照已经没有这份驻留
        assert sum(stat.size for stat in fresh.statistics('lineno')) < sum(
            stat.size for stat in snapshot_at_peak.statistics('lineno')
        )
        assert probe._dumped is True
    finally:
        probe.stop()

    """阈值触发的一次性调用链快照：站点榜只说在哪申请，链才说谁在申请。"""
    from main.monitoring.mem_probe import MemProbe

    output = tmp_path / 'probe.jsonl'
    probe = MemProbe(
        output_path=output,
        interval_seconds=1,
        top_limit=5,
        frames=8,
        dump_threshold_bytes=1,
    )
    probe.start()
    try:
        kept = [f'{index:08d}' * 256 for index in range(500)]
        time.sleep(1.4)
    finally:
        probe.stop()

    dump = probe.dump_path()
    assert dump.exists(), '越过阈值那一拍要落 deep dump'
    text = dump.read_text(encoding='utf-8')
    assert 'frames=8' in text
    assert 'test_worker_observability.py' in text
    assert ' <- ' in text, '链要有多个帧'
    assert len(dump.read_text(encoding='utf-8').splitlines()) > 1
    assert kept[0][:8] == '00000000'
