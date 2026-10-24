"""P6 wait-for graph: detect closed waiting cycles, never touch normal parking."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    set_acceptance_handshake_state,
)
from main.runtime.wait_graph import WaitNode, find_wait_cycles
from main.service.runtime_service import MainRuntimeService

TASK = 'task:wait-graph'
ROOT = 'node:root'
CHILD = 'node:child'
ACCEPTANCE = 'node:acceptance'
STAMP = '2026-10-24T00:00:00+00:00'


def _node(
    node_id: str,
    *,
    parent: str,
    kind: str,
    depth: int,
    phase: str,
) -> WaitNode:
    return WaitNode(
        node_id=node_id,
        parent_node_id=parent,
        depth=depth,
        node_kind=kind,
        status='in_progress',
        phase=phase,
    )


def test_cycle_between_subject_and_parked_acceptance_is_detected() -> None:
    subject = WaitNode(
        node_id=CHILD,
        parent_node_id=ROOT,
        depth=1,
        node_kind='execution',
        status='in_progress',
        phase='waiting_acceptance',
        acceptance_node_id=ACCEPTANCE,
        handshake_state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    )
    acceptance = WaitNode(
        node_id=ACCEPTANCE,
        parent_node_id=CHILD,
        depth=2,
        node_kind='acceptance',
        status='in_progress',
        phase='waiting_explicit',
        subject_node_id=CHILD,
    )

    cycles = find_wait_cycles([subject, acceptance])

    assert len(cycles) == 1
    # 最靠近根的节点排第一（强制唤醒对象）。
    assert cycles[0] == [CHILD, ACCEPTANCE]


def test_normal_parking_is_not_a_cycle() -> None:
    # 根停靠：任意消息都可唤醒，不构成闭合等待。
    root_any = _node(ROOT, parent='', kind='execution', depth=0, phase='waiting_explicit')
    assert find_wait_cycles([root_any]) == []

    # 验收已给出结论（握手 accepted），验收节点等待下一次提交：正常长寿命停靠。
    subject = WaitNode(
        node_id=CHILD,
        parent_node_id=ROOT,
        depth=1,
        node_kind='execution',
        status='in_progress',
        phase='before_model',
        acceptance_node_id=ACCEPTANCE,
        handshake_state=ACCEPTANCE_STATE_ACCEPTED,
    )
    acceptance = _node(ACCEPTANCE, parent=CHILD, kind='acceptance', depth=2, phase='waiting_explicit')
    acceptance = WaitNode(
        **{**acceptance.__dict__, 'subject_node_id': CHILD},
    )
    assert find_wait_cycles([subject, acceptance]) == []


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


@pytest.mark.asyncio
async def test_cycle_is_broken_by_waking_the_node_nearest_the_root(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        service.store.upsert_task(
            TaskRecord(
                task_id=TASK,
                title='t',
                user_request='u',
                root_node_id=ROOT,
                status='in_progress',
                created_at=STAMP,
                updated_at=STAMP,
            )
        )
        for node_id, parent, kind, depth in (
            (ROOT, '', 'execution', 0),
            (CHILD, ROOT, 'execution', 1),
            (ACCEPTANCE, CHILD, 'acceptance', 2),
        ):
            service.store.upsert_node(
                NodeRecord(
                    node_id=node_id,
                    task_id=TASK,
                    parent_node_id=parent,
                    root_node_id=ROOT,
                    depth=depth,
                    node_kind=kind,
                    goal=node_id,
                    prompt='p',
                    status='in_progress',
                    created_at=STAMP,
                    updated_at=STAMP,
                )
            )
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
            latest_execution_result_ref='artifact:p1',
            latest_execution_result_summary='done',
            latest_rejection_feedback_ref='',
            latest_rejection_feedback_summary='',
            updated_at=STAMP,
            latest_execution_submission_id='submission:1',
        )
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(
                update={'metadata': {**dict(record.metadata or {}), ACCEPTANCE_HANDSHAKE_KEY: handshake}}
            ),
        )
        service.log_service.update_frame(
            TASK, CHILD, lambda frame: {**dict(frame or {}), 'phase': 'waiting_acceptance'}
        )
        service.log_service.update_frame(
            TASK, ACCEPTANCE, lambda frame: {**dict(frame or {}), 'phase': 'waiting_explicit'}
        )

        resumed: list[str] = []
        notices: list[dict] = []

        async def _resume(task_id: str, node_id: str, **_kwargs):
            resumed.append(node_id)
            return 'resumed'

        service.task_actor_service.resume_node_entry = _resume
        service.task_actor_service.wait_cycle_notifier = lambda **kwargs: notices.append(dict(kwargs))

        broke = await service.task_actor_service._break_wait_cycles(TASK)

        assert broke is True
        assert resumed == [CHILD]
        assert notices and notices[0]['node_id'] == CHILD
        assert '等待成环' in notices[0]['reason']
    finally:
        await service.close()
