"""runtime.sqlite3 磁盘治理维护脚本（P3）。

功能：
1. 终态任务大行裁剪：对早于保留期（默认 14 天）的 success/failed 任务，删除
   task_model_calls / task_runtime_frames / task_node_tool_results /
   task_node_rounds / task_node_details 五张大行表的任务行。
   永久保留：tasks/nodes 结构、task_error_logs、task_events 行与
   event-history gz 归档（审计与回顾的唯一来源）。
2. auto_vacuum 迁移 + 空间收缩：新库由运行时建库即 INCREMENTAL；存量库
   （auto_vacuum=0）在 --vacuum-full 时执行 VACUUM 迁移，已是 INCREMENTAL
   的库执行 incremental_vacuum 归还 freelist 页。

**运行约定（务必遵守）**：在服务停机、或经 admin/bootstrap 排水
（POST /api/bootstrap/exit pause_running_work=true）之后运行——VACUUM 需要
独占连接且约一倍库大小的临时磁盘空间，磁盘接近满时禁止 --vacuum-full。
默认 dry-run 只报数；--apply 才写库；--backup 先镜像 db+wal+shm。

形态参照 scripts/cleanup_orphan_task_temp_dirs.py（argparse + 默认 dry-run）。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRUNE_TABLES = (
    'task_model_calls',
    'task_runtime_frames',
    'task_node_tool_results',
    'task_node_rounds',
    'task_node_details',
)

PRUNABLE_TASK_SUBQUERY = (
    "SELECT task_id FROM tasks WHERE status IN ('success','failed') "
    "AND COALESCE(NULLIF(json_extract(payload_json, '$.finished_at'), ''), updated_at) < ?"
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _pragma_one(conn: sqlite3.Connection, sql: str) -> int:
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='runtime.sqlite3 裁剪与空间收缩（默认 dry-run）')
    parser.add_argument('--runtime-db', default='.g3ku/main-runtime/runtime.sqlite3', help='runtime.sqlite3 路径')
    parser.add_argument('--retention-days', type=int, default=14, help='终态任务大行保留天数（0=跳过裁剪）')
    parser.add_argument('--apply', action='store_true', help='真正执行（缺省 dry-run 只报数）')
    parser.add_argument('--vacuum-full', action='store_true', help='auto_vacuum=0 的存量库执行 VACUUM 迁移（需约一倍库大小临时空间）')
    parser.add_argument('--backup', action='store_true', help='--apply 前先镜像 db+wal+shm（.bak-时间戳）')
    parser.add_argument('--batch', type=int, default=200, help='裁剪每批任务数')
    args = parser.parse_args(argv)

    db_path = Path(args.runtime_db).expanduser().resolve(strict=False)
    if not db_path.is_file():
        print(f'[skip] 数据库不存在: {db_path}')
        return 0

    conn = _connect(db_path)
    try:
        page_size = max(1, _pragma_one(conn, 'PRAGMA page_size'))
        page_count = _pragma_one(conn, 'PRAGMA page_count')
        freelist = _pragma_one(conn, 'PRAGMA freelist_count')
        auto_vacuum = _pragma_one(conn, 'PRAGMA auto_vacuum')
        print(f'数据库: {db_path}')
        print(f'  大小≈{(page_size * page_count) / 1024 / 1024:.1f} MiB  freelist≈{(page_size * freelist) / 1024 / 1024:.1f} MiB  auto_vacuum={auto_vacuum}')
        free_space = shutil.disk_usage(str(db_path.parent)).free
        print(f'  所在盘剩余≈{free_space / 1024 / 1024 / 1024:.2f} GiB')

        cutoff = ''
        prunable: dict[str, int] = {}
        task_count = 0
        if args.retention_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=args.retention_days)).isoformat(timespec='seconds')
            try:
                rows = conn.execute(f'{PRUNABLE_TASK_SUBQUERY}', (cutoff,)).fetchall()
                task_count = len(rows)
                for table in PRUNE_TABLES:
                    row = conn.execute(
                        f'SELECT COUNT(*) FROM {table} WHERE task_id IN ({PRUNABLE_TASK_SUBQUERY})',
                        (cutoff,),
                    ).fetchone()
                    prunable[table] = int(row[0] or 0) if row else 0
            except sqlite3.Error as exc:
                print(f'[warn] 裁剪预检失败: {exc}')
            print(f'  可裁剪终态任务: {task_count} 个（cutoff={cutoff}）')
            for table, count in prunable.items():
                print(f'    {table}: {count} 行')

        vacuum_needed = args.vacuum_full and auto_vacuum == 0
        if vacuum_needed:
            needed = page_size * page_count
            if free_space < needed * 1.2:
                print(f'[refuse] VACUUM 需要约 {needed / 1024 / 1024:.0f} MiB 临时空间，剩余不足（1.2x 安全系数），拒绝执行')
                return 2

        if not args.apply:
            print('[dry-run] 加 --apply 才会执行')
            return 0

        if args.backup:
            backups = _backup_files(db_path)
            print(f'  已镜像: {[str(p.name) for p in backups]}')

        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if task_count:
            conn.commit()
            deleted_total = 0
            with conn:
                rows = conn.execute(f'{PRUNABLE_TASK_SUBQUERY} ORDER BY updated_at ASC', (cutoff,)).fetchall()
                ids = [str(r['task_id']) for r in rows]
                # 分批删除，避免单语句变量上限
                for start in range(0, len(ids), args.batch):
                    batch = ids[start:start + args.batch]
                    marks = ','.join('?' * len(batch))
                    for table in PRUNE_TABLES:
                        cursor = conn.execute(f'DELETE FROM {table} WHERE task_id IN ({marks})', tuple(batch))
                        deleted_total += int(cursor.rowcount or 0)
            print(f'  裁剪完成: 任务 {len(ids)} 个，删除 {deleted_total} 行')

        if vacuum_needed:
            print('  执行 VACUUM（独占，可能需要数分钟）…')
            conn.isolation_level = None
            conn.execute('VACUUM')
            conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
            conn.execute('VACUUM')
            conn.isolation_level = ''
            print('  VACUUM 完成，auto_vacuum 已迁移为 INCREMENTAL')
        elif _pragma_one(conn, 'PRAGMA auto_vacuum') == 2:
            conn.isolation_level = None
            conn.execute('PRAGMA incremental_vacuum')
            conn.isolation_level = ''
            print('  incremental_vacuum 完成')

        page_count_after = _pragma_one(conn, 'PRAGMA page_count')
        freelist_after = _pragma_one(conn, 'PRAGMA freelist_count')
        print(f'  后态: 大小≈{(page_size * page_count_after) / 1024 / 1024:.1f} MiB  freelist≈{(page_size * freelist_after) / 1024 / 1024:.1f} MiB')
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
