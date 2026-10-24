"""`spawn_child_nodes.wait_all_children` holds child_final rows until the batch settles."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.runtime.internal_tools import SpawnChildNodesTool
from main.runtime.node_mailbox import notification_is_held
from main.service.runtime_service import MainRuntimeService

TASK = 'task:wait-all'
ROOT = 'node:root'
PARENT = 'node:parent'
CHILD1 = 'node:c1'
CHILD2 = 'node:c2'
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


def _node(node_id: str, *, parent: str | None, depth: int, status: str = 'in_progress', metadata=None):
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        status=status,
        goal=node_id,
        prompt='p',
        final_output='done' if status == 'success' else '',
        created_at=STAMP,
        updated_at=STAMP,
        metadata=dict(metadata or {}),
    )


def _seed(service: MainRuntimeService, *, wait_all: bool) -> None:
    service.store.upsert_task(
        TaskRecord(
            task_id=TASK,
            title='t',
            user_request='u',
            root_node_id=ROOT,
            session_id='web:test',
            created_at=STAMP,
            updated_at=STAMP,
        )
    )
    service.store.upsert_node(_node(ROOT, parent=None, depth=0))
    service.store.upsert_node(
        _node(
            PARENT,
            parent=ROOT,
            depth=1,
            metadata={
                'spawn_operations': {
                    'call:batch': {
                        'entries': [
                            {'child_node_id': CHILD1},
                            {'child_node_id': CHILD2},
                        ],
                        'completed': False,
                        'wait_all_children': wait_all,
                    }
                }
            },
        )
    )
    service.store.upsert_node(
        _node(
            CHILD1,
            parent=PARENT,
            depth=2,
            status='success',
            metadata={'spawn_owner_round_id': 'call:batch'},
        )
    )
    service.store.upsert_node(
        _node(
            CHILD2,
            parent=PARENT,
            depth=2,
            status='in_progress',
            metadata={'spawn_owner_round_id': 'call:batch'},
        )
    )


def _stub_wake(service: MainRuntimeService, monkeypatch) -> list[str]:
    woke: list[str] = []

    async def wake(*, task_id: str, node_id: str) -> str:
        woke.append(node_id)
        return 'resumed'

    monkeypatch.setattr(service, 'wake_node_on_message', wake)
    return woke


@pytest.mark.asyncio
async def test_wait_all_holds_then_releases_both_rows(tmp_path, monkeypatch):
    service = _service(tmp_path)
    _seed(service, wait_all=True)
    woke = _stub_wake(service, monkeypatch)

    first = await service._deliver_child_final_message(task_id=TASK, node_id=CHILD1)
    rows = service.store.list_task_node_notifications(TASK, PARENT)
    assert first['status'] == 'delivered'
    assert len(rows) == 1
    assert notification_is_held(rows[0]) is True
    assert rows[0].batch_id == 'call:batch'
    assert woke == []

    child2 = service.store.get_node(CHILD2)
    service.store.upsert_node(
        child2.model_copy(update={'status': 'success', 'final_output': 'done'})
    )
    second = await service._deliver_child_final_message(task_id=TASK, node_id=CHILD2)
    assert second['status'] == 'delivered'
    rows = service.store.list_task_node_notifications(TASK, PARENT)
    assert len(rows) == 2
    assert all(not notification_is_held(item) for item in rows)
    assert all(item.released_at for item in rows)
    assert woke == [PARENT]


@pytest.mark.asyncio
async def test_wait_all_false_delivers_each_row_immediately(tmp_path, monkeypatch):
    service = _service(tmp_path)
    _seed(service, wait_all=False)
    woke = _stub_wake(service, monkeypatch)

    await service._deliver_child_final_message(task_id=TASK, node_id=CHILD1)
    rows = service.store.list_task_node_notifications(TASK, PARENT)
    assert len(rows) == 1
    assert notification_is_held(rows[0]) is False
    assert woke == [PARENT]


@pytest.mark.asyncio
async def test_spawn_tool_forwards_wait_flag_and_echoes_batch():
    calls: list[tuple] = []

    async def spawn(specs, call_id, wait_all_children):
        calls.append((specs, call_id, wait_all_children))
        return []

    tool = SpawnChildNodesTool(spawn)
    raw = await tool.execute(
        children=[
            {'goal': 'g', 'prompt': 'p', 'execution_policy': {'mode': 'focus'}},
        ],
        wait_all_children=True,
        __g3ku_runtime={'current_tool_call_id': 'call:batch'},
    )
    payload = json.loads(raw)
    assert calls and calls[0][1] == 'call:batch' and calls[0][2] is True
    assert payload['wait_all_children'] is True
    assert payload['batch_id'] == 'call:batch'
