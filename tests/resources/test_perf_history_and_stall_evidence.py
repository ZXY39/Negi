"""Perf history: durable sampling, the bounded agent report, and stall evidence.

The task-hall performance bar only ever showed the latest worker snapshot, so a
stall notice that arrives 20 minutes late could not be checked against what the
machine was doing at the time. These tests pin the three parts of the fix:

- the worker heartbeat appends one downsampled `perf_samples` row per cadence
  window and prunes beyond retention;
- `perf_report` renders a bounded report and keeps "no samples" distinguishable
  from "no pressure" (both read the same in a snapshot-only world);
- `task_stall` payloads carry a one-line perf verdict for the silent window.
"""

from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import main.service.worker_heartbeat_service_v2 as heartbeat_module
from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane
from main.service.runtime_service import MainRuntimeService
from main.service.task_stall_callback import normalize_task_stall_payload
from main.service.worker_heartbeat_service_v2 import WorkerHeartbeatServiceV2
from main.storage.sqlite_store import SQLiteTaskStore

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_ID = 'worker:perf'


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called here: {kwargs!r}")


class _FakeScheduler:
    def active_task_count(self) -> int:
        return 0

    def queued_task_count(self) -> int:
        return 0


def _sample_payload(**overrides) -> dict[str, Any]:
    row: dict[str, Any] = {
        'budget_state': 'normal',
        'machine_pressure_state': 'normal',
        'local_pressure_state': 'normal',
        'machine_pressure_available': True,
        'machine_pressure_cpu_percent': 30.0,
        'machine_pressure_memory_percent': 60.0,
        'machine_pressure_disk_busy_percent': 10.0,
        'machine_pressure_disk_busy_available': True,
        'machine_disk_free_bytes': 50 * 1024 ** 3,
        'disk_emergency_active': False,
        'tool_queue_running_count': 1,
        'tool_queue_waiting_count': 0,
        'node_queue_running_count': 2,
        'node_queue_waiting_count': 0,
        'node_queue_frozen_count': 0,
        'worker_execution_oldest_wait_ms': 0.0,
        'tool_pressure_event_loop_lag_ms': 20.0,
        'tool_pressure_writer_queue_depth': 0,
        'sqlite_write_wait_ms': 5.0,
        'sqlite_query_latency_ms': 3.0,
        'active_task_count': 1,
        'debug': {'should_not_be_stored': True},
    }
    row.update(overrides)
    return row


def _local_iso(moment: datetime) -> str:
    return moment.astimezone().isoformat(timespec='seconds')


def _row(stamp: datetime, **overrides) -> dict[str, Any]:
    return {
        'sampled_at': _local_iso(stamp),
        'stamp': stamp.astimezone(timezone.utc),
        'payload': _sample_payload(**overrides),
    }


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


async def _make_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task
    await service.startup()
    return service


def test_perf_sampler_writes_one_row_per_cadence_and_drops_unreported_fields(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / 'perf.sqlite3')
    beat = WorkerHeartbeatServiceV2(
        store=store,
        scheduler=_FakeScheduler(),
        execution_mode='worker',
        worker_id=WORKER_ID,
        publish_status=lambda item: None,
        perf_history_interval_seconds=15.0,
    )
    try:
        beat._record_perf_history(_sample_payload(), _local_iso(datetime.now()))
        beat._record_perf_history(_sample_payload(budget_state='throttled'), _local_iso(datetime.now()))

        rows = store.list_perf_samples(since_iso='')
        assert len(rows) == 1, 'the second beat falls inside the sampling cadence gate'
        assert rows[0]['worker_id'] == WORKER_ID
        assert rows[0]['payload']['budget_state'] == 'normal'
        assert 'debug' not in rows[0]['payload'], 'only the reported axes are persisted'
    finally:
        store.close()


def test_perf_sampler_prunes_beyond_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SQLiteTaskStore(tmp_path / 'perf.sqlite3')
    # Prune every write so the assertion does not depend on how many beats the
    # monotonic cadence gate lets through in a tight loop.
    monkeypatch.setattr(heartbeat_module, '_PERF_HISTORY_PRUNE_EVERY_SAMPLES', 1)
    beat = WorkerHeartbeatServiceV2(
        store=store,
        scheduler=_FakeScheduler(),
        execution_mode='worker',
        worker_id=WORKER_ID,
        publish_status=lambda item: None,
        perf_history_interval_seconds=0.000001,
        perf_history_retention_seconds=3600.0,
    )
    try:
        stale_base = datetime.now().astimezone() - timedelta(hours=25)
        for index in range(4):
            # 采样节拍门用的是单调钟，Windows 上刻度约 15ms；这里逐拍放行，
            # 让断言只测保留期裁剪，不测节拍（节拍在上面的用例里已经钉住）。
            beat._last_perf_sample_mono = 0.0
            beat._record_perf_history(_sample_payload(), _local_iso(stale_base + timedelta(seconds=index)))
        assert store.count_perf_samples() == 0, 'the periodic prune must clear rows past retention'

        beat._last_perf_sample_mono = 0.0
        beat._record_perf_history(_sample_payload(budget_state='throttled'), _local_iso(datetime.now()))
        assert store.count_perf_samples() == 1, 'in-retention rows survive the same prune'
    finally:
        store.close()


def test_perf_samples_round_trip_filters_by_window(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / 'perf.sqlite3')
    now = datetime.now().astimezone()
    try:
        store.record_perf_sample(
            sampled_at=_local_iso(now - timedelta(hours=3)),
            worker_id=WORKER_ID,
            payload=_sample_payload(),
        )
        store.record_perf_sample(
            sampled_at=_local_iso(now),
            worker_id=WORKER_ID,
            payload=_sample_payload(budget_state='critical'),
        )
        recent = store.list_perf_samples(since_iso=_local_iso(now - timedelta(minutes=30)))
        assert [row['payload']['budget_state'] for row in recent] == ['critical']
        assert store.count_perf_samples() == 2
        assert store.newest_perf_sample()['payload']['budget_state'] == 'critical'

        assert store.prune_perf_samples(before_iso=_local_iso(now - timedelta(hours=1))) == 1
        assert store.count_perf_samples() == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_perf_report_window_reports_tiers_queues_and_series(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    try:
        now = datetime.now(timezone.utc)
        for index in range(24):
            throttled = 8 <= index < 16
            service.store.record_perf_sample(
                sampled_at=_local_iso(now - timedelta(minutes=23 - index)),
                worker_id=WORKER_ID,
                payload=_sample_payload(
                    budget_state='throttled' if throttled else 'normal',
                    tool_queue_waiting_count=4 if throttled else 0,
                    worker_execution_oldest_wait_ms=90000.0 if throttled else 0.0,
                ),
            )

        report = service.perf_report(mode='window', window_minutes=60)
        assert 'restricted_share=33%' in report
        assert 'Pressure runs: throttled' in report and '(8.0m)' in report
        assert 'tq_wait max=4' in report
        assert 'tq_oldest max=1.5m' in report
        series = [line for line in report.splitlines() if line[:2].isdigit()]
        assert series, 'the report must carry the downsampled series'
        assert all(len(line.split()) == 10 for line in series)
        assert any(line.endswith('throttled') for line in series)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_perf_report_series_stays_bounded_for_the_full_retention_window(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    try:
        origin = datetime.now(timezone.utc)
        rows = [_row(origin - timedelta(minutes=1439 - index), budget_state='critical') for index in range(1440)]
        report = service._render_perf_window_report(rows=rows, window_minutes=1440.0)
        series = [line for line in report.splitlines() if line[:2].isdigit()]
        assert len(series) <= 40, 'a wider window widens the bucket, never the report'
        assert len(report) < 4000, 'the report has to stay below the tool-result inline gate'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_perf_report_distinguishes_never_sampled_from_stopped_sampling(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    try:
        empty = service.perf_report(mode='window', window_minutes=10)
        assert 'Samples=0' in empty
        assert 'rows_total=0' in empty
        assert 'worker restart' in empty

        service.store.record_perf_sample(
            sampled_at=_local_iso(datetime.now(timezone.utc) - timedelta(hours=5)),
            worker_id=WORKER_ID,
            payload=_sample_payload(),
        )
        stopped = service.perf_report(mode='window', window_minutes=10)
        assert 'rows_total=1' in stopped
        assert 'sampling stopped' in stopped
        assert 'NOT that the machine was idle' in stopped
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_perf_report_validates_arguments(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    try:
        assert "mode='window'" in service.perf_report(mode='everything')
        assert 'number of minutes' in service.perf_report(mode='window', window_minutes='ten')
        assert '1440m requested' in service.perf_report(mode='window', window_minutes=999999)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_perf_report_live_mode_leads_with_the_current_snapshot(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    try:
        service.store.upsert_worker_status(
            worker_id=WORKER_ID,
            role='task_worker',
            status='running',
            updated_at=_local_iso(datetime.now()),
            payload={
                'budget_state': 'throttled',
                'machine_pressure_available': True,
                'machine_pressure_cpu_percent': 93.0,
                'tool_queue_running_count': 2,
                'tool_queue_waiting_count': 7,
                'worker_execution_oldest_wait_ms': 120000.0,
                'node_queue_waiting_count': 3,
                'pressure_sample_at': _local_iso(datetime.now()),
            },
        )
        live = service.perf_report(mode='live', window_minutes=10)
        assert 'Perf now (live worker snapshot)' in live
        assert 'tool_waiting=7' in live
        assert 'oldest_tool_wait=2.0m' in live
        assert 'Samples=0' in live, 'live mode still has to state what the history looks like'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stall_payload_carries_perf_verdict_for_the_silent_window(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    try:
        task = await service.create_task('stalled while the machine was busy', session_id='web:perf-stall')
        silent_since = datetime.now(timezone.utc) - timedelta(hours=2)
        service.log_service.update_task_runtime_meta(task.task_id, last_visible_output_at=silent_since.isoformat())
        service.store.record_perf_sample(
            sampled_at=_local_iso(silent_since + timedelta(minutes=10)),
            worker_id=WORKER_ID,
            payload=_sample_payload(
                budget_state='critical',
                tool_queue_waiting_count=6,
                worker_execution_oldest_wait_ms=240000.0,
            ),
        )

        payload = service.build_task_stall_payload(task.task_id, bucket_minutes=20)
        summary = str(payload.get('perf_window_summary') or '')
        assert 'critical=15s' in summary
        assert 'tq_wait max=6' in summary
        assert 'no resource restriction' not in summary
        assert len(summary) <= 400
        assert normalize_task_stall_payload(payload)['perf_window_summary'] == summary
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stall_perf_verdict_says_not_perf_caused_when_nothing_was_restricted(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    try:
        task = await service.create_task('stalled with an idle machine', session_id='web:perf-stall-idle')
        silent_since = datetime.now(timezone.utc) - timedelta(hours=2)
        service.log_service.update_task_runtime_meta(task.task_id, last_visible_output_at=silent_since.isoformat())
        for index in range(3):
            service.store.record_perf_sample(
                sampled_at=_local_iso(silent_since + timedelta(minutes=index)),
                worker_id=WORKER_ID,
                payload=_sample_payload(),
            )

        payload = service.build_task_stall_payload(task.task_id, bucket_minutes=20)
        assert 'no resource restriction in this window' in str(payload.get('perf_window_summary') or '')
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stall_perf_verdict_reports_missing_coverage_without_padding(tmp_path: Path) -> None:
    service = await _make_service(tmp_path)
    try:
        task = await service.create_task('stalled with no perf coverage', session_id='web:perf-stall-nodata')
        silent_since = datetime.now(timezone.utc) - timedelta(hours=2)
        service.log_service.update_task_runtime_meta(task.task_id, last_visible_output_at=silent_since.isoformat())

        summary = str(service.build_task_stall_payload(task.task_id, bucket_minutes=20).get('perf_window_summary') or '')
        assert summary.startswith('perf: no perf samples ever recorded')
        assert len(summary) < 120, 'the stall event stays one short line when there is nothing to report'
    finally:
        await service.close()


def test_stall_perf_line_renders_in_the_heartbeat_event_bundle() -> None:
    lane = build_heartbeat_prompt_lane(
        provider_model='',
        stable_rules_text='',
        events=[
            {
                'event_reason': 'task_stall',
                'task_id': 'task:perf',
                'title': 'perf stall',
                'reason': 'suspected_stall',
                'stalled_minutes': 25,
                'bucket_minutes': 20,
                'last_visible_output_at': '2026-09-22T10:00:00+08:00',
                'brief_text': 'brief',
                'latest_node_summary': 'node:1 [in_progress]',
                'runtime_summary_excerpt': 'node:1 phase=running',
                'perf_window_summary': 'perf samples=80; normal=18.5m throttled=1.5m; tq_wait max=5',
            }
        ],
    )
    assert 'Perf in stall window: perf samples=80' in lane.event_bundle_text


def test_perf_inspect_resource_tool_delegates_to_perf_report() -> None:
    spec = importlib.util.spec_from_file_location(
        'perf_inspect_tool',
        REPO_ROOT / 'tools' / 'perf_inspect_cn' / 'main' / 'tool.py',
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    seen: dict[str, Any] = {}

    class _Service:
        async def startup(self) -> None:
            return None

        def perf_report(self, *, mode: str, window_minutes: Any) -> str:
            seen['mode'] = mode
            seen['window_minutes'] = window_minutes
            return 'REPORT'

    handler = module._PerfInspectHandler(_Service())
    assert handler.name == 'perf_inspect'
    assert set(handler.parameters['properties']) == {'mode', 'window_minutes'}
    assert handler.parameters['required'] == ['mode']

    assert asyncio.run(handler.execute(mode='live', window_minutes=30)) == 'REPORT'
    assert seen == {'mode': 'live', 'window_minutes': 30}
