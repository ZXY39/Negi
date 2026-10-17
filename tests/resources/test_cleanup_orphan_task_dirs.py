"""孤儿任务目录清理脚本的拒绝判据。

文档把这条车道推荐给操作员（`operations-and-maintenance.md`「磁盘紧张时优先按该脚本处置」），
并断言「清理脚本保留在库任务目录，以数据库为准」。此前读不到库时降级成空任务集——那句保证
正好反过来自证失败：所有在库目录都被当成孤儿，`--purge-non-empty` 是不可逆删除。
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / 'scripts' / 'cleanup_orphan_task_temp_dirs.py'


def _load_script():
    spec = importlib.util.spec_from_file_location('cleanup_orphan_task_temp_dirs', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_install(root: Path, task_ids: list[str]) -> tuple[Path, Path]:
    runtime_db = root / '.g3ku' / 'main-runtime' / 'runtime.sqlite3'
    runtime_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(runtime_db))
    try:
        with conn:
            conn.execute('CREATE TABLE tasks (task_id TEXT PRIMARY KEY)')
            conn.executemany('INSERT INTO tasks (task_id) VALUES (?)', [(task_id,) for task_id in task_ids])
    finally:
        conn.close()
    tasks_root = root / 'temp' / 'tasks'
    tasks_root.mkdir(parents=True, exist_ok=True)
    return runtime_db, tasks_root


def _seed_dirs(tasks_root: Path, names: list[str]) -> None:
    for name in names:
        directory = tasks_root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'work.txt').write_text('payload', encoding='utf-8')


def _run(monkeypatch, argv: list[str]) -> object:
    monkeypatch.setattr('sys.argv', ['cleanup_orphan_task_temp_dirs.py', *argv])
    module = _load_script()
    return module, module.main()


def test_missing_database_refuses_instead_of_empty_task_set(tmp_path, monkeypatch) -> None:
    module = _load_script()
    with pytest.raises(SystemExit):
        module.load_known_task_dirs(tmp_path / 'nowhere' / 'runtime.sqlite3')


def test_apply_purge_with_unreadable_database_deletes_nothing(tmp_path, monkeypatch) -> None:
    root = tmp_path / 'install'
    runtime_db, tasks_root = _make_install(root, ['task_live'])
    _seed_dirs(tasks_root, ['task_live', 'task_orphan'])
    # 库文件被移走：判据缺失，不是「没有任务」
    runtime_db.unlink()

    with pytest.raises(SystemExit):
        _run(monkeypatch, ['--runtime-db', str(runtime_db), '--tasks-root', str(tasks_root), '--apply', '--purge-non-empty'])

    assert (tasks_root / 'task_live' / 'work.txt').exists()
    assert (tasks_root / 'task_orphan').exists()


def test_apply_refuses_tasks_root_from_another_install(tmp_path, monkeypatch) -> None:
    root = tmp_path / 'install'
    runtime_db, _ = _make_install(root, ['task_live'])
    other_tasks_root = tmp_path / 'elsewhere' / 'temp' / 'tasks'
    _seed_dirs(other_tasks_root, ['task_live'])

    with pytest.raises(SystemExit):
        _run(monkeypatch, ['--runtime-db', str(runtime_db), '--tasks-root', str(other_tasks_root), '--apply', '--purge-non-empty'])

    assert (other_tasks_root / 'task_live' / 'work.txt').exists()


def test_apply_purge_removes_only_orphans(tmp_path, monkeypatch) -> None:
    root = tmp_path / 'install'
    runtime_db, tasks_root = _make_install(root, ['task_live'])
    _seed_dirs(tasks_root, ['task_live', 'task_orphan'])

    _run(monkeypatch, ['--runtime-db', str(runtime_db), '--tasks-root', str(tasks_root), '--apply', '--purge-non-empty'])

    assert (tasks_root / 'task_live' / 'work.txt').exists()
    assert not (tasks_root / 'task_orphan').exists()
