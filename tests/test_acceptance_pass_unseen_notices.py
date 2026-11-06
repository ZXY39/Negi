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
    payload: dict | None = None,
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
            'payload': dict(payload or {}),
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


def _stamp_tail_anchor(service: MainRuntimeService, built_at: str) -> None:
    service.store.update_node(
        ACCEPTANCE,
        lambda record: record.model_copy(
            update={
                'metadata': {
                    **dict(record.metadata or {}),
                    'acceptance_tail_built_at': built_at,
                }
            }
        ),
    )


@pytest.mark.asyncio
async def test_notice_after_tail_assembly_is_held_before_the_hop_request_starts(tmp_path, monkeypatch):
    """锚是尾块装配时刻，不是最后一跳请求起点。

    通知在 10:00:01 投递：晚于尾块装配（10:00:00），但早于最后一跳请求起点
    （10:00:05 收口 - 2000ms = 10:00:03）。按请求起点判会误以为它在本轮请求里，
    按装配时刻判才知道这一轮根本看不见它。
    """
    service = _service(tmp_path)
    try:
        _seed(service)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={'status': 'success', 'final_output': '验收裁定：通过'}
            ),
        )
        _stamp_tail_anchor(service, '2026-10-24T10:00:00+08:00')
        _record_last_hop(service, created_at='2026-10-24T10:00:05+08:00', duration_ms=2000)
        _queue_delivered(
            service,
            node_id=CHILD,
            message='前门补充：装配后才到',
            notification_id='notif:unseen:assembly-race',
            kind='frontdoor_notice',
            delivered_at='2026-10-24T10:00:01+08:00',
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
        assert requested == [TASK]
        assert service.store.get_node(ACCEPTANCE).status == 'in_progress'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_acceptance_prompt_assembly_stamps_metadata_and_frame(tmp_path):
    service = _service(tmp_path)
    try:
        _seed(service)
        task = service.store.get_task(TASK)
        acceptance = service.store.get_node(ACCEPTANCE)

        await service.node_runner._build_messages(task=task, node=acceptance)

        stamped = str(
            dict(service.store.get_node(ACCEPTANCE).metadata or {}).get('acceptance_tail_built_at') or ''
        ).strip()
        assert stamped
        frame = service.log_service.read_runtime_frame(TASK, ACCEPTANCE) or {}
        assert str(frame.get('acceptance_tail_built_at') or '').strip() == stamped
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_resuming_from_a_frame_restores_that_frames_tail_anchor(tmp_path):
    """旧帧续跑时，锚要退回该帧装配时的时刻，不能被之后放弃的新构建推前。"""
    service = _service(tmp_path)
    try:
        _seed(service)
        task = service.store.get_task(TASK)
        service.log_service.update_frame(
            TASK,
            ACCEPTANCE,
            lambda frame: {
                **dict(frame or {}),
                'messages': [{'role': 'user', 'content': 'old acceptance context'}],
                'acceptance_tail_built_at': '2026-10-24T09:00:00+08:00',
            },
        )
        _stamp_tail_anchor(service, '2026-10-24T10:00:00+08:00')

        state = await service.node_runner._resume_react_state(
            task=task,
            node=service.store.get_node(ACCEPTANCE),
        )

        assert state['message_source'] == 'restored_frame'
        synced = str(
            dict(service.store.get_node(ACCEPTANCE).metadata or {}).get('acceptance_tail_built_at') or ''
        ).strip()
        assert synced == '2026-10-24T09:00:00+08:00'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_external_event_rows_do_not_hold_a_pass(tmp_path, monkeypatch):
    """外部订阅事件不进 I2 复核：事件流不参与"验收方看过没有"的判定。"""
    service = _service(tmp_path)
    try:
        _seed(service)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={'status': 'success', 'final_output': '验收裁定：通过'}
            ),
        )
        _stamp_tail_anchor(service, '2026-10-24T10:00:00+08:00')
        _record_last_hop(service, created_at='2026-10-24T10:00:05+08:00', duration_ms=1000)
        _queue_delivered(
            service,
            node_id=CHILD,
            message='External event from subscription feeds.',
            notification_id='notif:unseen:external',
            kind='frontdoor_notice',
            delivered_at='2026-10-24T10:00:03+08:00',
            payload={'external_event_id': 'event:stream-1'},
        )
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
