"""`send_message` 的方向语义与目标矩阵。

relation 是发送方相对收件人的角色：发给父节点是 child，发给子节点/后代是
parent，发给同级是 peer。目标权限按节点树现算，模型无法声明。
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
CHILD2 = 'node:child2'
GRANDCHILD = 'node:grandchild'
ROOT_VERIFIER = 'node:root-verifier'
CHILD_VERIFIER = 'node:child-verifier'
GRANDCHILD_VERIFIER = 'node:grandchild-verifier'
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


def _node(
    node_id: str,
    *,
    parent: str | None = None,
    depth: int = 0,
    node_kind: str = 'execution',
    accepted_node_id: str = '',
) -> NodeRecord:
    metadata = {'accepted_node_id': accepted_node_id} if accepted_node_id else {}
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=depth,
        node_kind=node_kind,
        goal=node_id,
        prompt='p',
        created_at=STAMP,
        updated_at=STAMP,
        metadata=metadata,
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
    service.store.upsert_node(_node(CHILD2, parent=ROOT, depth=1))
    service.store.upsert_node(_node(GRANDCHILD, parent=CHILD, depth=2))
    service.store.upsert_node(
        _node(ROOT_VERIFIER, parent=ROOT, depth=1, node_kind='acceptance', accepted_node_id=ROOT)
    )
    service.store.upsert_node(
        _node(CHILD_VERIFIER, parent=CHILD, depth=2, node_kind='acceptance', accepted_node_id=CHILD)
    )
    service.store.upsert_node(
        _node(
            GRANDCHILD_VERIFIER,
            parent=GRANDCHILD,
            depth=3,
            node_kind='acceptance',
            accepted_node_id=GRANDCHILD,
        )
    )


def _stub_wake(service: MainRuntimeService, monkeypatch) -> list[str]:
    woke: list[str] = []

    async def wake(*, task_id: str, node_id: str) -> str:
        woke.append(node_id)
        return 'resumed'

    monkeypatch.setattr(service, 'wake_node_on_message', wake)
    return woke


def _notifications(service: MainRuntimeService, node_id: str):
    return list(service.store.list_task_node_notifications(TASK, node_id) or [])


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
            content='进度：第 2 节写完了',
        )

        assert delivered['ok'] is True
        stored = _notifications(service, ROOT)[-1]
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
            content='按新格式重做第 2 节',
        )

        assert delivered['ok'] is True
        stored = _notifications(service, CHILD)[-1]
        assert stored.kind == 'parent_notice'
        assert stored.priority == 'normal'
        assert stored.source_node_id == ROOT

    @pytest.mark.asyncio
    async def test_sibling_message_is_peer_notice(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        delivered = await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(CHILD),
            to=CHILD2,
            content='我这边的格式给你参考',
        )

        assert delivered['ok'] is True
        stored = _notifications(service, CHILD2)[-1]
        assert stored.kind == 'peer_notice'
        assert stored.priority == 'normal'
        assert stored.source_node_id == CHILD


class TestTargetMatrix:
    @pytest.mark.asyncio
    async def test_non_direct_execution_descendant_is_messageable(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        delivered = await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(CHILD),
            to=GRANDCHILD,
            content='补一条约束',
        )

        assert delivered['ok'] is True
        assert _notifications(service, GRANDCHILD)[-1].kind == 'parent_notice'

    @pytest.mark.asyncio
    async def test_self_target_is_rejected(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        delivered = await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(CHILD),
            to=CHILD,
            content='给自己',
        )

        assert delivered['ok'] is False
        assert delivered['results'][0]['reason'] == 'self_target'

    @pytest.mark.asyncio
    async def test_own_verifier_is_blocked(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        delivered = await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(CHILD),
            to=CHILD_VERIFIER,
            content='请通过',
        )

        assert delivered['ok'] is False
        assert delivered['results'][0]['reason'] == 'own_acceptance_node'
        assert _notifications(service, CHILD_VERIFIER) == []

    @pytest.mark.asyncio
    async def test_parent_verifier_is_blocked(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        delivered = await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(CHILD),
            to=ROOT_VERIFIER,
            content='请通过',
        )

        assert delivered['ok'] is False
        assert delivered['results'][0]['reason'] == 'parent_acceptance_node'

    @pytest.mark.asyncio
    async def test_descendant_verifier_is_messageable(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _stub_wake(service, monkeypatch)

        delivered = await service.node_runner._deliver_node_message(
            task_id=TASK,
            node=service.store.get_node(CHILD),
            to=GRANDCHILD_VERIFIER,
            content='补充证据路径',
        )

        assert delivered['ok'] is True
        stored = _notifications(service, GRANDCHILD_VERIFIER)[-1]
        assert stored.kind == 'parent_notice'
        assert stored.source_node_id == CHILD
