"""migrate_slim_task_storage.py 迁移脚本单测：dry-run 报数 / apply 生效 / 判据与豁免。"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import time
import zipfile
from pathlib import Path

from main.models import TaskArtifactRecord, TaskRecord
from main.storage.sqlite_store import SQLiteTaskStore

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / 'scripts' / 'migrate_slim_task_storage.py'


def _load_script():
    spec = importlib.util.spec_from_file_location('migrate_slim_task_storage', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _task(task_id: str, metadata: dict | None = None, status: str = 'success') -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id='web:demo',
        title='migration',
        user_request='x',
        status=status,
        root_node_id='node-root',
        created_at='2026-09-01T00:00:00+08:00',
        updated_at='2026-09-10T00:00:00+08:00',
        finished_at='2026-09-10T00:00:00+08:00',
        metadata=dict(metadata or {}),
    )


def _seed(tmp_path: Path) -> Path:
    runtime = tmp_path / 'main-runtime'
    runtime.mkdir()
    db_path = runtime / 'runtime.sqlite3'
    store = SQLiteTaskStore(db_path, event_history_dir=runtime / 'event-history')
    task_id = 'task:mig1'
    store.upsert_task(_task(task_id, metadata={
        'archived_at': '2026-09-11T00:00:00+08:00',
        'archived_bytes': 100,
        'pinned': True,
        'purged_at': '',
    }))
    store.upsert_task(_task('task:purged-legacy', metadata={'purged_at': '2026-09-01T00:00:00+08:00'}))
    # live.patch 行 + 离散事件行
    for seq in range(3):
        store.append_task_event(
            task_id=task_id, session_id='web:demo', event_type='task.live.patch',
            created_at=f'2026-09-10T00:00:0{seq}+08:00', payload={'frame': {'phase': f'p{seq}'}},
        )
    store.append_task_event(
        task_id=task_id, session_id='web:demo', event_type='task.terminal',
        created_at='2026-09-10T00:01:00+08:00', payload={'ok': True},
    )
    # 存量归档命令 + 其它命令
    store.enqueue_task_command(
        command_id='cmd-1', task_id=task_id, session_id='web:demo', command_type='compress_task',
        payload={}, created_at='2026-09-10T00:00:00+08:00',
    )
    store.enqueue_task_command(
        command_id='cmd-2', task_id=task_id, session_id='web:demo', command_type='pause_task',
        payload={}, created_at='2026-09-10T00:00:00+08:00',
    )
    # artifacts 行：kept（report 标题）+ 非 kept
    artifacts_dir = runtime / 'artifacts' / 'task_mig1'
    artifacts_dir.mkdir(parents=True)
    kept_record = TaskArtifactRecord(
        artifact_id='artifact:keep0001', task_id=task_id, node_id='node-root', kind='node_output',
        title='weekly report', path=str(artifacts_dir / 'report.md'), mime_type='text/markdown',
        preview_text='', created_at='2026-09-10T00:00:00+08:00',
    )
    dropped_record = TaskArtifactRecord(
        artifact_id='artifact:drop0002', task_id=task_id, node_id='node-root', kind='task_actual_request',
        title='node-actual-request:node-root:1', path=str(artifacts_dir / 'req.json'), mime_type='application/json',
        preview_text='', created_at='2026-09-10T00:00:00+08:00',
    )
    store.upsert_artifact(kept_record)
    store.upsert_artifact(dropped_record)
    # 孤儿记账行 + 心跳行
    store.upsert_task_disk_usage('task:ghost', 999)
    store._execute_write(
        'INSERT INTO heartbeat_node_retry_state (node_id, task_id, session_id, consecutive_failures, '
        "first_failure_at, last_attempt_at, next_eligible_at, escalated, updated_at) VALUES ('n1', 'task:ghost', '', 1, '', '', '', 0, '')"
    )
    store.close()

    # event-history：3 个逐事件归档（gz），最大 seq=7
    eh_task = runtime / 'event-history' / 'task_mig1'
    eh_task.mkdir(parents=True)
    for seq in (2, 5, 7):
        with gzip.open(eh_task / f'{seq}.json.gz', 'wt', encoding='utf-8') as handle:
            handle.write(json.dumps({'seq': seq}))
    # 孤儿目录（mtime 10 天前）
    orphan = runtime / 'event-history' / 'task_ghost'
    orphan.mkdir(parents=True)
    (orphan / '1.json.gz').write_bytes(b'x')
    old = time.time() - 10 * 24 * 3600
    os.utime(orphan, (old, old))

    # task-archives zip：artifacts/report.md（kept）+ artifacts/req.json（非 kept）
    archives = runtime / 'task-archives'
    archives.mkdir()
    zip_path = archives / 'task_mig1.zip'
    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr('artifacts/report.md', '# weekly report body')
        handle.writestr('artifacts/req.json.gz', gzip.compress(b'{"messages": []}'))
        handle.writestr('.g3ku-archive.json', json.dumps({'task_id': task_id}))
    (archives / '.tmp-123-task_x.zip').write_bytes(b'half')
    return db_path


def test_migration_dry_run_reports_without_writing(tmp_path) -> None:
    migrate = _load_script()
    db_path = _seed(tmp_path)
    runtime = db_path.parent

    code = migrate.main(['--runtime-db', str(db_path)])
    assert code == 0
    # dry-run：一切原样
    assert len(list((runtime / 'event-history' / 'task_mig1').glob('*.json.gz'))) == 3
    assert (runtime / 'event-history' / 'task_ghost').exists()
    assert (runtime / 'task-archives' / 'task_mig1.zip').exists()
    assert not (runtime / 'deliverables').exists()
    store = SQLiteTaskStore(db_path)
    try:
        events = store.list_task_events(task_id='task:mig1', limit=50)
        assert len([e for e in events if e['event_type'] == 'task.live.patch']) == 3
    finally:
        store.close()


def test_migration_apply_full_flow(tmp_path) -> None:
    migrate = _load_script()
    db_path = _seed(tmp_path)
    runtime = db_path.parent

    code = migrate.main(['--runtime-db', str(db_path), '--apply', '--backup'])
    assert code == 0
    assert list(db_path.parent.glob('runtime.sqlite3.bak-*')), '--backup 应生成镜像'

    # 1) event-history：只留 seq 最大的一份并改名 latest.json.gz；孤儿目录被清
    kept_files = sorted(f.name for f in (runtime / 'event-history' / 'task_mig1').iterdir())
    assert kept_files == ['latest.json.gz']
    with gzip.open(runtime / 'event-history' / 'task_mig1' / 'latest.json.gz', 'rt', encoding='utf-8') as handle:
        assert json.loads(handle.read())['seq'] == 7
    assert not (runtime / 'event-history' / 'task_ghost').exists()

    # 2) zip：产出导出（kept 命中 report 标题），zip 与工作文件删除，目录移除
    assert not (runtime / 'task-archives').exists()
    deliverables = runtime / 'deliverables' / 'task_mig1'
    names = sorted(f.name for f in deliverables.iterdir())
    assert 'manifest.json' in names
    exported = [n for n in names if n != 'manifest.json']
    assert len(exported) == 1 and exported[0].startswith('weekly_report') and 'keep0001'[-8:] in exported[0]
    assert (deliverables / exported[0]).read_text(encoding='utf-8') == '# weekly report body'
    manifest = json.loads((deliverables / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['reason'] == 'migration' and manifest['counts']['exported'] == 1

    # 3) DB：live.patch 行删除、离散事件保留、metadata 弹除归档/pin、purged_at 保留、
    #    归档命令删除、pause 命令保留、孤儿行清理
    store = SQLiteTaskStore(db_path)
    try:
        events = store.list_task_events(task_id='task:mig1', limit=50)
        assert [e['event_type'] for e in events] == ['task.terminal']
        task = store.get_task('task:mig1')
        assert task is not None
        assert 'archived_at' not in (task.metadata or {})
        assert 'pinned' not in (task.metadata or {})
        legacy = store.get_task('task:purged-legacy')
        assert legacy is not None and legacy.metadata.get('purged_at'), 'purged_at 必须保留给运行时 sweep'
        commands = store.list_unfinished_task_commands()
        assert [c['command_type'] for c in commands] == ['pause_task']
        assert 'task:ghost' not in store.get_task_disk_usages(None)
        row = store._fetchone("SELECT COUNT(*) AS n FROM heartbeat_node_retry_state WHERE task_id = 'task:ghost'", ())
        assert int(row['n']) == 0
        # disk usage 重算：event-history latest.json.gz 字节入账
        usage = store.get_task_disk_usages(['task:mig1']).get('task:mig1', 0)
        assert usage > 0
    finally:
        store.close()
