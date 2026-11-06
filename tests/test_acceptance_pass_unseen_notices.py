"""验收 pass 落账复核：裁决期间到达的未读通知必须先被看见。

I2：不存在"投递成功、但执行方和验收方都没读过"的用户/前门消息。验收回合的尾块
镜像了信箱里的未读行，但仍有一条缝：消息在验收方最后一次请求**之后**才到达。
这里用逐次调用账本给出裁决那一跳的请求起点，差分为空的才允许落 pass。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeFinalResult, NodeRecord, TaskRecord
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    set_acceptance_handshake_state,
)
from main.service.runtime_service import MainRuntimeService

TASK = 'task:acceptance-pass-unseen'
ROOT = 'node:root'
CHILD = 'node:child'
ACCEPTANCE = 'node:acceptance'
STAMP = '2026-10-24T00:00:00+08:00'
RESULT_REF = 'artifact:payload-1'


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


def _node(node_id: str, *, parent: str | None, kind: str = 'execution', depth: int = 0) -> NodeRecord:
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
    service.store.upsert_node(_node(ACCEPTANCE, parent=CHILD, kind='acceptance', depth=2))
    service.store.update_node(
        ACCEPTANCE,
        lambda record: record.model_copy(
            update={'metadata': {**dict(record.metadata or {}), 'accepted_node_id': CHILD}}
        ),
    )
    handshake = set_acceptance_handshake_state(
        {},
        state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
        acceptance_node_id=ACCEPTANCE,
        rejection_count=0,
        latest_execution_result_ref=RESULT_REF,
        latest_execution_result_summary='child done',
        latest_rejection_feedback_ref='',
        latest_rejection_feedback_summary='',
        updated_at=STAMP,
    )
    service.store.update_node(
        CHILD,
        lambda record: record.model_copy(
            update={'metadata': {**dict(record.metadata or {}), ACCEPTANCE_HANDSHAKE_KEY: handshake}}
        ),
    )


def _queue_delivered(
    service: MainRuntimeService,
    *,
    node_id: str,
    message: str,
    notification_id: str,
    kind: str,
    delivered_at: str = STAMP,
) -> None:
    service.store.upsert_task_node_notification(
        {
            'notification_id': notification_id,
            'task_id': TASK,
            'node_id': node_id,
            'epoch_id': '',
            'source_node_id': ROOT,
            'message': message,
            'status': 'delivered',
            'created_at': delivered_at,
            'delivered_at': delivered_at,
            'kind': kind,
            'priority': 'normal',
        }
    )


def _pass_result() -> NodeFinalResult:
    return NodeFinalResult(
        status='success',
        delivery_status='final',
        summary='验收裁定：通过',
        answer='正文',
        evidence=[],
        remaining_work=[],
        blocking_reason='',
        verdict='pass',
        submission_id='submission:pass',
    )


def _record_last_hop(service: MainRuntimeService, *, created_at: str, duration_ms: int) -> None:
    service.store.append_task_model_call(
        task_id=TASK,
        node_id=ACCEPTANCE,
        created_at=created_at,
        payload={'duration_ms': duration_ms},
    )


@pytest.mark.asyncio
async def test_pass_is_held_when_a_notice_arrived_after_the_last_hop_started(tmp_path, monkeypatch):
    service = _service(tmp_path)
    try:
        _seed(service)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={'status': 'success', 'final_output': '验收裁定：通过'}
            ),
        )
        # 最后一跳：10:00:05 收口、耗时 5000ms → 请求起点 10:00:00。
        _record_last_hop(service, created_at='2026-10-24T10:00:05+08:00', duration_ms=5000)
        _queue_delivered(
            service,
            node_id=CHILD,
            message='前门补充：裁决期间到达',
            notification_id='notif:unseen:1',
            kind='frontdoor_notice',
            delivered_at='2026-10-24T10:00:03+08:00',
        )
        requested: list[str] = []
        monkeypatch.setattr(service.node_runner, 'acceptance_handoff_requeue_callback', requested.append)

        handled = await service.node_runner._handle_acceptance_node_result(
            task=service.store.get_task(TASK),
            acceptance=service.store.get_node(ACCEPTANCE),
            result=_pass_result(),
        )

        assert handled.delivery_status == 'partial'
        assert '验收暂缓' in handled.summary
        child = service.store.get_node(CHILD)
        handshake = dict((child.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) or {})
        assert handshake['state'] == ACCEPTANCE_STATE_WAITING_ACCEPTANCE
        assert handshake['rejection_count'] == 0
        acceptance = service.store.get_node(ACCEPTANCE)
        assert acceptance.status == 'in_progress'
        assert 'result_payload' not in dict(acceptance.metadata or {})
        assert requested == [TASK]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pass_lands_when_the_last_hop_started_after_the_notice(tmp_path, monkeypatch):
    service = _service(tmp_path)
    try:
        _seed(service)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={'status': 'success', 'final_output': '验收裁定：通过'}
            ),
        )
        _queue_delivered(
            service,
            node_id=CHILD,
            message='前门补充：早于最后一跳',
            notification_id='notif:unseen:seen',
            kind='frontdoor_notice',
            delivered_at='2026-10-24T10:00:03+08:00',
        )
        # 最后一跳：10:00:10 收口、耗时 1000ms → 请求起点 10:00:09，晚于投递。
        _record_last_hop(service, created_at='2026-10-24T10:00:10+08:00', duration_ms=1000)
        requested: list[str] = []
        monkeypatch.setattr(service.node_runner, 'acceptance_handoff_requeue_callback', requested.append)

        handled = await service.node_runner._handle_acceptance_node_result(
            task=service.store.get_task(TASK),
            acceptance=service.store.get_node(ACCEPTANCE),
            result=_pass_result(),
        )

        assert handled.delivery_status == 'final'
        assert requested == []
        child = service.store.get_node(CHILD)
        handshake = dict((child.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) or {})
        assert handshake['state'] == ACCEPTANCE_STATE_ACCEPTED
        assert service.store.get_node(ACCEPTANCE).status == 'success'
    finally:
        await service.close()
