"""`spawn_child_nodes.wait_all_children` keeps a batch quiet: every result lands as an ordinary
mailbox row and is injected on any turn that runs, but the batch itself only wakes the parent
once its last created execution child is terminal."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.runtime.internal_tools import SpawnChildNodesTool
from main.runtime.node_mailbox import notification_is_held
from main.runtime.node_runner import _notification_awaits_injection
from main.runtime.task_actor_service import TaskActorService
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

    async def wake(*, task_id: str, node_id: str, allow_pause_clear: bool = False) -> str:
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
async def test_held_row_is_injectable_but_not_a_wake_source(tmp_path, monkeypatch):
    """held 只压制"为它开回合"：父节点因别的事运行时，这些结果照常并入。"""
    service = _service(tmp_path)
    _seed(service, wait_all=True)
    woke = _stub_wake(service, monkeypatch)

    await service._deliver_child_final_message(task_id=TASK, node_id=CHILD1)
    rows = service.store.list_task_node_notifications(TASK, PARENT)
    assert notification_is_held(rows[0]) is True

    # 注入车道：恢复/在途刷新选中它，任务级待注入计数也认它。
    assert _notification_awaits_injection(rows[0]) is True
    pending = service.node_runner._pending_node_notifications(task_id=TASK, node_id=PARENT)
    assert [item.notification_id for item in pending] == [rows[0].notification_id]
    assert service.store.map_task_notifications_awaiting_injection(TASK) == {PARENT: 1}

    # 展示读数：它就是一条没读过的消息。
    assert service.node_runner._unread_from_node(TASK, PARENT) == 1
    assert len(service.store.list_task_delivered_notifications(TASK)) == 1

    # 唤醒车道：未读复派/派发仍排除 held，停靠的父节点不会被它单独叫醒。
    actor = object.__new__(TaskActorService)
    actor._store = service.store
    assert actor._unread_rows(TASK, PARENT) == []
    assert woke == []


@pytest.mark.asyncio
async def test_release_skips_wake_when_batch_rows_already_merged(tmp_path, monkeypatch):
    """批次行已在别的回合里消费并入时，释放只补 released_at，不再开空回合。"""
    service = _service(tmp_path)
    _seed(service, wait_all=True)
    woke = _stub_wake(service, monkeypatch)

    await service._deliver_child_final_message(task_id=TASK, node_id=CHILD1)
    row = service.store.list_task_node_notifications(TASK, PARENT)[0]
    service.store.upsert_task_node_notification(
        row.model_copy(update={'status': 'consumed', 'consumed_at': STAMP, 'merged_at': STAMP})
    )
    child2 = service.store.get_node(CHILD2)
    service.store.upsert_node(
        child2.model_copy(update={'status': 'success', 'final_output': 'done'})
    )

    released = await service._release_wait_all_batch_if_settled(
        task_id=TASK,
        parent_node_id=PARENT,
        batch_id='call:batch',
    )

    assert released is True
    rows = service.store.list_task_node_notifications(TASK, PARENT)
    assert all(item.released_at for item in rows)
    assert woke == []


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


@pytest.mark.asyncio
async def test_startup_reconcile_detail_names_task_pause_and_pending_entries(tmp_path):
    """放不出来时把任务暂停位与没终态的 entry 直接印出来，省掉逐层重查。"""
    service = _service(tmp_path)
    _seed(service, wait_all=True)
    task = service.store.get_task(TASK)
    service.store.upsert_task(
        task.model_copy(update={'is_paused': True, 'pause_requested': True})
    )
    child2 = service.store.get_node(CHILD2)
    service.store.upsert_node(
        child2.model_copy(
            update={'is_paused': True, 'pause_requested': True, 'pause_reason': 'task'}
        )
    )

    detail = service._describe_unreleased_wait_all_batch(
        task_id=TASK,
        parent_node_id=PARENT,
        batch_id='call:batch',
    )

    assert 'task_paused=True' in detail
    assert f'{CHILD1}' not in detail
    assert f'{CHILD2}:in_progress/task' in detail


@pytest.mark.asyncio
async def test_startup_reconcile_detail_reports_missing_batch_payload(tmp_path):
    service = _service(tmp_path)
    _seed(service, wait_all=True)

    detail = service._describe_unreleased_wait_all_batch(
        task_id=TASK,
        parent_node_id=PARENT,
        batch_id='call:absent',
    )

    assert 'batch_payload=missing' in detail


@pytest.mark.asyncio
async def test_startup_reconcile_warning_carries_detail(tmp_path, monkeypatch):
    service = _service(tmp_path)
    _seed(service, wait_all=True)
    _stub_wake(service, monkeypatch)
    await service._deliver_child_final_message(task_id=TASK, node_id=CHILD1)

    captured: list[tuple] = []

    class _Logger:
        def warning(self, message, *args, **kwargs):
            captured.append((message, args))

    monkeypatch.setattr('main.service.runtime_service.logger', _Logger())

    await service._reconcile_wait_all_batches()

    assert captured, 'an unreleased held batch must still warn'
    message, args = captured[0]
    assert 'detail={}' in message
    assert 'pending_entries=' in str(args[-1])
    assert f'{CHILD2}:in_progress' in str(args[-1])
