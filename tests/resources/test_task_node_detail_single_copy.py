"""明细/工具结果表的单份存储契约。

同一段正文曾在库里存多次：平铺列 + payload_json 顶层 + payload_json.payload
嵌套（工具结果表另有 parsed_payload）。本文件钉住三件事：

- 两张表都只剩主键/索引列 + payload_json；
- payload 嵌套字典不允许出现与平铺字段同名的键；
- 存量库的瘦身是**一次**表重建，不是按列数线性的 N 次 DROP COLUMN
  （11 次整表重写实测把启动阻塞近 3 分钟并把 WAL 顶到 2.7 GB）。
"""

from __future__ import annotations

import json
import sqlite3

from main.monitoring.models import TaskProjectionNodeDetailRecord, TaskProjectionToolResultRecord
from main.storage.sqlite_store import SQLiteTaskStore

_DETAIL_COLUMNS = ['node_id', 'task_id', 'updated_at', 'payload_json']
_TOOL_COLUMNS = ['task_id', 'node_id', 'tool_call_id', 'order_index', 'payload_json']

# 平铺字段名：只允许出现在 payload_json 顶层，不允许再进 payload 嵌套字典。
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

_TOOL_FLAT_FIELDS = [
    'task_id',
    'node_id',
    'tool_call_id',
    'order_index',
    'tool_name',
    'arguments_text',
    'status',
    'started_at',
    'finished_at',
    'elapsed_seconds',
    'output_preview_text',
    'output_ref',
    'ephemeral',
]

_BODY = '[{"role":"system","content":"' + ('x' * 4096) + '"}]'
_ARGS = '{"path":"' + ('a' * 2048) + '"}'
_PREVIEW = 'tool output body: ' + ('b' * 2048)


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


def _tool_record(tool_call_id: str = 'call:1', task_id: str = 'task:demo', node_id: str = 'node:root', **overrides):
    base = {
        'task_id': task_id,
        'node_id': node_id,
        'tool_call_id': tool_call_id,
        'order_index': 7,
        'tool_name': 'filesystem_edit',
        'arguments_text': _ARGS,
        'status': 'completed',
        'started_at': '2026-09-24T10:00:00+08:00',
        'finished_at': '2026-09-24T10:00:02+08:00',
        'elapsed_seconds': 2.0,
        'output_preview_text': _PREVIEW,
        'output_ref': 'artifact:deadbeef',
        'ephemeral': False,
        'payload': {'parsed_payload': {'kind': 'filesystem_edit', 'ok': True}},
    }
    base.update(overrides)
    return TaskProjectionToolResultRecord(**base)


def _table_columns(store: SQLiteTaskStore, table: str) -> list[str]:
    return [str(row[1]) for row in store._conn.execute(f'PRAGMA table_info({table})').fetchall()]


def _index_names(store: SQLiteTaskStore) -> set[str]:
    return {
        str(row[0])
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }


# --- 新库形状 ---


def test_fresh_store_keeps_only_routing_columns(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        assert _table_columns(store, 'task_node_details') == _DETAIL_COLUMNS
        assert _table_columns(store, 'task_node_tool_results') == _TOOL_COLUMNS
    finally:
        store.close()


def test_detail_row_stores_body_text_once(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_node_detail(_record())
        raw = store._conn.execute(
            'SELECT payload_json FROM task_node_details WHERE node_id = ?', ('node:root',)
        ).fetchone()[0]
        parsed = json.loads(raw)

        assert parsed['input_text'] == _BODY
        assert 'input_text' not in parsed['payload']
        assert raw.count(json.dumps(_BODY)[1:-1]) == 1
        assert sorted(set(parsed['payload']) & set(_FLAT_FIELD_NAMES)) == []
        for key in ('parent_node_id', 'depth', 'node_kind', 'status', 'goal', 'token_usage_by_model'):
            assert key in parsed['payload'], key
    finally:
        store.close()


def test_tool_result_row_stores_text_once_and_roundtrips(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_node_tool_result(_tool_record())
        raw = store._conn.execute(
            'SELECT payload_json FROM task_node_tool_results'
        ).fetchone()[0]
        parsed = json.loads(raw)

        assert parsed['arguments_text'] == _ARGS
        assert parsed['output_preview_text'] == _PREVIEW
        assert raw.count(json.dumps(_ARGS)[1:-1]) == 1
        assert raw.count(json.dumps(_PREVIEW)[1:-1]) == 1
        assert sorted(set(parsed['payload']) & set(_TOOL_FLAT_FIELDS)) == []
        # parsed_payload 是列里没有的第三份内容，必须留在嵌套里
        assert parsed['payload']['parsed_payload']['kind'] == 'filesystem_edit'

        records = store.list_task_node_tool_results('task:demo', 'node:root')
        assert len(records) == 1
        assert records[0].tool_name == 'filesystem_edit'
        assert records[0].elapsed_seconds == 2.0
        assert records[0].ephemeral is False
        assert records[0].order_index == 7
    finally:
        store.close()


def test_tool_result_upsert_updates_in_place_on_composite_key(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    try:
        store.upsert_task_node_tool_result(_tool_record('call:1', status='queued'))
        store.upsert_task_node_tool_result(_tool_record('call:1', status='completed'))
        store.upsert_task_node_tool_result(_tool_record('call:2', status='queued'))

        rows = store.list_task_node_tool_results('task:demo', 'node:root')
        assert len(rows) == 2
        assert next(row for row in rows if row.tool_call_id == 'call:1').status == 'completed'
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
            assert (detail.payload.get(key) or getattr(detail, key)) is not None
        assert detail.payload['goal'] == 'goal text'
    finally:
        store.close()


# --- 存量库迁移 ---

_LEGACY_DETAIL_DDL = '''
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

_LEGACY_TOOL_DDL = '''
        CREATE TABLE task_node_tool_results (
            task_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            tool_call_id TEXT NOT NULL,
            order_index INTEGER NOT NULL,
            tool_name TEXT NOT NULL,
            arguments_text TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            elapsed_seconds REAL,
            output_preview_text TEXT NOT NULL,
            output_ref TEXT NOT NULL,
            ephemeral INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (task_id, node_id, tool_call_id)
        )
'''


def _create_legacy_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(_LEGACY_DETAIL_DDL)
    conn.execute(_LEGACY_TOOL_DDL)
    # 旧形状 = 平铺列 + payload_json 顶层 + payload 嵌套，同一段正文三份。
    flat = {key: _BODY for key in _FLAT_FIELD_NAMES}
    flat.update({'node_id': 'node:legacy', 'task_id': 'task:old'})
    detail_json = json.dumps({**flat, 'payload': dict(flat, goal='old goal', execution_trace_summary={})})
    conn.execute(
        'INSERT INTO task_node_details (node_id, task_id, updated_at, input_text, input_ref, output_text, '
        'output_ref, check_result, check_result_ref, final_output, final_output_ref, failure_reason, '
        'prompt_summary, execution_trace_ref, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            'node:legacy', 'task:old', '2026-09-01T10:00:00+08:00', _BODY, '', 'out', '', '', '', '', '',
            '', 'p', '', detail_json,
        ),
    )
    tool_flat = {key: _ARGS for key in _TOOL_FLAT_FIELDS}
    tool_flat.update({
        'order_index': 3,
        'elapsed_seconds': 1.0,
        'ephemeral': False,
        'output_preview_text': _PREVIEW,
        'output_ref': '',
        'task_id': 'task:old',
        'node_id': 'node:legacy',
        'tool_call_id': 'call:legacy',
        'tool_name': 'exec',
        'status': 'completed',
    })
    tool_json = json.dumps({**tool_flat, 'payload': {'parsed_payload': {'k': 1}}})
    conn.execute(
        'INSERT INTO task_node_tool_results (task_id, node_id, tool_call_id, order_index, tool_name, '
        'arguments_text, status, started_at, finished_at, elapsed_seconds, output_preview_text, output_ref, '
        'ephemeral, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (
            'task:old', 'node:legacy', 'call:legacy', 3, 'exec', _ARGS, 'completed',
            '2026-09-01T10:00:00+08:00', '2026-09-01T10:00:01+08:00', 1.0, _PREVIEW, '', 0, tool_json,
        ),
    )
    conn.commit()
    conn.close()


def test_legacy_db_migrates_and_stays_readable(tmp_path) -> None:
    path = tmp_path / 'runtime.sqlite3'
    _create_legacy_db(path)

    store = SQLiteTaskStore(path)
    try:
        assert _table_columns(store, 'task_node_details') == _DETAIL_COLUMNS
        assert _table_columns(store, 'task_node_tool_results') == _TOOL_COLUMNS
        # 重建丢弃了旧索引，索引段必须把它们再建回来
        assert 'idx_task_node_tool_results_task_node_order' in _index_names(store)
        assert 'idx_task_node_details_task_id' in _index_names(store)

        # 旧行的 payload_json 原样保留，读侧仍拿得到正文
        legacy = store.get_task_node_detail('node:legacy')
        assert legacy is not None
        assert legacy.input_text == _BODY
        assert legacy.payload['input_text'] == _BODY
        legacy_tools = store.list_task_node_tool_results('task:old', 'node:legacy')
        assert len(legacy_tools) == 1
        assert legacy_tools[0].arguments_text == _ARGS
        assert legacy_tools[0].output_ref == ''

        # 重写后按新形状落盘，重复键消失
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

    # 迁移是一次性的：再次打开不得因为列已不存在而报错或重复重建
    reopened = SQLiteTaskStore(path)
    try:
        assert _table_columns(reopened, 'task_node_details') == _DETAIL_COLUMNS
        assert _table_columns(reopened, 'task_node_tool_results') == _TOOL_COLUMNS
        assert reopened.get_task_node_detail('node:legacy') is not None
        assert len(reopened.list_task_node_tool_results('task:old', 'node:legacy')) == 1
    finally:
        reopened.close()


class _RecordingConnection:
    """记录发往 SQLite 的语句，用来钉住"一次重建而不是 N 次 DROP COLUMN"。"""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql, params=()):
        self.statements.append(' '.join(str(sql).split()))
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_slimming_issues_one_table_rebuild_per_table(tmp_path) -> None:
    path = tmp_path / 'runtime.sqlite3'
    _create_legacy_db(path)
    conn = sqlite3.connect(str(path))
    recorder = _RecordingConnection(conn)
    try:
        for table, legacy in SQLiteTaskStore._LEGACY_BODY_COLUMNS:
            SQLiteTaskStore._drop_legacy_columns(recorder, table, legacy)
        joined = '\n'.join(recorder.statements)
        assert 'DROP COLUMN' not in joined
        for table, _legacy in SQLiteTaskStore._LEGACY_BODY_COLUMNS:
            creates = [item for item in recorder.statements if item.startswith(f'CREATE TABLE {table}__slim')]
            assert len(creates) == 1, table
            assert 'DROP TABLE' in joined
        # 主键定义必须被合成回来，否则 composite ON CONFLICT 会失效
        tool_create = next(item for item in recorder.statements if 'task_node_tool_results__slim (' in item)
        assert 'PRIMARY KEY (task_id, node_id, tool_call_id)' in tool_create
        detail_create = next(item for item in recorder.statements if 'task_node_details__slim (' in item)
        assert 'PRIMARY KEY (node_id)' in detail_create
    finally:
        conn.close()


def test_slimming_is_a_noop_when_already_lean(tmp_path) -> None:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    recorder = _RecordingConnection(store._conn)
    try:
        for table, legacy in SQLiteTaskStore._LEGACY_BODY_COLUMNS:
            before = recorder.statements
            SQLiteTaskStore._drop_legacy_columns(recorder, table, legacy)
            assert [item for item in recorder.statements if item not in before] == []
    finally:
        store.close()
