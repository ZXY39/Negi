"""清理 temp/tasks 下不属于任何现存任务的孤儿 task_* 目录。

背景：历史上测试在未隔离工作区时把任务临时目录建进了真实仓库的
temp/tasks（见 tests/conftest.py 与 MainRuntimeService.workspace_root），
累积出 26000+ 孤儿目录。本脚本以 runtime 数据库的 tasks 表为准：

- 空孤儿目录：--apply 时直接删除；
- 非空孤儿目录：--apply --move-non-empty 时整体移动到备份目录（可逆），
  或 --apply --purge-non-empty 时直接删除（不可逆，需二次确认）；
- 默认 dry-run，只打印统计与操作清单，不做任何修改。

示例：
  python scripts/cleanup_orphan_task_temp_dirs.py
  python scripts/cleanup_orphan_task_temp_dirs.py --apply
  python scripts/cleanup_orphan_task_temp_dirs.py --apply --move-non-empty
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_RUNTIME_DB = REPO_ROOT / '.g3ku' / 'main-runtime' / 'runtime.sqlite3'
DEFAULT_TASKS_ROOT = REPO_ROOT / 'temp' / 'tasks'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='清理 temp/tasks 下无主的孤儿任务目录')
    parser.add_argument('--runtime-db', type=Path, default=DEFAULT_RUNTIME_DB, help='runtime 数据库路径')
    parser.add_argument('--tasks-root', type=Path, default=DEFAULT_TASKS_ROOT, help='任务临时目录根路径')
    parser.add_argument('--apply', action='store_true', help='实际执行删除/移动；缺省为 dry-run')
    parser.add_argument('--move-non-empty', action='store_true', help='非空孤儿目录移动到备份目录（需 --apply）')
    parser.add_argument('--purge-non-empty', action='store_true', help='非空孤儿目录直接删除（需 --apply，不可逆）')
    return parser.parse_args()


def load_known_task_dirs(runtime_db: Path) -> set[str]:
    if not runtime_db.exists():
        print(f'[warn] runtime 数据库不存在：{runtime_db}，按没有任务处理')
        return set()
    conn = sqlite3.connect(str(runtime_db))
    try:
        cur = conn.cursor()
        cur.execute('SELECT task_id FROM tasks')
        task_ids = [str(row[0] or '') for row in cur.fetchall()]
    finally:
        conn.close()
    return {task_id.replace(':', '_').replace('/', '_').replace('\\', '_') for task_id in task_ids}


def scan(tasks_root: Path, known: set[str]) -> tuple[list[Path], list[Path], list[Path]]:
    orphans_empty: list[Path] = []
    orphans_nonempty: list[Path] = []
    others: list[Path] = []
    for entry in sorted(tasks_root.iterdir()):
        if not entry.is_dir():
            others.append(entry)
            continue
        if entry.name not in known:
            children = list(entry.iterdir())
            (orphans_empty if not children else orphans_nonempty).append(entry)
    return orphans_empty, orphans_nonempty, others


def main() -> None:
    args = parse_args()
    runtime_db: Path = args.runtime_db
    tasks_root: Path = args.tasks_root
    if args.move_non_empty and args.purge_non_empty:
        raise SystemExit('--move-non-empty 与 --purge-non-empty 不能同时使用')
    if (args.move_non_empty or args.purge_non_empty) and not args.apply:
        raise SystemExit('--move-non-empty / --purge-non-empty 需要同时给 --apply')

    known = load_known_task_dirs(runtime_db)
    orphans_empty, orphans_nonempty, others = scan(tasks_root, known)
    total_dirs = sum(1 for entry in tasks_root.iterdir())
    kept = total_dirs - len(orphans_empty) - len(orphans_nonempty) - len(others)

    print(f'[scan] {tasks_root}')
    print(f'  共有目录/文件：{total_dirs}')
    print(f'  在库任务目录（保留）：{kept}（库中共 {len(known)} 个任务）')
    print(f'  空孤儿目录：{len(orphans_empty)}')
    print(f'  非空孤儿目录：{len(orphans_nonempty)}')
    print(f'  其他文件/非 task 目录（不动）：{len(others)}')
    if others:
        for item in others[:20]:
            print(f'    [other] {item.name}')

    if not args.apply:
        print('\n[dry-run] 未做任何修改；加 --apply 执行删除/移动。')
        return

    backup_root = tasks_root.parent / (tasks_root.name + '_orphan_backup')
    for item in orphans_empty:
        try:
            item.rmdir()
        except OSError as exc:
            print(f'[skip] {item.name}: {exc}')
    print(f'[apply] 已删除空孤儿目录 {len(orphans_empty)} 个。')

    for item in orphans_nonempty:
        if args.purge_non_empty:
            shutil.rmtree(item, ignore_errors=False)
            print(f'[purge] 已删除 {item.name}')
        elif args.move_non_empty:
            target = backup_root / item.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(target))
            print(f'[move] 已移动 {item.name} -> {target}')
        else:
            print(f'[keep] 非空孤儿 {item.name} 未处理；加 --move-non-empty 或 --purge-non-empty 处理。')
    if args.move_non_empty:
        print(f'[apply] 非空孤儿已移动到 {backup_root}')


if __name__ == '__main__':
    main()