"""Node-side terminate is a subtree terminate; the sender's verifier is not controllable."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.service.runtime_service import MainRuntimeService

TASK = 'task:terminate'
ROOT = 'node:root'
SENDER = 'node:sender'
CHILD = 'node:child'
GRANDCHILD = 'node:grandchild'
CHILD_VERIFIER = 'node:child-verifier'
SENDER_VERIFIER = 'node:sender-verifier'
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


def _node(node_id, *, parent, depth, kind='execution', accepted=''):
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind=kind,
        goal=node_id,
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        metadata={'accepted_node_id': accepted} if accepted else {},
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
    for item in [
        _node(ROOT, parent=None, depth=0),
        _node(SENDER, parent=ROOT, depth=1),
        _node(CHILD, parent=SENDER, depth=2),
        _node(GRANDCHILD, parent=CHILD, depth=3),
        _node(CHILD_VERIFIER, parent=CHILD, depth=3, kind='acceptance', accepted=CHILD),
        _node(SENDER_VERIFIER, parent=SENDER, depth=2, kind='acceptance', accepted=SENDER),
    ]:
        service.store.upsert_node(item)


@pytest.mark.asyncio
async def test_terminate_cascades_including_descendant_verifier(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    result = await service._manage_child_nodes_from_node(
        task_id=TASK,
        sender_node_id=SENDER,
        ops=[{'node_id': CHILD, 'action': 'terminate', 'reason': 'give up branch'}],
    )

    assert result['ok'] is True
    assert set(result['items'][0]['terminated_node_ids']) == {
        CHILD,
        GRANDCHILD,
        CHILD_VERIFIER,
    }
    for node_id in (CHILD, GRANDCHILD, CHILD_VERIFIER):
        assert service.store.get_node(node_id).status == 'failed'
    assert service.store.get_node(SENDER).status == 'in_progress'
    assert service.store.get_node(SENDER_VERIFIER).status == 'in_progress'


@pytest.mark.asyncio
async def test_sender_cannot_terminate_its_own_verifier(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    result = await service._manage_child_nodes_from_node(
        task_id=TASK,
        sender_node_id=SENDER,
        ops=[{'node_id': SENDER_VERIFIER, 'action': 'terminate', 'reason': 'no'}],
    )

    assert result['ok'] is False
    assert result['items'][0]['reason'] == 'acceptance_node_not_controllable'
    assert service.store.get_node(SENDER_VERIFIER).status == 'in_progress'
