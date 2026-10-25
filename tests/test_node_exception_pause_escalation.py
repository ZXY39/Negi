"""A node that dies on an unexpected exception must still escalate to its parent.

Two lanes write `pause_reason='error'`: the `failure_disposition='pause'` result lane and the
generic exception lane. Only the first one used to notify the parent, so a chain-exhausted 400
(live 2026-10-26, task:578bebcdf701 node:20bb6c2ea7d1) left the root node parked in `enter_wait`
waiting for a child that would never mail it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.errors import NodePausedError
from main.models import NodeRecord, TaskRecord
from main.runtime.model_size_span import EXCEPTION_REPORT_ATTR
from main.runtime.node_message_kind import KIND_CHILD_ABNORMAL_PAUSE
from main.service.runtime_service import MainRuntimeService

TASK = 'task:exception-escalation'
ROOT = 'node:root'
CHILD = 'node:child'
STAMP = '2026-10-26T01:50:00+08:00'

PROVIDER_TEXT = (
    'RuntimeError: BadRequestError: Error code: 400 - '
    "{'error': {'message': 'inference request is invalid', 'type': 'invalid_request_error', "
    "'code': '400'}} code=400 status=400"
)
SPAN_REPORT = (
    'model_size_span: sensenova-6.8-flash-lite-2 answered 288,991 tokens and rejected '
    '325,000 (same send-side ruler; span width 36,009 = 12.5%).'
)


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


def _node(node_id: str, *, parent: str | None, depth: int = 0) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind='execution',
        goal=f'goal of {node_id}',
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


class _RaisingLoop:
    """Stands in for the ReAct loop: the provider chain raises, exactly like a 400 storm."""

    _max_parallel_tool_calls = None
    _chat_backend = None

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(self, **_kwargs):  # noqa: ANN003 - stub signature
        raise self._exc


def _notifications(service: MainRuntimeService, node_id: str) -> list[dict]:
    return [
        {
            'kind': str(getattr(item, 'kind', '') or ''),
            'message': str(getattr(item, 'message', '') or ''),
            'source_node_id': str(getattr(item, 'source_node_id', '') or ''),
        }
        for item in list(service.store.list_task_node_notifications(TASK, node_id) or [])
    ]


@pytest.mark.asyncio
async def test_exception_lane_pauses_the_child_as_an_error(tmp_path: Path):
    service = _service(tmp_path)
    _seed(service)
    service.node_runner._react_loop = _RaisingLoop(RuntimeError(PROVIDER_TEXT))

    with pytest.raises(NodePausedError):
        await service.node_runner._run_node_turn(TASK, CHILD)

    node = service.store.get_node(CHILD)
    assert bool(getattr(node, 'is_paused', False)) is True
    assert str(getattr(node, 'pause_reason', '') or '') == 'error'


@pytest.mark.asyncio
async def test_exception_lane_escalates_to_the_parent_mailbox(tmp_path: Path):
    service = _service(tmp_path)
    _seed(service)
    service.node_runner._react_loop = _RaisingLoop(RuntimeError(PROVIDER_TEXT))

    with pytest.raises(NodePausedError):
        await service.node_runner._run_node_turn(TASK, CHILD)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if _notifications(service, ROOT):
            break

    items = _notifications(service, ROOT)
    assert len(items) == 1
    body = str(items[0].get('message') or '')
    assert PROVIDER_TEXT in body
    assert 'frontdoor' in body or 'parent' in body


@pytest.mark.asyncio
async def test_exception_lane_carries_the_size_span_upstream(tmp_path: Path):
    """The rejected hop has no reply, so the numbers reach the parent on the exception."""
    service = _service(tmp_path)
    _seed(service)
    exc = RuntimeError(PROVIDER_TEXT)
    setattr(exc, EXCEPTION_REPORT_ATTR, SPAN_REPORT)
    service.node_runner._react_loop = _RaisingLoop(exc)

    with pytest.raises(NodePausedError):
        await service.node_runner._run_node_turn(TASK, CHILD)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if _notifications(service, ROOT):
            break

    items = _notifications(service, ROOT)
    assert [str(item.get('kind') or '') for item in items] == [KIND_CHILD_ABNORMAL_PAUSE]
    body = str(items[0].get('message') or '')
    assert SPAN_REPORT in body


@pytest.mark.asyncio
async def test_root_node_error_does_not_mail_itself(tmp_path: Path):
    """The root has no parent; its failure reaches the session through the existing lanes."""
    service = _service(tmp_path)
    _seed(service)
    service.node_runner._react_loop = _RaisingLoop(RuntimeError(PROVIDER_TEXT))

    with pytest.raises(NodePausedError):
        await service.node_runner._run_node_turn(TASK, ROOT)
    await asyncio.sleep(0.05)

    assert _notifications(service, ROOT) == []
