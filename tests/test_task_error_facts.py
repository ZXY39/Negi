"""Failure facts, error-log row kinds, per-node counts and the error reader.

The runtime reports facts, never a cause: the provider's raw error verbatim plus
the request parameters of the failed hop. Error rows are written on the node
that actually raised, and only `kind='failure'` rows feed pause bodies and
counts. This module covers those three contracts plus the two tool surfaces
(`task_list` / `task_progress`) and the `task_node_detail` reader.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from main.models import NodeRecord, TaskRecord
from main.monitoring.models import TaskProjectionNodeDetailRecord
from main.runtime.model_failure_facts import (
    EXCEPTION_FACTS_ATTR,
    attach,
    build_model_failure_facts,
    facts_for,
)
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore

TASK = 'task:error-facts'
ROOT = 'node:root'
CHILD = 'node:child'
STAMP = '2026-11-09T13:00:00+08:00'

RAW_PROVIDER_ERROR = (
    "BadRequestError: Error code: 400 - {'error': {'message': 'safety check failed', "
    "'type': 'failed_precondition_error', 'code': '9'}} code=9 status=400"
)


class _StatusError(RuntimeError):
    status_code = 400


def _service(tmp_path: Path) -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )


def _node(node_id: str, *, parent: str | None, depth: int = 0) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind='execution',
        goal=f'goal of {node_id}',
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
    )


def _seed(service: MainRuntimeService) -> None:
    service.store.upsert_task(
        TaskRecord(
            task_id=TASK,
            title='t',
            user_request='u',
            root_node_id=ROOT,
            created_at=STAMP,
            updated_at=STAMP,
        )
    )
    service.store.upsert_node(_node(ROOT, parent=None))
    service.store.upsert_node(_node(CHILD, parent=ROOT, depth=1))
    for node_id in (ROOT, CHILD):
        node = service.store.get_node(node_id)
        service.store.upsert_task_node(service.log_service._task_projection_node_record(node))
        service.store.upsert_task_node_detail(
            TaskProjectionNodeDetailRecord(node_id=node_id, task_id=TASK, updated_at=STAMP)
        )


def test_fact_card_is_verbatim_and_fact_only() -> None:
    exc = _StatusError(RAW_PROVIDER_ERROR)
    text = build_model_failure_facts(
        error=exc,
        diagnostics={
            'final_request_tokens': 282_874,
            'context_window_tokens': 500_000,
            'effective_trigger_tokens': 380_000,
            'estimate_source': 'usage_plus_delta',
            'provider_model': 'sensenova-6.8-flash',
            'compaction_applied': True,
            'compaction_mode': 'llm',
        },
        model_ref='sensenova-6.8-flash-lite',
        route_group='seva',
        route_index=1,
        route_escalation_count=1,
        route_escalation_reason='route_entry',
        request_message_count=184,
        actual_request_hash='abc123',
    )
    assert RAW_PROVIDER_ERROR in text
    assert 'http_status=400 request_shape=yes' in text
    assert 'member=sensenova-6.8-flash-lite wire_model=sensenova-6.8-flash' in text
    assert 'route_group=seva route_index=1 escalation_count=1' in text
    assert 'send_tokens=282874 ruler=usage_plus_delta' in text
    assert 'declared_window=500000 trigger_line=380000' in text
    assert 'request_messages=184 actual_request_hash=abc123 compaction=llm' in text
    assert 'runtime_action: none' in text
    # 只给事实：不写成因、不写处置建议。
    assert 'cause' not in text.lower()
    assert '请' not in text
    assert 'should ' not in text.lower()


def test_fact_card_attaches_to_the_exception() -> None:
    exc = RuntimeError('boom')
    assert facts_for(exc) == ''
    attach(exc, 'card')
    assert getattr(exc, EXCEPTION_FACTS_ATTR) == 'card'
    assert facts_for(exc) == 'card'


def test_legacy_span_rows_are_marked_diagnostic_on_open(tmp_path: Path) -> None:
    path = tmp_path / 'legacy.sqlite3'
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE task_error_logs ('
        'seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, node_id TEXT NOT NULL, '
        "node_title TEXT NOT NULL DEFAULT '', error_text TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO task_error_logs (task_id, node_id, error_text, created_at) "
        "VALUES ('task:old', 'node:old', 'model_size_span: legacy readout', '2026-11-09T13:00:00+08:00')"
    )
    conn.execute('PRAGMA user_version = 1')
    conn.commit()
    conn.close()

    store = SQLiteTaskStore(path)
    try:
        rows = store.list_task_error_logs('task:old')
        assert [row.kind for row in rows] == ['diagnostic']
        assert store.count_task_error_logs_by_node('task:old') == {}
    finally:
        store.close()


def test_diagnostic_rows_do_not_feed_pause_or_counts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service)
    failure = service.log_service.append_task_error_log(TASK, ROOT, error_text='raw failure')
    service.log_service.append_task_error_log(
        TASK,
        ROOT,
        error_text='model_size_span: legacy diagnostic',
        kind='diagnostic',
    )
    service.log_service.set_node_pause_state(
        TASK,
        ROOT,
        pause_requested=True,
        is_paused=True,
        pause_reason='error',
        remark='raw failure',
    )

    pauses = service.store.list_new_error_pauses()
    assert [item['error_text'] for item in pauses] == ['raw failure']
    assert service.store.count_task_error_logs_by_node(TASK) == {ROOT: 1}
    assert service.store.latest_task_error_log(TASK).seq == failure.seq
    counts = service.store.count_task_error_logs_by_task([TASK])
    assert counts[TASK] == {'error_count': 1, 'node_count': 1}


def test_long_error_text_is_externalized_and_keeps_a_ref(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service)

    class _FakeContentStore:
        def summarize_for_storage(self, value, **_kwargs):
            return 'summary text', 'artifact:errorfull'

    service.log_service._content_store = _FakeContentStore()
    long_text = 'x' * (service.log_service._ERROR_LOG_INLINE_TEXT_LIMIT + 100)
    record = service.log_service.append_task_error_log(TASK, ROOT, error_text=long_text)
    assert record.text_ref == 'artifact:errorfull'
    assert record.error_text == 'summary text'


def test_tools_show_counts_and_point_at_the_error_reader(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _seed(service)
    service.log_service.append_task_error_log(TASK, ROOT, error_text='root failure')
    service.log_service.append_task_error_log(TASK, CHILD, error_text='child failure')

    items = service.query_service.get_tasks(None)
    item = next(entry for entry in items if entry.task_id == TASK)
    assert item.error_count == 2
    assert item.error_node_count == 2
    list_text = service.get_tasks(None)
    assert '错误：2（2 个节点）' in list_text

    progress_text = service.view_progress(TASK)
    assert '错误: 2 条（2 个节点' in progress_text
    assert '错误:1' in progress_text
    assert f'task_node_detail(task_id={TASK}' in progress_text

    payload = service.get_node_detail_payload(TASK, CHILD)
    assert payload is not None
    item_payload = payload['item']
    assert item_payload['error_count'] == 1
    assert item_payload['errors'][-1]['error_text'] == 'child failure'
