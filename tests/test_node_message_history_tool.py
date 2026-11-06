"""Node message-history tool: all received rows, artifact ref, held contents hidden."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.runtime.append_notice_context import (
    APPEND_NOTICE_CONTEXT_KEY,
    PENDING_APPEND_NOTICE_RECORDS_KEY,
)
from main.service.runtime_service import MainRuntimeService

TASK = 'task:history'
ROOT = 'node:root'
CHILD = 'node:child'
STAMP = '2026-10-25T00:00:00+08:00'


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


def _seed(service):
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
    for item in (
        NodeRecord(
            node_id=ROOT,
            task_id=TASK,
            parent_node_id=None,
            root_node_id=ROOT,
            depth=0,
            goal='root',
            prompt='p',
            created_at=STAMP,
            updated_at=STAMP,
        ),
        NodeRecord(
            node_id=CHILD,
            task_id=TASK,
            parent_node_id=ROOT,
            root_node_id=ROOT,
            depth=1,
            goal='child',
            prompt='p',
            created_at=STAMP,
            updated_at=STAMP,
        ),
    ):
        service.store.upsert_node(item)


@pytest.mark.asyncio
async def test_history_returns_artifact_and_hides_held_content(tmp_path, monkeypatch):
    service = _service(tmp_path)
    _seed(service)

    async def wake(*, task_id, node_id, allow_pause_clear: bool = False):
        return 'resumed'

    monkeypatch.setattr(service, 'wake_node_on_message', wake)
    await service.deliver_node_message(
        task_id=TASK,
        target_node_id=ROOT,
        message='visible body',
        origin='node',
        relation='child',
        source_node_id=CHILD,
    )
    await service.deliver_node_message(
        task_id=TASK,
        target_node_id=ROOT,
        message='held body',
        origin='node',
        relation='child',
        source_node_id=CHILD,
        held=True,
        batch_id='call:1',
    )

    payload = await service.node_runner._view_message_history(
        task=service.store.get_task(TASK),
        node=service.store.get_node(ROOT),
    )
    assert payload['ok'] is True
    assert payload['count'] == 2
    assert str(payload['ref']).startswith('artifact:')
    text = service.log_service.resolve_content_ref(payload['ref'])
    assert 'visible body' in text
    assert 'held body' not in text
    assert '"status": "held"' in text


@pytest.mark.asyncio
async def test_history_includes_ledger_and_pending_notices(tmp_path, monkeypatch):
    service = _service(tmp_path)
    _seed(service)

    async def wake(*, task_id, node_id, allow_pause_clear: bool = False):
        return 'resumed'

    monkeypatch.setattr(service, 'wake_node_on_message', wake)
    await service.deliver_node_message(
        task_id=TASK,
        target_node_id=ROOT,
        message='consumed body',
        origin='node',
        relation='child',
        source_node_id=CHILD,
    )

    def _write_context(metadata):
        metadata[APPEND_NOTICE_CONTEXT_KEY] = {
            'notice_records': [
                {
                    'notification_id': 'root-notice:1',
                    'epoch_id': 'epoch:1',
                    'source_node_id': CHILD,
                    'kind': 'user_direct',
                    'message': 'consumed body',
                    'received_at': STAMP,
                    'consumed_at': STAMP,
                    'merged_at': STAMP,
                    'compression_stage_id': '',
                    'superseded_at': '',
                },
                {
                    'notification_id': 'root-notice:2',
                    'epoch_id': 'epoch:1',
                    'source_node_id': CHILD,
                    'kind': 'user_direct',
                    'message': 'superseded body',
                    'received_at': STAMP,
                    'consumed_at': STAMP,
                    'merged_at': STAMP,
                    'compression_stage_id': '',
                    'superseded_at': STAMP,
                },
            ],
            'compression_segments': [],
        }
        metadata[PENDING_APPEND_NOTICE_RECORDS_KEY] = [
            {
                'notification_id': 'root-notice:3',
                'source_node_id': ROOT,
                'message': 'pending body',
                'created_at': STAMP,
                'order_index': 0,
                'processed_at': '',
            }
        ]
        return metadata

    service.log_service.update_node_metadata(ROOT, _write_context)
    payload = await service.node_runner._view_message_history(
        task=service.store.get_task(TASK),
        node=service.store.get_node(ROOT),
    )
    assert payload['ok'] is True
    # 同文双写去重：信箱行与台账记录只算一条。
    assert payload['count'] == 3
    text = service.log_service.resolve_content_ref(payload['ref'])
    assert 'consumed body' in text
    assert 'superseded body' in text
    assert 'pending body' in text
    assert '"status": "superseded"' in text
    assert '"status": "pending_append"' in text
    assert '"source": "notice_ledger"' in text
