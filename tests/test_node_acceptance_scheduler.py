"""Scheduler-driven acceptance: a child's verdict no longer needs the parent's pipeline.

These tests pin R13: after `_run_child_pipeline` stops driving
execution -> acceptance, a submitted child must still be inspected, finalised and
reported to its parent. The drain lane is the replacement: it looks only at the
durable handshake + acceptance row, so it works whether the acceptance turn was
started by the old pipeline or by the dispatcher.
"""

from __future__ import annotations

import asyncio
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

TASK = 'task:acceptance-scheduler'
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


def _seed(service: MainRuntimeService, *, handshake_state: str = ACCEPTANCE_STATE_WAITING_ACCEPTANCE) -> None:
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
    service.store.upsert_node(
        _node(ACCEPTANCE, parent=CHILD, kind='acceptance', depth=2)
    )
    service.store.update_node(
        ACCEPTANCE,
        lambda record: record.model_copy(
            update={'metadata': {**dict(record.metadata or {}), 'accepted_node_id': CHILD}}
        ),
    )
    payload = NodeFinalResult(
        status='success',
        delivery_status='final',
        summary='child done',
        answer='the answer',
        evidence=[],
        remaining_work=[],
        blocking_reason='',
    ).model_dump(mode='json')
    handshake = set_acceptance_handshake_state(
        {},
        state=handshake_state,
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
            update={
                'metadata': {
                    **dict(record.metadata or {}),
                    'result_payload': payload,
                    ACCEPTANCE_HANDSHAKE_KEY: handshake,
                }
            }
        ),
    )


def _capture_calls(service: MainRuntimeService, monkeypatch) -> tuple[list[str], list[dict]]:
    wakes: list[str] = []
    deliveries: list[dict] = []

    async def wake(*, task_id: str, node_id: str) -> str:
        wakes.append(node_id)
        return 'resumed'

    original = service.deliver_node_message

    async def deliver(**kwargs):
        deliveries.append(kwargs)
        return await original(**kwargs)

    monkeypatch.setattr(service, 'wake_node_on_message', wake)
    monkeypatch.setattr(service, 'deliver_node_message', deliver)
    return wakes, deliveries


async def _settle(service: MainRuntimeService) -> None:
    await service.node_runner.wait_pending_terminal_messages()


class TestVerdictDrain:
    @pytest.mark.asyncio
    async def test_pass_finalises_child_and_reports_to_parent(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        wakes, deliveries = _capture_calls(service, monkeypatch)
        # 验收节点的回合已经跑完并落了成功裁决。
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={
                    'status': 'success',
                    'final_output': '验收通过',
                    'metadata': {
                        **dict(record.metadata or {}),
                        'result_payload': NodeFinalResult(
                            status='success',
                            delivery_status='final',
                            summary='验收通过',
                            answer='',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                        ).model_dump(mode='json'),
                    },
                }
            ),
        )

        progressed = await service.task_actor_service._drain_acceptance_verdicts(TASK)
        await _settle(service)

        assert progressed is True
        child = service.store.get_node(CHILD)
        assert child.status == 'success'
        handshake = dict((child.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) or {})
        assert handshake['state'] == ACCEPTANCE_STATE_ACCEPTED
        assert deliveries, 'child_final must be delivered to the parent'
        assert deliveries[0]['target_node_id'] == ROOT
        assert deliveries[0]['system_kind'] == 'child_final'
        assert deliveries[0]['source_node_id'] == CHILD
        assert wakes == [ROOT]

    @pytest.mark.asyncio
    async def test_drain_is_idempotent(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_calls(service, monkeypatch)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={
                    'status': 'success',
                    'metadata': {
                        **dict(record.metadata or {}),
                        'result_payload': NodeFinalResult(
                            status='success',
                            delivery_status='final',
                            summary='验收通过',
                            answer='',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                        ).model_dump(mode='json'),
                    },
                }
            ),
        )
        await service.task_actor_service._drain_acceptance_verdicts(TASK)
        await _settle(service)
        before = len(service.store.list_task_node_notifications(TASK, ROOT))

        second = await service.task_actor_service._drain_acceptance_verdicts(TASK)
        await _settle(service)
        assert second is False
        assert len(service.store.list_task_node_notifications(TASK, ROOT)) == before

    @pytest.mark.asyncio
    async def test_reject_reactivates_the_child_without_finalising_it(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_calls(service, monkeypatch)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={
                    'status': 'failed',
                    'failure_reason': '第 2 节缺少实测数据',
                    'metadata': {
                        **dict(record.metadata or {}),
                        'result_payload': NodeFinalResult(
                            status='failed',
                            delivery_status='final',
                            summary='第 2 节缺少实测数据',
                            answer='',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                        ).model_dump(mode='json'),
                    },
                }
            ),
        )

        progressed = await service.task_actor_service._drain_acceptance_verdicts(TASK)
        await _settle(service)

        assert progressed is True
        child = service.store.get_node(CHILD)
        assert child.status == 'in_progress'
        acceptance = service.store.get_node(ACCEPTANCE)
        assert acceptance.status == 'in_progress'
        handshake = dict((child.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) or {})
        assert handshake['state'] == 'waiting_execution_retry'
        assert handshake['rejection_count'] == 1
        feedback = service.store.list_task_node_notifications(TASK, CHILD)
        assert feedback and '第 2 节缺少实测数据' in feedback[0].message
        # 打回不是终局：父节点不应收到 child_final。
        assert service.store.list_task_node_notifications(TASK, ROOT) == []

    @pytest.mark.asyncio
    async def test_no_progress_verdict_pauses_the_child_and_escalates(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_calls(service, monkeypatch)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={
                    'status': 'failed',
                    'failure_reason': '第 2 节仍缺少实测数据（与上一轮相同）',
                    'metadata': {
                        **dict(record.metadata or {}),
                        'result_payload': NodeFinalResult(
                            status='failed',
                            delivery_status='final',
                            summary='第 2 节仍缺少实测数据（与上一轮相同）',
                            answer='第 2 节仍缺少实测数据（与上一轮相同）',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                            verdict='reject_no_progress',
                            submission_id='submission:test',
                        ).model_dump(mode='json'),
                    },
                }
            ),
        )

        progressed = await service.task_actor_service._drain_acceptance_verdicts(TASK)
        await _settle(service)

        assert progressed is True
        child = service.store.get_node(CHILD)
        # 第三档不是 failed：被验节点停在人工可见的异常暂停上，现场保留。
        assert child.status == 'in_progress'
        assert child.pause_reason == 'error'
        assert child.is_paused is True
        assert '缺少实测数据' in (child.failure_reason or '') or '缺少实测数据' in (
            (service.store.get_task_node_pause(CHILD).remark if service.store.get_task_node_pause(CHILD) else '')
        )
        parent_notices = service.store.list_task_node_notifications(TASK, ROOT)
        assert any(
            '子节点' in str(item.message or '') and '暂停' in str(item.message or '')
            for item in parent_notices
        )


    @pytest.mark.asyncio
    async def test_accepted_handshake_still_finalises_the_live_child(self, tmp_path, monkeypatch):
        # 旧管线把裁决记在握手/spawn entry 上，执行节点行停在 in_progress。
        # 这是拆管线后最容易留下的半截形态：握手已 accepted，节点却永远不结清。
        service = _service(tmp_path)
        _seed(service, handshake_state=ACCEPTANCE_STATE_ACCEPTED)
        _capture_calls(service, monkeypatch)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={
                    'status': 'success',
                    'metadata': {
                        **dict(record.metadata or {}),
                        'result_payload': NodeFinalResult(
                            status='success',
                            delivery_status='final',
                            summary='验收通过',
                            answer='',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                        ).model_dump(mode='json'),
                    },
                }
            ),
        )

        progressed = await service.task_actor_service._drain_acceptance_verdicts(TASK)
        await _settle(service)

        assert progressed is True
        assert service.store.get_node(CHILD).status == 'success'
        assert service.store.list_task_node_notifications(TASK, ROOT)

    @pytest.mark.asyncio
    async def test_rejected_terminal_handshake_finalises_the_child_as_failed(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service, handshake_state='rejected_terminal')
        _capture_calls(service, monkeypatch)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={
                    'status': 'failed',
                    'failure_reason': '不可修复',
                    'metadata': {
                        **dict(record.metadata or {}),
                        'result_payload': NodeFinalResult(
                            status='failed',
                            delivery_status='blocked',
                            summary='不可修复',
                            answer='',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='不可修复',
                        ).model_dump(mode='json'),
                    },
                }
            ),
        )

        progressed = await service.task_actor_service._drain_acceptance_verdicts(TASK)
        await _settle(service)

        assert progressed is True
        assert service.store.get_node(CHILD).status == 'failed'
        assert service.store.list_task_node_notifications(TASK, ROOT)


class TestNonRootAcceptanceHandshake:
    @pytest.mark.asyncio
    async def test_non_root_submission_enters_waiting_acceptance(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_calls(service, monkeypatch)
        task = service.store.get_task(TASK)
        child = service.store.get_node(CHILD)
        result = NodeFinalResult(
            status='success',
            delivery_status='final',
            summary='child done',
            answer='the answer',
            evidence=[],
            remaining_work=[],
            blocking_reason='',
        )

        handled = service.node_runner._maybe_start_acceptance_handshake(
            task=task, node=child, result=result
        )
        await _settle(service)

        assert handled is not None
        assert handled.delivery_status == 'partial'
        refreshed = service.store.get_node(CHILD)
        handshake = dict((refreshed.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) or {})
        assert handshake['state'] == ACCEPTANCE_STATE_WAITING_ACCEPTANCE
        assert handshake['acceptance_node_id'] == ACCEPTANCE
        assert refreshed.status == 'in_progress'
        assert service.store.list_task_node_notifications(TASK, ACCEPTANCE)


class TestVerdictContext:
    def test_first_round_tail_forbids_no_progress(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _seed(service)
        task = service.store.get_task(TASK)
        acceptance = service.store.get_node(ACCEPTANCE)

        tail = service.node_runner._acceptance_turn_tail(task=task, node=acceptance)

        assert '首次裁定' in tail
        assert 'reject_no_progress' in tail

    def test_verdict_history_is_injected_each_round(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _seed(service)
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={
                    'metadata': {
                        **dict(record.metadata or {}),
                        'rejection_history': [
                            {'at': STAMP, 'status': 'failed', 'final_output': '第 2 节缺少实测数据', 'failure_reason': ''},
                            {'at': STAMP, 'status': 'failed', 'final_output': '第 2 节仍缺少实测数据', 'failure_reason': ''},
                        ],
                    }
                }
            ),
        )
        task = service.store.get_task(TASK)
        acceptance = service.store.get_node(ACCEPTANCE)

        tail = service.node_runner._acceptance_turn_tail(task=task, node=acceptance)

        assert '#1 [reject]' in tail
        assert '#2 [reject]' in tail
        assert '第 2 节缺少实测数据' in tail
        assert '第 2 节仍缺少实测数据' in tail
        assert '首次裁定' not in tail

    def test_acceptance_sees_non_root_notices(self, tmp_path: Path) -> None:
        """决策 J：验收节点看被验节点的追加要求，不再只对根节点开放。"""
        service = _service(tmp_path)
        _seed(service)
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(
                update={
                    'metadata': {
                        **dict(record.metadata or {}),
                        'pending_append_notice_records': [
                            {
                                'notification_id': 'notif:1',
                                'message': '验收口径改为 B：必须给出实测值',
                                'created_at': STAMP,
                                'order_index': 1,
                            }
                        ],
                    }
                }
            ),
        )
        task = service.store.get_task(TASK)
        acceptance = service.store.get_node(ACCEPTANCE)

        tail = service.node_runner._acceptance_turn_tail(task=task, node=acceptance)

        assert '验收口径改为 B' in tail

    @pytest.mark.asyncio
    async def test_first_round_cannot_select_no_progress(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _seed(service)
        task = service.store.get_task(TASK)
        acceptance = service.store.get_node(ACCEPTANCE)

        with pytest.raises(ValueError):
            await service.node_runner._submit_final_result(
                task=task,
                node=acceptance,
                payload={'verdict': 'reject_no_progress', 'feedback': '毫无变化'},
            )

    def test_submission_schema_has_no_status_or_blocked_shape(self) -> None:
        from main.runtime.internal_tools import SubmitFinalResultTool

        async def _callback(payload):
            return payload

        execution = SubmitFinalResultTool(_callback, node_kind='execution')
        acceptance = SubmitFinalResultTool(_callback, node_kind='acceptance')

        execution_props = set(execution.parameters['properties'])
        assert 'status' not in execution_props
        assert 'delivery_status' not in execution_props
        assert 'blocking_reason' not in execution_props
        assert 'remaining_work' in execution_props
        assert set(acceptance.parameters['properties']) == {'verdict', 'feedback'}
        assert acceptance.parameters['properties']['verdict']['enum'] == [
            'pass',
            'reject',
            'reject_no_progress',
        ]

    @pytest.mark.asyncio
    async def test_persisted_verdict_is_settled_when_node_was_reopened(self, tmp_path, monkeypatch):
        """裁决已落盘但节点行被重放回路重置：只认绑定当前提交的裁决，幂等收口。"""
        service = _service(tmp_path)
        _seed(service)
        _capture_calls(service, monkeypatch)
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(
                update={
                    'metadata': {
                        **dict(record.metadata or {}),
                        'acceptance_handshake': {
                            **dict((record.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) or {}),
                            'latest_execution_submission_id': 'submission:current',
                        },
                    }
                }
            ),
        )
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={
                    'status': 'in_progress',
                    'metadata': {
                        **dict(record.metadata or {}),
                        'result_payload': NodeFinalResult(
                            status='success',
                            delivery_status='final',
                            summary='验收通过',
                            answer='验收通过',
                            evidence=[],
                            remaining_work=[],
                            blocking_reason='',
                            verdict='pass',
                            submission_id='submission:current',
                        ).model_dump(mode='json'),
                    },
                }
            ),
        )

        progressed = await service.task_actor_service._drain_acceptance_verdicts(TASK)
        await _settle(service)

        assert progressed is True
        assert service.store.get_node(ACCEPTANCE).status == 'success'
        assert service.store.get_node(CHILD).status == 'success'
        handshake = dict((service.store.get_node(CHILD).metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) or {})
        assert handshake['state'] == ACCEPTANCE_STATE_ACCEPTED
        # 幂等：再来一次不再产生动作。
        assert await service.task_actor_service._drain_acceptance_verdicts(TASK) is False

    def test_waiting_acceptance_node_is_not_redispatched_without_unread(self, tmp_path: Path) -> None:
        """收尾回合会删运行帧；“无帧”不等于“没起跑过”。

        等裁决的执行节点被再次派发，会在验收进行期间重复提交，把刚通过的验收反复重开。
        """
        service = _service(tmp_path)
        try:
            _seed(service)
            dispatcher = service.task_actor_service._create_dispatcher(TASK)

            asyncio.run(service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher))

            # 验收节点该被派发；等裁决的执行节点不该被再派一遍。
            assert dispatcher._entries.get(CHILD) is None
            assert dispatcher._entries.get(ACCEPTANCE) is not None

            async def _cleanup() -> None:
                await dispatcher.close()

            asyncio.run(_cleanup())
        finally:
            asyncio.run(service.close())

    def test_acceptance_follows_subject_terminal_state(self, tmp_path: Path) -> None:
        """T15：任务收口时验收节点随被验节点终局、状态一致，不按残留节点判失败。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            service.store.update_node(
                CHILD,
                lambda record: record.model_copy(update={'status': 'success'}),
            )
            service.store.update_node(
                ROOT,
                lambda record: record.model_copy(update={'status': 'success'}),
            )
            dispatcher = service.task_actor_service._create_dispatcher(TASK)

            service.task_actor_service._fail_remaining_nodes(TASK, dispatcher, 'parent task terminal')

            acceptance = service.store.get_node(ACCEPTANCE)
            assert acceptance.status == 'success'
            assert acceptance.failure_reason == ''
        finally:
            import asyncio

            async def _cleanup():
                await dispatcher.close()
                await service.close()

            asyncio.run(_cleanup())
