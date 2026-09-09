"""P2 磁盘治理单测：任务级 zip 归档、解压预检、中断恢复、读端回退、压缩渐进候选。"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

import main.service.runtime_service as runtime_service_module
from main.models import TaskArtifactRecord, TaskRecord
from main.storage import disk_guard
from main.storage.artifact_store import read_artifact_text, set_task_archiver
from main.storage.disk_guard import DiskPolicies, configure_disk_policies
from main.storage.sqlite_store import SQLiteTaskStore
from main.storage.task_archive import TaskArchiver

GB = 1024 ** 3


@pytest.fixture()
def policies_guard():
    previous = disk_guard.disk_policies()
    yield
    configure_disk_policies(previous)
    disk_guard.invalidate_disk_usage_cache()


@pytest.fixture()
def archiver_guard():
    yield
    set_task_archiver(None)


def _task_record(task_id='task:t1', status='success', metadata=None, is_paused=False, **kwargs) -> TaskRecord:
    now = datetime.now(timezone.utc)
    base = dict(
        task_id=task_id,
        title='t',
        user_request='r',
        root_node_id='node:root',
        status=status,
        created_at=(now - timedelta(days=2)).isoformat(timespec='seconds'),
        updated_at=(now - timedelta(days=1)).isoformat(timespec='seconds'),
        finished_at=(now - timedelta(days=1)).isoformat(timespec='seconds'),
        is_paused=is_paused,
        metadata=dict(metadata or {}),
    )
    base.update(kwargs)
    return TaskRecord(**base)


def _make_task_dirs(root: Path) -> dict[str, Path]:
    dirs = {
        'artifacts': root / 'artifacts' / 'task_t1',
        'event-history': root / 'event-history' / 'task_t1',
        'files': root / 'files' / 'task_t1',
        'temp': root / 'temp' / 'tasks' / 'task_t1',
    }
    (dirs['artifacts']).mkdir(parents=True, exist_ok=True)
    (dirs['artifacts'] / 'a.md').write_text('artifact content ' * 100, encoding='utf-8')
    (dirs['artifacts'] / 'b.json').write_text(json.dumps({'k': 'v' * 500}), encoding='utf-8')
    (dirs['event-history']).mkdir(parents=True, exist_ok=True)
    with gzip.open(dirs['event-history'] / '12.json.gz', 'wt', encoding='utf-8') as handle:
        handle.write(json.dumps({'seq': 12}))
    (dirs['files']).mkdir(parents=True, exist_ok=True)
    (dirs['files'] / 'nested').mkdir(parents=True, exist_ok=True)
    (dirs['files'] / 'nested' / 'deep.txt').write_text('deep', encoding='utf-8')
    (dirs['temp']).mkdir(parents=True, exist_ok=True)
    (dirs['temp'] / 'scratch.log').write_text('scratch', encoding='utf-8')
    return dirs


# --- zip 往返 ---


def test_archive_roundtrip(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    dirs = _make_task_dirs(tmp_path)
    archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
    result = archiver.archive('task:t1', dirs)
    assert result is not None
    assert result.file_count == 5
    assert result.uncompressed_bytes > 0
    assert result.compressed_bytes < result.uncompressed_bytes  # 文本压缩有效
    zip_path = Path(result.archive_path)
    assert zip_path.is_file()
    # 源目录删空（提交点之后逐文件删源）
    for path in dirs.values():
        assert not path.exists() or not any(path.rglob('*'))
    # manifest
    manifest = archiver.read_manifest('task:t1')
    assert manifest['file_count'] == 5
    assert manifest['uncompressed_bytes'] == result.uncompressed_bytes
    # 解压还原
    assert archiver.decompress('task:t1', dirs) is True
    assert (dirs['artifacts'] / 'a.md').read_text(encoding='utf-8') == 'artifact content ' * 100
    assert json.loads((dirs['artifacts'] / 'b.json').read_text(encoding='utf-8'))['k'] == 'v' * 500
    assert (dirs['files'] / 'nested' / 'deep.txt').read_text(encoding='utf-8') == 'deep'
    assert (dirs['temp'] / 'scratch.log').read_text(encoding='utf-8') == 'scratch'
    with gzip.open(dirs['event-history'] / '12.json.gz', 'rt', encoding='utf-8') as handle:
        assert json.loads(handle.read()) == {'seq': 12}
    assert not zip_path.exists()  # 解压成功后删 zip
    # 幂等：无归档再解压返回 True
    assert archiver.decompress('task:t1', dirs) is True


def test_decompress_precheck_rejects_without_space(tmp_path, monkeypatch, policies_guard):
    configure_disk_policies(DiskPolicies())
    dirs = _make_task_dirs(tmp_path)
    archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
    result = archiver.archive('task:t1', dirs)
    assert result is not None
    # 剩余空间 < uncompressed×1.2 → 拒绝解压
    monkeypatch.setattr(
        'main.storage.task_archive.disk_waterline_snapshot',
        lambda paths: (int(result.uncompressed_bytes * 0.5), 100 * GB),
    )
    assert archiver.decompress('task:t1', dirs) is False
    assert Path(result.archive_path).is_file()  # zip 保留，未破坏
    # 空间足够 → 成功
    monkeypatch.setattr(
        'main.storage.task_archive.disk_waterline_snapshot',
        lambda paths: (int(result.uncompressed_bytes * 5), 100 * GB),
    )
    assert archiver.decompress('task:t1', dirs) is True


def test_recover_interrupted(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    dirs = _make_task_dirs(tmp_path)
    archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
    result = archiver.archive('task:t1', dirs)
    assert result is not None
    # 模拟"zip 已写完、源删到一半崩溃"：手工恢复一个源文件
    dirs['artifacts'].mkdir(parents=True, exist_ok=True)
    (dirs['artifacts'] / 'a.md').write_text('artifact content ' * 100, encoding='utf-8')
    assert archiver.recover_interrupted('task:t1', dirs) == 'completed'
    assert not (dirs['artifacts'] / 'a.md').exists()
    # 无效 zip → 回滚（删 zip 保源）
    bad = archiver.archive_path_for('task:t2')
    bad.write_bytes(b'not a zip')
    dirs2 = {'artifacts': tmp_path / 'artifacts2'}
    dirs2['artifacts'].mkdir(parents=True, exist_ok=True)
    (dirs2['artifacts'] / 'keep.md').write_text('keep', encoding='utf-8')
    assert archiver.recover_interrupted('task:t2', dirs2) == 'rolled_back'
    assert not bad.exists()
    assert (dirs2['artifacts'] / 'keep.md').read_text(encoding='utf-8') == 'keep'
    assert archiver.recover_interrupted('task:t3', dirs2) == 'none'


def test_archive_lock_blocks_concurrent(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    dirs = _make_task_dirs(tmp_path)
    archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
    lock = archiver._acquire_lock('task:t1')
    assert lock is not None
    try:
        assert archiver.archive('task:t1', dirs) is None  # 锁被占 → 跳过，源完好
        assert (dirs['artifacts'] / 'a.md').exists()
    finally:
        archiver._release_lock(lock)
    assert archiver.archive('task:t1', dirs) is not None  # 释放后可压


def test_archive_skips_when_no_sources(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
    assert archiver.archive('task:t1', {'artifacts': tmp_path / 'missing'}) is None


# --- 读端归档回退 ---


def test_read_artifact_text_archive_fallback(tmp_path, policies_guard, archiver_guard):
    configure_disk_policies(DiskPolicies())
    dirs = {'artifacts': tmp_path / 'artifacts' / 'task_t1'}
    dirs['artifacts'].mkdir(parents=True)
    (dirs['artifacts'] / 'plain.md').write_text('plain member', encoding='utf-8')
    gz_bytes = gzip.compress('gz member'.encode('utf-8'))
    (dirs['artifacts'] / 'big.md.gz').write_bytes(gz_bytes)
    archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
    result = archiver.archive('task:t1', dirs)
    assert result is not None
    set_task_archiver(archiver)
    missing_plain = TaskArtifactRecord(
        artifact_id='a1', task_id='task:t1', kind='node_output', title='t',
        path=str(dirs['artifacts'] / 'plain.md'), created_at='',
    )
    assert read_artifact_text(missing_plain) == 'plain member'
    missing_gz = TaskArtifactRecord(
        artifact_id='a2', task_id='task:t1', kind='node_output', title='t',
        path=str(dirs['artifacts'] / 'big.md.gz'), created_at='', content_encoding='gzip',
    )
    assert read_artifact_text(missing_gz) == 'gz member'
    # 未归档任务的缺失文件仍返回 ''
    other = TaskArtifactRecord(
        artifact_id='a3', task_id='task:t2', kind='node_output', title='t',
        path=str(tmp_path / 'nope.md'), created_at='',
    )
    assert read_artifact_text(other) == ''


# --- 压缩渐进候选与服务层 ---


def _bind_service_harness(store, archiver, dirs_by_task):
    harness = SimpleNamespace()
    harness.store = store
    harness.task_archiver = archiver
    harness.log_service = SimpleNamespace(append_task_event=lambda **_kw: 0)
    harness.get_task = lambda task_id: store.get_task(task_id)
    harness.normalize_task_id = lambda task_id: str(task_id or '').strip()
    harness._task_archive_dirs = lambda task_id: dirs_by_task.get(task_id, {})
    harness._task_disk_usage_bytes = lambda task_id: sum(
        int(p.stat().st_size)
        for d in dirs_by_task.get(task_id, {}).values()
        for p in Path(d).rglob('*') if p.is_file()
    ) if task_id in dirs_by_task else 0
    harness._publish_task_archive_state_changed = MethodType(
        runtime_service_module.MainRuntimeService._publish_task_archive_state_changed, harness,
    )
    harness._query_archive_candidates = MethodType(
        runtime_service_module.MainRuntimeService._query_archive_candidates, harness,
    )
    harness._archive_one_task = MethodType(
        runtime_service_module.MainRuntimeService._archive_one_task, harness,
    )
    harness._decompress_one_task = MethodType(
        runtime_service_module.MainRuntimeService._decompress_one_task, harness,
    )
    harness._ensure_task_decompressed = MethodType(
        runtime_service_module.MainRuntimeService._ensure_task_decompressed, harness,
    )
    harness._reconcile_task_disk_usage = lambda task_id: None
    harness._decompress_inflight = set()
    harness._parse_task_timestamp = runtime_service_module.MainRuntimeService._parse_task_timestamp
    return harness


def test_query_archive_candidates_ordering_and_filters(tmp_path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        # 大且老 / 小且新 / pinned / archived / 运行中 / 暂停中
        store.upsert_task(_task_record('task:big_old'))
        store.upsert_task(_task_record('task:small_new', finished_at=datetime.now(timezone.utc).isoformat(timespec='seconds')))
        store.upsert_task(_task_record('task:pinned', metadata={'pinned': True}))
        store.upsert_task(_task_record('task:archived', metadata={'archived_at': '2026-09-01T00:00:00+00:00'}))
        store.upsert_task(_task_record('task:active', status='in_progress'))
        store.upsert_task(_task_record('task:paused', status='in_progress', is_paused=True))
        store.upsert_task_disk_usage('task:big_old', 500 * 1024 * 1024)
        store.upsert_task_disk_usage('task:small_new', 1024)
        store.upsert_task_disk_usage('task:paused', 100 * 1024 * 1024)
        harness = _bind_service_harness(store, None, {})
        candidates = harness._query_archive_candidates(batch=10)
        assert candidates[0] == 'task:big_old'          # size×age 加权第一
        assert 'task:paused' in candidates               # 暂停任务参与
        assert 'task:pinned' not in candidates
        assert 'task:archived' not in candidates
        assert 'task:active' not in candidates
    finally:
        store.close()


async def test_archive_one_task_updates_metadata_and_usage(tmp_path, monkeypatch, policies_guard):
    configure_disk_policies(DiskPolicies())
    monkeypatch.setattr(
        runtime_service_module, 'disk_waterline_snapshot',
        lambda paths: (100 * GB, 1000 * GB),
    )
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task(_task_record('task:t1'))
        dirs = _make_task_dirs(tmp_path)
        archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
        harness = _bind_service_harness(store, archiver, {'task:t1': dirs})
        result = await harness._archive_one_task('task:t1', reason='disk_cleanup')
        assert result['result'] == 'archived'
        task = store.get_task('task:t1')
        assert task.metadata.get('archived_at')
        assert int(task.metadata.get('archived_bytes') or 0) == result['uncompressed_bytes']
        # 占用记账切换为压缩后大小
        assert store.get_task_disk_usages(['task:t1'])['task:t1'] == result['compressed_bytes']
        # 重复压缩 → already_archived
        again = await harness._archive_one_task('task:t1', reason='manual')
        assert again['result'] == 'already_archived'
        # 运行中任务 → active_skipped；pinned → pinned_skipped
        store.upsert_task(_task_record('task:active', status='in_progress'))
        active = await harness._archive_one_task('task:active', reason='manual')
        assert active['result'] == 'active_skipped'
        store.upsert_task(_task_record('task:pinned', metadata={'pinned': True}))
        pinned = await harness._archive_one_task('task:pinned', reason='manual')
        assert pinned['result'] == 'pinned_skipped'
        # 解压回退：metadata 清空 + 文件还原
        dec = await harness._decompress_one_task('task:t1')
        assert dec['result'] == 'decompressed'
        task = store.get_task('task:t1')
        assert not task.metadata.get('archived_at')
        # 解压宽限：成功解压写 decompressed_at（窗口内不被压缩渐进重新归档）
        assert task.metadata.get('decompressed_at')
        assert (dirs['artifacts'] / 'a.md').exists()
        # _ensure_task_decompressed 幂等
        assert await harness._ensure_task_decompressed('task:t1') is True
    finally:
        store.close()


def test_decompress_grace_blocks_rearchive(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())  # 默认宽限 60 分钟
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        now = datetime.now(timezone.utc)
        fresh = (now - timedelta(minutes=5)).isoformat(timespec='seconds')
        expired = (now - timedelta(hours=3)).isoformat(timespec='seconds')
        store.upsert_task(_task_record('task:grace', metadata={'decompressed_at': fresh}))
        store.upsert_task(_task_record('task:expired', metadata={'decompressed_at': expired}))
        store.upsert_task(_task_record('task:plain'))
        for tid in ('task:grace', 'task:expired', 'task:plain'):
            store.upsert_task_disk_usage(tid, 1000)
        harness = _bind_service_harness(store, None, {})
        candidates = harness._query_archive_candidates(batch=10)
        assert 'task:grace' not in candidates       # 宽限期内豁免
        assert 'task:expired' in candidates         # 宽限过期，磁盘压力照常
        assert 'task:plain' in candidates
        # 宽限关闭 → 解压后立即可被重新压缩
        configure_disk_policies(DiskPolicies(decompress_grace_minutes=0))
        candidates = harness._query_archive_candidates(batch=10)
        assert 'task:grace' in candidates
    finally:
        store.close()


async def test_manual_compress_ignores_grace(tmp_path, monkeypatch, policies_guard):
    configure_disk_policies(DiskPolicies())
    monkeypatch.setattr(
        runtime_service_module, 'disk_waterline_snapshot',
        lambda paths: (100 * GB, 1000 * GB),
    )
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        # 宽限期只挡自动压缩渐进；用户手动压缩不受限
        store.upsert_task(_task_record(
            'task:t1',
            metadata={'decompressed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')},
        ))
        dirs = _make_task_dirs(tmp_path)
        archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
        harness = _bind_service_harness(store, archiver, {'task:t1': dirs})
        result = await harness._archive_one_task('task:t1', reason='manual')
        assert result['result'] == 'archived'
    finally:
        store.close()


async def test_archive_one_task_insufficient_space(tmp_path, monkeypatch, policies_guard):
    configure_disk_policies(DiskPolicies())
    # free 低于 紧急线+源×0.35 → insufficient_space（源完好）
    monkeypatch.setattr(
        runtime_service_module, 'disk_waterline_snapshot',
        lambda paths: (10 * 1024 * 1024, 100 * GB),
    )
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task(_task_record('task:t1'))
        dirs = _make_task_dirs(tmp_path)
        archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
        harness = _bind_service_harness(store, archiver, {'task:t1': dirs})
        result = await harness._archive_one_task('task:t1', reason='disk_cleanup')
        assert result['result'] == 'insufficient_space'
        assert (dirs['artifacts'] / 'a.md').exists()
    finally:
        store.close()


# --- 闪退自愈与进度状态 ---


def test_cleanup_stale_work_files(tmp_path, policies_guard):
    import os
    import time as _time

    configure_disk_policies(DiskPolicies())
    archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
    archive_dir = tmp_path / 'task-archives'
    stale_zip = archive_dir / '.tmp-999-task_x.zip'
    fresh_zip = archive_dir / '.tmp-1000-task_y.zip'
    stale_extract = archive_dir / '.extract-task_z-999'
    fresh_extract = archive_dir / '.extract-task_w-1000'
    stale_zip.write_bytes(b'partial')
    fresh_zip.write_bytes(b'partial')
    stale_extract.mkdir()
    (stale_extract / 'f.txt').write_text('x', encoding='utf-8')
    fresh_extract.mkdir()
    (fresh_extract / 'f.txt').write_text('x', encoding='utf-8')
    old_ts = _time.time() - 3600
    os.utime(stale_zip, (old_ts, old_ts))
    os.utime(stale_extract, (old_ts, old_ts))
    result = archiver.cleanup_stale_work_files(max_age_seconds=600)
    assert result == {'tmp_zips': 1, 'extract_dirs': 1}
    assert not stale_zip.exists()
    assert not stale_extract.exists()
    assert fresh_zip.exists() and fresh_extract.exists()  # 在途文件不动


async def test_repair_interrupted_archives(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        # 场景 A：闪退在"源删到一半"——zip 有效、源有残留、元数据缺失
        store.upsert_task(_task_record('task:t1'))
        dirs = _make_task_dirs(tmp_path)
        archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
        harness = _bind_service_harness(store, archiver, {'task:t1': dirs})
        harness._repair_interrupted_archives = MethodType(
            runtime_service_module.MainRuntimeService._repair_interrupted_archives, harness,
        )
        result = await harness._archive_one_task('task:t1', reason='disk_cleanup')
        assert result['result'] == 'archived'
        # 模拟闪退：清掉元数据里的归档标记，并恢复一个残留源文件
        task = store.get_task('task:t1')
        clean_meta = {k: v for k, v in (task.metadata or {}).items() if not k.startswith('archive') and k != 'archived_at'}
        store.upsert_task(task.model_copy(update={'metadata': clean_meta}))
        dirs['artifacts'].mkdir(parents=True, exist_ok=True)
        (dirs['artifacts'] / 'a.md').write_text('artifact content ' * 100, encoding='utf-8')
        await harness._repair_interrupted_archives()
        repaired = store.get_task('task:t1')
        assert repaired.metadata.get('archived_at')
        assert repaired.metadata.get('archive_reason') == 'crash_recovery'
        assert int(repaired.metadata.get('archived_bytes') or 0) > 0
        assert not (dirs['artifacts'] / 'a.md').exists()  # 残留源被补删
        # 场景 B：损坏的 zip → 回滚删除，不写元数据
        bad_zip = archiver.archive_path_for('task:t2')
        bad_zip.write_bytes(b'corrupted zip')
        store.upsert_task(_task_record('task:t2'))
        await harness._repair_interrupted_archives()
        assert not bad_zip.exists()
        assert not (store.get_task('task:t2').metadata or {}).get('archived_at')
    finally:
        store.close()


async def test_decompress_missing_zip_tombstones(tmp_path, policies_guard):
    configure_disk_policies(DiskPolicies())
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        # 元数据标已归档但 zip 不存在（删除渐进后崩溃/手工删）→ 转墓碑
        store.upsert_task(_task_record(
            'task:t1', metadata={'archived_at': '2026-09-01T00:00:00+00:00', 'archived_bytes': 100},
        ))
        archiver = TaskArchiver(archive_dir=tmp_path / 'task-archives')
        harness = _bind_service_harness(store, archiver, {})
        result = await harness._decompress_one_task('task:t1')
        assert result['result'] == 'purged'
        task = store.get_task('task:t1')
        assert task.is_purged()
        assert task.metadata.get('purge_reason') == 'archive_missing'
        assert not task.metadata.get('archived_at')
    finally:
        store.close()


def test_archive_sweep_snapshot_shape():
    harness = SimpleNamespace(
        _archive_sweep_running=True,
        _disk_archive_sweep_state={
            'running': True, 'current_task': 'task:x',
            'archived_count': 3, 'purged_count': 0, 'last_finished_at': '',
        },
    )
    snap = MethodType(
        runtime_service_module.MainRuntimeService._disk_archive_sweep_snapshot, harness,
    )()
    assert snap['running'] is True
    assert snap['current_task'] == 'task:x'
    assert snap['archived_count'] == 3
