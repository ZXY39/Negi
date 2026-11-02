"""Point-named resume is durable and truthful.

Covers the incident shape: resume clears the pause flag, the pickup then reaps
the "in_progress without executor" node as an orphan, and the node never runs.
The durable intent is consumed before the orphan reaper, and `resumed` is only
reported when an executor is actually alive (or was just created).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.service.runtime_service import MainRuntimeService

TASK = 'task:resume-intent'
ROOT = 'node:root'
CHILD = 'node:child'
STAMP = '2026-11-02T00:00:00+08:00'


class _FakeDispatcher:
    def __init__(self) -> None:
        self.driven: dict[str, bool] = {}

    def node_is_being_driven(self, node_id: str):
        return self.driven.get(node_id)


def _service(tmp_path: Path, *, mode: str = 'web') -> MainRuntimeService:
    return MainRuntimeService(
        chat_backend=SimpleNamespace(),
        workspace_root=tmp_path,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode=mode,
    )


def _node(node_id: str, *, parent: str | None, kind: str = 'execution', depth: int = 0, metadata: dict | None = None) -> NodeRecord:
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
        metadata=dict(metadata or {}),
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
    service.store.upsert_node(
        _node(
            CHILD,
            parent=ROOT,
            depth=1,
            metadata={'spawn_owner_kind': 'child', 'spawn_owner_round_id': 'round:1'},
        )
    )


def _pause(service: MainRuntimeService) -> None:
    service.log_service.set_node_pause_state(
        TASK,
        CHILD,
        pause_requested=True,
        is_paused=True,
        pause_reason='error',
        remark='provider unavailable',
    )


def test_store_claim_allows_exactly_one_consumer(tmp_path: Path) -> None:
    store = _service(tmp_path).store
    try:
        store.upsert_task_node_resume_intent(
            task_id=TASK,
            node_id=CHILD,
            command_id='command:1',
            requested_at=STAMP,
            updated_at=STAMP,
        )
        first = store.claim_task_node_resume_intent(CHILD, claimed_at=STAMP)
        second = store.claim_task_node_resume_intent(CHILD, claimed_at='2026-11-02T00:00:01+08:00')
        assert first is not None and first.command_id == 'command:1'
        assert second is None
        assert store.finish_task_node_resume_intent(
            node_id=CHILD,
            command_id='command:1',
            state='applied',
            outcome='resumed',
            finished_at='2026-11-02T00:00:02+08:00',
        )
        intent = store.get_task_node_resume_intent(CHILD)
        assert intent is not None
        assert intent.state == 'applied' and intent.outcome == 'resumed'
        # 新请求可以把已落定的意图重新置为 pending。
        store.upsert_task_node_resume_intent(
            task_id=TASK,
            node_id=CHILD,
            command_id='command:2',
            requested_at='2026-11-02T00:00:03+08:00',
            updated_at='2026-11-02T00:00:03+08:00',
        )
        assert store.get_task_node_resume_intent(CHILD).state == 'pending'
    finally:
        store.close()


@pytest.mark.asyncio
async def test_web_resume_only_records_intent_and_enqueues_command(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._assert_worker_available = lambda: None
    try:
        _seed(service)
        _pause(service)
        result = await service.control_nodes(TASK, [CHILD], 'resume')
        item = result['items'][0]
        assert item['result'] == 'queued'
        assert item['command_id']
        # leader 不本地清位：复活只能由 worker 的 triage 证明。
        node = service.get_node(CHILD)
        assert node is not None and node.is_paused is True
        intent = service.store.get_task_node_resume_intent(CHILD)
        assert intent is not None and intent.state == 'pending'
        assert intent.command_id == item['command_id']
        commands = service.store.list_unfinished_task_commands(task_id=TASK)
        assert [(row['command_type'], row['command_id']) for row in commands] == [
            ('resume_node', item['command_id'])
        ]
        command = service.store.get_task_command(item['command_id'])
        assert command is not None
        assert command['payload']['node_ids'] == [CHILD]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_task_level_gates_refuse_node_resume(tmp_path: Path) -> None:
    service = _service(tmp_path, mode='embedded')
    try:
        _seed(service)
        _pause(service)
        service.log_service.set_pause_state(TASK, pause_requested=True, is_paused=True)
        paused = await service._apply_resume_node_command(
            TASK, node_ids=[CHILD], schedule_if_inactive=False
        )
        assert paused['items'][0]['result'] == 'task_paused'
        assert service.get_node(CHILD).is_paused is True

        service.log_service.set_pause_state(TASK, pause_requested=False, is_paused=False)
        service.log_service.request_cancel(TASK)
        canceled = await service._apply_resume_node_command(
            TASK, node_ids=[CHILD], schedule_if_inactive=False
        )
        assert canceled['items'][0]['result'] == 'cancel_requested'
        assert service.get_node(CHILD).is_paused is True

        service.store.update_task(
            TASK,
            lambda record: record.model_copy(update={'status': 'success', 'cancel_requested': False}),
        )
        terminal = await service._apply_resume_node_command(
            TASK, node_ids=[CHILD], schedule_if_inactive=False
        )
        assert terminal['items'][0]['result'] == 'task_terminal'
        assert service.get_node(CHILD).is_paused is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_resume_with_dispatcher_reports_resumed_and_settles_intent(tmp_path: Path) -> None:
    service = _service(tmp_path, mode='embedded')
    try:
        _seed(service)
        _pause(service)
        calls: list[str] = []

        async def fake_resume_entry(task_id: str, node_id: str, **_kwargs) -> str:
            calls.append(node_id)
            return 'resumed'

        service.task_actor_service._dispatchers[TASK] = _FakeDispatcher()
        service.task_actor_service.resume_node_entry = fake_resume_entry  # type: ignore[method-assign]
        result = await service._apply_resume_node_command(
            TASK, node_ids=[CHILD], force=True, command_id='command:worker'
        )
        assert result['items'][0]['result'] == 'resumed'
        assert calls == [CHILD]
        node = service.get_node(CHILD)
        assert node is not None and node.is_paused is False
        intent = service.store.get_task_node_resume_intent(CHILD)
        assert intent is not None and intent.state == 'applied' and intent.outcome == 'resumed'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_root_resume_unpauses_the_task_and_requeues_it(tmp_path: Path) -> None:
    service = _service(tmp_path, mode='embedded')
    try:
        _seed(service)
        service.log_service.set_node_pause_state(
            TASK, ROOT, pause_requested=True, is_paused=True, pause_reason='manual'
        )
        enqueued: list[str] = []

        class _Scheduler:
            async def enqueue_task(self, task_id: str) -> None:
                enqueued.append(task_id)

            async def close(self) -> None:
                return None

        service.global_scheduler = _Scheduler()  # type: ignore[assignment]
        result = await service._apply_resume_node_command(TASK, node_ids=[ROOT])
        assert result['items'][0]['result'] == 'queued'
        task = service.get_task(TASK)
        assert task is not None and task.is_paused is False and task.pause_requested is False
        assert enqueued == [TASK]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_without_dispatcher_queues_then_pickup_consumes_intent(tmp_path: Path) -> None:
    service = _service(tmp_path, mode='embedded')
    try:
        _seed(service)
        _pause(service)
        result = await service._apply_resume_node_command(
            TASK, node_ids=[CHILD], force=True, schedule_if_inactive=False
        )
        assert result['items'][0]['result'] == 'queued'
        assert service.store.get_task_node_resume_intent(CHILD).state == 'pending'
        node = service.get_node(CHILD)
        assert node is not None and node.is_paused is False

        calls: list[str] = []

        async def fake_resume_entry(task_id: str, node_id: str, **_kwargs) -> str:
            calls.append(node_id)
            return 'resumed'

        service.task_actor_service.resume_node_entry = fake_resume_entry  # type: ignore[method-assign]
        await service.task_actor_service._consume_resume_intents(TASK, _FakeDispatcher())
        assert calls == [CHILD]
        intent = service.store.get_task_node_resume_intent(CHILD)
        assert intent is not None and intent.state == 'applied' and intent.outcome == 'resumed'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pickup_consumer_runs_before_orphan_reaper_and_reaper_skips_pending_intent(tmp_path: Path) -> None:
    service = _service(tmp_path, mode='embedded')
    try:
        _seed(service)
        _pause(service)
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD, phase='before_model')
        )
        service.store.upsert_task_node_resume_intent(
            task_id=TASK,
            node_id=CHILD,
            command_id='command:pickup',
            requested_at=STAMP,
            updated_at=STAMP,
        )

        dispatcher = _FakeDispatcher()

        async def fake_resume_entry(task_id: str, node_id: str, **_kwargs) -> str:
            dispatcher.driven[node_id] = True
            return 'resumed'

        service.task_actor_service.resume_node_entry = fake_resume_entry  # type: ignore[method-assign]
        await service.task_actor_service._consume_resume_intents(TASK, dispatcher)
        await service.task_actor_service._reconcile_orphan_in_progress_nodes(TASK, dispatcher)
        child = service.get_node(CHILD)
        assert child is not None and child.pause_reason != 'error'
        assert child.is_paused is False
        assert service.store.list_task_error_logs(TASK) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_reaper_skips_a_node_whose_intent_is_still_pending(tmp_path: Path) -> None:
    service = _service(tmp_path, mode='embedded')
    try:
        _seed(service)
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD, phase='before_model')
        )
        service.store.upsert_task_node_resume_intent(
            task_id=TASK,
            node_id=CHILD,
            command_id='command:still-pending',
            requested_at=STAMP,
            updated_at=STAMP,
        )
        await service.task_actor_service._reconcile_orphan_in_progress_nodes(TASK, _FakeDispatcher())
        child = service.get_node(CHILD)
        assert child is not None and child.is_paused is False
        assert service.store.list_task_error_logs(TASK) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_reaper_still_parks_a_node_without_any_intent(tmp_path: Path) -> None:
    service = _service(tmp_path, mode='embedded')
    try:
        _seed(service)
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD, phase='before_model')
        )
        await service.task_actor_service._reconcile_orphan_in_progress_nodes(TASK, _FakeDispatcher())
        child = service.get_node(CHILD)
        assert child is not None and child.is_paused is True and child.pause_reason == 'error'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_terminal_node_and_task_delete_retire_intents(tmp_path: Path) -> None:
    service = _service(tmp_path, mode='embedded')
    try:
        _seed(service)
        _pause(service)
        service.store.upsert_task_node_resume_intent(
            task_id=TASK,
            node_id=CHILD,
            command_id='command:terminal',
            requested_at=STAMP,
            updated_at=STAMP,
        )
        service.node_runner.fail_paused_node(TASK, CHILD, 'operator decided')
        assert service.store.get_task_node_resume_intent(CHILD) is None

        service.store.upsert_task_node_resume_intent(
            task_id=TASK,
            node_id=ROOT,
            command_id='command:delete',
            requested_at=STAMP,
            updated_at=STAMP,
        )
        service.store.delete_task(TASK)
        assert service.store.get_task_node_resume_intent(ROOT) is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_worker_command_lane_keeps_the_leaders_command_id(tmp_path: Path) -> None:
    """从 worker 的命令入口喂：台账行必须留着发起方（leader）那条命令的 id。

    实盘读数是 leader 回执写 `command:0d8487287a60`、行里却是 worker 自造的
    `command:9b172f5360a1`——拿回执上的编号查不到台账，追踪链在第二个进程里断掉。
    """
    service = _service(tmp_path, mode='worker')
    try:
        _seed(service)
        _pause(service)
        enqueued: list[str] = []

        class _Scheduler:
            async def enqueue_task(self, task_id: str) -> None:
                enqueued.append(task_id)

            async def close(self) -> None:
                return None

        service.global_scheduler = _Scheduler()  # type: ignore[assignment]
        await service._process_worker_command(
            {
                'command_id': 'command:leader',
                'command_type': 'resume_node',
                'task_id': TASK,
                'payload': {'node_ids': [CHILD]},
            }
        )
        intent = service.store.get_task_node_resume_intent(CHILD)
        assert intent is not None
        assert intent.command_id == 'command:leader'
        assert intent.state == 'pending'
        assert enqueued == [TASK]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pickup_settled_receipt_does_not_keep_the_queued_reason(tmp_path: Path) -> None:
    """拾取道把意图落成 `resumed` 后，回执不能再留着 queued 阶段那句"等下一次拾取"。

    实盘字节是 `{"result": "resumed", "reason": "awaiting_worker_pickup"}`：同一行里
    同时说"执行器已活"和"还没人消费"，正是这一刀要消灭的混合语义。
    """
    service = _service(tmp_path, mode='worker')
    try:
        _seed(service)
        _pause(service)
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD, phase='before_model')
        )
        dispatcher = _FakeDispatcher()

        async def fake_resume_entry(task_id: str, node_id: str, **_kwargs) -> str:
            dispatcher.driven[node_id] = True
            return 'resumed'

        service.task_actor_service.resume_node_entry = fake_resume_entry  # type: ignore[method-assign]

        class _PickupScheduler:
            async def enqueue_task(self, task_id: str) -> None:
                # 拾取真的发生（不只是排进去），意图在下一次轮询前就被消费成 resumed。
                await service.task_actor_service._consume_resume_intents(task_id, dispatcher)

            async def close(self) -> None:
                return None

        service.global_scheduler = _PickupScheduler()  # type: ignore[assignment]
        result = await service._apply_resume_node_command(
            TASK, node_ids=[CHILD], force=True, command_id='command:leader'
        )
        item = result['items'][0]
        assert item['result'] == 'resumed'
        assert 'reason' not in item
        intent = service.store.get_task_node_resume_intent(CHILD)
        assert intent is not None and intent.state == 'applied' and intent.outcome == 'resumed'
    finally:
        await service.close()
