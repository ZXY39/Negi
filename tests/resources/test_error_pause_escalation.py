"""异常暂停的向上通知合同：非根节点的每一条 error pause 都必须到父节点信箱。

锁定四件事：

1. `_mark_node_paused(reason='error')` 是唯一的 error 暂停入口，写入成功后对非根
   节点发 `child_abnormal_pause`；根节点没有父节点，留给 `task_node_error` 心跳。
2. 同一暂停的重复写入（NodePausedError 兜底再 mark 一次）不重复升级；恢复后再次
   暂停会产生新的暂停行，必须重新升级。
3. 孤儿收尸的 `_report_orphan_candidate` 不绕过这条道——这正是实盘
   task:9c08e7128dd4 的缺口：park 了但父节点信箱一条都没有。
4. P8 补发按暂停行 `created_at` 与通知 `created_at` 对账，且覆盖验收节点。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskNodeNotification, TaskNodePauseRecord, TaskRecord
from main.protocol import now_iso
from main.runtime.node_message_kind import KIND_CHILD_ABNORMAL_PAUSE
from main.runtime.node_runner import NodeRunner
from main.runtime.task_actor_service import TaskActorService
from main.service.runtime_service import MainRuntimeService

TASK_ID = 'task:esc'
ROOT_ID = 'node:root'
CHILD_ID = 'node:child'
ACCEPTANCE_ID = 'node:acceptance'


class _PauseStore:
    def __init__(self, nodes: dict[str, NodeRecord]) -> None:
        self.nodes = dict(nodes)
        self.pauses: dict[str, TaskNodePauseRecord] = {}
        self.notifications: dict[tuple[str, str], list[TaskNodeNotification]] = {}

    def get_node(self, node_id: str):
        return self.nodes.get(str(node_id or '').strip())

    def get_task_node_pause(self, node_id: str):
        return self.pauses.get(str(node_id or '').strip())

    def list_nodes(self, task_id: str):
        return [node for node in self.nodes.values() if str(node.task_id or '').strip() == str(task_id or '').strip()]

    def list_task_node_notifications(self, task_id: str, node_id: str):
        return list(self.notifications.get((str(task_id or ''), str(node_id or '')), []))

    def update_node(self, node_id: str, mutator):
        key = str(node_id or '').strip()
        current = self.nodes.get(key)
        if current is None:
            return None
        updated = mutator(current)
        self.nodes[key] = updated
        return updated


class _PauseLogService:
    """Minimal `set_node_pause_state` that mirrors row lifetime.

    Resume deletes the row in production; the tests pop it explicitly to model that.
    """

    def __init__(self, store: _PauseStore) -> None:
        self.store = store

    def set_node_pause_state(
        self,
        task_id: str,
        node_id: str,
        *,
        pause_requested: bool | None = None,
        is_paused: bool | None = None,
        pause_reason: str | None = None,
        remark: str = '',
        delivered: bool = False,
    ):
        key = str(node_id or '').strip()
        current = self.store.get_node(key)
        if current is None:
            return None
        reason = str(pause_reason or '').strip().lower()
        updated = current.model_copy(
            update={
                'pause_requested': True,
                'is_paused': True,
                'pause_reason': reason,
                'updated_at': now_iso(),
            }
        )
        self.store.nodes[key] = updated
        existing = self.store.pauses.get(key)
        reuse_created_at = existing is not None and str(existing.pause_reason or '') == reason
        self.store.pauses[key] = TaskNodePauseRecord(
            id=int(existing.id if existing is not None else 1),
            task_id=str(task_id or ''),
            node_id=key,
            pause_reason=reason,
            remark=str(remark or ''),
            delivered=bool(delivered),
            created_at=str(existing.created_at if reuse_created_at else now_iso()),
            updated_at=now_iso(),
        )
        return updated


def _node(
    node_id: str,
    *,
    parent: str = '',
    kind: str = 'execution',
) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=TASK_ID,
        root_node_id=ROOT_ID,
        parent_node_id=parent or None,
        depth=0 if not parent else 1,
        node_kind=kind,  # type: ignore[arg-type]
        status='in_progress',
        goal='do the work',
        prompt='p',
        created_at=now_iso(),
        updated_at=now_iso(),
        metadata={},
    )


def _runner(store: _PauseStore) -> tuple[NodeRunner, list[dict]]:
    runner = object.__new__(NodeRunner)
    runner._store = store  # type: ignore[attr-defined]
    runner._log_service = _PauseLogService(store)  # type: ignore[attr-defined]
    calls: list[dict] = []
    runner.notify_parent_abnormal_pause = lambda **kwargs: calls.append(kwargs)  # type: ignore[assignment]
    return runner, calls


def test_error_pause_escalates_once_and_again_after_resume() -> None:
    store = _PauseStore({ROOT_ID: _node(ROOT_ID), CHILD_ID: _node(CHILD_ID, parent=ROOT_ID)})
    runner, calls = _runner(store)

    runner.mark_node_error_paused(TASK_ID, CHILD_ID, remark='boom')
    assert len(calls) == 1
    assert calls[0]['node_id'] == CHILD_ID
    assert 'boom' in calls[0]['reason']

    # NodePausedError 兜底会再 mark 一次；同一暂停不得重复升级。
    runner._mark_node_paused(TASK_ID, CHILD_ID)
    assert len(calls) == 1

    # 恢复会清掉暂停行；再次 error 暂停是一个新意图，必须重新升级。
    store.pauses.pop(CHILD_ID, None)
    runner.mark_node_error_paused(TASK_ID, CHILD_ID, remark='boom again')
    assert len(calls) == 2
    assert 'boom again' in calls[1]['reason']


def test_root_error_pause_and_manual_pause_do_not_escalate() -> None:
    store = _PauseStore({ROOT_ID: _node(ROOT_ID), CHILD_ID: _node(CHILD_ID, parent=ROOT_ID)})
    runner, calls = _runner(store)

    runner._mark_node_paused(TASK_ID, ROOT_ID, reason='error', remark='root boom')
    runner._mark_node_paused(TASK_ID, CHILD_ID, reason='manual', remark='operator requested')

    assert calls == []


def test_orphan_candidate_park_goes_through_the_escalation_choke_point() -> None:
    store = _PauseStore({ROOT_ID: _node(ROOT_ID), CHILD_ID: _node(CHILD_ID, parent=ROOT_ID)})
    runner, calls = _runner(store)
    logged: list[dict] = []
    actor = object.__new__(TaskActorService)
    actor._log_service = SimpleNamespace(append_task_error_log=lambda **kwargs: logged.append(kwargs))  # type: ignore[attr-defined]
    actor._node_runner = runner  # type: ignore[attr-defined]

    actor._report_orphan_candidate(  # type: ignore[attr-defined]
        task_id=TASK_ID,
        node=store.get_node(CHILD_ID),
        node_id=CHILD_ID,
    )

    assert logged and 'spawn_orphan_candidate' in str(logged[0].get('error_text') or '')
    assert len(calls) == 1
    assert calls[0]['node_id'] == CHILD_ID
    parked = store.get_node(CHILD_ID)
    assert parked is not None
    assert parked.status == 'in_progress'
    assert parked.pause_reason == 'error'


def _notification(*, status: str, created_at: str) -> TaskNodeNotification:
    return TaskNodeNotification(
        notification_id=f'notif:{created_at}',
        task_id=TASK_ID,
        node_id=ROOT_ID,
        epoch_id='',
        source_node_id=ACCEPTANCE_ID,
        status=status,
        created_at=created_at,
        kind=KIND_CHILD_ABNORMAL_PAUSE,
    )


def _p8_service(store: _PauseStore) -> tuple[MainRuntimeService, list[dict]]:
    service = object.__new__(MainRuntimeService)
    service.store = store  # type: ignore[attr-defined]
    delivered: list[dict] = []

    async def _deliver(**kwargs):
        delivered.append(kwargs)
        return {'status': 'delivered'}

    service._deliver_child_abnormal_pause = _deliver  # type: ignore[attr-defined]
    return service, delivered


def _acceptance_pause_store() -> _PauseStore:
    store = _PauseStore(
        {
            ROOT_ID: _node(ROOT_ID),
            ACCEPTANCE_ID: _node(ACCEPTANCE_ID, parent=ROOT_ID, kind='acceptance'),
        }
    )
    store.pauses[ACCEPTANCE_ID] = TaskNodePauseRecord(
        id=7,
        task_id=TASK_ID,
        node_id=ACCEPTANCE_ID,
        pause_reason='error',
        remark='acceptance lane broke',
        delivered=False,
        created_at='2026-11-02T17:24:12+08:00',
        updated_at='2026-11-02T17:24:12+08:00',
    )
    store.nodes[ACCEPTANCE_ID] = store.nodes[ACCEPTANCE_ID].model_copy(
        update={'pause_requested': True, 'is_paused': True, 'pause_reason': 'error'}
    )
    return store


def test_resend_ignores_an_older_notification_and_covers_acceptance_nodes() -> None:
    store = _acceptance_pause_store()
    store.notifications[(TASK_ID, ROOT_ID)] = [
        _notification(status='consumed', created_at='2026-11-02T16:00:00+08:00'),
    ]
    service, delivered = _p8_service(store)

    resent = asyncio.run(service.resend_lost_abnormal_pause_escalations(TASK_ID))

    assert resent == [ACCEPTANCE_ID]
    assert delivered and delivered[0]['node_id'] == ACCEPTANCE_ID
    assert delivered[0]['reason'] == 'acceptance lane broke'


def test_resend_skips_when_the_notification_covers_the_pause_row() -> None:
    store = _acceptance_pause_store()
    store.notifications[(TASK_ID, ROOT_ID)] = [
        _notification(status='delivered', created_at='2026-11-02T17:24:13+08:00'),
    ]
    service, delivered = _p8_service(store)

    resent = asyncio.run(service.resend_lost_abnormal_pause_escalations(TASK_ID))

    assert resent == []
    assert delivered == []


def test_error_reason_pause_command_uses_the_choke_point() -> None:
    store = _PauseStore({ROOT_ID: _node(ROOT_ID), CHILD_ID: _node(CHILD_ID, parent=ROOT_ID)})
    recorded: list[tuple] = []

    def _mark_error(task_id: str, node_id: str, **kwargs):
        recorded.append((task_id, node_id, kwargs))
        return store.get_node(node_id)

    service = object.__new__(MainRuntimeService)
    service.store = store  # type: ignore[attr-defined]
    service.normalize_task_id = lambda value: str(value or '').strip()  # type: ignore[assignment]
    service.get_task = lambda task_id: SimpleNamespace(task_id=task_id, session_id='web:test')  # type: ignore[assignment]
    service.get_node = lambda node_id: store.get_node(node_id)  # type: ignore[assignment]
    service.node_runner = SimpleNamespace(mark_node_error_paused=_mark_error)  # type: ignore[attr-defined]
    service.log_service = SimpleNamespace(  # type: ignore[attr-defined]
        set_node_pause_state=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('error pause must not bypass mark_node_error_paused')
        )
    )
    service._land_pause_for_stranded_nodes = lambda task_id: []  # type: ignore[attr-defined]

    result = asyncio.run(
        service._apply_pause_node_command(
            TASK_ID,
            node_ids=[CHILD_ID],
            reason='error',
            remark='operator says error',
        )
    )

    assert result['ok'] is True
    assert recorded == [
        (TASK_ID, CHILD_ID, {'remark': 'operator says error'}),
    ]


def _real_service(tmp_path: Path) -> MainRuntimeService:
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
async def test_orphan_candidate_reaches_the_real_parent_mailbox(tmp_path: Path) -> None:
    """实盘 task:9c08e7128dd4 的最小重放：park 之后父节点信箱必须有 child_abnormal_pause。"""
    service = _real_service(tmp_path)
    stamp = now_iso()
    service.store.upsert_task(
        TaskRecord(
            task_id=TASK_ID,
            title='t',
            user_request='u',
            root_node_id=ROOT_ID,
            created_at=stamp,
            updated_at=stamp,
        )
    )
    service.store.upsert_node(_node(ROOT_ID))
    service.store.upsert_node(_node(CHILD_ID, parent=ROOT_ID))

    service.task_actor_service._report_orphan_candidate(  # type: ignore[attr-defined]
        task_id=TASK_ID,
        node=service.store.get_node(CHILD_ID),
        node_id=CHILD_ID,
    )
    await service.node_runner.wait_pending_terminal_messages()
    notifications = []
    for _ in range(20):
        notifications = list(service.store.list_task_node_notifications(TASK_ID, ROOT_ID) or [])
        if notifications:
            break
        await asyncio.sleep(0.01)

    assert [str(item.kind or '') for item in notifications] == [KIND_CHILD_ABNORMAL_PAUSE]
    assert CHILD_ID in str(notifications[0].message or '')
