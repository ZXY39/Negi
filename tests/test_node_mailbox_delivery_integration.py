"""Mailbox delivery against a real store: persistence, derivation, dedupe, matrix, wake."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord
from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.runtime.node_wake import WakeDeps
from main.service.runtime_service import MainRuntimeService

TASK = 'task:mailbox'
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


def _seed(service: MainRuntimeService, *, phase: str = 'waiting_explicit') -> None:
    stamp = '2026-10-23T00:00:00+08:00'
    service.store.upsert_node(
        NodeRecord(
            node_id=ROOT,
            task_id=TASK,
            root_node_id=ROOT,
            goal='root',
            prompt='p',
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
            created_at=stamp,
            updated_at=stamp,
        )
    )
    if phase:
        service.store.upsert_task_runtime_frame(
            TaskProjectionRuntimeFrameRecord(task_id=TASK, node_id=CHILD, phase=phase)
        )


async def _boom(task_id: str, node_id: str) -> str:
    raise RuntimeError('scheduler offline')


def _stub_wake(monkeypatch, service, *, throw: bool = False):
    calls: list[tuple[str, str]] = []

    async def resume(task_id: str, node_id: str) -> str:
        calls.append((task_id, node_id))
        return 'resumed'

    async def enqueue(task_id: str) -> None:
        calls.append(('enqueue', task_id))

    deps = WakeDeps(
        get_node=service.store.get_node,
        get_phase=service._frame_phase,
        get_task=lambda task_id: None,
        dispatcher_for=lambda task_id: object(),
        resume_node_entry=_boom if throw else resume,
        enqueue_task=enqueue,
        clear_pause=None,
    )
    monkeypatch.setattr(service, 'node_wake_deps', lambda: deps)
    return calls


class TestDelivery:
    @pytest.mark.asyncio
    async def test_persists_derives_and_wakes(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        calls = _stub_wake(monkeypatch, service)

        result = await service.deliver_node_message(
            task_id=TASK,
            target_node_id=CHILD,
            message='send me the result',
            origin='node',
            relation='parent',
            source_node_id=ROOT,
        )

        assert result['status'] == 'delivered'
        assert result['kind'] == 'parent_notice'
        assert result['priority'] == 'normal'
        assert result['wake'] == 'resumed'
        assert calls == [(TASK, CHILD)]

        stored = service.store.list_task_node_notifications(TASK, CHILD)
        assert len(stored) == 1
        assert stored[0].kind == 'parent_notice'
        assert stored[0].priority == 'normal'
        assert stored[0].fingerprint

    @pytest.mark.asyncio
    async def test_second_identical_send_is_deduplicated(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service)
        kwargs = dict(
            task_id=TASK,
            target_node_id=CHILD,
            message='same again',
            origin='node',
            relation='parent',
            source_node_id=ROOT,
        )
        first = await service.deliver_node_message(**kwargs)
        second = await service.deliver_node_message(**kwargs)
        assert first['status'] == 'delivered'
        assert second['status'] == 'deduplicated'
        assert second['message_id'] == first['message_id']
        assert len(service.store.list_task_node_notifications(TASK, CHILD)) == 1

    @pytest.mark.asyncio
    async def test_terminal_target_is_rejected_with_reason(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service)
        service.store.update_node(
            CHILD, lambda record: record.model_copy(update={'status': 'success'})
        )

        result = await service.deliver_node_message(
            task_id=TASK, target_node_id=CHILD, message='x', origin='node', relation='parent'
        )
        assert result['status'] == 'rejected'
        assert result['reason'] == 'target_terminal'
        assert service.store.list_task_node_notifications(TASK, CHILD) == []

    @pytest.mark.asyncio
    async def test_paused_target_keeps_human_text_as_unconsumed_mail(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service, phase='before_model')
        calls = _stub_wake(monkeypatch, service)
        service.store.update_node(
            CHILD, lambda record: record.model_copy(update={'is_paused': True})
        )

        result = await service.deliver_node_message(
            task_id=TASK, target_node_id=CHILD, message='are you ok', origin='user'
        )

        assert result['status'] == 'delivered'
        assert result['wake'] == ''
        assert calls == []
        stored = service.store.list_task_node_notifications(TASK, CHILD)
        assert len(stored) == 1
        assert stored[0].status == 'delivered'
        assert stored[0].kind == 'user_direct'
        # 免唤醒存在的理由就是这一位：叫醒路径会顺手清掉操作员按下的暂停。
        assert service.store.get_node(CHILD).is_paused is True

    @pytest.mark.asyncio
    async def test_resending_to_a_paused_target_stays_unwoken(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service, phase='before_model')
        calls = _stub_wake(monkeypatch, service)
        service.store.update_node(
            CHILD, lambda record: record.model_copy(update={'is_paused': True})
        )
        kwargs = dict(task_id=TASK, target_node_id=CHILD, message='are you ok', origin='user')

        first = await service.deliver_node_message(**kwargs)
        second = await service.deliver_node_message(**kwargs)

        assert first['status'] == 'delivered'
        assert second['status'] == 'deduplicated'
        assert calls == []

    @pytest.mark.asyncio
    async def test_paused_target_accepts_a_machine_message(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service, phase='before_model')
        calls = _stub_wake(monkeypatch, service)
        service.store.update_node(
            CHILD, lambda record: record.model_copy(update={'is_paused': True})
        )

        result = await service.deliver_node_message(
            task_id=TASK,
            target_node_id=CHILD,
            message='here is the fix',
            origin='node',
            relation='parent',
        )
        assert result['status'] == 'delivered'
        assert calls == [(TASK, CHILD)]

    @pytest.mark.asyncio
    async def test_wake_failure_does_not_undo_the_delivery(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service, throw=True)

        result = await service.deliver_node_message(
            task_id=TASK,
            target_node_id=CHILD,
            message='wake will fail',
            origin='node',
            relation='parent',
        )
        assert result['status'] == 'delivered'
        assert str(result['wake']).startswith('wake_error:')
        assert len(service.store.list_task_node_notifications(TASK, CHILD)) == 1


class TestAddressingRejectionArms:
    """寻址层必须把"改地址"和"改形状"分开报——实盘那 24 条全靠这一格指认原因。"""

    @pytest.mark.asyncio
    async def test_token_naming_no_node_is_the_address_arm(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service)

        result = await service._deliver_node_message_from_node(
            task_id=TASK,
            sender_node_id=CHILD,
            sender_is_root=False,
            to='node:nobody-here',
            content='hi',
        )
        rejection = result['results'][0]
        assert rejection['status'] == 'rejected'
        assert rejection['reason'] == 'target_not_in_task'
        assert 'hint' not in rejection
        assert ROOT in result['legal_targets']

    @pytest.mark.asyncio
    async def test_container_text_that_did_not_parse_is_the_shape_arm(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service)

        result = await service._deliver_node_message_from_node(
            task_id=TASK,
            sender_node_id=CHILD,
            sender_is_root=False,
            to='["node:root"',
            content='hi',
        )
        rejection = result['results'][0]
        assert rejection['reason'] == 'target_list_unparsed'
        assert 'JSON list of node ids' in rejection['hint']

    @pytest.mark.asyncio
    async def test_repaired_list_shape_delivers_to_every_target(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(monkeypatch, service)

        result = await service._deliver_node_message_from_node(
            task_id=TASK,
            sender_node_id=ROOT,
            sender_is_root=True,
            to=[CHILD],
            content='hi',
        )
        assert result['ok'] is True
        assert result['results'][0]['status'] == 'delivered'
