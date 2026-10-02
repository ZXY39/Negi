"""读连接的长块榜必须带表名。

实盘刚加"最长 8 条"那一本时，第一名是 1,872 ms 的 `sqlite.query.fetchall`——
这个名字不指向任何一条查询，块越大越无名。
"""

from __future__ import annotations

from pathlib import Path

from main.storage.sqlite_store import SQLiteTaskStore


class _CapturingRecorder:
    def __init__(self) -> None:
        self.sections: list[str] = []

    def record(self, *, section: str, elapsed_ms: float, started_at: str | None = None) -> None:
        self.sections.append(section)


def test_query_section_names_the_first_from_table():
    assert SQLiteTaskStore._query_section('sqlite.query.fetchall', 'SELECT * FROM nodes WHERE task_id=?') == 'sqlite.query.fetchall:nodes'
    assert SQLiteTaskStore._query_section('sqlite.query.fetchall', 'select n.payload_json from task_runtime_frames n') == 'sqlite.query.fetchall:task_runtime_frames'
    assert SQLiteTaskStore._query_section('sqlite.query.fetchall', 'PRAGMA table_info(nodes)') == 'sqlite.query.fetchall'


def test_fetchall_records_the_tagged_section(tmp_path: Path):
    recorder = _CapturingRecorder()
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3', debug_recorder=recorder)
    try:
        store.list_nodes('task:absent')
    finally:
        store.close()
    assert 'sqlite.query.fetchall:nodes' in recorder.sections
    assert 'sqlite.query.fetchall' not in recorder.sections
