"""检查点车道的暂停窄读。

契约：
1) `_check_pause_or_cancel` 只问"这个节点该不该停摆"，所以只取 `pause_requested` /
   `is_paused` / `pause_reason` 三个键，不搬整行正文（实盘 `nodes.payload_json`
   平均 254 KB、单任务 888 行合计 220 MB，大头是 input/output 正文）。
2) 三个键的读回值必须与整行建模一致，包括缺键的存量行（按 False / '' 处理）。
3) 判定类读取走主读连接：轻读连接可能落后于写者（#24 的约束）。
"""

from __future__ import annotations

import json
from pathlib import Path

from main.models import NodeRecord, TokenUsageSummary
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:narrowpause'


def _node(node_id: str, **overrides: object) -> NodeRecord:
    payload = {
        'node_id': node_id,
        'task_id': TASK_ID,
        'parent_node_id': None,
        'root_node_id': 'node:root',
        'depth': 0,
        'node_kind': 'execution',
        'status': 'in_progress',
        'goal': 'demo',
        'prompt': 'demo',
        # 检查点不读的正文：这一行让"不搬正文"有实际含义
        'input': 'x' * 200_000,
        'output': [],
        'check_result': '',
        'final_output': '',
        'can_spawn_children': False,
        'created_at': '2026-10-02T10:00:00+08:00',
        'updated_at': '2026-10-02T10:00:00+08:00',
        'token_usage': TokenUsageSummary(tracked=True),
    }
    payload.update(overrides)
    return NodeRecord.model_validate(payload)


def test_narrow_read_matches_the_full_record(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_node(_node('node:paused', is_paused=True, pause_requested=True, pause_reason='operator'))
    store.upsert_node(_node('node:idle'))

    for node_id in ('node:paused', 'node:idle'):
        full = store.get_node(node_id)
        assert store.get_node_pause_flags(node_id) == {
            'pause_requested': bool(full.pause_requested),
            'is_paused': bool(full.is_paused),
            'pause_reason': str(full.pause_reason or ''),
        }, node_id


def test_legacy_row_without_the_keys_reads_as_not_paused(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')

    def operation(conn):
        conn.execute(
            'INSERT INTO nodes (node_id, task_id, parent_node_id, root_node_id, depth, status,'
            ' created_at, updated_at, payload_json) VALUES (?, ?, NULL, ?, 0, ?,'
            " '2026-10-02T10:00:00+08:00', '2026-10-02T10:00:00+08:00', ?)",
            (
                'node:legacy',
                TASK_ID,
                'node:root',
                'in_progress',
                json.dumps({
                    # 旧版写帧时还没有这三个键：必填字段齐、只缺暂停键
                    'node_id': 'node:legacy',
                    'task_id': TASK_ID,
                    'parent_node_id': None,
                    'root_node_id': 'node:root',
                    'depth': 0,
                    'node_kind': 'execution',
                    'status': 'in_progress',
                    'goal': 'demo',
                    'prompt': 'demo',
                    'input': '',
                    'output': [],
                    'check_result': '',
                    'final_output': '',
                    'can_spawn_children': False,
                    'created_at': '2026-10-02T10:00:00+08:00',
                    'updated_at': '2026-10-02T10:00:00+08:00',
                }),
            ),
        )

    store._run_write(operation)
    legacy = store.get_node('node:legacy')
    assert legacy is not None and legacy.is_paused is False and legacy.pause_requested is False
    assert store.get_node_pause_flags('node:legacy') == {
        'pause_requested': False,
        'is_paused': False,
        'pause_reason': '',
    }


def test_missing_node_reads_as_none(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    assert store.get_node_pause_flags('node:nope') is None
    assert store.get_node_pause_flags('') is None


def test_narrow_read_never_parses_the_row_body(tmp_path: Path) -> None:
    """整行建模一次都不该发生——否则这条窄读还是在搬 254 KB。"""
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_node(_node('node:paused', is_paused=True))
    parsed: list[str] = []
    original = SQLiteTaskStore.__dict__['_parse'].__func__

    def spying(cls, payload_json: str, model_cls):
        parsed.append(model_cls.__name__)
        return original(cls, payload_json, model_cls)

    store._parse = spying  # type: ignore[method-assign]

    assert store.get_node_pause_flags('node:paused')['is_paused'] is True
    assert parsed == []


def test_narrow_read_survives_a_dead_light_connection(tmp_path: Path) -> None:
    """判定必须落在主读连接上：轻读连接坏掉不得影响暂停判定（#24）。"""
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_node(_node('node:paused', is_paused=True, pause_requested=True))

    def broken_light(sql: str, params=()):
        raise AssertionError('判定类读取不许走轻读连接')

    store._fetchone_light = broken_light  # type: ignore[method-assign]

    assert store.get_node_pause_flags('node:paused') == {
        'pause_requested': True,
        'is_paused': True,
        'pause_reason': '',
    }
