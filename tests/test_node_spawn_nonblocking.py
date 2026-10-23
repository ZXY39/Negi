"""Non-blocking spawn: creation receipts, the runnable scan, and all_children wake."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import (
    NodeFinalResult,
    NodeRecord,
    SpawnChildResult,
    SpawnChildSpec,
    TaskRecord,
)
from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    set_acceptance_handshake_state,
)
from main.runtime.internal_tools import SpawnChildNodesTool
from main.service.runtime_service import MainRuntimeService

TASK = 'task:spawn-nonblocking'
ROOT = 'node:root'
CHILD_A = 'node:a'
CHILD_B = 'node:b'
ACCEPTANCE = 'node:accept'
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
    service.store.upsert_node(_node(CHILD_A, parent=ROOT, depth=1))
    service.store.upsert_node(_node(CHILD_B, parent=ROOT, depth=1))


def _spec(goal: str) -> SpawnChildSpec:
    return SpawnChildSpec(goal=goal, prompt='p', execution_policy={'mode': 'focus'})


class TestCreationGates:
    def _runner(self, service, max_nodes: int = 200):
        service.node_runner._max_task_nodes = max_nodes
        return service.node_runner

    def test_depth_gate_rejects_the_whole_batch(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        task = service.store.get_task(TASK)
        task = task.model_copy(update={'max_depth': 1})
        root = service.store.get_node(ROOT)
        runner = self._runner(service)
        specs = [_spec('a'), _spec('b')]
        payload = {'entries': [{'goal': 'a'}, {'goal': 'b'}]}
        kept = runner._apply_spawn_creation_gates(
            task=task, parent=root, specs=specs, allowed_indexes=[0, 1], cached_payload=payload
        )
        assert kept == []
        assert payload['entries'][0]['creation_gate'] == 'depth'
        receipts = runner._spawn_creation_receipts(specs=specs, entries=payload['entries'])
        assert [item.state for item in receipts] == ['rejected', 'rejected']
        assert receipts[0].gate == 'depth'
        assert 'max_depth' in receipts[0].reject_reason

    def test_max_nodes_budget_counts_acceptance_nodes(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)  # root + 2 children = 3 nodes
        task = service.store.get_task(TASK)
        task = task.model_copy(update={'max_depth': 10})
        root = service.store.get_node(ROOT)
        runner = self._runner(service, max_nodes=5)
        with_acceptance = _spec('a')
        with_acceptance = with_acceptance.model_copy(
            update={'acceptance_prompt': 'check', 'requires_acceptance': True}
        )
        specs = [with_acceptance, _spec('b')]
        payload = {'entries': [{'goal': 'a'}, {'goal': 'b'}]}
        kept = runner._apply_spawn_creation_gates(
            task=task, parent=root, specs=specs, allowed_indexes=[0, 1], cached_payload=payload
        )
        # 3 + (1 child + 1 acceptance) = 5 → 第一个刚好用满，第二个再无额度。
        assert kept == [0]
        assert payload['entries'][1]['creation_gate'] == 'max_nodes'
        receipts = runner._spawn_creation_receipts(specs=specs, entries=[
            {'goal': 'a', 'child_node_id': CHILD_A, 'acceptance_node_id': ACCEPTANCE},
            payload['entries'][1],
        ])
        assert [item.state for item in receipts] == ['created', 'rejected']
        assert receipts[1].gate == 'max_nodes'
        assert '上限' in receipts[1].reject_reason


class TestCreationReceipts:
    def test_created_blocked_and_unmaterialized(self):
        service = _service(Path('.'))
        specs = [_spec('a'), _spec('b'), _spec('c')]
        entries = [
            {'goal': 'a', 'child_node_id': CHILD_A, 'acceptance_node_id': ''},
            {'goal': 'b', 'review_decision': 'blocked', 'blocked_reason': '与已有分支重复', 'blocked_suggestion': '合并'},
            {'goal': 'c', 'runtime_error_text': 'materialization failed'},
        ]
        receipts = service.node_runner._spawn_creation_receipts(specs=specs, entries=entries)
        assert [item.state for item in receipts] == ['created', 'rejected', 'rejected']
        assert receipts[0].child_node_id == CHILD_A
        assert receipts[1].gate == 'review'
        assert '重复' in receipts[1].reject_reason
        assert receipts[2].gate == 'materialization'

    def test_replays_are_idempotent_receipts(self):
        service = _service(Path('.'))
        specs = [_spec('a')]
        entries = [{'goal': 'a', 'child_node_id': CHILD_A, 'acceptance_node_id': ACCEPTANCE}]
        first = service.node_runner._spawn_creation_receipts(specs=specs, entries=entries)
        second = service.node_runner._spawn_creation_receipts(specs=specs, entries=entries)
        assert first == second
        assert first[0].acceptance_node_id == ACCEPTANCE


class TestToolReturnShape:
    @pytest.mark.asyncio
    async def test_returns_created_and_rejected_not_results(self):
        async def spawn(specs, call_id):
            return [
                SpawnChildResult(goal='a', state='created', child_node_id=CHILD_A),
                SpawnChildResult(
                    goal='b',
                    state='rejected',
                    gate='review',
                    reject_reason='重复',
                    review_blocked=True,
                ),
            ]

        raw = await SpawnChildNodesTool(spawn).execute(
            children=[
                {'goal': 'a', 'prompt': 'p', 'execution_policy': {'mode': 'focus'}},
                {'goal': 'b', 'prompt': 'p', 'execution_policy': {'mode': 'focus'}},
            ]
        )
        payload = json.loads(raw)
        assert payload['created'] == [
            {'node_id': CHILD_A, 'goal': 'a', 'acceptance_node_id': ''}
        ]
        assert payload['rejected'] == [{'goal': 'b', 'gate': 'review', 'reason': '重复'}]
        assert 'child_final' in payload['summary']
        assert 'children' not in payload

    def test_no_longer_exempts_the_universal_timeout(self):
        tool = SpawnChildNodesTool(_noop)
        assert getattr(tool, 'exempt_universal_timeout', False) is False


class _FakeDispatcher:
    def __init__(self) -> None:
        self.scheduled: list[str] = []
        self.driven: dict[str, bool] = {}

    def node_is_being_driven(self, node_id: str):
        return self.driven.get(node_id)

    def schedule_node(self, node_id: str) -> None:
        self.scheduled.append(node_id)


class TestRunnableScan:
    @pytest.mark.asyncio
    async def test_fresh_child_is_dispatched(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        dispatcher = _FakeDispatcher()
        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is True
        assert sorted(dispatcher.scheduled) == sorted([CHILD_A, CHILD_B])

    @pytest.mark.asyncio
    async def test_parked_child_is_not_redispatched_without_mail(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        for node_id in (CHILD_A, CHILD_B):
            service.store.upsert_task_runtime_frame(
                TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=node_id, phase='waiting_explicit')
            )
        dispatcher = _FakeDispatcher()
        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is False
        assert dispatcher.scheduled == []

    @pytest.mark.asyncio
    async def test_mail_wakes_a_parked_child(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD_A, phase='waiting_explicit')
        )
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD_B, phase='waiting_explicit')
        )
        service.store.upsert_task_node_notification(
            {
                'notification_id': 'notif:1',
                'task_id': TASK,
                'node_id': CHILD_B,
                'epoch_id': '',
                'source_node_id': CHILD_A,
                'message': 'here',
                'status': 'delivered',
                'created_at': STAMP,
                'delivered_at': STAMP,
            }
        )
        dispatcher = _FakeDispatcher()
        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is True
        assert dispatcher.scheduled == [CHILD_B]

    @pytest.mark.asyncio
    async def test_running_child_is_left_alone(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        dispatcher = _FakeDispatcher()
        dispatcher.driven = {CHILD_A: True, CHILD_B: True}
        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is False

    @pytest.mark.asyncio
    async def test_acceptance_is_dispatched_only_when_a_verdict_is_pending(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        service.store.upsert_node(_node(ACCEPTANCE, parent=CHILD_A, kind='acceptance', depth=2))
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD_A, phase='before_model')
        )
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD_B, phase='before_model')
        )
        service.store.update_node(
            ACCEPTANCE,
            lambda record: record.model_copy(
                update={'metadata': {**dict(record.metadata or {}), 'accepted_node_id': CHILD_A}}
            ),
        )
        dispatcher = _FakeDispatcher()
        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is False

        handshake = set_acceptance_handshake_state(
            {},
            state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
            acceptance_node_id=ACCEPTANCE,
            rejection_count=0,
            latest_execution_result_ref='artifact:1',
            latest_execution_result_summary='x',
            latest_rejection_feedback_ref='',
            latest_rejection_feedback_summary='',
            updated_at=STAMP,
        )
        service.store.update_node(
            CHILD_A,
            lambda record: record.model_copy(
                update={
                    'metadata': {
                        **dict(record.metadata or {}),
                        ACCEPTANCE_HANDSHAKE_KEY: handshake,
                    }
                }
            ),
        )
        dispatcher.scheduled.clear()
        assert await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher) is True
        assert dispatcher.scheduled == [ACCEPTANCE]

    @pytest.mark.asyncio
    async def test_root_is_left_parked_without_mail_but_woken_with_mail(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        for node_id in (CHILD_A, CHILD_B):
            service.store.upsert_task_runtime_frame(
                TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=node_id, phase='before_model')
            )
        dispatcher = _FakeDispatcher()
        await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher)
        assert ROOT not in dispatcher.scheduled

        service.store.upsert_task_node_notification(
            {
                'notification_id': 'notif:root',
                'task_id': TASK,
                'node_id': ROOT,
                'epoch_id': '',
                'source_node_id': CHILD_A,
                'message': 'child finished',
                'status': 'delivered',
                'created_at': STAMP,
                'delivered_at': STAMP,
            }
        )
        dispatcher.scheduled.clear()
        await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher)
        assert dispatcher.scheduled == [ROOT]

    @pytest.mark.asyncio
    async def test_paused_child_is_skipped(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        service.store.update_node(
            CHILD_A, lambda record: record.model_copy(update={'pause_requested': True})
        )
        dispatcher = _FakeDispatcher()
        await service.task_actor_service._dispatch_runnable_nodes(TASK, dispatcher)
        assert dispatcher.scheduled == [CHILD_B]


class TestAllChildrenWake:
    @pytest.mark.asyncio
    async def test_wakes_only_after_every_child_reported(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=ROOT, phase='waiting_explicit')
        )
        service.store.update_node(
            ROOT,
            lambda record: record.model_copy(
                update={'metadata': {**dict(record.metadata or {}), 'node_wait_wake_on': 'all_children'}}
            ),
        )
        wakes: list[str] = []

        async def wake(*, task_id: str, node_id: str) -> str:
            wakes.append(node_id)
            return 'resumed'

        monkeypatch.setattr(service, 'wake_node_on_message', wake)

        def finish(node_id: str) -> None:
            service.store.update_node(
                node_id,
                lambda record: record.model_copy(
                    update={
                        'status': 'success',
                        'metadata': {
                            **dict(record.metadata or {}),
                            'result_payload': NodeFinalResult(
                                status='success',
                                delivery_status='final',
                                summary='done',
                                answer='',
                                evidence=[],
                                remaining_work=[],
                                blocking_reason='',
                            ).model_dump(mode='json'),
                        },
                    }
                ),
            )

        finish(CHILD_A)
        await service.deliver_node_message(
            task_id=TASK,
            target_node_id=ROOT,
            message='a done',
            origin='node',
            relation='parent',
            system_kind='child_final',
            source_node_id=CHILD_A,
        )
        assert wakes == []

        finish(CHILD_B)
        await service.deliver_node_message(
            task_id=TASK,
            target_node_id=ROOT,
            message='b done',
            origin='node',
            relation='parent',
            system_kind='child_final',
            source_node_id=CHILD_B,
        )
        assert wakes == [ROOT]


async def _noop(specs, call_id):
    return []
