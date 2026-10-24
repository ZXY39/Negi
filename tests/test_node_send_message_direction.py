"""`send_message` 的方向语义：relation 是发送方相对收件人的角色。

实盘 bug（2026-10-24）：发给父节点时传 relation='parent'，分类器把 'parent'
读成"父→子"，于是子→父的上行消息被标成 parent_notice，父→子的下行被标成
child_progress。优先级不受影响（只看 intent_kind），所以没有任何报错——只有
取证和前端的方向是反的。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.service.runtime_service import MainRuntimeService

TASK = 'task:send-direction'
ROOT = 'node:root'
CHILD = 'node:child'
GRANDCHILD = 'node:grandchild'
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


def _node(node_id: str, *, parent: str | None = None, depth: int = 0) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
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
    service.store.upsert_node(_node(ROOT))
    service.store.upsert_node(_node(CHILD, parent=ROOT, depth=1))
    service.store.upsert_node(_node(GRANDCHILD, parent=CHILD, depth=2))


def _stub_wake(service: MainRuntimeService, monkeypatch) -> list[str]:
    woke: list[str] = []

    async def wake(*, task_id: str, node_id: str) -> str:
        woke.append(node_id)
        return 'resumed'

    monkeypatch.setattr(service, 'wake_node_on_message', wake)
    return woke


def _only_notification(service: MainRuntimeService, node_id: str):
    rows = list(service.store.list_task_node_notifications(TASK, node_id) or [])
    assert len(rows) == 1
    return rows[0]


class TestSenderRelativeRelation:
    @pytest.mark.asyncio
    async def test_child_to_parent_is_child_progress(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        delivered = await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(CHILD),
            to='parent',
            intent_kind='进展',
            content='进度：第 2 节写完了',
        )

        assert delivered['ok'] is True
        stored = _only_notification(service, ROOT)
        assert stored.kind == 'child_progress'
        assert stored.priority == 'normal'
        assert stored.source_node_id == CHILD

    @pytest.mark.asyncio
    async def test_parent_to_child_is_parent_notice(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        delivered = await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(ROOT),
            to=CHILD,
            intent_kind='指令',
            content='按新格式重做第 2 节',
        )

        assert delivered['ok'] is True
        stored = _only_notification(service, CHILD)
        assert stored.kind == 'parent_notice'
        assert stored.priority == 'normal'
        assert stored.source_node_id == ROOT

    @pytest.mark.asyncio
    async def test_request_stays_high_in_both_directions(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(CHILD),
            to='parent',
            intent_kind='请求',
            content='请确认验收口径',
        )
        await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(ROOT),
            to=CHILD,
            intent_kind='请求',
            content='把第 1 节的原始数据发我',
        )

        up = _only_notification(service, ROOT)
        down = _only_notification(service, CHILD)
        assert (up.kind, up.priority) == ('child_progress', 'high')
        assert (down.kind, down.priority) == ('parent_notice', 'high')
