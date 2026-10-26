"""`schedule_wake` 的到点投递：扫的是表，不是协程里的睡眠。

四条判据都对应一种会静默丢唤醒的形状（实盘 task:4555863a6a49 的报时节点用
`exec Start-Sleep 5400` 当时钟，08:30 设的 10:00 被 09:02 的重启打断后又被重发成
完整 5400 秒）：

- 期限已经过了才被发现 ⇒ 照样投，不重置成"再过 90 分钟"。
- 收件人被暂停 ⇒ 落箱但不叫醒，叫醒会顺手清掉操作员按下的暂停位。
- 节点或任务已终态 ⇒ 删行，表不会一直涨。
- 投递本身抛异常 ⇒ 领取凭证留着，下一拍不重投（磁盘满一类的故障下 5 秒一拍就是热循环）。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.runtime.wake_clock import now_ms
from main.models import NodeRecord, TaskRecord
from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.runtime.node_message_kind import KIND_WAKE_TIMER
from main.runtime.node_wake import WakeDeps
from main.service.runtime_service import MainRuntimeService

TASK = 'task:sweep'
ROOT = 'node:root'
CHILD = 'node:child'


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


def _seed(
    service: MainRuntimeService,
    *,
    node_status: str = 'in_progress',
    task_status: str = 'in_progress',
    paused: bool = False,
    phase: str = 'waiting_explicit',
) -> None:
    stamp = '2026-10-26T08:00:00+08:00'
    service.store.upsert_task(
        TaskRecord(
            task_id=TASK,
            session_id='web:shared',
            status=task_status,
            root_node_id=ROOT,
            title='t',
            user_request='r',
            created_at=stamp,
            updated_at=stamp,
        )
    )
    service.store.upsert_node(
        NodeRecord(
            node_id=ROOT,
            task_id=TASK,
            root_node_id=ROOT,
            goal='root',
            prompt='p',
            status='in_progress',
            created_at=stamp,
            updated_at=stamp,
        )
    )
    service.store.upsert_node(
        NodeRecord(
            node_id=CHILD,
            task_id=TASK,
            parent_node_id=ROOT,
            root_node_id=ROOT,
            depth=1,
            goal='child',
            prompt='p',
            status=node_status,
            is_paused=paused,
            created_at=stamp,
            updated_at=stamp,
        )
    )
    if phase:
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD, phase=phase)
        )


def _stub_wake(monkeypatch, service: MainRuntimeService) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def resume(task_id: str, node_id: str) -> str:
        calls.append((task_id, node_id))
        return 'resumed'

    async def enqueue(task_id: str) -> None:
        calls.append(('enqueue', task_id))

    deps = WakeDeps(
        get_node=service.store.get_node,
        get_phase=service._frame_phase,
        get_task=lambda task_id: service.store.get_task(task_id),
        dispatcher_for=lambda task_id: object(),
        resume_node_entry=resume,
        enqueue_task=enqueue,
        clear_pause=None,
    )
    monkeypatch.setattr(service, 'node_wake_deps', lambda: deps)
    return calls


def _arm(service: MainRuntimeService, *, wake_at_ms: int, note: str = '播报下一幕') -> None:
    service.store.upsert_task_node_timer(
        task_id=TASK,
        node_id=CHILD,
        wake_at_ms=wake_at_ms,
        note=note,
        armed_at='2026-10-26T08:00:00+08:00',
    )


class TestSweep:
    @pytest.mark.asyncio
    async def test_due_wake_lands_a_notice_and_wakes_the_parked_node(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        calls = _stub_wake(monkeypatch, service)
        _arm(service, wake_at_ms=now_ms() + 5_000)  # 未到点：不该被扫到
        assert await service.sweep_due_node_wakes() == 0
        assert service.store.list_task_node_notifications(TASK, CHILD) == []

        _arm(service, wake_at_ms=now_ms() - 1)
        assert await service.sweep_due_node_wakes() == 1
        rows = service.store.list_task_node_notifications(TASK, CHILD)
        assert len(rows) == 1
        assert rows[0].kind == KIND_WAKE_TIMER
        assert rows[0].priority == 'normal'
        assert '播报下一幕' in rows[0].message
        assert calls == [(TASK, CHILD)]
        assert service.store.get_task_node_timer(TASK, CHILD) is None

    @pytest.mark.asyncio
    async def test_deadline_that_passed_while_the_process_was_down_still_fires(self, tmp_path, monkeypatch):
        """重启也认：期限在停机期间过了，恢复后第一次扫描就投，不重新等一整个时长。"""
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service)
        _arm(service, wake_at_ms=now_ms() - 5_400_000)  # 90 分钟前就该响
        assert await service.sweep_due_node_wakes() == 1
        rows = service.store.list_task_node_notifications(TASK, CHILD)
        assert len(rows) == 1
        # 迟到要说出来并给出"重新校时"的出路：模型原本按幕次算好的时刻已经偏了。
        assert 'after its due time' in rows[0].message
        assert 'Re-check the clock' in rows[0].message

    @pytest.mark.asyncio
    async def test_paused_recipient_gets_mail_without_being_woken(self, tmp_path, monkeypatch):
        """叫醒会清掉操作员按下的暂停位，所以这一档只落箱。"""
        service = _service(tmp_path)
        _seed(service, paused=True)
        calls = _stub_wake(monkeypatch, service)
        _arm(service, wake_at_ms=now_ms() - 1)
        assert await service.sweep_due_node_wakes() == 1
        assert calls == []
        rows = service.store.list_task_node_notifications(TASK, CHILD)
        assert len(rows) == 1
        assert 'paused' in rows[0].message
        assert bool(service.store.get_node(CHILD).is_paused)

    @pytest.mark.asyncio
    @pytest.mark.parametrize('node_status', ['success', 'failed'])
    async def test_terminal_recipient_drops_the_deadline_instead_of_retrying(
        self, tmp_path, monkeypatch, node_status
    ):
        service = _service(tmp_path)
        _seed(service, node_status=node_status)
        _stub_wake(monkeypatch, service)
        _arm(service, wake_at_ms=now_ms() - 1)
        assert await service.sweep_due_node_wakes() == 1
        assert service.store.get_task_node_timer(TASK, CHILD) is None
        assert service.store.list_task_node_notifications(TASK, CHILD) == []

    @pytest.mark.asyncio
    async def test_terminal_task_drops_the_deadline_too(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service, task_status='success')
        _stub_wake(monkeypatch, service)
        _arm(service, wake_at_ms=now_ms() - 1)
        assert await service.sweep_due_node_wakes() == 1
        assert service.store.get_task_node_timer(TASK, CHILD) is None
        assert service.store.list_task_node_notifications(TASK, CHILD) == []

    @pytest.mark.asyncio
    async def test_failed_delivery_keeps_its_claim_and_does_not_refire_next_tick(
        self, tmp_path, monkeypatch
    ):
        """异常不退领取位：这就是失败道唯一的退避，否则磁盘满时每 5 秒重投一次。"""
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service)
        _arm(service, wake_at_ms=now_ms() - 1)

        async def raise_delivery(**kwargs):
            raise RuntimeError('disk full')

        monkeypatch.setattr(service, 'deliver_node_message', raise_delivery)
        assert await service.sweep_due_node_wakes() == 1
        timer = service.store.get_task_node_timer(TASK, CHILD)
        assert timer is not None and timer.claimed_by
        assert service.store.list_task_node_notifications(TASK, CHILD) == []
        # 下一拍（凭证还新鲜）不再重投，也不给第二个 worker。
        assert await service.sweep_due_node_wakes() == 0
        assert service.store.claim_due_task_node_timers(now_ms=now_ms(), claimed_by='worker:other') == []
        # 凭证过期后才可以被重领——进程死在投递中间时不会永久丢唤醒。判据取领取时刻，
        # 不是唤醒时刻：领取发生在扫描那一拍，比 `wake_at_ms` 晚几毫秒，用后者会差这几毫秒。
        stale = service.store.claim_due_task_node_timers(
            now_ms=int(timer.claimed_at_ms) + service.store._WAKE_CLAIM_STALE_MS + 1,
            claimed_by='worker:other',
        )
        assert [item.node_id for item in stale] == [CHILD]

    @pytest.mark.asyncio
    async def test_sweep_is_empty_and_cheap_when_nothing_is_armed(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service)
        assert await service.sweep_due_node_wakes() == 0
