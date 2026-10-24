"""`manage_child_nodes`: direct-child scope, pause/resume/terminate semantics, tool surface."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from main.models import NodeRecord, TaskRecord
from main.runtime.child_tools import ManageChildNodesTool
from main.runtime.node_runner import NodeRunner
from main.runtime.tool_names import MANAGE_CHILD_NODES_TOOL_NAME
from main.service.runtime_service import MainRuntimeService

TASK = 'task:manage-children'
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


def _node(node_id: str, *, parent: str | None = None) -> NodeRecord:
    return NodeRecord(
        node_id=node_id,
        task_id=TASK,
        parent_node_id=parent,
        root_node_id=ROOT,
        depth=0 if parent is None else 1,
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
    service.store.upsert_node(_node(CHILD, parent=ROOT))
    service.store.upsert_node(_node(GRANDCHILD, parent=CHILD))


def _capture_wakes(service: MainRuntimeService, monkeypatch) -> list[str]:
    calls: list[str] = []

    async def wake(*, task_id: str, node_id: str) -> str:
        calls.append(node_id)
        return 'resumed'

    monkeypatch.setattr(service, 'wake_node_on_message', wake)
    return calls


async def _manage(service, ops):
    return await service._manage_child_nodes_from_node(
        task_id=TASK,
        sender_node_id=ROOT,
        ops=ops,
    )


class TestToolSurface:
    def test_name_and_required(self):
        tool = ManageChildNodesTool(_noop)
        assert tool.name == MANAGE_CHILD_NODES_TOOL_NAME
        assert tool.parameters['required'] == ['ops']
        assert tool.hide_universal_timeout_parameter is True

    def test_action_enum(self):
        schema = ManageChildNodesTool(_noop).parameters['properties']['ops']['items']
        assert schema['properties']['action']['enum'] == ['pause', 'resume', 'terminate']

    @pytest.mark.asyncio
    async def test_terminate_without_reason_rejects_the_whole_batch(self):
        seen: list[dict] = []

        async def manage(**payload):
            seen.append(payload)
            return {'ok': True, 'items': []}

        raw = await ManageChildNodesTool(manage).execute(
            ops=[
                {'node_id': 'node:a', 'action': 'pause'},
                {'node_id': 'node:b', 'action': 'terminate'},
            ]
        )
        payload = json.loads(raw)
        assert payload['error'] == 'invalid_param'
        assert 'reason' in payload['details']
        # 参数不合格的批次一条都不下发，避免半批副作用。
        assert seen == []

    @pytest.mark.asyncio
    async def test_forwards_valid_batch(self):
        seen: list[dict] = []

        async def manage(**payload):
            seen.append(payload)
            return {'ok': True, 'items': [{'node_id': 'node:a', 'result': 'paused'}], 'legal_targets': ['node:a']}

        raw = await ManageChildNodesTool(manage).execute(
            ops=[{'node_id': 'node:a', 'action': 'terminate', 'reason': '放弃该分支'}]
        )
        assert seen == [{'ops': [{'node_id': 'node:a', 'action': 'terminate', 'reason': '放弃该分支'}]}]
        assert json.loads(raw)['ok'] is True


class TestScope:
    @pytest.mark.asyncio
    async def test_grandchild_is_not_addressable(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)

        result = await _manage(service, [{'node_id': GRANDCHILD, 'action': 'pause'}])
        assert result['items'][0]['reason'] == 'not_a_direct_child'
        assert result['legal_targets'] == [CHILD]
        assert result['ok'] is False

    @pytest.mark.asyncio
    async def test_unknown_sender_is_refused(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)
        result = await service._manage_child_nodes_from_node(
            task_id=TASK, sender_node_id='node:ghost', ops=[{'node_id': CHILD, 'action': 'pause'}]
        )
        assert result['error'] == 'sender_missing'
        assert result['items'] == []


class TestPause:
    @pytest.mark.asyncio
    async def test_pause_sets_cooperative_flag(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)

        result = await _manage(service, [{'node_id': CHILD, 'action': 'pause', 'note': '先停一下'}])
        assert result['items'][0]['result'] == 'paused'
        child = service.store.get_node(CHILD)
        assert child.pause_requested is True
        assert child.is_paused is False
        assert child.pause_reason == 'agent'

    @pytest.mark.asyncio
    async def test_pause_is_idempotent(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)

        await _manage(service, [{'node_id': CHILD, 'action': 'pause'}])
        second = await _manage(service, [{'node_id': CHILD, 'action': 'pause'}])
        assert second['items'][0] == {
            'node_id': CHILD,
            'action': 'pause',
            'result': 'paused',
            'reason': 'already_paused',
        }
        assert second['ok'] is True


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_writes_a_parent_notice_and_wakes(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        wakes = _capture_wakes(service, monkeypatch)
        await _manage(service, [{'node_id': CHILD, 'action': 'pause'}])

        result = await _manage(
            service,
            [{'node_id': CHILD, 'action': 'resume', 'note': '按新格式重做第 2 节'}],
        )
        assert result['items'][0]['result'] == 'resumed'
        assert result['items'][0]['delivery'] == 'delivered'
        assert wakes == [CHILD]
        stored = service.store.list_task_node_notifications(TASK, CHILD)
        assert len(stored) == 1
        assert stored[0].kind == 'parent_notice'
        assert stored[0].source_node_id == ROOT
        assert '重做第 2 节' in stored[0].message

    @pytest.mark.asyncio
    async def test_resume_of_waiting_node_is_refused_with_the_reason(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)
        service.log_service.update_frame(
            TASK, CHILD, lambda frame: {**frame, 'phase': 'waiting_explicit'}
        )

        result = await _manage(service, [{'node_id': CHILD, 'action': 'resume', 'note': 'x'}])
        item = result['items'][0]
        assert item['result'] == 'rejected'
        assert item['reason'] == 'node_waiting'
        assert 'terminate' in item['hint']

    @pytest.mark.asyncio
    async def test_resume_of_running_node_is_refused(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)
        result = await _manage(service, [{'node_id': CHILD, 'action': 'resume'}])
        assert result['items'][0]['reason'] == 'node_not_paused'


class TestTerminate:
    @pytest.mark.asyncio
    async def test_terminate_any_non_terminal_node(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)

        result = await _manage(
            service, [{'node_id': CHILD, 'action': 'terminate', 'reason': '分支舍弃'}]
        )
        assert result['items'][0]['result'] == 'terminated'
        child = service.store.get_node(CHILD)
        assert child.status == 'failed'
        assert '分支舍弃' in (child.failure_reason or '')

    @pytest.mark.asyncio
    async def test_terminate_without_reason_is_refused_at_the_service_layer(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)
        result = await _manage(service, [{'node_id': CHILD, 'action': 'terminate'}])
        assert result['items'][0]['reason'] == 'reason_required'
        assert service.store.get_node(CHILD).status == 'in_progress'

    @pytest.mark.asyncio
    async def test_terminal_node_rejects_every_action(self, tmp_path, monkeypatch):
        service = _service(tmp_path)
        _seed(service)
        _capture_wakes(service, monkeypatch)
        service.store.update_node(CHILD, lambda record: record.model_copy(update={'status': 'success'}))

        result = await _manage(service, [{'node_id': CHILD, 'action': 'terminate', 'reason': 'x'}])
        assert result['items'][0]['reason'] == 'node_terminal'
        assert service.store.get_node(CHILD).status == 'success'


class TestRunnerForwarding:
    @pytest.mark.asyncio
    async def test_runner_filters_non_children_before_the_service(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        calls: list[dict] = []

        async def manage(**payload):
            calls.append(payload)
            return {'items': [{'node_id': CHILD, 'action': 'pause', 'result': 'paused'}]}

        runner = NodeRunner.__new__(NodeRunner)
        runner._store = service.store
        runner.manage_child_nodes_callback = manage
        root = service.store.get_node(ROOT)

        result = await runner._manage_child_nodes(
            task_id=TASK,
            node=root,
            ops=[
                {'node_id': GRANDCHILD, 'action': 'pause'},
                {'node_id': CHILD, 'action': 'pause'},
            ],
        )
        assert calls == [
            {
                'task_id': TASK,
                'sender_node_id': ROOT,
                'ops': [{'node_id': CHILD, 'action': 'pause'}],
            }
        ]
        assert result['legal_targets'] == [CHILD]
        assert [item['result'] for item in result['items']] == ['rejected', 'paused']
        assert result['ok'] is False

    @pytest.mark.asyncio
    async def test_missing_callback_reports_instead_of_silently_succeeding(self, tmp_path):
        service = _service(tmp_path)
        _seed(service)
        runner = NodeRunner.__new__(NodeRunner)
        runner._store = service.store
        result = await runner._manage_child_nodes(
            task_id=TASK, node=service.store.get_node(ROOT), ops=[{'node_id': CHILD, 'action': 'pause'}]
        )
        assert result['error'] == 'manage_child_nodes_unavailable'


async def _noop(**payload):
    return {'ok': True, 'items': [], 'legal_targets': [], 'payload': payload}
