"""live.patch 单份最新快照 + actual-request 每节点一份 单测。

覆盖：
- store.write_task_live_snapshot 覆盖写单文件（latest.json.gz）、不写 task_events 行、
  磁盘记账按差值修正、event_history 关闭时完全不落盘；
- log_service 缓冲窗口聚合只留最新 payload、terminal/pause 立即冲刷；
- _persist_actual_request_artifact 每 (task, node) 只保留最新一份（旧文件+旧行删除）。
"""

from __future__ import annotations

import gzip
import json
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from main.models import TaskRecord
from main.storage import disk_guard
from main.storage.disk_guard import configure_disk_policies, disk_policies
from main.storage.sqlite_store import SQLiteTaskStore


def _task_record(task_id: str = 'task:t1', status: str = 'in_progress', **kwargs) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        title='t',
        user_request='r',
        root_node_id='node:root',
        status=status,
        created_at='2026-09-09T00:00:00+08:00',
        updated_at='2026-09-09T00:00:00+08:00',
        **kwargs,
    )


@pytest.fixture()
def policies_guard():
    previous = disk_guard.disk_policies()
    yield
    configure_disk_policies(previous)
    disk_guard.invalidate_disk_usage_cache()


def _make_log_service(store, *, window_ms: int = 1000, history_enabled: bool = True):
    from main.monitoring.log_service import TaskLogService

    service = TaskLogService.__new__(TaskLogService)
    service._store = store
    service._event_history_enabled = history_enabled
    service._live_patch_persist_window_ms = window_ms
    service._live_patch_history_guard = threading.Lock()
    service._pending_live_patch_history = {}
    service._live_patch_history_timers = {}
    service._event_write_failures = 0
    service._last_event_failure_warn_mono = None
    return service


# ------------------------------------------------------------------
# store.write_task_live_snapshot
# ------------------------------------------------------------------


def test_snapshot_overwrites_single_file_and_skips_db_rows(tmp_path, policies_guard):
    configure_disk_policies(disk_guard.DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3', event_history_enabled=True)
    try:
        assert store.write_task_live_snapshot('task:t1', json.dumps({'frame': {'phase': 'p1'}}))
        task_dir = store._event_history_dir / 'task_t1'
        first = task_dir / 'latest.json.gz'
        assert first.exists()
        first_bytes = first.stat().st_size
        assert store.get_task_disk_usages(['task:t1']).get('task:t1', 0) == first_bytes

        # 覆盖写：目录里永远只有一个 latest.json.gz，无 .tmp 残留
        assert store.write_task_live_snapshot('task:t1', json.dumps({'frame': {'phase': 'p2'}, 'pad': 'x' * 4000}))
        names = sorted(item.name for item in task_dir.iterdir())
        assert names == ['latest.json.gz']
        with gzip.open(first, 'rt', encoding='utf-8') as handle:
            assert json.loads(handle.read())['frame']['phase'] == 'p2'
        # 记账按差值修正（变大了）
        assert store.get_task_disk_usages(['task:t1'])['task:t1'] == first.stat().st_size

        # 不写 task_events 行
        assert store.list_task_events(task_id='task:t1', limit=10) == []
    finally:
        store.close()


def test_snapshot_disabled_or_invalid_inputs(tmp_path, policies_guard):
    configure_disk_policies(disk_guard.DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3', event_history_enabled=False)
    try:
        assert not store.write_task_live_snapshot('task:t1', json.dumps({'frame': {}}))
        assert not (store._event_history_dir / 'task_t1').exists()
    finally:
        store.close()
    enabled = SQLiteTaskStore(tmp_path / 'runtime2.sqlite3', event_history_enabled=True)
    try:
        assert not enabled.write_task_live_snapshot('', '{}')
        assert not enabled.write_task_live_snapshot('task:t1', '')
    finally:
        enabled.close()


# ------------------------------------------------------------------
# log_service 缓冲窗口
# ------------------------------------------------------------------


def test_buffer_window_coalesces_to_latest_payload(tmp_path, policies_guard):
    configure_disk_policies(disk_guard.DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3', event_history_enabled=True)
    try:
        service = _make_log_service(store, window_ms=60_000)  # 大窗口：手动冲刷
        task = _task_record(status='in_progress')
        for phase in ('p1', 'p2', 'p3'):
            MethodType(type(service)._buffer_task_live_patch_locked, service)(
                task=task, payload={'frame': {'phase': phase}},
            )
        with service._live_patch_history_guard:
            assert set(service._pending_live_patch_history) == {'task:t1'}
            timer = service._live_patch_history_timers.get('task:t1')
        assert timer is not None
        service.flush_live_patch_history('task:t1')
        snapshot = store._event_history_dir / 'task_t1' / 'latest.json.gz'
        with gzip.open(snapshot, 'rt', encoding='utf-8') as handle:
            assert json.loads(handle.read())['frame']['phase'] == 'p3'  # 只留最新
    finally:
        if timer is not None:
            timer.cancel()
        store.close()


def test_buffer_terminal_and_pause_flush_immediately(tmp_path, policies_guard):
    configure_disk_policies(disk_guard.DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3', event_history_enabled=True)
    try:
        service = _make_log_service(store, window_ms=60_000)
        terminal = _task_record(task_id='task:done', status='success')
        MethodType(type(service)._buffer_task_live_patch_locked, service)(
            task=terminal, payload={'frame': {'phase': 'final'}},
        )
        snapshot = store._event_history_dir / 'task_done' / 'latest.json.gz'
        assert snapshot.exists()  # 终态立即落盘，不等窗口
        with service._live_patch_history_guard:
            assert 'task:done' not in service._pending_live_patch_history
            assert 'task:done' not in service._live_patch_history_timers

        paused = _task_record(task_id='task:paused', status='in_progress', is_paused=True)
        MethodType(type(service)._buffer_task_live_patch_locked, service)(
            task=paused, payload={'frame': {'phase': 'paused'}},
        )
        assert (store._event_history_dir / 'task_paused' / 'latest.json.gz').exists()
    finally:
        store.close()


def test_buffer_disabled_persists_nothing(tmp_path, policies_guard):
    configure_disk_policies(disk_guard.DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3', event_history_enabled=True)
    try:
        service = _make_log_service(store, window_ms=1000, history_enabled=False)
        MethodType(type(service)._buffer_task_live_patch_locked, service)(
            task=_task_record(), payload={'frame': {'phase': 'p1'}},
        )
        with service._live_patch_history_guard:
            assert not service._pending_live_patch_history
            assert not service._live_patch_history_timers
        assert not store._event_history_dir.exists() or not any(store._event_history_dir.rglob('*'))
    finally:
        store.close()


# ------------------------------------------------------------------
# actual-request 每节点只留最新一份
# ------------------------------------------------------------------


def test_actual_request_keeps_latest_per_node(tmp_path, monkeypatch, policies_guard):
    import main.monitoring.log_service as log_service_module
    from main.monitoring.log_service import TaskLogService

    configure_disk_policies(disk_guard.DiskPolicies())
    monkeypatch.setattr(log_service_module, 'has_emergency_disk_budget', lambda _paths, **_kw: True)

    artifact_dir = tmp_path / 'artifacts'
    artifact_dir.mkdir()

    class _SpyArtifactStore:
        _artifact_dir = artifact_dir

        def __init__(self):
            self.registry: dict[str, SimpleNamespace] = {}
            self._content_index: dict = {}
            self._counter = 0

        def create_json_artifact(self, **kwargs):
            self._counter += 1
            artifact_id = f'artifact:ar{self._counter}'
            path = artifact_dir / f'{artifact_id.split(":")[-1]}.json'
            path.write_text(json.dumps(kwargs.get('payload') or {}), encoding='utf-8')
            record = SimpleNamespace(
                artifact_id=artifact_id,
                task_id=kwargs.get('task_id'),
                node_id=kwargs.get('node_id'),
                kind=kwargs.get('kind'),
                path=str(path),
            )
            self.registry[artifact_id] = record
            return record

        def list_artifacts(self, task_id):
            return [item for item in self.registry.values() if item.task_id == task_id]

    spy = _SpyArtifactStore()

    class _StubStore:
        def delete_artifacts_by_ids(self, ids):
            removed = 0
            for artifact_id in ids:
                if self.registry_pop(artifact_id):
                    removed += 1
            return removed

        def registry_pop(self, artifact_id):
            return spy.registry.pop(artifact_id, None) is not None

    service = TaskLogService.__new__(TaskLogService)
    service._content_store = SimpleNamespace(_artifact_store=spy)
    service._store = _StubStore()
    persist = MethodType(TaskLogService._persist_actual_request_artifact, service)

    payload = {'messages': [{'role': 'user', 'content': 'x' * 100}]}
    ref1 = persist(task_id='task:t1', node_id='node:n1', call_index=0, payload=payload)
    ref2 = persist(task_id='task:t1', node_id='node:n1', call_index=1, payload=payload)
    assert ref1 and ref2 and ref1 != ref2
    # 同节点只留最新一份：旧文件已删、registry 只剩 ref2
    assert set(spy.registry) == {ref2}
    assert not (artifact_dir / 'ar1.json').exists()
    assert (artifact_dir / 'ar2.json').exists()

    # 不同节点各自保留最新一份
    persist(task_id='task:t1', node_id='node:n2', call_index=0, payload=payload)
    assert len(spy.registry) == 2
    assert {item.node_id for item in spy.registry.values()} == {'node:n1', 'node:n2'}


# ------------------------------------------------------------------
# live.patch 热路径：内容未变不推送、缓冲不再整包深拷贝
# ------------------------------------------------------------------


class _NoChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f'chat backend must not be used here: {kwargs!r}')


def _live_service(tmp_path):
    from main.service.runtime_service import MainRuntimeService

    return MainRuntimeService(
        chat_backend=_NoChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
        execution_model_refs=['fake'],
        acceptance_model_refs=['fake'],
    )


@pytest.mark.asyncio
async def test_frame_rewrite_with_same_content_pushes_no_live_patch(tmp_path):
    """内容指纹没变的 update_frame 不再组装全任务摘要往外推。

    实盘 135 帧任务上一次推送要 ~174ms / ~18MB，而 await 标记这类写点会反复
    交回同一个值。
    """
    service = _live_service(tmp_path)
    events: list[dict] = []
    service.log_service.add_live_snapshot_publisher(
        lambda task, envelope, immediate: events.append(envelope)
    )
    record = await service.create_task('dedupe live patch', session_id='web:ceo-dedupe')
    task_id = record.task_id
    node_id = record.root_node_id

    service.log_service.upsert_frame(
        task_id,
        {'node_id': node_id, 'phase': 'before_model', 'await_marker': 'context_preparer'},
        publish_snapshot=True,
    )
    assert len(events) == 1

    service.log_service.update_frame(
        task_id,
        node_id,
        lambda frame: {**frame, 'phase': 'before_model', 'await_marker': 'context_preparer'},
        publish_snapshot=True,
    )
    assert len(events) == 1, '同值重写不应再推一份 live.patch'

    service.log_service.update_frame(
        task_id,
        node_id,
        lambda frame: {**frame, 'await_marker': 'react_loop.run'},
        publish_snapshot=True,
    )
    assert len(events) == 2, '内容真变了必须推'


@pytest.mark.asyncio
async def test_frame_writers_do_not_build_runtime_state(tmp_path):
    """帧写路径不再返回/构造整份 runtime state（135 帧实测 233ms/次，且无读者）。"""
    service = _live_service(tmp_path)
    reads: list[str] = []
    real_read = type(service.log_service).read_runtime_state

    def _counting_read(task_id: str):
        reads.append(task_id)
        return real_read(service.log_service, task_id)

    service.log_service.read_runtime_state = _counting_read
    record = await service.create_task('no state read', session_id='web:ceo-noread')
    task_id = record.task_id
    node_id = record.root_node_id

    service.log_service.upsert_frame(task_id, {'node_id': node_id, 'phase': 'before_model'}, publish_snapshot=True)
    service.log_service.update_frame(
        task_id, node_id, lambda frame: {**frame, 'phase': 'after_model'}, publish_snapshot=True,
    )
    service.log_service.replace_runtime_frames(
        task_id, frames=[{'node_id': node_id, 'phase': 'before_model'}],
        active_node_ids=[node_id], publish_snapshot=True,
    )
    service.log_service.remove_frame(task_id, node_id, publish_snapshot=True)
    assert reads == [], '帧写点不该再水合全任务状态'


@pytest.mark.asyncio
async def test_live_patch_buffer_holds_dispatched_payload_by_reference(tmp_path):
    """缓冲与推送共用同一份 payload：整包 deepcopy 已去掉（每次 ~120ms/~18MB）。"""
    configure_disk_policies(disk_guard.DiskPolicies())
    service = _live_service(tmp_path)
    envelopes: list[dict] = []
    service.log_service.add_live_snapshot_publisher(
        lambda task, envelope, immediate: envelopes.append(envelope)
    )
    record = await service.create_task('shared payload', session_id='web:ceo-share')
    service.log_service.upsert_frame(
        record.task_id,
        {'node_id': record.root_node_id, 'phase': 'before_model'},
        publish_snapshot=True,
    )
    with service.log_service._live_patch_history_guard:
        entry = service.log_service._pending_live_patch_history.get(record.task_id)
    assert isinstance(entry, dict)
    assert entry['payload']['frame'] is envelopes[-1]['data']['frame'], '缓冲应持引用而不是深拷贝'
    with service.log_service._live_patch_history_guard:
        timers = list(service.log_service._live_patch_history_timers.values())
    for timer in timers:
        timer.cancel()
    service.log_service._pending_live_patch_history.pop(record.task_id, None)
