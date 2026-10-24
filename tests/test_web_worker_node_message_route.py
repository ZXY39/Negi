"""Web role must delegate node delivery and waking to the worker command lane."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord
from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.service.runtime_service import MainRuntimeService

TASK = 'task:webroute'
ROOT = 'node:root-webroute'
CHILD = 'node:child-webroute'


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


def _seed(service: MainRuntimeService) -> None:
    stamp = '2026-10-25T00:00:00+08:00'
    service.store.upsert_node(
        NodeRecord(
            node_id=ROOT,
            task_id=TASK,
            root_node_id=ROOT,
            goal='root',
            prompt='p',
            created_at=stamp,
            updated_at=stamp,
        )
    )
    service.store.upsert_node(
        NodeRecord(
            node_id=CHILD,
            task_id=TASK,
            parent_node_id=ROOT,
            root_node_id=ROOT,
            depth=1,
            goal='child',
            prompt='p',
            created_at=stamp,
            updated_at=stamp,
        )
    )
    service.store.upsert_task_runtime_frame(
        TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD, phase='waiting_explicit')
    )


@pytest.mark.asyncio
async def test_web_control_delivery_enqueues_worker_command_and_returns_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    _seed(service)
    enqueued: list[dict] = []

    def _enqueue(**kwargs):
        enqueued.append(kwargs)
        return 'command:send-1'

    monkeypatch.setattr(service, '_assert_worker_available', lambda: None)
    monkeypatch.setattr(service, '_enqueue_task_command', _enqueue)
    monkeypatch.setattr(
        service.store,
        'get_task_command',
        lambda command_id: {
            'status': 'completed',
            'result': {
                'target': CHILD,
                'status': 'delivered',
                'kind': 'frontdoor_notice',
                'message_id': 'notif:from-worker',
            },
        },
    )
    monkeypatch.setattr(
        service,
        'deliver_node_message',
        lambda **kwargs: pytest.fail('web role must not write the mailbox locally'),
    )

    result = await service.deliver_control_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='hello from the frontdoor',
        origin='frontdoor',
    )

    assert result['status'] == 'delivered'
    assert result['message_id'] == 'notif:from-worker'
    assert result['worker_command_id'] == 'command:send-1'
    assert enqueued and enqueued[0]['command_type'] == 'send_node_message'
    assert enqueued[0]['payload']['target_node_id'] == CHILD
    assert enqueued[0]['payload']['message'] == 'hello from the frontdoor'
    assert enqueued[0]['payload']['origin'] == 'frontdoor'


@pytest.mark.asyncio
async def test_web_control_delivery_rejects_when_worker_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    _seed(service)
    enqueued: list[dict] = []

    def _offline() -> None:
        raise ValueError('task_worker_offline')

    monkeypatch.setattr(service, '_assert_worker_available', _offline)
    monkeypatch.setattr(service, '_enqueue_task_command', lambda **kwargs: enqueued.append(kwargs))
    monkeypatch.setattr(
        service,
        'deliver_node_message',
        lambda **kwargs: pytest.fail('offline web role must not write the mailbox locally'),
    )

    result = await service.deliver_control_node_message(
        task_id=TASK,
        target_node_id=CHILD,
        message='x',
        origin='frontdoor',
    )

    assert result['status'] == 'rejected'
    assert result['reason'] == 'task_worker_offline'
    assert enqueued == []


@pytest.mark.asyncio
async def test_worker_command_executes_local_delivery_and_reports_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    _seed(service)
    service.execution_mode = 'worker'

    async def _wake(task_id: str, node_id: str) -> str:
        return 'resumed'

    monkeypatch.setattr(service, 'wake_node_on_message', _wake)
    command_id = service._enqueue_task_command(
        command_type='send_node_message',
        task_id=TASK,
        session_id='web:shared',
        payload={
            'target_node_id': CHILD,
            'message': 'delivered by worker',
            'origin': 'frontdoor',
        },
    )
    command = service.store.get_task_command(command_id)

    await service._process_worker_command(command)

    stored = service.store.list_task_node_notifications(TASK, CHILD)
    assert len(stored) == 1
    assert stored[0].message == 'delivered by worker'
    assert stored[0].kind == 'frontdoor_notice'
    finished = service.store.get_task_command(command_id)
    assert finished['status'] == 'completed'
    assert finished['result']['status'] == 'delivered'
    assert finished['result']['target'] == CHILD


@pytest.mark.asyncio
async def test_web_wake_delegates_resume_command_instead_of_local_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    enqueued: list[dict] = []

    def _enqueue(**kwargs):
        enqueued.append(kwargs)
        return 'command:resume-1'

    async def _boom(task_id: str) -> None:
        pytest.fail('web role must not enqueue a local task actor')

    monkeypatch.setattr(service, '_enqueue_task_command', _enqueue)
    monkeypatch.setattr(service.global_scheduler, 'enqueue_task', _boom)
    monkeypatch.setattr(service, 'worker_state', lambda: 'online')

    outcome = await service.wake_node_on_message(task_id=TASK, node_id=CHILD)

    assert outcome == 'delegated_worker'
    assert enqueued and enqueued[0]['command_type'] == 'resume_node'
    assert enqueued[0]['payload'] == {'node_ids': [CHILD]}


@pytest.mark.asyncio
async def test_web_role_refuses_task_actor_when_worker_online(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, 'worker_state', lambda: 'online')
    fake_store = SimpleNamespace(
        get_task=lambda task_id: SimpleNamespace(task_id=task_id, root_node_id=ROOT)
    )
    monkeypatch.setattr(service.task_actor_service, '_store', fake_store)

    await service.task_actor_service.run_task(TASK)

    assert service.task_actor_service._dispatchers == {}


def test_web_role_allows_local_actor_when_worker_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, 'worker_state', lambda: 'offline')

    assert service.task_actor_service.web_role_blocked() is False
