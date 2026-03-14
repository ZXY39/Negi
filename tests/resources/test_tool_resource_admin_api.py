from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from loguru import logger

from g3ku.resources import ResourceManager
from g3ku.runtime.engine import AgentRuntimeEngine
from main.api import admin_rest
from main.governance.models import ToolActionRecord, ToolFamilyRecord
from main.service.runtime_service import MainRuntimeService

REPO_ROOT = Path(__file__).resolve().parents[2]


def _resource_app_config() -> SimpleNamespace:
    return SimpleNamespace(
        resources=SimpleNamespace(
            enabled=True,
            skills_dir='skills',
            tools_dir='tools',
            manifest_name='resource.yaml',
            state_path='.g3ku/resources.state.json',
            reload=SimpleNamespace(
                enabled=True,
                poll_interval_ms=200,
                debounce_ms=100,
                lazy_reload_on_access=True,
                keep_last_good_version=True,
            ),
            locks=SimpleNamespace(
                lock_dir='.g3ku/resource-locks',
                logical_delete_guard=True,
                windows_fs_lock=True,
            ),
        )
    )


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f'chat backend should not be used in this test: {kwargs!r}')


def _build_app(service) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    admin_rest.get_agent = lambda: SimpleNamespace(main_task_service=service)
    return app


@pytest.mark.asyncio
async def test_main_runtime_service_reads_toolskill_from_primary_executor(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        resource_manager=manager,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
    )

    try:
        await service.startup()
        payload = service.get_tool_toolskill('exec_runtime')
        assert payload is not None
        assert payload['tool_id'] == 'exec_runtime'
        assert payload['primary_executor_name'] == 'exec'
        assert '# exec' in payload['content']
    finally:
        await service.close()
        manager.close()


def test_admin_toolskill_endpoint_returns_content():
    family = ToolFamilyRecord(
        tool_id='exec_runtime',
        display_name='Exec Runtime',
        description='Execute shell commands.',
        primary_executor_name='exec',
        enabled=True,
        available=True,
        source_path='tools/exec',
        actions=[ToolActionRecord(action_id='run', label='Run Command', allowed_roles=['ceo', 'execution'])],
        metadata={},
    )

    class _StubService:
        async def startup(self) -> None:
            return None

        def get_tool_family(self, tool_id: str):
            return family if tool_id == family.tool_id else None

        def get_tool_toolskill(self, tool_id: str):
            if tool_id != family.tool_id:
                return None
            return {
                'tool_id': tool_id,
                'primary_executor_name': 'exec',
                'content': '# exec\n\nUsage guide',
            }

    client = TestClient(_build_app(_StubService()))
    response = client.get('/api/resources/tools/exec_runtime/toolskill')

    assert response.status_code == 200
    assert response.json() == {
        'ok': True,
        'tool_id': 'exec_runtime',
        'primary_executor_name': 'exec',
        'content': '# exec\n\nUsage guide',
    }


def test_admin_tool_policy_endpoint_accepts_actions_mapping():
    captured: dict[str, object] = {}
    family = ToolFamilyRecord(
        tool_id='filesystem',
        display_name='Filesystem',
        description='Read, list, write, edit, delete workspace files, and create reviewable patch artifacts.',
        primary_executor_name='filesystem',
        enabled=True,
        available=True,
        source_path='tools/filesystem',
        actions=[ToolActionRecord(action_id='read', label='Read File', allowed_roles=['ceo'])],
        metadata={},
    )

    class _StubService:
        async def startup(self) -> None:
            return None

        def update_tool_policy(self, tool_id: str, *, session_id: str, enabled=None, allowed_roles_by_action=None):
            captured['tool_id'] = tool_id
            captured['session_id'] = session_id
            captured['enabled'] = enabled
            captured['allowed_roles_by_action'] = allowed_roles_by_action
            return family.model_copy(
                update={
                    'enabled': bool(enabled),
                    'actions': [
                        family.actions[0].model_copy(
                            update={'allowed_roles': list((allowed_roles_by_action or {}).get('read') or [])}
                        )
                    ],
                }
            )

    client = TestClient(_build_app(_StubService()))
    response = client.put(
        '/api/resources/tools/filesystem/policy',
        params={'session_id': 'web:shared'},
        json={'enabled': True, 'actions': {'read': ['ceo', 'inspection']}},
    )

    assert response.status_code == 200
    assert captured == {
        'tool_id': 'filesystem',
        'session_id': 'web:shared',
        'enabled': True,
        'allowed_roles_by_action': {'read': ['ceo', 'inspection']},
    }
    assert response.json()['item']['actions'][0]['allowed_roles'] == ['ceo', 'inspection']


def test_main_runtime_service_filters_visible_actions_for_shared_executor():
    family = ToolFamilyRecord(
        tool_id='filesystem',
        display_name='Filesystem',
        description='Workspace file operations.',
        primary_executor_name='filesystem',
        enabled=True,
        available=True,
        source_path='tools/filesystem',
        actions=[
            ToolActionRecord(action_id='read', label='Read File', allowed_roles=['ceo', 'execution', 'inspection'], executor_names=['filesystem']),
            ToolActionRecord(action_id='write', label='Write File', allowed_roles=['ceo', 'execution'], executor_names=['filesystem']),
        ],
        metadata={},
    )

    class _Registry:
        def list_tool_families(self):
            return [family]

    class _PolicyEngine:
        def evaluate_tool_action(self, *, subject, tool_id: str, action_id: str):
            _ = subject, tool_id
            return SimpleNamespace(allowed=action_id == 'read')

    service = object.__new__(MainRuntimeService)
    service.resource_registry = _Registry()
    service.policy_engine = _PolicyEngine()
    service.list_effective_tool_names = lambda **kwargs: ['filesystem']
    service._subject = lambda **kwargs: SimpleNamespace(**kwargs)

    visible = service.list_visible_tool_families(actor_role='inspection', session_id='web:shared')

    assert len(visible) == 1
    assert [action.action_id for action in visible[0].actions] == ['read']


def test_agent_runtime_engine_ignores_mcp_servers(tmp_path: Path):
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record['message']), level='WARNING')
    try:
        engine = AgentRuntimeEngine(
            bus=None,
            provider=None,
            workspace=tmp_path,
            mcp_servers={'demo': {'url': 'https://example.com'}},
        )
    finally:
        logger.remove(sink_id)

    assert engine.mcp_servers == {}
    assert any('tools.mcp_servers is deprecated and ignored' in message for message in messages)
