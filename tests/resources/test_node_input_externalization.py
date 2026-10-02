"""`nodes.input` 正文外置（治本 B1–B3 的输入侧）。

契约：
1) 超过内联阈值的 input 正文（含消息列表）落 artifact，行内只留 envelope 摘要 + `input_ref`；
   要 verbatim 的读点走 `resolve_node_input_text`，字节级原样返回。
2) 短正文与历史行（ref 为空）仍按行内正文读，不引入第二次读盘。
3) 每个 (task, node) 只留一份 `node_input` artifact：`update_node_input` 每回合都跑，
   非单例会让正文按回合数线性堆积。
4) 终态清理必须保留 `node_input`：行内已不再存正文，删了就等于删掉唯一副本。
"""

from __future__ import annotations

import json
from pathlib import Path

from g3ku.content import ContentNavigationService
from main.models import NodeRecord, TaskRecord, TokenUsageSummary
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.service.runtime_service import MainRuntimeService
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore

TASK_ID = 'task:nodeinput'
NODE_ID = 'node:1'


def _services(tmp_path: Path) -> tuple[SQLiteTaskStore, TaskLogService, TaskArtifactStore]:
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    store.upsert_task(TaskRecord(
        task_id=TASK_ID, session_id='web:shared', title='demo', user_request='demo',
        status='in_progress', root_node_id=NODE_ID, max_depth=1,
        created_at='2026-10-02T10:00:00+08:00', updated_at='2026-10-02T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True), metadata={},
    ))
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    content_store = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    log_service = TaskLogService(
        store=store,
        file_store=TaskFileStore(tmp_path / 'files'),
        registry=None,
        event_history_enabled=False,
        content_store=content_store,
    )
    log_service.create_node(TASK_ID, NodeRecord(
        node_id=NODE_ID, task_id=TASK_ID, parent_node_id=None, root_node_id=NODE_ID,
        depth=0, node_kind='execution', status='in_progress', goal='demo', prompt='demo',
        input='', output=[], check_result='', final_output='', can_spawn_children=False,
        created_at='2026-10-02T10:00:00+08:00', updated_at='2026-10-02T10:00:00+08:00',
        token_usage=TokenUsageSummary(tracked=True),
    ))
    return store, log_service, artifact_store


def _messages(count: int, size: int) -> str:
    return json.dumps(
        [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': ('m%d' % i) + ('x' * size)} for i in range(count)],
        ensure_ascii=False,
    )


class _Artifact:
    def __init__(self, kind: str, title: str) -> None:
        self.kind = kind
        self.title = title


def test_big_message_list_is_externalized_and_resolves_byte_exact(tmp_path: Path) -> None:
    store, log_service, _artifacts = _services(tmp_path)
    body = _messages(40, 400)
    assert len(body) > 12_000

    log_service.update_node_input(TASK_ID, NODE_ID, body)

    row = store.get_node(NODE_ID)
    assert str(row.input_ref or '').strip(), 'input_ref 应指向外置正文'
    assert len(str(row.input or '')) < len(body) // 4, '行内不该再带整份正文'
    assert log_service.resolve_node_input_text(row) == body


def test_small_input_stays_inline_without_a_ref(tmp_path: Path) -> None:
    store, log_service, _artifacts = _services(tmp_path)

    log_service.update_node_input(TASK_ID, NODE_ID, '[{"role":"user","content":"short"}]')

    row = store.get_node(NODE_ID)
    assert not str(row.input_ref or '').strip()
    assert log_service.resolve_node_input_text(row) == '[{"role":"user","content":"short"}]'


def test_legacy_inline_row_still_resolves(tmp_path: Path) -> None:
    """历史行（ref 为空、正文内联）必须原样读出——这是「不批量迁移」的前提。"""
    store, log_service, _artifacts = _services(tmp_path)
    legacy = '[{"role":"user","content":"' + ('y' * 50_000) + '"}]'

    store.update_node(NODE_ID, lambda record: record.model_copy(update={'input': legacy, 'input_ref': ''}))

    assert log_service.resolve_node_input_text(store.get_node(NODE_ID)) == legacy


def test_repeated_turns_keep_one_node_input_artifact(tmp_path: Path) -> None:
    """`update_node_input` 每回合都跑：正文必须原地替换，不按回合堆积。"""
    store, log_service, artifact_store = _services(tmp_path)

    for index in range(6):
        log_service.update_node_input(TASK_ID, NODE_ID, _messages(30, 300) + str(index))

    node_inputs = [record for record in artifact_store.list_artifacts(TASK_ID) if str(record.kind) == 'node_input']
    assert len(node_inputs) == 1, '单例失效：攒了 %d 份正文副本' % len(node_inputs)
    assert log_service.resolve_node_input_text(store.get_node(NODE_ID)).endswith('5')


def test_resume_snapshot_reads_messages_from_the_externalized_body(tmp_path: Path) -> None:
    store, log_service, _artifacts = _services(tmp_path)
    body = _messages(8, 800)
    log_service.update_node_input(TASK_ID, NODE_ID, body)

    snapshot = log_service.capture_retry_resume_snapshot(TASK_ID, NODE_ID, failure_reason='net')

    assert snapshot is not None
    assert snapshot['node_input_text'] == body
    messages = list(snapshot['frame'].get('messages') or [])
    assert [str(item.get('content') or '')[:3] for item in messages[:2]] == ['m0x', 'm1x']


def test_terminal_cleanup_keeps_node_input_artifacts() -> None:
    service = object.__new__(MainRuntimeService)
    keep = MainRuntimeService._terminal_artifact_keep_policy

    assert keep(service, None, _Artifact('node_input', 'node-input:node:1')) is True
    assert keep(service, None, _Artifact('patch', 'x')) is True
    assert keep(service, None, _Artifact('node_result_payload', 'payload')) is False
