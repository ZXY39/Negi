"""A verifier's own error pause reaches the source session directly.

The parent of an acceptance node sits in `waiting_acceptance` and is not woken
by non-verdict messages, so the verifier's self-fault is only actionable through
the `task_node_error` lane. These tests pin the selection predicate, the payload
extension, the prompt branch and the fail exit.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.heartbeat.node_error_scanner import NodeErrorScanner
from g3ku.heartbeat.prompt_lane import _task_node_error_lines
from main.models import NodeRecord, TaskRecord
from main.runtime.acceptance_handshake import ACCEPTANCE_HANDSHAKE_KEY
from main.service.runtime_service import MainRuntimeService

TASK = 'task:acceptance-self-fault'
ROOT = 'node:root'
CHILD = 'node:child'
ACCEPTANCE = 'node:acceptance'
STAMP = '2026-11-02T00:00:00+08:00'
REF = 'artifact:submission-1'


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


def _node(
    node_id: str,
    *,
    parent: str | None,
    kind: str = 'execution',
    depth: int = 0,
    metadata: dict | None = None,
    is_paused: bool = False,
    status: str = 'in_progress',
) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind=kind,
        status=status,
        goal=node_id,
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        is_paused=is_paused,
        pause_reason='error' if is_paused else '',
        metadata=dict(metadata or {}),
    )


def _seed(service: MainRuntimeService, *, handshake_state: str = 'waiting_acceptance', submission_ref: str = REF) -> None:
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
    service.store.upsert_node(
        _node(
            CHILD,
            parent=ROOT,
            depth=1,
            metadata={
                ACCEPTANCE_HANDSHAKE_KEY: {
                    'state': handshake_state,
                    'latest_execution_result_ref': submission_ref,
                }
            },
            status='in_progress',
        )
    )
    service.store.upsert_node(
        _node(
            ACCEPTANCE,
            parent=CHILD,
            kind='acceptance',
            depth=2,
            metadata={'accepted_node_id': CHILD, 'final_acceptance': False},
            is_paused=True,
        )
    )
    service.store.upsert_task_node_pause(
        task_id=TASK,
        node_id=ACCEPTANCE,
        pause_reason='error',
        remark='provider unavailable while verifying',
        delivered=False,
        created_at=STAMP,
        updated_at=STAMP,
    )


@pytest.mark.asyncio
async def test_error_pause_selection_includes_acceptance_but_not_non_root_execution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        _seed(service)
        selected = {row['node_id'] for row in service.store.list_new_error_pauses()}
        assert ACCEPTANCE in selected
        assert CHILD not in selected

        # 非根执行节点的 error pause 仍不直达（走父节点信箱）。
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(
                update={'is_paused': True, 'pause_reason': 'error'}
            ),
        )
        service.store.upsert_task_node_pause(
            task_id=TASK,
            node_id=CHILD,
            pause_reason='error',
            remark='child failed',
            delivered=False,
            created_at=STAMP,
            updated_at=STAMP,
        )
        selected = {row['node_id'] for row in service.store.list_new_error_pauses()}
        assert ACCEPTANCE in selected
        assert CHILD not in selected
    finally:
        await service.close()


class _FakeHeartbeat:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    def enqueue_task_node_error_payload(self, session_id: str, items: list[dict]) -> bool:
        self.calls.append((session_id, list(items)))
        return True


def _scanner(service: MainRuntimeService, heartbeat: _FakeHeartbeat) -> NodeErrorScanner:
    return NodeErrorScanner(main_task_service=service, heartbeat=heartbeat, interval_seconds=60)


@pytest.mark.asyncio
async def test_scanner_extends_acceptance_payload_and_skips_scars(tmp_path: Path) -> None:
    service = _service(tmp_path)
    heartbeat = _FakeHeartbeat()
    try:
        _seed(service)
        await _scanner(service, heartbeat).scan_once()
        assert len(heartbeat.calls) == 1
        item = heartbeat.calls[0][1][0]
        assert item['node_role'] == 'acceptance'
        assert item['inspected_node_id'] == CHILD
        assert item['inspected_node_status'] == 'in_progress'
        assert item['handshake_state'] == 'waiting_acceptance'
        assert item['submission_ref'] == REF
        assert item['final_acceptance'] is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_scanner_skips_terminal_inspected_node_and_handshake_without_submission(tmp_path: Path) -> None:
    service = _service(tmp_path)
    heartbeat = _FakeHeartbeat()
    try:
        _seed(service)
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(update={'status': 'success'}),
        )
        await _scanner(service, heartbeat).scan_once()
        assert heartbeat.calls == []

        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(
                update={
                    'status': 'in_progress',
                    'metadata': {
                        **dict(record.metadata or {}),
                        ACCEPTANCE_HANDSHAKE_KEY: {'state': 'idle', 'latest_execution_result_ref': ''},
                    },
                }
            ),
        )
        await _scanner(service, heartbeat).scan_once()
        assert heartbeat.calls == []
    finally:
        await service.close()


def test_prompt_lane_renders_acceptance_role_and_inspected_node() -> None:
    lines = _task_node_error_lines(
        {
            'task_id': TASK,
            'task_title': 'task title',
            'node_id': ACCEPTANCE,
            'node_title': 'acceptance',
            'pause_reason': 'error',
            'error_text': 'provider chain exhausted',
            'node_role': 'acceptance',
            'inspected_node_id': CHILD,
            'inspected_node_status': 'in_progress',
            'handshake_state': 'waiting_acceptance',
            'final_acceptance': False,
        },
        [],
        output_inline_limit=4000,
        externalizer=None,
    )
    text = '\n'.join(lines)
    assert 'Role: acceptance node' in text
    assert 'not a verdict on the inspected node' in text
    assert f'Inspected node: {CHILD} [in_progress], handshake=waiting_acceptance' in text
    assert 'Final acceptance: no' in text
    assert 'Do not fail the verifier node itself' in text


@pytest.mark.asyncio
async def test_fail_on_acceptance_node_is_rejected_with_a_real_exit_hint(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        _seed(service)
        result = await service._apply_fail_node_command(
            TASK, node_ids=[ACCEPTANCE], reason='give up'
        )
        item = result['items'][0]
        assert item['result'] == 'conflict'
        assert item['reason'] == 'acceptance_fail_does_not_settle_task'
        assert item['inspected_node_id'] == CHILD
        assert 'fail the inspected execution node' in item['hint']
        latest = service.get_node(ACCEPTANCE)
        assert latest is not None and latest.status == 'in_progress'
    finally:
        await service.close()
