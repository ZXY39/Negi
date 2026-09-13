"""任务存储瘦身一次性迁移脚本（配套 live.patch 单份快照 + 删除全量清除改造）。

功能：
1. event-history 瘦身：每任务目录只保留最新一份快照并规范为 latest.json.gz
   （存量逐事件归档实测单任务可达 4.1 GiB / 3.8 万文件）；孤儿目录
   （tasks 表已无行且超过宽限期）整删。
2. 存量 task-archives zip 处理：按终态保留清单判据（kind∈{patch,final_output}、
   task.final_output_ref 指向、标题含 report/summary）从 zip 导出产出到
   deliverables/<safe_task_id>/（含 manifest.json），随后删除 zip；
   全部处理完删除 task-archives 目录。
3. DB 清理：分批删除 task_events 的 task.live.patch 行（单份快照文件已替代）；
   tasks.metadata 弹除 archived_at/archived_bytes/archive_compressed_bytes/
   archive_reason/decompressed_at/pinned（**purged_at 保留**——遗留墓碑任务由
   运行时删除台账 sweep 走 wipe 闭环）；删除存量 compress_task/decompress_task
   命令行；清孤儿记账行（task_disk_usage/heartbeat_node_retry_state 无 tasks 行）；
   按目录实测重算 task_disk_usage。

**运行约定（务必遵守）**：在服务停机、或经 admin/bootstrap 排水
（POST /api/bootstrap/exit pause_running_work=true）之后运行。
默认 dry-run 只报数；--apply 才写；--backup 先镜像 db+wal+shm。

形态参照 scripts/compact_task_database.py（argparse + 默认 dry-run）。
"""

from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

KEEP_KINDS = {'patch', 'final_output'}
KEEP_TITLE_TOKENS = ('report', 'summary')
METADATA_KEYS_TO_POP = (
    '$.metadata.archived_at',
    '$.metadata.archived_bytes',
    '$.metadata.archive_compressed_bytes',
    '$.metadata.archive_reason',
    '$.metadata.decompressed_at',
    '$.metadata.pinned',
)
LEGACY_COMMAND_TYPES = ('compress_task', 'decompress_task', 'start_archive_sweep')


def _safe_task_dir_name(task_id: str) -> str:
    return str(task_id or '').strip().replace(':', '_').replace('/', '_').replace('\\', '_')


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _backup_files(db_path: Path) -> list[Path]:
    stamp = datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    backups: list[Path] = []
    for suffix in ('', '-wal', '-shm'):
        source = Path(f'{db_path}{suffix}')
        if source.exists():
            target = source.with_name(f'{source.name}.bak-{stamp}')
            shutil.copy2(source, target)
            backups.append(target)
    return backups


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob('*'):
        try:
            if item.is_file():
                total += int(item.stat().st_size)
        except OSError:
            continue
    return total


def _deliverable_file_name(*, title: str, artifact_id: str, member_name: str, binary: bool) -> str:
    suffix = Path(member_name).suffix.lower()
    if suffix == '.gz':
        suffix = Path(Path(member_name).stem).suffix.lower()
    if not suffix:
        suffix = '.bin' if binary else '.md'
    slug_source = str(title or '').strip() or str(artifact_id or 'deliverable')
    slug = ''.join(ch if (ch.isalnum() or ch in '-_.') else '_' for ch in slug_source)[:60].strip('_.') or 'deliverable'
    tail = ''.join(ch for ch in str(artifact_id or '') if ch.isalnum())[-8:] or '00000000'
    return f'{slug}__{tail}{suffix}'


# ----------------------------------------------------------------------
# 1. event-history 瘦身
# ----------------------------------------------------------------------

def slim_event_history(conn: sqlite3.Connection, eh_dir: Path, *, grace_seconds: float, apply: bool) -> dict[str, int]:
    stats = {'kept': 0, 'deleted_files': 0, 'deleted_bytes': 0, 'orphan_dirs': 0}
    if not eh_dir.is_dir():
        return stats
    known = {
        _safe_task_dir_name(str(row['task_id'] or ''))
        for row in conn.execute('SELECT task_id FROM tasks').fetchall()
    }
    now = time.time()
    for task_dir in sorted(eh_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        if task_dir.name not in known:
            try:
                if now - task_dir.stat().st_mtime >= grace_seconds:
                    stats['orphan_dirs'] += 1
                    stats['deleted_bytes'] += _dir_bytes(task_dir)
                    if apply:
                        shutil.rmtree(task_dir, ignore_errors=True)
            except OSError:
                pass
            continue
        def _stem_key(name: str) -> str:
            # '<seq>.json.gz' / 'latest.json.gz' → 首个 '.' 前的 token
            return str(name).split('.', 1)[0]

        files = [f for f in task_dir.iterdir() if f.is_file()]
        latest = [f for f in files if _stem_key(f.name) == 'latest']
        seq_files = [f for f in files if _stem_key(f.name).isdigit()]
        tmp_files = [f for f in files if f.name.endswith('.tmp')]
        if not latest and seq_files:
            newest = max(seq_files, key=lambda f: int(_stem_key(f.name)))
            target = task_dir / f'latest{newest.name[len(_stem_key(newest.name)):]}'
            stats['kept'] += 1
            if apply:
                try:
                    newest.rename(target)
                except OSError:
                    continue
            seq_files = [f for f in seq_files if f != newest]
        elif latest:
            stats['kept'] += 1
        for stale in (*seq_files, *tmp_files):
            try:
                stats['deleted_bytes'] += int(stale.stat().st_size)
                stats['deleted_files'] += 1
                if apply:
                    stale.unlink(missing_ok=True)
            except OSError:
                continue
    return stats


# ----------------------------------------------------------------------
# 2. task-archives zip：导出产出后删除
# ----------------------------------------------------------------------

def _zip_keep_members(conn: sqlite3.Connection, zip_path: Path, task_id: str) -> list[dict]:
    """从 artifacts 表行 + keep policy 选出 zip 内需要导出的成员。"""
    rows = conn.execute('SELECT payload_json FROM artifacts WHERE task_id = ?', (task_id,)).fetchall()
    task_row = conn.execute('SELECT payload_json FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
    final_ref = ''
    if task_row is not None:
        try:
            task_payload = json.loads(task_row['payload_json'])
            final_ref = str(task_payload.get('final_output_ref') or '').strip()
        except (json.JSONDecodeError, TypeError):
            final_ref = ''
    final_id = final_ref.split(':', 1)[1] if ':' in final_ref else final_ref
    kept: list[dict] = []
    try:
        with zipfile.ZipFile(zip_path, 'r') as handle:
            names = set(handle.namelist())
            for row in rows:
                try:
                    artifact = json.loads(row['payload_json'])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(artifact, dict):
                    continue
                kind = str(artifact.get('kind') or '').strip()
                title = str(artifact.get('title') or '').strip()
                artifact_id = str(artifact.get('artifact_id') or '').strip()
                keep = (
                    kind in KEEP_KINDS
                    or (final_id and artifact_id == final_id)
                    or any(token in title.lower() for token in KEEP_TITLE_TOKENS)
                )
                if not keep:
                    continue
                member = f"artifacts/{Path(str(artifact.get('path') or '')).name}"
                if member in names:
                    kept.append({'artifact': artifact, 'member': member})
    except (OSError, zipfile.BadZipFile):
        return []
    return kept


def export_zips(conn: sqlite3.Connection, archives_dir: Path, deliverables_dir: Path, *, apply: bool) -> dict[str, int]:
    stats = {'zips': 0, 'zip_bytes': 0, 'exported_tasks': 0, 'exported_files': 0, 'orphan_zips': 0}
    if not archives_dir.is_dir():
        return stats
    # zip 文件名是 safe 化 task_id（':'→'_'，有损），用 DB 全量建反向映射
    safe_to_task: dict[str, str] = {}
    for row in conn.execute('SELECT task_id FROM tasks').fetchall():
        real_id = str(row['task_id'] or '')
        if real_id:
            safe_to_task.setdefault(_safe_task_dir_name(real_id), real_id)
    zips = sorted(archives_dir.glob('task_*.zip'))
    work_files = sorted(
        item for item in archives_dir.iterdir()
        if item.name.startswith('.tmp-') or item.name.startswith('.extract-') or item.name.endswith('.lock')
    )
    for zip_path in zips:
        stats['zips'] += 1
        try:
            stats['zip_bytes'] += int(zip_path.stat().st_size)
        except OSError:
            pass
        task_id = safe_to_task.get(zip_path.stem, '')
        if not task_id:
            stats['orphan_zips'] += 1
            if apply:
                zip_path.unlink(missing_ok=True)
            continue
        kept = _zip_keep_members(conn, zip_path, task_id)
        if kept:
            target_dir = deliverables_dir / _safe_task_dir_name(task_id)
            files_meta = []
            exported = 0
            if apply:
                target_dir.mkdir(parents=True, exist_ok=True)
            for item in kept:
                artifact = item['artifact']
                member = item['member']
                artifact_id = str(artifact.get('artifact_id') or '')
                title = str(artifact.get('title') or '')
                state = 'missing'
                exported_name = ''
                content = b''
                try:
                    with zipfile.ZipFile(zip_path, 'r') as handle:
                        raw = handle.read(member)
                    try:
                        if raw[:2] == b'\x1f\x8b':
                            raw = gzip.decompress(raw)
                        text = raw.decode('utf-8')
                        state = 'ok'
                        content = text.encode('utf-8')
                    except UnicodeDecodeError:
                        state = 'raw'
                        content = raw
                except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
                    state = 'missing'
                if state != 'missing':
                    exported_name = _deliverable_file_name(
                        title=title, artifact_id=artifact_id, member_name=member, binary=(state == 'raw'),
                    )
                    if apply:
                        try:
                            (target_dir / exported_name).write_bytes(content)
                            exported += 1
                        except OSError:
                            state = 'missing'
                            exported_name = ''
                            content = b''
                    else:
                        exported += 1
                files_meta.append({
                    'artifact_id': artifact_id,
                    'kind': str(artifact.get('kind') or ''),
                    'title': title,
                    'exported_name': exported_name,
                    'source_encoding': str(artifact.get('content_encoding') or 'plain'),
                    'bytes': len(content),
                    'state': state,
                })
            if exported:
                stats['exported_tasks'] += 1
                stats['exported_files'] += exported
                if apply:
                    manifest = {
                        'schema_version': 1,
                        'task_id': task_id,
                        'reason': 'migration',
                        'exported_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                        'files': files_meta,
                        'counts': {'kept': len(kept), 'exported': exported},
                    }
                    try:
                        (target_dir / 'manifest.json').write_text(
                            json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8',
                        )
                    except OSError:
                        pass
        if apply:
            zip_path.unlink(missing_ok=True)
    if apply:
        for item in work_files:
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except OSError:
                continue
        # zip 全部处理完后 task-archives 目录整体移除
        if not any(archives_dir.glob('task_*.zip')):
            shutil.rmtree(archives_dir, ignore_errors=True)
    return stats


# ----------------------------------------------------------------------
# 3. DB 清理
# ----------------------------------------------------------------------

def clean_database(conn: sqlite3.Connection, runtime_root: Path, *, batch: int, apply: bool) -> dict[str, int]:
    stats = {'live_patch_rows': 0, 'metadata_rows': 0, 'command_rows': 0, 'orphan_usage_rows': 0, 'orphan_heartbeat_rows': 0, 'usage_recomputed': 0}

    row = conn.execute("SELECT COUNT(*) AS n FROM task_events WHERE event_type = 'task.live.patch'").fetchone()
    stats['live_patch_rows'] = int(row['n'] if row else 0)
    if apply and stats['live_patch_rows']:
        with conn:
            while True:
                cursor = conn.execute(
                    'DELETE FROM task_events WHERE seq IN ('
                    "SELECT seq FROM task_events WHERE event_type = 'task.live.patch' LIMIT ?)",
                    (batch,),
                )
                if not (cursor.rowcount or 0):
                    break

    # metadata 弹除归档/pin 标记（purged_at 保留给运行时台账 sweep）
    where_clause = ' OR '.join(
        f"json_extract(payload_json, '{key}') IS NOT NULL" for key in METADATA_KEYS_TO_POP
    )
    row = conn.execute(f'SELECT COUNT(*) AS n FROM tasks WHERE {where_clause}').fetchone()
    stats['metadata_rows'] = int(row['n'] if row else 0)
    if apply and stats['metadata_rows']:
        remove_expr = ', '.join(f"'{key}'" for key in METADATA_KEYS_TO_POP)
        with conn:
            conn.execute(f'UPDATE tasks SET payload_json = json_remove(payload_json, {remove_expr}) WHERE {where_clause}')

    marks = ','.join('?' * len(LEGACY_COMMAND_TYPES))
    row = conn.execute(f'SELECT COUNT(*) AS n FROM task_commands WHERE command_type IN ({marks})', LEGACY_COMMAND_TYPES).fetchone()
    stats['command_rows'] = int(row['n'] if row else 0)
    if apply and stats['command_rows']:
        with conn:
            conn.execute(f'DELETE FROM task_commands WHERE command_type IN ({marks})', LEGACY_COMMAND_TYPES)

    row = conn.execute('SELECT COUNT(*) AS n FROM task_disk_usage WHERE task_id NOT IN (SELECT task_id FROM tasks)').fetchone()
    stats['orphan_usage_rows'] = int(row['n'] if row else 0)
    row = conn.execute("SELECT COUNT(*) AS n FROM heartbeat_node_retry_state WHERE task_id != '' AND task_id NOT IN (SELECT task_id FROM tasks)").fetchone()
    stats['orphan_heartbeat_rows'] = int(row['n'] if row else 0)
    if apply:
        with conn:
            conn.execute('DELETE FROM task_disk_usage WHERE task_id NOT IN (SELECT task_id FROM tasks)')
            conn.execute("DELETE FROM heartbeat_node_retry_state WHERE task_id != '' AND task_id NOT IN (SELECT task_id FROM tasks)")

    # task_disk_usage 重算：event-history + artifacts + files 目录实测（temp 在
    # workspace 侧，由运行时小时级对账兜底）
    eh_dir = runtime_root / 'event-history'
    artifacts_dir = runtime_root / 'artifacts'
    files_dir = runtime_root / 'tasks'
    for row in conn.execute('SELECT task_id FROM tasks').fetchall():
        task_id = str(row['task_id'] or '')
        safe = _safe_task_dir_name(task_id)
        measured = _dir_bytes(eh_dir / safe) + _dir_bytes(artifacts_dir / safe) + _dir_bytes(files_dir / safe)
        stats['usage_recomputed'] += 1
        if apply:
            with conn:
                conn.execute(
                    'INSERT INTO task_disk_usage (task_id, total_bytes, updated_at) VALUES (?, ?, ?) '
                    'ON CONFLICT(task_id) DO UPDATE SET total_bytes = excluded.total_bytes, updated_at = excluded.updated_at',
                    (task_id, measured, datetime.now(timezone.utc).isoformat(timespec='seconds')),
                )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='任务存储瘦身一次性迁移（默认 dry-run）')
    parser.add_argument('--runtime-db', default='.g3ku/main-runtime/runtime.sqlite3', help='runtime.sqlite3 路径')
    parser.add_argument('--apply', action='store_true', help='真正执行（缺省 dry-run 只报数）')
    parser.add_argument('--backup', action='store_true', help='--apply 前先镜像 db+wal+shm（.bak-时间戳）')
    parser.add_argument('--batch', type=int, default=5000, help='task_events 分批删除的批大小')
    parser.add_argument('--orphan-grace-days', type=float, default=7.0, help='孤儿 event-history 目录宽限天数')
    args = parser.parse_args(argv)

    db_path = Path(args.runtime_db).expanduser().resolve(strict=False)
    if not db_path.is_file():
        print(f'[skip] 数据库不存在: {db_path}')
        return 0
    runtime_root = db_path.parent

    conn = _connect(db_path)
    try:
        conn.execute('SELECT 1 FROM task_events LIMIT 1')
    except sqlite3.Error as exc:
        print(f'[refuse] 数据库不可读: {exc}')
        return 2

    print(f'数据库: {db_path}')
    print(f'  大小≈{db_path.stat().st_size / 1024 / 1024:.1f} MiB')
    if args.apply and args.backup:
        backups = _backup_files(db_path)
        print(f'  已镜像: {[p.name for p in backups]}')
    if args.apply:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    eh_stats = slim_event_history(
        conn, runtime_root / 'event-history',
        grace_seconds=args.orphan_grace_days * 24 * 3600, apply=args.apply,
    )
    print('event-history 瘦身:')
    print(f"  保留快照任务: {eh_stats['kept']}  删除文件: {eh_stats['deleted_files']} (~{eh_stats['deleted_bytes'] / 1024 / 1024:.1f} MiB)  孤儿目录: {eh_stats['orphan_dirs']}")

    zip_stats = export_zips(
        conn, runtime_root / 'task-archives', runtime_root / 'deliverables', apply=args.apply,
    )
    print('task-archives 处理:')
    print(f"  zip: {zip_stats['zips']} 个 (~{zip_stats['zip_bytes'] / 1024 / 1024:.1f} MiB)  导出产出: {zip_stats['exported_tasks']} 任务 / {zip_stats['exported_files']} 文件  孤儿 zip: {zip_stats['orphan_zips']}")

    db_stats = clean_database(conn, runtime_root, batch=max(100, int(args.batch)), apply=args.apply)
    print('DB 清理:')
    print(f"  task.live.patch 行: {db_stats['live_patch_rows']}  metadata 归档/pin 标记任务: {db_stats['metadata_rows']}")
    print(f"  存量归档命令行: {db_stats['command_rows']}  孤儿记账行: {db_stats['orphan_usage_rows']}  孤儿心跳行: {db_stats['orphan_heartbeat_rows']}")
    print(f"  task_disk_usage 重算: {db_stats['usage_recomputed']} 任务")

    if not args.apply:
        print('[dry-run] 加 --apply 才会执行')
        return 0
    print(f'迁移完成: 现库大小≈{db_path.stat().st_size / 1024 / 1024:.1f} MiB（WAL 页归还可另跑 compact_task_database.py）')
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
