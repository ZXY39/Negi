"""Node message-history tool: all received rows, artifact ref, held contents hidden."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
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

    async def wake(*, task_id, node_id):
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
