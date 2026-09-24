"""task_node_details 单份存储契约。

同一段节点正文曾在库里出现 4 次（平铺列 / payload_json 顶层 / payload_json.payload
嵌套 / nodes.payload_json）。本文件钉住这条表内不再重复的两半：

- 平铺正文列在建表与存量迁移后都不存在；
- payload 嵌套字典里不允许出现与平铺字段同名的键。
"""

from __future__ import annotations

import json
import sqlite3

from main.monitoring.models import TaskProjectionNodeDetailRecord
from main.storage.sqlite_store import SQLiteTaskStore

_LEAN_COLUMNS = ['node_id', 'task_id', 'updated_at', 'payload_json']

# 平铺字段名：这些键只允许出现在 payload_json 顶层，不允许再进 payload 嵌套字典。
_FLAT_FIELD_NAMES = [
    'node_id',
    'task_id',
    'updated_at',
    'input_text',
    'input_ref',
    'output_text',
    'output_ref',
    'check_result',
    'check_result_ref',
    'final_output',
    'final_output_ref',
    'failure_reason',
    'prompt_summary',
    'execution_trace_ref',
]

_BODY = '[{"role":"system","content":"' + ('x' * 4096) + '"}]'


def _record(node_id: str = 'node:root', task_id: str = 'task:demo') -> TaskProjectionNodeDetailRecord:
    return TaskProjectionNodeDetailRecord(
        node_id=node_id,
        task_id=task_id,
        updated_at='2026-09-24T10:00:00+08:00',
        input_text=_BODY,
        output_text='done',
        prompt_summary='p',
        payload={
            'parent_node_id': None,
            'depth': 0,
            'node_kind': 'execution',
            'status': 'success',
            'goal': 'goal text',
            'token_usage_by_model': [{'model': 'm', 'prompt_tokens': 1}],
            'execution_trace_summary': {'steps': 1},
        },
    )


def _table_columns(store: SQLiteTaskStore, table: str) -> list[str]:
    return [str(row[1]) for row in store._conn.execute(f'PRAGMA table_info({table})').fetchall()]


def test_fresh_store_keeps_only_routing_columns(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        assert _table_columns(store, 'task_node_details') == _LEAN_COLUMNS
    finally:
        store.close()


def test_written_row_stores_body_text_once(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_node_detail(_record())
        raw = store._conn.execute(
            'SELECT payload_json FROM task_node_details WHERE node_id = ?', ('node:root',)
        ).fetchone()[0]
        parsed = json.loads(raw)

        assert parsed['input_text'] == _BODY
        assert 'input_text' not in parsed['payload']
        # 正文在整行里只出现一次；嵌进 payload_json 时是转义态，按转义形式比对。
        assert raw.count(json.dumps(_BODY)[1:-1]) == 1

        # 平铺字段名一律不得作为 payload 嵌套键回潮。
        shadowed = sorted(set(parsed['payload']) & set(_FLAT_FIELD_NAMES))
        assert shadowed == []

        # 没有平铺字段承载的键必须留下，否则读侧会静默丢数据。
        for key in ('parent_node_id', 'depth', 'node_kind', 'status', 'goal', 'token_usage_by_model'):
            assert key in parsed['payload'], key
    finally:
        store.close()


def test_replace_writes_same_lean_shape(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.replace_task_node_details('task:demo', [_record('node:a'), _record('node:b')])
        rows = store._conn.execute(
            'SELECT node_id, payload_json FROM task_node_details WHERE task_id = ? ORDER BY node_id',
            ('task:demo',),
        ).fetchall()
        assert [str(row[0]) for row in rows] == ['node:a', 'node:b']
        for row in rows:
            payload = json.loads(str(row[1]))['payload']
            assert set(payload) & set(_FLAT_FIELD_NAMES) == set()
    finally:
        store.close()


def test_record_roundtrip_reads_flat_fields_without_payload_shadow(tmp_path) -> None:
    """读侧形状：正文只在顶层，payload.get(k) 靠 record.k 兜底仍然取得到。"""
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_node_detail(_record())
        detail = store.get_task_node_detail('node:root')
        assert detail is not None
        assert detail.input_text == _BODY
        assert detail.output_text == 'done'
        for key in _FLAT_FIELD_NAMES:
            assert key not in detail.payload
            # query_service 的取值式：payload.get(key) or getattr(detail, key)
            assert (detail.payload.get(key) or getattr(detail, key)) is not None
        assert detail.payload['goal'] == 'goal text'
    finally:
        store.close()


def _create_legacy_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        '''
        CREATE TABLE task_node_details (
            node_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            input_text TEXT NOT NULL,
            input_ref TEXT NOT NULL,
            output_text TEXT NOT NULL,
            output_ref TEXT NOT NULL,
            check_result TEXT NOT NULL,
            check_result_ref TEXT NOT NULL,
            final_output TEXT NOT NULL,
            final_output_ref TEXT NOT NULL,
            failure_reason TEXT NOT NULL,
            prompt_summary TEXT NOT NULL,
            execution_trace_ref TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        '''
    )
    # 旧形状 = 平铺列 + payload_json 顶层 + payload 嵌套，同一段正文三份。
    flat = {key: _BODY for key in _FLAT_FIELD_NAMES}
    flat.update({'node_id': 'node:legacy', 'task_id': 'task:old'})
    legacy_json = json.dumps({**flat, 'payload': dict(flat, goal='old goal', execution_trace_summary={})})
    conn.execute(
        'INSERT INTO task_node_details (node_id, task_id, updated_at, input_text, input_ref, output_text, '
        'output_ref, check_result, check_result_ref, final_output, final_output_ref, failure_reason, '
        'prompt_summary, execution_trace_ref, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            'node:legacy', 'task:old', '2026-09-01T10:00:00+08:00', _BODY, '', 'out', '', '', '', '', '',
            '', 'p', '', legacy_json,
        ),
    )
    conn.commit()
    conn.close()


def test_legacy_db_migrates_and_stays_readable(tmp_path) -> None:
    path = tmp_path / 'runtime.sqlite3'
    _create_legacy_db(path)

    store = SQLiteTaskStore(path)
    try:
        assert _table_columns(store, 'task_node_details') == _LEAN_COLUMNS
        # 旧行的 payload_json 原样保留，读侧仍拿得到正文。
        legacy = store.get_task_node_detail('node:legacy')
        assert legacy is not None
        assert legacy.input_text == _BODY
        assert legacy.payload['input_text'] == _BODY
        # 重写后按新形状落盘，重复键消失。
        store.upsert_task_node_detail(_record('node:legacy', 'task:old'))
        stored = json.loads(
            store._conn.execute(
                'SELECT payload_json FROM task_node_details WHERE node_id = ?', ('node:legacy',)
            ).fetchone()[0]
        )
        assert set(stored['payload']) & set(_FLAT_FIELD_NAMES) == set()
        assert stored['input_text'] == _BODY
    finally:
        store.close()

    # 迁移是一次性的：再次打开不应因为列已不存在而报错。
    reopened = SQLiteTaskStore(path)
    try:
        assert _table_columns(reopened, 'task_node_details') == _LEAN_COLUMNS
        assert reopened.get_task_node_detail('node:legacy') is not None
    finally:
        reopened.close()
