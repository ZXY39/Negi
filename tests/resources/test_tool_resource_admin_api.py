from __future__ import annotations

import shutil
from pathlib import Path
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.resources import ResourceManager
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


def test_load_config_rejects_legacy_tools_config(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / '.g3ku').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools' / 'memory_runtime').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools' / 'memory_runtime' / 'resource.yaml').write_text(
        'schema_version: 1\nkind: tool\nname: memory_runtime\nsettings:\n  enabled: false\n',
        encoding='utf-8',
    )
    (workspace / '.g3ku' / 'config.json').write_text(
        json.dumps({
            'agents': {'defaults': {'workspace': '.', 'runtime': 'langgraph', 'maxTokens': 1, 'temperature': 0.1, 'maxToolIterations': 1, 'memoryWindow': 1, 'reasoningEffort': 'low'}, 'multiAgent': {'orchestratorModelKey': None}},
            'models': {'catalog': [{'key': 'm', 'providerModel': 'openai:gpt-4.1', 'apiKey': '', 'apiBase': None, 'extraHeaders': None, 'enabled': True, 'maxTokens': 1, 'temperature': 0.1, 'reasoningEffort': 'low', 'retryOn': [], 'description': ''}], 'roles': {'ceo': ['m'], 'execution': ['m'], 'inspection': ['m']}},
            'providers': {},
            'gateway': {'host': '127.0.0.1', 'port': 1, 'heartbeat': {'enabled': True, 'intervalS': 1}},
            'tools': {'exec': {'timeout': 10}},
            'resources': {'enabled': True, 'skillsDir': 'skills', 'toolsDir': 'tools', 'manifestName': 'resource.yaml', 'reload': {'enabled': True, 'pollIntervalMs': 1000, 'debounceMs': 400, 'lazyReloadOnAccess': True, 'keepLastGoodVersion': True}, 'locks': {'lockDir': '.g3ku/resource-locks', 'logicalDeleteGuard': True, 'windowsFsLock': True}, 'statePath': '.g3ku/resources.state.json'},
            'mainRuntime': {'enabled': True, 'storePath': '.g3ku/main-runtime/runtime.sqlite3', 'filesBaseDir': '.g3ku/main-runtime/tasks', 'artifactDir': '.g3ku/main-runtime/artifacts', 'governanceStorePath': '.g3ku/main-runtime/governance.sqlite3', 'defaultMaxDepth': 1, 'hardMaxDepth': 4},
        }),
        encoding='utf-8',
    )

    monkeypatch.chdir(workspace)

    from g3ku.config.loader import load_config

    with pytest.raises(ValueError, match='config.tools has been removed'):
        load_config()


def test_admin_memory_trace_endpoints_return_payload():
    class _StubService:
        async def startup(self) -> None:
            return None

        async def get_context_traces(self, *, trace_kind: str, limit: int = 20):
            return {'ok': True, 'items': [{'trace_kind': trace_kind, 'limit': limit}], 'trace_kind': trace_kind, 'limit': limit}

    client = TestClient(_build_app(_StubService()))

    retrieval = client.get('/api/memory/retrieval-traces?limit=3')
    assert retrieval.status_code == 200
    assert retrieval.json()['items'][0]['trace_kind'] == 'retrieval'

    assembly = client.get('/api/memory/context-assembly-traces?limit=2')
    assert assembly.status_code == 200
    assert assembly.json()['items'][0]['trace_kind'] == 'context_assembly'


def _write_runtime_config(workspace: Path) -> None:
    (workspace / '.g3ku').mkdir(parents=True, exist_ok=True)
    (workspace / '.g3ku' / 'config.json').write_text(
        json.dumps({
            'agents': {'defaults': {'workspace': '.', 'runtime': 'langgraph', 'maxTokens': 1, 'temperature': 0.1, 'maxToolIterations': 1, 'memoryWindow': 1, 'reasoningEffort': 'low'}, 'multiAgent': {'orchestratorModelKey': None}},
            'models': {'catalog': [{'key': 'm', 'providerModel': 'openai:gpt-4.1', 'apiKey': 'demo-key', 'apiBase': None, 'extraHeaders': None, 'enabled': True, 'maxTokens': 1, 'temperature': 0.1, 'reasoningEffort': 'low', 'retryOn': [], 'description': ''}], 'roles': {'ceo': ['m'], 'execution': ['m'], 'inspection': ['m']}},
            'providers': {'openai': {'apiKey': '', 'apiBase': None, 'extraHeaders': None}},
            'gateway': {'host': '127.0.0.1', 'port': 1, 'heartbeat': {'enabled': True, 'intervalS': 1}},
            'toolSecrets': {},
            'resources': {'enabled': True, 'skillsDir': 'skills', 'toolsDir': 'tools', 'manifestName': 'resource.yaml', 'reload': {'enabled': True, 'pollIntervalMs': 1000, 'debounceMs': 400, 'lazyReloadOnAccess': True, 'keepLastGoodVersion': True}, 'locks': {'lockDir': '.g3ku/resource-locks', 'logicalDeleteGuard': True, 'windowsFsLock': True}, 'statePath': '.g3ku/resources.state.json'},
            'mainRuntime': {'enabled': True, 'storePath': '.g3ku/main-runtime/runtime.sqlite3', 'filesBaseDir': '.g3ku/main-runtime/tasks', 'artifactDir': '.g3ku/main-runtime/artifacts', 'governanceStorePath': '.g3ku/main-runtime/governance.sqlite3', 'defaultMaxDepth': 1, 'hardMaxDepth': 4},
            'chinaBridge': {
                'enabled': False,
                'bindHost': '0.0.0.0',
                'publicPort': 18889,
                'controlHost': '127.0.0.1',
                'controlPort': 18989,
                'controlToken': '',
                'autoStart': True,
                'nodeBin': 'node',
                'npmClient': 'pnpm',
                'stateDir': '.g3ku/china-bridge',
                'logLevel': 'info',
                'sendProgress': True,
                'sendToolHints': False,
                'channels': {
                    'qqbot': {'enabled': False, 'accounts': {}},
                    'dingtalk': {'enabled': False, 'accounts': {}},
                    'wecom': {'enabled': False, 'accounts': {}},
                    'wecomApp': {'enabled': False, 'accounts': {}},
                    'feishuChina': {'enabled': False, 'accounts': {}},
                },
            },
        }),
        encoding='utf-8',
    )


def test_china_bridge_channels_endpoint_lists_supported_channels(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.get('/api/china-bridge/channels')

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert [item['id'] for item in payload['items']] == ['qqbot', 'dingtalk', 'wecom', 'wecomApp', 'feishuChina']


def test_china_bridge_channel_save_updates_config_file(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/china-bridge/channels/qqbot',
        json={'enabled': True, 'config': {'appId': '123456', 'clientSecret': 'demo-secret', 'accounts': {'default': {'token': 'demo-token'}}}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['item']['enabled'] is True
    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['chinaBridge']['enabled'] is True
    assert saved['chinaBridge']['channels']['qqbot']['enabled'] is True
    assert saved['chinaBridge']['channels']['qqbot']['appId'] == '123456'


def test_china_bridge_channel_test_reports_disabled_or_validated(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    disabled = client.post('/api/china-bridge/channels/qqbot/test')
    assert disabled.status_code == 200
    assert disabled.json()['result']['status'] == 'disabled'

    client.put(
        '/api/china-bridge/channels/qqbot',
        json={'enabled': True, 'config': {'appId': '123456', 'clientSecret': 'demo-secret'}},
    )
    validated = client.post('/api/china-bridge/channels/qqbot/test')

    assert validated.status_code == 200
    assert validated.json()['result']['status'] in {'success', 'warning'}


@pytest.mark.asyncio
async def test_write_skill_file_async_triggers_targeted_catalog_sync(tmp_path: Path):
    skill_file = tmp_path / 'demo' / 'SKILL.md'
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text('before', encoding='utf-8')

    captured: dict[str, object] = {}

    class _Registry:
        def skill_file_map(self, skill_id: str):
            assert skill_id == 'demo_skill'
            return {'skill_doc': skill_file}

    class _MemoryManager:
        async def sync_catalog(self, service, *, skill_ids=None, tool_ids=None):
            captured['skill_ids'] = set(skill_ids or set())
            captured['tool_ids'] = set(tool_ids or set())
            return {'created': 0, 'updated': 1, 'removed': 0}

    service = object.__new__(MainRuntimeService)
    service.resource_registry = _Registry()
    service.memory_manager = _MemoryManager()
    service.reload_resources = lambda **kwargs: {'ok': True}

    item = await service.write_skill_file_async('demo_skill', 'skill_doc', 'after', session_id='web:shared')

    assert skill_file.read_text(encoding='utf-8') == 'after'
    assert captured == {'skill_ids': {'demo_skill'}, 'tool_ids': set()}
    assert item['catalog_synced'] is True
    assert item['catalog']['updated'] == 1
