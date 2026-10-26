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
from main.runtime.append_notice_context import (
    APPEND_NOTICE_CONTEXT_KEY,
    PENDING_APPEND_NOTICE_RECORDS_KEY,
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
                            verdict='pass',
                            submission_id='submission:pass',
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
        finals = [item for item in deliveries if item.get('system_kind') == 'child_final']
        assert finals, 'child_final must be delivered to the parent'
        assert finals[0]['target_node_id'] == ROOT
        assert finals[0]['source_node_id'] == CHILD
        # pass 不回流：收件人随即终局，投递只会留下永远读不到的未读裁决。
        verdicts = [item for item in deliveries if item.get('system_kind') == 'acceptance_verdict']
        assert verdicts == []
        assert handshake['latest_execution_submission_id'] == 'submission:pass'
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
        assert feedback[0].kind == 'acceptance_verdict'
        assert feedback[0].verdict == 'reject'
        assert feedback[0].priority == 'normal'
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
        # 裁决本身也要落到被验节点信箱：kind/priority 由分类器推导，
        # reject_no_progress 必须是 high（P6：父节点必须动作）。
        verdicts = [
            item
            for item in service.store.list_task_node_notifications(TASK, CHILD)
            if item.kind == 'acceptance_verdict'
        ]
        assert verdicts, 'reject_no_progress must leave a typed verdict message'
        assert verdicts[-1].priority == 'high'
        assert verdicts[-1].verdict == 'reject_no_progress'
        assert verdicts[-1].submission_id == 'submission:test'


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

        handled = await service.node_runner._maybe_start_acceptance_handshake(
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


class _GateDispatcher:
    """派发扫描的最小替身：只回答"这个节点此刻是否被驱动"。"""

    def __init__(self, driven: dict[str, bool] | None = None) -> None:
        self.scheduled: list[str] = []
        self.driven = dict(driven or {})

    def node_is_being_driven(self, node_id: str):
        return self.driven.get(node_id)

    def schedule_node(self, node_id: str) -> None:
        self.scheduled.append(node_id)


class TestAcceptanceActivationGate:
    @pytest.mark.asyncio
    async def test_submission_parks_the_execution_node_in_waiting_acceptance(self, tmp_path, monkeypatch):
        """提交即停靠：帧相位必须写成 waiting_acceptance。

        旧实现留成 waiting_tool_results，看门狗于是把"等验收"当"运行中"，
        10 分钟无事件就判停（实盘 task:a0d1bd9bc9fa：14:29:36 提交 → 14:42:52 判停，
        验收其实还在跑）。
        """
        service = _service(tmp_path)
        _seed(service)

        async def noop_wake(*, task_id: str, node_id: str) -> str:
            return 'resumed'

        monkeypatch.setattr(service, 'wake_node_on_message', noop_wake)
        task = service.store.get_task(TASK)
        result = NodeFinalResult(
            status='success',
            delivery_status='final',
            summary='done',
            answer='a',
            evidence=[],
            remaining_work=[],
            blocking_reason='',
            submission_id='submission:park',
        )

        returned = await service.node_runner._maybe_start_acceptance_handshake(
            task=task,
            node=service.store.get_node(CHILD),
            result=result,
        )

        assert returned is not None and returned.delivery_status == 'partial'
        frame = service.log_service.read_runtime_frame(TASK, CHILD)
        assert (frame or {}).get('phase') == 'waiting_acceptance'

    @pytest.mark.asyncio
    async def test_acceptance_does_not_start_while_the_subject_runs(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        dispatcher = _GateDispatcher({CHILD: True})

        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is False
        assert dispatcher.scheduled == []

        dispatcher = _GateDispatcher({CHILD: False})
        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is True
        assert dispatcher.scheduled == [ACCEPTANCE]

    @pytest.mark.asyncio
    async def test_acceptance_does_not_start_for_a_paused_subject(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(update={'is_paused': True, 'pause_reason': 'error'}),
        )
        dispatcher = _GateDispatcher({CHILD: False})

        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is False
        assert dispatcher.scheduled == []

    @pytest.mark.asyncio
    async def test_resume_is_deferred_while_a_verdict_is_pending(self, tmp_path, monkeypatch):
        """已提交、裁决在途的节点：resume 只清暂停位，不起新执行回合。

        实盘 task:a0d1bd9bc9fa：15:48 resume 根节点后，它与仍在核验的最终验收同时
        跑到 15:53，验收通过把任务置 success，迟到的执行回合又被写成 failed。
        """
        service = _service(tmp_path)
        _seed(service)
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(update={'is_paused': True, 'pause_reason': 'error'}),
        )
        resumed: list[str] = []

        async def fake_resume(task_id: str, node_id: str, **_kwargs) -> str:
            resumed.append(node_id)
            return 'resumed'

        monkeypatch.setattr(service.task_actor_service, 'resume_node_entry', fake_resume)

        result = await service._apply_resume_node_command(TASK, node_ids=[CHILD])

        assert result['items'] == [
            {'node_id': CHILD, 'result': 'deferred', 'reason': 'waiting_acceptance'}
        ]
        assert resumed == []
        # 暂停位照常清掉：节点不再"暂停"，但仍在等裁决。
        assert service.store.get_node(CHILD).is_paused is False


class TestTerminalIsAbsorbing:
    @pytest.mark.asyncio
    async def test_a_late_turn_failure_does_not_rewrite_a_successful_node(self, tmp_path):
        """验收通过后，迟到的执行回合不得把已 success 的节点改成 failed。

        实盘 task:a0d1bd9bc9fa：15:53:09 最终验收通过 → 15:53:10 任务 success；
        15:53:15 那个 15:48 resume 出来的根回合撞上 "task already completed"，
        旧实现把它写成了 failed，任务随之翻成失败。
        """
        service = _service(tmp_path)
        _seed(service)
        service.store.update_node(
            CHILD,
            lambda record: record.model_copy(update={'status': 'success', 'final_output': 'accepted'}),
        )

        returned = service.node_runner._mark_failed(TASK, CHILD, reason='task already completed')

        kept = service.store.get_node(CHILD)
        assert kept.status == 'success'
        assert kept.final_output == 'accepted'
        assert kept.failure_reason == ''
        assert returned.status == 'success'


def _patch_handshake(service: MainRuntimeService, **updates) -> None:
    def _mutate(record: NodeRecord) -> NodeRecord:
        metadata = dict(record.metadata or {})
        handshake = dict(metadata.get(ACCEPTANCE_HANDSHAKE_KEY) or {})
        handshake.update(updates)
        metadata[ACCEPTANCE_HANDSHAKE_KEY] = handshake
        return record.model_copy(update={'metadata': metadata})

    service.store.update_node(CHILD, _mutate)


def _queue_delivered(
    service: MainRuntimeService,
    *,
    node_id: str,
    message: str,
    notification_id: str,
    kind: str = 'peer_notice',
) -> str:
    service.store.upsert_task_node_notification(
        {
            'notification_id': notification_id,
            'task_id': TASK,
            'node_id': node_id,
            'epoch_id': '',
            'source_node_id': ROOT,
            'message': message,
            'status': 'delivered',
            'created_at': STAMP,
            'delivered_at': STAMP,
            'kind': kind,
            'priority': 'normal',
        }
    )
    return notification_id


def _sync_projection(service: MainRuntimeService) -> None:
    """`_seed` 直写 store，投影表要补一份才进得了通知复派的范围。"""
    for node in list(service.store.list_nodes(TASK) or []):
        service.log_service._sync_node_read_models_locked(node)


class TestStrictAcceptanceWake:
    @pytest.mark.asyncio
    async def test_unread_mail_alone_does_not_schedule_acceptance(self, tmp_path):
        """验收节点没有待裁定的闭合提交时，信箱里的未读不构成派发理由。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            _patch_handshake(service, latest_execution_result_ref='')
            _queue_delivered(
                service,
                node_id=ACCEPTANCE,
                message='操作员补充要求',
                notification_id='notif:strict:acceptance',
            )
            dispatcher = _GateDispatcher({CHILD: False})

            assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is False
            assert dispatcher.scheduled == []
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_pending_submission_schedules_acceptance_without_unread(self, tmp_path):
        """真正的激活源是绑定执行节点的待裁定提交，且不依赖信箱未读。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            dispatcher = _GateDispatcher({CHILD: False})

            assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is True
            assert dispatcher.scheduled == [ACCEPTANCE]
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_submission_bound_to_another_acceptance_is_ignored(self, tmp_path):
        """握手点名的不是这颗验收节点：未读再多也不派发。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            _patch_handshake(service, acceptance_node_id='node:other')
            _queue_delivered(
                service,
                node_id=ACCEPTANCE,
                message='任意消息',
                notification_id='notif:strict:other-binding',
            )
            dispatcher = _GateDispatcher({CHILD: False})

            assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is False
            assert dispatcher.scheduled == []
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_notice_resume_skips_acceptance_without_pending_submission(self, tmp_path, monkeypatch):
        """通知复派也不能用一条未读把验收节点叫起来。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            _sync_projection(service)
            _patch_handshake(service, latest_execution_result_ref='')
            _queue_delivered(
                service,
                node_id=ACCEPTANCE,
                message='堆积消息',
                notification_id='notif:strict:resume-skip',
            )
            executed: list[str] = []

            async def fake_execute(task_id: str, node_id: str):
                executed.append(node_id)
                return NodeFinalResult(
                    status='success',
                    delivery_status='partial',
                    summary='test',
                    answer='',
                    evidence=[],
                    remaining_work=[],
                    blocking_reason='',
                )

            monkeypatch.setattr(service.task_actor_service, '_execute_node', fake_execute)

            assert await service.task_actor_service._resume_pending_notice_nodes(TASK) is False
            assert executed == []
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_notice_resume_runs_acceptance_with_pending_submission(self, tmp_path, monkeypatch):
        """有绑定执行节点的待裁定提交时，通知复派照常把验收回合拉起来。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            _sync_projection(service)
            _queue_delivered(
                service,
                node_id=ACCEPTANCE,
                message='待并入消息',
                notification_id='notif:strict:resume-run',
            )
            executed: list[str] = []

            async def fake_execute(task_id: str, node_id: str):
                executed.append(node_id)
                return NodeFinalResult(
                    status='success',
                    delivery_status='partial',
                    summary='test',
                    answer='',
                    evidence=[],
                    remaining_work=[],
                    blocking_reason='',
                )

            monkeypatch.setattr(service.task_actor_service, '_execute_node', fake_execute)

            assert await service.task_actor_service._resume_pending_notice_nodes(TASK) is True
            assert executed == [ACCEPTANCE]
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_wake_timer_does_not_wake_acceptance(self, tmp_path, monkeypatch):
        """`schedule_wake` 到点、peer 消息、操作员消息都只落箱，不唤醒验收节点。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            wakes, _ = _capture_calls(service, monkeypatch)

            receipt = await service.deliver_node_message(
                task_id=TASK,
                target_node_id=ACCEPTANCE,
                message='Scheduled wake-up',
                origin='node',
                system_kind='wake_timer',
                source_node_id=ACCEPTANCE,
            )

            assert receipt['status'] == 'delivered'
            assert wakes == []
        finally:
            await service.close()


class TestAcceptanceNoticeMirror:
    @pytest.mark.asyncio
    async def test_only_user_and_frontdoor_notifications_reach_the_acceptance_tail(self, tmp_path):
        """验收只判用户/前门的要求；节点之间的消息是执行上下文，不镜像。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            user_id = _queue_delivered(
                service,
                node_id=CHILD,
                message='用户补充：必须覆盖 Safari',
                notification_id='notif:mirror:user',
                kind='user_direct',
            )
            frontdoor_id = _queue_delivered(
                service,
                node_id=CHILD,
                message='前门补充：验收标准加一条',
                notification_id='notif:mirror:frontdoor',
                kind='frontdoor_notice',
            )
            child_id = _queue_delivered(
                service,
                node_id=CHILD,
                message='子节点交付：报告 v2',
                notification_id='notif:mirror:child',
                kind='child_final',
            )
            peer_id = _queue_delivered(
                service,
                node_id=CHILD,
                message='兄弟节点进度：50%',
                notification_id='notif:mirror:peer',
                kind='peer_notice',
            )
            service.node_runner._consume_inflight_notice_ids(
                task_id=TASK,
                node_id=CHILD,
                pending_notification_ids=[user_id, frontdoor_id, child_id, peer_id],
                pending_root_notice_ids=[],
            )

            child = service.store.get_node(CHILD)
            block = service.node_runner._acceptance_notice_block(accepted_node=child)
            assert block.startswith('追加任务要求')
            assert '用户补充：必须覆盖 Safari' in block
            assert '前门补充：验收标准加一条' in block
            assert '子节点交付：报告 v2' not in block
            assert '兄弟节点进度：50%' not in block

            task = service.store.get_task(TASK)
            acceptance = service.store.get_node(ACCEPTANCE)
            tail = service.node_runner._acceptance_turn_tail(task=task, node=acceptance)
            assert '用户补充：必须覆盖 Safari' in tail
            assert '前门补充：验收标准加一条' in tail
            assert '子节点交付：报告 v2' not in tail
            assert '兄弟节点进度：50%' not in tail
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_unread_superseded_and_node_notices_do_not_reach_the_acceptance_tail(self, tmp_path):
        """未消费的消息不镜像；superseded 与节点消息不作为当前验收依据。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            _queue_delivered(
                service,
                node_id=CHILD,
                message='未消费的消息',
                notification_id='notif:mirror:unread',
            )

            def _mutate(record: NodeRecord) -> NodeRecord:
                metadata = dict(record.metadata or {})
                metadata[APPEND_NOTICE_CONTEXT_KEY] = {
                    'notice_records': [
                        {
                            'notification_id': 'notif:current',
                            'kind': 'user_direct',
                            'message': '当前要求',
                            'superseded_at': '',
                        },
                        {
                            'notification_id': 'notif:old',
                            'kind': 'user_direct',
                            'message': '旧要求',
                            'superseded_at': STAMP,
                        },
                        {
                            'notification_id': 'notif:node',
                            'kind': 'parent_notice',
                            'message': '父节点补充',
                            'superseded_at': '',
                        },
                        {
                            'notification_id': 'notif:legacy',
                            'message': '旧版通知',
                            'superseded_at': '',
                        },
                    ]
                }
                metadata[PENDING_APPEND_NOTICE_RECORDS_KEY] = [
                    {'notification_id': 'root-notice:1', 'message': '待消费要求'}
                ]
                return record.model_copy(update={'metadata': metadata})

            service.store.update_node(CHILD, _mutate)
            child = service.store.get_node(CHILD)
            block = service.node_runner._acceptance_notice_block(accepted_node=child)

            assert '当前要求' in block
            assert '旧版通知' in block
            assert '待消费要求' in block
            assert '旧要求' not in block
            assert '父节点补充' not in block
            assert '未消费的消息' not in block
        finally:
            await service.close()


class TestWaitingAcceptanceDelivery:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ('origin', 'relation', 'expected_kind'),
        [
            ('user', '', 'user_direct'),
            ('frontdoor', '', 'frontdoor_notice'),
            ('node', 'parent', 'parent_notice'),
        ],
    )
    async def test_user_parent_frontdoor_messages_are_queued_with_english_notice(
        self, tmp_path, monkeypatch, origin, relation, expected_kind
    ):
        """提交待验期间：用户/父级/前门消息落箱不唤醒，回执说明消费时机。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            wakes, _ = _capture_calls(service, monkeypatch)

            receipt = await service.deliver_node_message(
                task_id=TASK,
                target_node_id=CHILD,
                message='late requirement',
                origin=origin,
                relation=relation,
                source_node_id=ROOT if origin == 'node' else '',
            )

            assert receipt['status'] == 'delivered'
            assert receipt['kind'] == expected_kind
            assert receipt['wake'] == ''
            assert receipt['target_state'] == 'waiting_acceptance'
            assert 'awaiting acceptance' in receipt['notice']
            assert 'will not be consumed' in receipt['notice']
            assert wakes == []
            rows = service.store.list_task_node_notifications(TASK, CHILD)
            assert any(row.message == 'late requirement' for row in rows)
        finally:
            await service.close()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('relation', ['peer', 'child'])
    async def test_peer_and_child_messages_are_still_rejected(self, tmp_path, relation):
        """peer/child 的消息仍是执行期上下文，不进入待验节点的信箱。"""
        service = _service(tmp_path)
        try:
            _seed(service)
            receipt = await service.deliver_node_message(
                task_id=TASK,
                target_node_id=CHILD,
                message='operational chatter',
                origin='node',
                relation=relation,
                source_node_id=ROOT,
            )

            assert receipt['status'] == 'rejected'
            assert receipt['reason'] == 'target_under_acceptance'
            assert receipt['target_state'] == 'waiting_acceptance'
            assert 'notice' not in receipt
        finally:
            await service.close()
