"""P8 crash recovery: unfinished acceptance + lost abnormal-pause escalation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    set_acceptance_handshake_state,
)
from main.runtime.node_message_kind import KIND_CHILD_ABNORMAL_PAUSE
from main.service.runtime_service import MainRuntimeService

TASK = 'task:recovery'
ROOT = 'node:root'
CHILD = 'node:child'
ACCEPTANCE = 'node:acceptance'
STAMP = '2026-10-24T00:00:00+00:00'


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
        latest_execution_result_ref='artifact:payload-1',
        latest_execution_result_summary='child done',
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


@pytest.mark.asyncio
async def test_unfinished_acceptance_round_is_dispatched_after_restart(tmp_path: Path) -> None:
    first = _service(tmp_path)
    try:
        _seed(first)
    finally:
        await first.close()

    restarted = _service(tmp_path)
    try:
        dispatcher = restarted.task_actor_service._create_dispatcher(TASK)
        dispatched = await restarted.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher)

        assert dispatched is True
        assert dispatcher._entries.get(ACCEPTANCE) is not None
        await dispatcher.close()
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_abnormal_pause_message_resent_if_lost(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        _seed(service)
        service.log_service.set_node_pause_state(
            TASK,
            CHILD,
            pause_requested=True,
            is_paused=True,
            pause_reason='error',
            remark='没有实质变化',
        )
        # 崩溃场景：升级消息没有落到父节点信箱。
        resent = await service.resend_lost_abnormal_pause_escalations(TASK)

        assert resent == [CHILD]
        notices = service.store.list_task_node_notifications(TASK, ROOT)
        assert any(str(item.kind or '') == KIND_CHILD_ABNORMAL_PAUSE for item in notices)

        # 幂等：已在信箱里的不重发。
        assert await service.resend_lost_abnormal_pause_escalations(TASK) == []
    finally:
        await service.close()
