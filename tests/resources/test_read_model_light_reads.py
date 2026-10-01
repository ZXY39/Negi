from __future__ import annotations

import json
from pathlib import Path

from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:lightreads'
NODE_ID = 'node:a'


class _ContentStore:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages
        self.hits = 0

    def _resolve(self, *, ref: str, path: object) -> tuple[str, None]:
        self.hits += 1
        return json.dumps({'messages': self._messages}, ensure_ascii=False), None


def _service(tmp_path: Path) -> tuple[TaskLogService, _ContentStore]:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(TaskRecord(
        task_id=TASK_ID, session_id='web:shared', title='demo', user_request='demo',
        status='in_progress', root_node_id=NODE_ID, max_depth=1,
        created_at='2026-10-01T10:00:00+08:00', updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True), metadata={},
    ))
    store.upsert_node(NodeRecord(
        node_id=NODE_ID, task_id=TASK_ID, parent_node_id=None, root_node_id=NODE_ID,
        depth=0, node_kind='execution', status='in_progress', goal='demo', prompt='demo',
        input='demo', output=[], check_result='', final_output='', can_spawn_children=False,
        created_at='2026-10-01T10:00:00+08:00', updated_at='2026-10-01T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
    ))
    content_store = _ContentStore([
        {'role': 'assistant', 'content': 'first'},
        {'role': 'tool', 'content': 'second'},
    ])
    service = TaskLogService(
        store=store, file_store=TaskFileStore(tmp_path / 'files'), registry=None,
        event_history_enabled=False, content_store=content_store,
    )
    service.upsert_frame(TASK_ID, {
        'node_id': NODE_ID, 'depth': 0, 'node_kind': 'execution', 'phase': 'running',
        'active': False, 'runnable': True, 'waiting': False,
        'stage_goal': 'demo goal', 'stage_status': '进行中',
        'messages_ref': 'content:messages/node-a', 'messages_count': 2,
        'tool_calls': [{'tool_call_id': 'call-1', 'name': 'filesystem_read', 'status': 'running'}],
    })
    return service, content_store


def test_update_frame_does_not_resolve_the_message_blob(tmp_path) -> None:
    """读-改-写一帧不该把会话历史读回来：指针在，正文就还在原地。"""
    service, content_store = _service(tmp_path)

    service.update_frame(TASK_ID, NODE_ID, lambda frame: {**frame, 'stage_goal': 'changed'})

    assert content_store.hits == 0
    record = service._store.get_task_runtime_frame(TASK_ID, NODE_ID)
    payload = dict(record.payload or {})
    assert payload['stage_goal'] == 'changed'
    # 指针与计数必须原样留着——写回时 mutator 没带 messages，护栏靠这两个字段续命
    assert payload['messages_ref'] == 'content:messages/node-a'
    assert int(payload['messages_count'] or 0) == 2


def test_history_is_still_readable_after_a_light_update(tmp_path) -> None:
    service, content_store = _service(tmp_path)

    service.update_frame(TASK_ID, NODE_ID, lambda frame: {**frame, 'stage_goal': 'changed'})
    frame = service.read_runtime_frame(TASK_ID, NODE_ID) or {}

    assert [item['content'] for item in frame['messages']] == ['first', 'second']
    assert content_store.hits == 1
