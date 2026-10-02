"""v3 根准备只在"这块根上没有库"时清残留。

`_prepare_task_runtime_v3_root` 旧写法：目录里没有 `.task-runtime-v3` 标记就 unlink 库文件
与 `-wal/-shm`，再 `shutil.rmtree` 掉 tasks / artifacts / event-history。2026-10-03 一份用
sqlite3 backup API 造的 1.88 GB 生产副本（有库、无标记）被 `MainRuntimeService` 构造一次
删成 364 KB / 0 行。等价的生产场景是从备份恢复库、或把数据根指向一个已有库的目录。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from main.protocol import now_iso
from main.service.runtime_service import (  # noqa: SLF001
    _TASK_RUNTIME_V3_MARKER,
    _prepare_task_runtime_v3_root,
)
from main.storage.sqlite_store import SQLiteTaskStore


def _paths(root: Path) -> dict[str, Path]:
    return {
        'store_path': root / 'runtime.sqlite3',
        'files_base_dir': root / 'tasks',
        'artifact_dir': root / 'artifacts',
        'event_history_dir': root / 'event-history',
    }


def _prepare(root: Path) -> None:
    paths = _paths(root)
    _prepare_task_runtime_v3_root(**paths)


def _seed_store(store_path: Path, *, rows: int = 3) -> int:
    store = SQLiteTaskStore(store_path)
    try:
        for index in range(rows):
            store._execute_write(  # noqa: SLF001
                'INSERT INTO task_model_calls (task_id, node_id, created_at, payload_json) VALUES (?, ?, ?, ?)',
                (f'task:keep{index}', 'node:a', now_iso(), '{"prompt": "keep me"}'),
            )
    finally:
        store.close()
    with sqlite3.connect(store_path) as conn:
        return int(conn.execute('SELECT COUNT(*) FROM task_model_calls').fetchone()[0])


def test_existing_store_survives_a_missing_marker(tmp_path: Path):
    root = tmp_path / 'runtime'
    root.mkdir()
    store_path = root / 'runtime.sqlite3'
    seeded = _seed_store(store_path)
    assert seeded == 3
    size_before = store_path.stat().st_size
    assert not (root / _TASK_RUNTIME_V3_MARKER).exists()

    artifact = root / 'artifacts' / 'task_keep' / 'artifact_keep.txt'
    artifact.parent.mkdir(parents=True)
    artifact.write_text('正文不许被删', encoding='utf-8')
    history = root / 'event-history'
    history.mkdir(exist_ok=True)
    (history / 'events.jsonl').write_text('{}\n', encoding='utf-8')

    _prepare(root)

    assert store_path.exists()
    assert store_path.stat().st_size == size_before
    with sqlite3.connect(store_path) as conn:
        assert int(conn.execute('SELECT COUNT(*) FROM task_model_calls').fetchone()[0]) == 3
    assert artifact.exists()
    assert (history / 'events.jsonl').exists()
    assert (root / _TASK_RUNTIME_V3_MARKER).exists()


def test_empty_root_still_gets_the_v3_cleanup(tmp_path: Path):
    """新装的根（没有库文件）照旧清残留：这条行为不许因为我加的保护而变。"""
    root = tmp_path / 'runtime'
    root.mkdir()
    junk = root / 'artifacts' / 'old'
    junk.mkdir(parents=True)
    (junk / 'old_artifact.txt').write_text('v2 残留', encoding='utf-8')
    (root / 'tasks').mkdir()
    (root / 'event-history').mkdir()

    _prepare(root)

    assert not junk.exists()
    assert (root / _TASK_RUNTIME_V3_MARKER).exists()


def test_zero_byte_store_is_treated_as_no_data(tmp_path: Path):
    """0 字节库没有数据可丢（实盘根目录那个 runtime.sqlite3 死文件就是这种形态）。"""
    root = tmp_path / 'runtime'
    root.mkdir()
    (root / 'runtime.sqlite3').write_bytes(b'')
    junk = root / 'tasks' / 'leftover'
    junk.mkdir(parents=True, exist_ok=True)
    (junk / 'a.txt').write_text('x', encoding='utf-8')

    _prepare(root)

    assert not junk.exists()
    assert (root / _TASK_RUNTIME_V3_MARKER).exists()
