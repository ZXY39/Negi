"""P0 磁盘治理止血包单测。

覆盖：DiskFullError 分类、_run_write 包装与失败计数、artifact 超限 gzip 往返、
阈值边界、去重快路径、终态清理保留清单与后台清理体、live patch flush 有界重试、
node_runner 错误记录写保护、应急写预算预检、旧 payload_json 行兼容。
"""

from __future__ import annotations

import errno
import gzip
import json
import sqlite3
import threading
from collections import deque
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from main.models import TaskArtifactRecord, TaskRecord
from main.storage import disk_guard
from main.storage.artifact_store import TaskArtifactStore, read_artifact_text
from main.storage.disk_guard import (
    DiskFullError,
    DiskPolicies,
    classify_write_error,
    configure_disk_policies,
    is_disk_full_error,
)
from main.storage.sqlite_store import SQLiteTaskStore


def _sqlite_full_error() -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError('database or disk is full')
    try:
        exc.sqlite_errorname = 'SQLITE_FULL'
    except Exception:
        pass
    return exc


def _task_record(task_id: str = 'task:t1', status: str = 'success', final_output_ref: str = '') -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        title='t',
        user_request='r',
        root_node_id='node:root',
        status=status,
        created_at='2026-09-09T00:00:00+08:00',
        updated_at='2026-09-09T00:00:00+08:00',
        final_output_ref=final_output_ref,
    )


@pytest.fixture()
def policies_guard():
    previous = disk_guard.disk_policies()
    yield
    configure_disk_policies(previous)
    disk_guard.invalidate_disk_usage_cache()


# 1. DiskFullError 分类 ×3


def test_is_disk_full_error_classification():
    assert is_disk_full_error(_sqlite_full_error())
    assert is_disk_full_error(OSError(errno.ENOSPC, 'No space left on device'))
    # 无 sqlite_errorname 属性时按消息兜底
    assert is_disk_full_error(sqlite3.OperationalError('database or disk is full'))
    assert not is_disk_full_error(ValueError('boom'))
    assert not is_disk_full_error(None)
    wrapped = classify_write_error(_sqlite_full_error())
    assert isinstance(wrapped, DiskFullError)
    assert isinstance(wrapped, OSError)  # 既有 except OSError 调用方兼容
    plain = ValueError('boom')
    assert classify_write_error(plain) is plain


# 2. _run_write 包装 + 失败计数


def test_run_write_wraps_disk_full_and_counts(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        def _boom(conn):
            raise _sqlite_full_error()

        with pytest.raises(DiskFullError):
            store._run_write(_boom)
        counts = store.write_failure_counts()
        assert counts.get('disk_full', 0) >= 1
        snapshot = store.runtime_metrics_snapshot()
        assert snapshot.get('write_failure_disk_full', 0.0) >= 1.0
    finally:
        store.close()


# 3+4+5. artifact gzip 往返 / json 超限 / 阈值边界


def test_text_artifact_gzip_roundtrip_and_dedup(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies(artifact_gzip_threshold_bytes=64))
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        content = 'x' * 200
        record = artifacts.create_text_artifact(
            task_id='task:t1', node_id='node:n1', kind='node_output', title='big', content=content,
        )
        assert record.content_encoding == 'gzip'
        assert record.path.endswith('.gz')
        assert record.size_bytes == len(content.encode('utf-8'))
        assert Path(record.path).exists()
        assert read_artifact_text(record) == content
        # 同内容二次写入 → 去重快路径（同 artifact_id，且不重复落盘）
        again = artifacts.create_text_artifact(
            task_id='task:t1', node_id='node:n1', kind='node_output', title='big', content=content,
        )
        assert again.artifact_id == record.artifact_id
        rows = store.list_artifacts('task:t1')
        assert len(rows) == 1
    finally:
        store.close()


def test_json_artifact_gzip(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies(artifact_gzip_threshold_bytes=32))
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        record = artifacts.create_json_artifact(
            task_id='task:t1', node_id=None, kind='task_actual_request', title='req',
            payload={'messages': [{'role': 'user', 'content': 'y' * 120}]},
        )
        assert record.content_encoding == 'gzip'
        restored = json.loads(read_artifact_text(record))
        assert restored['messages'][0]['content'] == 'y' * 120
    finally:
        store.close()


def test_threshold_boundary(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies(artifact_gzip_threshold_bytes=100))
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        exact = artifacts.create_text_artifact(
            task_id='task:t1', node_id=None, kind='node_output', title='exact', content='z' * 100,
        )
        assert exact.content_encoding == 'plain'
        assert not exact.path.endswith('.gz')
        over = artifacts.create_text_artifact(
            task_id='task:t1', node_id=None, kind='node_output', title='over', content='z' * 101,
        )
        assert over.content_encoding == 'gzip'
        # 阈值 <=0 关闭压缩
        configure_disk_policies(DiskPolicies(artifact_gzip_threshold_bytes=0))
        disabled = artifacts.create_text_artifact(
            task_id='task:t1', node_id=None, kind='node_output', title='disabled', content='z' * 5000,
        )
        assert disabled.content_encoding == 'plain'
    finally:
        store.close()


def test_singleton_replace_switches_encoding(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies(artifact_gzip_threshold_bytes=64))
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        small = artifacts.create_or_replace_singleton_text_artifact(
            task_id='task:t1', node_id='node:n1', kind='task_runtime_messages', title='frames', content='a' * 10,
        )
        assert small.content_encoding == 'plain'
        big = artifacts.create_or_replace_singleton_text_artifact(
            task_id='task:t1', node_id='node:n1', kind='task_runtime_messages', title='frames', content='b' * 200,
        )
        assert big.artifact_id == small.artifact_id
        assert big.content_encoding == 'gzip'
        assert big.path.endswith('.gz')
        assert read_artifact_text(big) == 'b' * 200
        assert not Path(small.path).exists()  # 旧 plain 文件被替换清理
        back = artifacts.create_or_replace_singleton_text_artifact(
            task_id='task:t1', node_id='node:n1', kind='task_runtime_messages', title='frames', content='c' * 10,
        )
        assert back.content_encoding == 'plain'
        assert not back.path.endswith('.gz')
        assert read_artifact_text(back) == 'c' * 10
    finally:
        store.close()


# 6. 终态清理：保留清单 + 后台清理体


def _bind_cleanup_harness(store, artifacts, tmp_path):
    """用 MethodType 把 MainRuntimeService 的清理函数绑到轻量 harness 上，
    避免实例化完整运行时服务。"""
    from main.service.runtime_service import MainRuntimeService

    task_temp_dir = tmp_path / 'temp' / 'tasks' / 'task_t1'
    task_temp_dir.mkdir(parents=True, exist_ok=True)
    (task_temp_dir / 'scratch.txt').write_text('scratch', encoding='utf-8')

    harness = SimpleNamespace()
    harness.store = store
    harness.artifact_store = artifacts
    harness.log_service = SimpleNamespace(
        append_task_event=lambda **_kwargs: 0,
        read_task_runtime_meta=lambda _task_id: {'task_temp_dir': str(task_temp_dir)},
    )
    harness.get_task = lambda task_id: store.get_task(task_id)
    harness.list_artifacts = lambda task_id: artifacts.list_artifacts(task_id)
    harness._effective_task_temp_dir = lambda task_id: task_temp_dir
    harness._terminal_artifact_keep_policy = MethodType(
        MainRuntimeService._terminal_artifact_keep_policy, harness,
    )
    harness._TERMINAL_CLEANUP_KEEP_TITLE_TOKENS = MainRuntimeService._TERMINAL_CLEANUP_KEEP_TITLE_TOKENS
    harness._run_terminal_intermediate_cleanup = MethodType(
        MainRuntimeService._run_terminal_intermediate_cleanup, harness,
    )
    return harness, task_temp_dir


def test_terminal_cleanup_keep_policy_and_run(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task(_task_record(final_output_ref='artifact:keep-ref'))
        artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        keep_patch = artifacts.create_text_artifact(
            task_id='task:t1', node_id=None, kind='patch', title='patch', content='diff',
        )
        keep_report = artifacts.create_text_artifact(
            task_id='task:t1', node_id=None, kind='node_output', title='Final Report v1', content='report body',
        )
        drop_request = artifacts.create_json_artifact(
            task_id='task:t1', node_id=None, kind='task_actual_request', title='node-actual-request:n:0',
            payload={'messages': ['m']},
        )
        drop_tool = artifacts.create_text_artifact(
            task_id='task:t1', node_id=None, kind='tool_result:shell', title='tool output', content='o' * 10,
        )
        drop_trace = artifacts.create_text_artifact(
            task_id='task:t1', node_id=None, kind='task_execution_trace', title='trace', content='t' * 10,
        )

        harness, task_temp_dir = _bind_cleanup_harness(store, artifacts, tmp_path)
        task = store.get_task('task:t1')
        # 纯判定
        assert harness._terminal_artifact_keep_policy(task, keep_patch)
        assert harness._terminal_artifact_keep_policy(task, keep_report)
        assert not harness._terminal_artifact_keep_policy(task, drop_request)
        assert not harness._terminal_artifact_keep_policy(task, drop_tool)
        assert not harness._terminal_artifact_keep_policy(task, drop_trace)
        # final_output_ref 指向的保留
        ref_record = TaskArtifactRecord(
            artifact_id='keep-ref', task_id='task:t1', kind='node_output',
            title='whatever', path='', created_at='',
        )
        assert harness._terminal_artifact_keep_policy(task, ref_record)

        # 后台清理体
        harness._run_terminal_intermediate_cleanup('task:t1')
        remaining = {row.artifact_id for row in store.list_artifacts('task:t1')}
        assert remaining == {keep_patch.artifact_id, keep_report.artifact_id}
        assert not Path(drop_request.path).exists()
        assert not Path(drop_tool.path).exists()
        assert not Path(drop_trace.path).exists()
        assert Path(keep_patch.path).exists()
        assert not task_temp_dir.exists()  # temp/tasks 草稿目录硬删
    finally:
        store.close()


# 7. live patch flush 有界重试


def _make_log_service_harness(writer):
    from main.monitoring.log_service import TaskLogService

    service = TaskLogService.__new__(TaskLogService)
    service._event_writer = writer
    service._live_patch_history_guard = threading.Lock()
    service._pending_live_patch_history = {}
    service._live_patch_history_timers = {}
    service._last_live_patch_boundary_key = {}
    service._event_write_failures = 0
    service._live_patch_retry_counts = {}
    service._live_patch_dropped_events = 0
    return service


def test_flush_live_patch_retries_then_succeeds():
    calls = {'n': 0}

    class _Writer:
        def append_task_event(self, **_kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError(errno.ENOSPC, 'No space left on device')
            return 7

    service = _make_log_service_harness(_Writer())
    task = _task_record(status='in_progress')
    payload = {'runtime_summary': {}, 'frame': {}}
    with service._live_patch_history_guard:
        service._pending_live_patch_history['task:t1'] = {'task': task, 'payload': payload}
    service.flush_live_patch_history('task:t1')  # 第一次：写失败 → 重挂
    assert service._event_write_failures == 1
    assert service._live_patch_retry_counts.get('task:t1') == 1
    retry_timer = service._live_patch_history_timers.pop('task:t1', None)
    if retry_timer is not None:
        retry_timer.cancel()
    with service._live_patch_history_guard:
        assert 'task:t1' in service._pending_live_patch_history  # entry 已重新入队
    service.flush_live_patch_history('task:t1')  # 第二次：成功
    assert calls['n'] == 2
    assert 'task:t1' not in service._live_patch_retry_counts
    assert service._live_patch_dropped_events == 0


def test_flush_live_patch_drops_after_three_retries():
    class _AlwaysFailWriter:
        def append_task_event(self, **_kwargs):
            raise OSError(errno.ENOSPC, 'No space left on device')

    service = _make_log_service_harness(_AlwaysFailWriter())
    task = _task_record(status='in_progress')
    with service._live_patch_history_guard:
        service._pending_live_patch_history['task:t1'] = {'task': task, 'payload': {'frame': {}}}
    for _round in range(4):
        service.flush_live_patch_history('task:t1')
        timer = service._live_patch_history_timers.pop('task:t1', None)
        if timer is not None:
            timer.cancel()
    assert service._live_patch_dropped_events == 1
    assert 'task:t1' not in service._live_patch_retry_counts


# 8. node_runner 错误记录写保护


def test_persist_error_and_pause_best_effort_survives_disk_full():
    from main.runtime.node_runner import NodeRunner

    runner = NodeRunner.__new__(NodeRunner)
    runner._unpersisted_error_logs = deque(maxlen=64)

    class _LogService:
        def append_task_error_log(self, *_args, **_kwargs):
            raise DiskFullError('database or disk is full')

    runner._log_service = _LogService()

    def _pause_boom(*_args, **_kwargs):
        raise DiskFullError('database or disk is full')

    runner._mark_node_paused = _pause_boom
    # 两个写点全炸也必须不外抛
    runner._persist_error_and_pause_best_effort(
        task_id='task:t1', node_id='node:n1', text='boom text', node_goal='goal',
    )
    assert list(runner._unpersisted_error_logs) == [('task:t1', 'node:n1', 'boom text')]


# 9. 应急写预算：无预算时 actual_request 只写 minimal


def test_actual_request_skips_full_payload_without_budget(tmp_path, monkeypatch):
    import main.monitoring.log_service as log_service_module

    service = _make_log_service_harness(SimpleNamespace(append_task_event=lambda **_kwargs: 0))
    calls: list[dict] = []

    class _SpyArtifactStore:
        _artifact_dir = tmp_path / 'artifacts'

        def create_json_artifact(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(artifact_id=f'artifact:spy{len(calls)}')

    service._content_store = SimpleNamespace(_artifact_store=_SpyArtifactStore())
    monkeypatch.setattr(log_service_module, 'has_emergency_disk_budget', lambda _paths, **_kw: False)
    from main.monitoring.log_service import TaskLogService

    persist = MethodType(TaskLogService._persist_actual_request_artifact, service)
    ref = persist(task_id='task:t1', node_id='node:n1', call_index=0, payload={'messages': [{'role': 'user'}]})
    assert ref  # minimal 仍产出引用
    assert len(calls) == 1  # full/degraded 均被跳过
    assert 'disk-emergency minimal' in str(calls[0].get('preview_text', ''))


# 10. 旧 payload_json 行兼容（零迁移）


def test_legacy_artifact_row_defaults(tmp_path):
    legacy_payload = {
        'artifact_id': 'artifact:legacy',
        'task_id': 'task:t1',
        'node_id': None,
        'kind': 'node_output',
        'title': 'legacy',
        'path': '',
        'mime_type': 'text/markdown',
        'preview_text': '',
        'created_at': '2026-09-01T00:00:00+08:00',
    }
    record = TaskArtifactRecord.model_validate_json(json.dumps(legacy_payload))
    assert record.size_bytes == 0
    assert record.content_encoding == 'plain'
    assert record.content_hash == ''
    # 旧行去重兜底：content_hash 为空 → 回读文件比对（plain 路径）
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        artifacts = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
        content = 'legacy content'
        first = artifacts.create_text_artifact(
            task_id='task:t2', node_id=None, kind='node_output', title='legacy2', content=content,
        )
        # 模拟旧行：清空 content_hash 后仍能命中去重（回读比对路径）
        legacy_like = first.model_copy(update={'content_hash': ''})
        assert artifacts._artifact_matches_content(legacy_like, content=content,
                                                   content_hash=first.content_hash)
    finally:
        store.close()


def test_read_artifact_text_gzip_and_missing(tmp_path):
    missing = TaskArtifactRecord(
        artifact_id='a', task_id='t', kind='k', title='t',
        path=str(tmp_path / 'nope.md'), created_at='',
    )
    assert read_artifact_text(missing) == ''
    gz_path = tmp_path / 'file.md.gz'
    with gzip.open(gz_path, 'wt', encoding='utf-8') as handle:
        handle.write('压缩内容')
    gz_record = TaskArtifactRecord(
        artifact_id='b', task_id='t', kind='k', title='t',
        path=str(gz_path), created_at='', content_encoding='gzip',
    )
    assert read_artifact_text(gz_record) == '压缩内容'
    # 无 content_encoding 字段时按 .gz 后缀推断
    inferred = gz_record.model_copy(update={'content_encoding': ''})
    assert read_artifact_text(inferred) == '压缩内容'
