from __future__ import annotations

from pathlib import Path

from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.protocol import now_iso
from main.storage.sqlite_store import SQLiteTaskStore


def _record(node_id: str, *, task_id: str = 'task:frames') -> TaskProjectionRuntimeFrameRecord:
    return TaskProjectionRuntimeFrameRecord(
        task_id=task_id,
        node_id=node_id,
        depth=1,
        node_kind='execution',
        phase='before_model',
        active=True,
        runnable=True,
        updated_at=now_iso(),
        payload={'bulk': 'x' * 2048},
    )


def test_existence_probe_tracks_frame_rows_per_task(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        assert store.has_task_runtime_frame('task:frames', 'node:a') is False
        store.upsert_task_runtime_frame(_record('node:a'))
        assert store.has_task_runtime_frame('task:frames', 'node:a') is True
        assert store.has_task_runtime_frame('task:frames', 'node:b') is False
        assert store.has_task_runtime_frame('task:other', 'node:a') is False
    finally:
        store.close()


def test_existence_probe_does_not_parse_frame_payload(tmp_path: Path) -> None:
    """存在性判定只许命中主键：帧正文平均几十 KB，解析它就是白烧事件循环。"""

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_runtime_frame(_record('node:a'))
        original = store._parse

        def _boom(*_args, **_kwargs):
            raise AssertionError('existence probe must not parse payload_json')

        store._parse = _boom  # type: ignore[method-assign]
        try:
            assert store.has_task_runtime_frame('task:frames', 'node:a') is True
        finally:
            store._parse = original  # type: ignore[method-assign]
    finally:
        store.close()
