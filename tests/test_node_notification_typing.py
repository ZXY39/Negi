"""结构性用例：运行时产出的每一条节点通知都必须带 kind 与 priority。

实盘 `task:eb3f3a7753f7`：验收车道的交接单、打回意见、通过意见全部是
`kind=''`，因为它们绕开 `deliver_node_message` 直写通知表。这里跑一条最小的
「执行 → 交接 → 打回 → 重交 → 通过 → 终局」流程，然后逐行断言：
**任何一行通知都不允许缺类型**——直写通知表的老路径不得再回来。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeFinalResult, NodeRecord, TaskRecord
from main.service.runtime_service import MainRuntimeService

TASK = 'task:notification-typing'
ROOT = 'node:root'
CHILD = 'node:child'
ACCEPTANCE = 'node:acceptance'
STAMP = '2026-10-24T00:00:00+08:00'


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


def _execution_submission(text: str, *, submission_id: str) -> NodeFinalResult:
    return NodeFinalResult(
        status='success',
        delivery_status='final',
        summary=text,
        answer=text,
        evidence=[],
        remaining_work=[],
        blocking_reason='',
        submission_id=submission_id,
    )


def _verdict(
    text: str,
    *,
    verdict: str,
    submission_id: str,
) -> NodeFinalResult:
    return NodeFinalResult(
        status='success' if verdict == 'pass' else 'failed',
        delivery_status='final',
        summary=text,
        answer=text,
        evidence=[],
        remaining_work=[],
        blocking_reason='',
        verdict=verdict,
        submission_id=submission_id,
    )


def _all_rows(service: MainRuntimeService) -> list:
    rows = []
    for node in list(service.store.list_nodes(TASK) or []):
        rows.extend(list(service.store.list_task_node_notifications(TASK, node.node_id) or []))
    return rows


class TestEveryNotificationIsTyped:
    @pytest.mark.asyncio
    async def test_minimal_acceptance_flow_leaves_no_untyped_row(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        try:
            _seed(service)

            async def noop_wake(*, task_id: str, node_id: str) -> str:
                return 'resumed'

            monkeypatch.setattr(service, 'wake_node_on_message', noop_wake)
            task = service.store.get_task(TASK)

            # 第一份提交 → 交接单（执行节点 → 它的验收子节点，下行 parent_notice）。
            await service.node_runner._maybe_start_acceptance_handshake(
                task=task,
                node=service.store.get_node(CHILD),
                result=_execution_submission('draft v1', submission_id='submission:1'),
            )
            handoffs = [
                item
                for item in service.store.list_task_node_notifications(TASK, ACCEPTANCE)
                if item.kind == 'parent_notice'
            ]
            assert handoffs, 'handoff must be a typed parent_notice'
            assert handoffs[-1].source_node_id == CHILD

            # 驳回 → acceptance_verdict（发回被验节点，带 submission_id / verdict）。
            await service.node_runner._handle_acceptance_node_result(
                task=task,
                acceptance=service.store.get_node(ACCEPTANCE),
                result=_verdict('缺少第 2 节', verdict='reject', submission_id='submission:1'),
            )

            # 重交 → 再一张交接单；通过 → 第二条 acceptance_verdict。
            await service.node_runner._maybe_start_acceptance_handshake(
                task=task,
                node=service.store.get_node(CHILD),
                result=_execution_submission('draft v2', submission_id='submission:2'),
            )
            await service.node_runner._handle_acceptance_node_result(
                task=task,
                acceptance=service.store.get_node(ACCEPTANCE),
                result=_verdict('验收通过', verdict='pass', submission_id='submission:2'),
            )

            # 执行节点终局 → child_final（系统消息，路径不变）。
            service.node_runner._mark_finished(
                TASK,
                CHILD,
                _execution_submission('draft v2', submission_id='submission:2'),
            )
            await service.node_runner.wait_pending_terminal_messages()

            rows = _all_rows(service)
            assert rows, 'the flow must leave notifications behind'
            untyped = [
                (item.notification_id, item.node_id, item.message[:40])
                for item in rows
                if not str(item.kind or '').strip() or not str(item.priority or '').strip()
            ]
            assert untyped == [], f'every notification row needs kind/priority: {untyped}'

            kinds = {item.kind for item in rows}
            assert {'parent_notice', 'acceptance_verdict', 'child_final'} <= kinds

            verdicts = [item for item in rows if item.kind == 'acceptance_verdict']
            assert {item.verdict for item in verdicts} == {'reject', 'pass'}
            assert {item.submission_id for item in verdicts} == {'submission:1', 'submission:2'}

            # 直写通知表的老路径必须已删除（否则无类型行还会回来）。
            assert not hasattr(service.node_runner, '_persist_node_notification_direct')
        finally:
            await service.close()
