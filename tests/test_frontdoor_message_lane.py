"""Frontdoor inbox lane: root-only inbound, execution-node outbound, scanner wake."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.heartbeat.frontdoor_message_scanner import FrontdoorMessageScanner
from main.models import NodeRecord, TaskRecord
from main.service.runtime_service import MainRuntimeService

TASK = 'task:frontdoor'
ROOT = 'node:root'
CHILD = 'node:child'
VERIFIER = 'node:verifier'
SESSION = 'web:frontdoor'
STAMP = '2026-10-25T00:00:00+08:00'


def _service(tmp_path: Path, *, execution_mode: str = 'web') -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode=execution_mode,
    )


def _node(node_id, *, parent=None, depth=0, kind='execution', metadata=None):
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind=kind,
        goal=f'goal-{node_id}',
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        metadata=dict(metadata or {}),
    )


def _seed(service: MainRuntimeService, *, verifier_accepted=ROOT):
    service.store.upsert_task(
        TaskRecord(
            task_id=TASK,
            title='t',
            user_request='u',
            root_node_id=ROOT,
            session_id=SESSION,
            created_at=STAMP,
            updated_at=STAMP,
        )
    )
    service.store.upsert_node(_node(ROOT))
    service.store.upsert_node(_node(CHILD, parent=ROOT, depth=1))
    service.store.upsert_node(
        _node(
            VERIFIER,
            parent=ROOT,
            depth=1,
            kind='acceptance',
            metadata={'accepted_node_id': verifier_accepted},
        )
    )


@pytest.mark.asyncio
async def test_root_can_write_frontdoor_inbox(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    delivered = await service.deliver_frontdoor_message(
        task_id=TASK,
        source_node_id=ROOT,
        content='需要你确认一下',
        ref='artifact:1',
    )

    assert delivered['status'] == 'delivered'
    rows = service.list_frontdoor_node_messages(SESSION)
    assert len(rows) == 1
    assert rows[0].source_node_id == ROOT
    assert rows[0].content == '需要你确认一下'
    assert rows[0].status == 'pending'


@pytest.mark.asyncio
async def test_non_root_cannot_write_frontdoor_inbox(tmp_path):
    service = _service(tmp_path)
    _seed(service)

    delivered = await service.deliver_frontdoor_message(
        task_id=TASK,
        source_node_id=CHILD,
        content='x',
    )
    assert delivered['status'] == 'rejected'
    assert delivered['reason'] == 'frontdoor_root_only'


@pytest.mark.asyncio
async def test_frontdoor_can_message_execution_node_only(tmp_path):
    service = _service(tmp_path, execution_mode='worker')
    _seed(service)

    result = await service.deliver_frontdoor_node_message(
        session_id=SESSION,
        to=[CHILD, VERIFIER],
        content='请继续',
    )
    assert result['results'][0]['status'] == 'delivered'
    assert result['results'][1]['reason'] == 'acceptance_node_not_messageable'
    rows = service.store.list_task_node_notifications(TASK, CHILD)
    assert rows[-1].kind == 'frontdoor_notice'
    assert rows[-1].priority == 'high'


@pytest.mark.asyncio
async def test_frontdoor_rejects_wrong_session_and_under_acceptance(tmp_path):
    service = _service(tmp_path, execution_mode='worker')
    _seed(service)
    wrong = await service.deliver_frontdoor_node_message(
        session_id='web:other', to=CHILD, content='x'
    )
    assert wrong['results'][0]['reason'] == 'not_session_task'

    child = service.store.get_node(CHILD)
    service.store.upsert_node(
        child.model_copy(
            update={
                'metadata': {
                    **dict(child.metadata or {}),
                    'acceptance_handshake': {'state': 'waiting_acceptance'},
                }
            }
        )
    )
    result = await service.deliver_frontdoor_node_message(
        session_id=SESSION, to=CHILD, content='x'
    )
    assert result['results'][0]['reason'] == 'target_under_acceptance'


@pytest.mark.asyncio
async def test_scanner_enqueues_and_consumes_pending_row(tmp_path):
    service = _service(tmp_path)
    _seed(service)
    await service.deliver_frontdoor_message(task_id=TASK, source_node_id=ROOT, content='hello')
    captured: list[tuple[str, dict]] = []

    class _Heartbeat:
        def enqueue_frontdoor_node_message_payload(self, session_id, payload):
            captured.append((session_id, payload))
            return True

    scanner = FrontdoorMessageScanner(
        main_task_service=service,
        heartbeat=_Heartbeat(),
        interval_seconds=60,
    )
    assert await scanner.scan_once() == 1
    assert captured[0][0] == SESSION
    assert captured[0][1]['source_node_id'] == ROOT
    rows = service.list_frontdoor_node_messages(SESSION)
    assert rows[0].status == 'consumed'
