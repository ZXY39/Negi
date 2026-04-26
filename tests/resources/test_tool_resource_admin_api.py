from __future__ import annotations

import inspect
import json
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import g3ku.runtime.web_ceo_sessions as web_ceo_sessions
from g3ku.china_bridge.registry import china_channel_template
from g3ku.config.loader import ensure_startup_config_ready
import g3ku.config.model_manager as model_manager
from g3ku.llm_config.enums import ProbeStatus
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.resources import ResourceManager
from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport
from g3ku.security import get_bootstrap_security_service
from g3ku.runtime.session_agent import RuntimeAgentSession
from g3ku.session.manager import Session
from main.api import admin_rest
from main.governance.models import ToolActionRecord, ToolFamilyRecord
from main.governance.resource_filter import list_effective_tool_names
from main.models import TaskRecord
from main.protocol import now_iso
from main.service.runtime_service import CreateAsyncTaskTool, MainRuntimeService, TaskNodeDetailTool

REPO_ROOT = Path(__file__).resolve().parents[2]


async def _noop_enqueue_task(_task_id: str) -> None:
    return None


def _mock_ceo_catalog_config(monkeypatch) -> None:
    monkeypatch.setattr(
        web_ceo_sessions,
        "load_config",
        lambda: SimpleNamespace(
            china_bridge=SimpleNamespace(
                channels=SimpleNamespace(
                    qqbot=SimpleNamespace(enabled=False, accounts={}),
                    dingtalk=SimpleNamespace(enabled=False, accounts={}),
                    wecom=SimpleNamespace(enabled=False, accounts={}),
                    wecom_app=SimpleNamespace(enabled=False, accounts={}),
                    feishu_china=SimpleNamespace(enabled=False, accounts={}),
                )
            )
        ),
    )


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


def _raise_no_model_configured():
    raise ValueError("No model configured for role 'ceo'.")


def _write_external_tool(workspace: Path, *, name: str = 'external_browser') -> None:
    root = workspace / 'tools' / name
    (root / 'toolskills').mkdir(parents=True, exist_ok=True)
    (workspace / '.g3ku' / 'external-tools' / name).mkdir(parents=True, exist_ok=True)
    (root / 'resource.yaml').write_text(
        f"""schema_version: 1
kind: tool
name: {name}
display_name: External Browser
description: Registered external browser automation tool.
tool_type: external
install_dir: .g3ku/external-tools/{name}
protocol: mcp
mcp:
  transport: embedded
requires:
  tools: []
  bins: []
  env: []
permissions:
  network: true
  filesystem:
    - workspace
parameters:
  type: object
  properties: {{}}
  required: []
exposure:
  agent: true
  main_runtime: true
governance:
  family: {name}
  display_name: External Browser
  description: Registered external browser automation tool.
  actions:
    - id: use
      label: Use External Browser
      risk_level: medium
      destructive: false
      allowed_roles:
        - ceo
        - execution
toolskill:
  enabled: true
""",
        encoding='utf-8',
    )
    (root / 'toolskills' / 'SKILL.md').write_text(
        '# External Browser\n\n## 何时使用\n\nNeed a browser.\n\n## 安装\n\nInstall outside tools.\n\n## 更新\n\nPull upstream.\n\n## 使用\n\nUse install_dir.\n',
        encoding='utf-8',
    )


def _write_skill(workspace: Path, *, name: str = 'demo_skill', content: str | None = None) -> None:
    root = workspace / 'skills' / name
    root.mkdir(parents=True, exist_ok=True)
    (root / 'resource.yaml').write_text(
        f"""schema_version: 1
kind: skill
name: {name}
description: Demo skill for admin delete tests.
trigger:
  keywords: []
  always: false
requires:
  tools: []
  bins: []
  env: []
content:
  main: SKILL.md
exposure:
  agent: true
  main_runtime: true
""",
        encoding='utf-8',
    )
    (root / 'SKILL.md').write_text(
        content if content is not None else '# Demo Skill\n\nThis skill is used by tests.\n',
        encoding='utf-8',
    )


def _copy_repo_tools(workspace: Path, *names: str) -> None:
    for name in names:
        shutil.copytree(REPO_ROOT / 'tools' / name, workspace / 'tools' / name)


def _running_task_record(
    *,
    task_id: str,
    session_id: str,
    title: str,
    created_at: str = '2026-03-17T00:00:00',
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        session_id=session_id,
        title=title,
        user_request=title,
        status='in_progress',
        root_node_id=f'node:{task_id}',
        max_depth=1,
        cancel_requested=False,
        pause_requested=False,
        is_paused=False,
        is_unread=True,
        brief_text='',
        created_at=created_at,
        updated_at=created_at,
        metadata={},
    )


def _frontdoor_stage_archive_task_id(session_id: str) -> str:
    return f"frontdoor-stage-archive:{str(session_id or '').strip()}"


def test_main_runtime_session_task_counts_distinguish_running_paused_and_deletable_records():
    tasks = [
        SimpleNamespace(task_id='task:done', status='success', is_paused=False),
        SimpleNamespace(task_id='task:paused', status='in_progress', is_paused=True),
        SimpleNamespace(task_id='task:busy', status='in_progress', is_paused=False),
    ]

    class _Service:
        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return tasks

    counts = MainRuntimeService.get_session_task_counts(_Service(), 'web:ceo-demo')

    assert counts == {
        'total': 3,
        'unfinished': 1,
        'in_progress': 1,
        'paused': 1,
        'terminal': 1,
        'deletable': 2,
    }


@pytest.mark.asyncio
async def test_main_runtime_delete_task_records_for_session_pauses_in_progress_records_before_deleting():
    actions: list[tuple[str, str]] = []
    tasks = [
        SimpleNamespace(task_id='task:done', status='success', is_paused=False),
        SimpleNamespace(task_id='task:paused', status='in_progress', is_paused=True),
        SimpleNamespace(task_id='task:busy', status='in_progress', is_paused=False),
    ]

    class _Service:
        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return tasks

        async def pause_task(self, task_id: str):
            actions.append(('pause', task_id))
            return None

        async def delete_task(self, task_id: str):
            actions.append(('delete', task_id))
            return object()

    deleted = await MainRuntimeService.delete_task_records_for_session(_Service(), 'web:ceo-demo')

    assert deleted == 3
    assert actions == [
        ('delete', 'task:done'),
        ('delete', 'task:paused'),
        ('pause', 'task:busy'),
        ('delete', 'task:busy'),
    ]


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
        assert payload['tool_type'] == 'internal'
        assert payload['install_dir'] is None
        assert payload['callable'] is True
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_load_tool_context_prefers_requested_executor_toolskill_over_family_primary(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'memory_note', 'memory_write', 'memory_runtime')

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
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-service-bind')
    service.bind_resource_manager(manager)

    try:
        await service.startup()

        toolskill = service.get_tool_toolskill('memory_write')
        assert toolskill is not None
        assert toolskill['tool_id'] == 'memory_write'
        assert toolskill['family_tool_id'] == 'memory'
        assert toolskill['toolskill_source_name'] == 'memory_write'
        assert toolskill['available'] is False
        assert toolskill['repair_required'] is True
        assert 'tool_handler_unavailable' in toolskill['errors']
        assert '# memory_write' in toolskill['content']
        assert toolskill['required_parameters'] == ['content']
        assert toolskill['parameters_schema']['properties']['content']['type'] == 'string'
        assert 'content' in str(toolskill['parameter_contract_markdown'])
        assert dict(toolskill['example_arguments']).get('content')

        seeded = service.update_tool_policy(
            'memory',
            session_id='web:shared',
            allowed_roles_by_action={'write': ['ceo']},
        )
        assert seeded is not None

        payload = service.load_tool_context(
            actor_role='ceo',
            session_id='web:shared',
            tool_id='memory_write',
        )
        assert payload['ok'] is True
        assert payload['tool_id'] == 'memory_write'
        assert payload['available'] is False
        assert '# memory_write' in payload['content']

        payload_v2 = service.load_tool_context_v2(
            actor_role='ceo',
            session_id='web:shared',
            tool_id='memory_write',
        )
        assert payload_v2['required_parameters'] == ['content']
        assert payload_v2['parameters_schema']['properties']['content']['type'] == 'string'
        assert 'content' in str(payload_v2['parameter_contract_markdown'])
        assert dict(payload_v2['example_arguments']).get('content')
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_load_tool_context_filesystem_edit_prefers_callable_schema_for_agent_docs(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'filesystem_edit')

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
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-service-bind')
    service.bind_resource_manager(manager)

    try:
        await service.startup()

        toolskill = service.get_tool_toolskill('filesystem_edit')
        assert toolskill is not None
        assert toolskill['required_parameters'] == ['path', 'target', 'new_text']
        assert toolskill['parameters_schema']['properties']['target']['properties']['by']['enum'] == [
            'exact_text',
            'anchor_pair',
            'line_range',
        ]
        assert dict(toolskill['example_arguments']).get('target', {}).get('by') == 'exact_text'

        payload_v2 = service.load_tool_context_v2(
            actor_role='ceo',
            session_id='web:shared',
            tool_id='filesystem_edit',
        )
        assert payload_v2['required_parameters'] == ['path', 'target', 'new_text']
        assert payload_v2['parameters_schema']['properties']['target']['properties']['by']['enum'] == [
            'exact_text',
            'anchor_pair',
            'line_range',
        ]
        assert dict(payload_v2['example_arguments']).get('target', {}).get('by') == 'exact_text'
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_load_tool_context_marks_result_delivery_contract_violation_as_repair_required(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)

    tool_root = workspace / 'tools' / 'demo_contract'
    (tool_root / 'main').mkdir(parents=True, exist_ok=True)
    (tool_root / 'toolskills').mkdir(parents=True, exist_ok=True)
    (tool_root / 'main' / 'tool.py').write_text(
        'from g3ku.agent.tools.base import Tool\n'
        'class DemoContractTool(Tool):\n'
        '    @property\n'
        '    def name(self):\n'
        '        return "demo_contract"\n'
        '    @property\n'
        '    def description(self):\n'
        '        return "demo contract tool"\n'
        '    @property\n'
        '    def parameters(self):\n'
        '        return {"type":"object","properties":{},"required":[]}\n'
        '    async def execute(self, **kwargs):\n'
        '        return "ok"\n'
        'def build(runtime):\n'
        '    return DemoContractTool()\n',
        encoding='utf-8',
    )
    (tool_root / 'toolskills' / 'SKILL.md').write_text('# demo_contract\n\nRepair me.\n', encoding='utf-8')
    (tool_root / 'resource.yaml').write_text(
        yaml.safe_dump(
                {
                    'schema_version': 1,
                    'kind': 'tool',
                    'name': 'demo_contract',
                    'description': 'Demo contract tool',
                    'governance': {
                        'family': 'demo_contract',
                        'display_name': 'Demo Contract',
                        'description': 'Demo contract tool',
                        'actions': [
                            {
                                'id': 'run',
                                'label': 'Run Demo Contract Tool',
                                'risk_level': 'medium',
                                'destructive': False,
                                'allowed_roles': ['ceo'],
                            }
                        ],
                    },
                },
                sort_keys=False,
            ),
        encoding='utf-8',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-contract-violation')

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        resource_manager=manager,
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
    )
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-contract-violation-bind')
    service.bind_resource_manager(manager)

    try:
        await service.startup()

        payload = service.load_tool_context(
            actor_role='ceo',
            session_id='web:shared',
            tool_id='demo_contract',
        )

        assert payload['ok'] is True
        assert payload['available'] is False
        assert payload['repair_required'] is True
        assert any('tool_result_delivery_contract' in str(item or '') for item in list(payload['errors'] or []))
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_admin_endpoints_expose_external_tool_fields(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _write_external_tool(workspace)

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
        client = TestClient(_build_app(service))

        item_response = client.get('/api/resources/tools/external_browser')
        assert item_response.status_code == 200
        item = item_response.json()['item']
        assert item['tool_type'] == 'external'
        assert item['callable'] is False
        assert item['install_dir'] == str((workspace / '.g3ku' / 'external-tools' / 'external_browser').resolve())

        toolskill_response = client.get('/api/resources/tools/external_browser/toolskill')
        assert toolskill_response.status_code == 200
        payload = toolskill_response.json()
        assert payload['tool_type'] == 'external'
        assert payload['callable'] is False
        assert payload['install_dir'] == str((workspace / '.g3ku' / 'external-tools' / 'external_browser').resolve())
        assert '## 安装' in payload['content']
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
        description='Workspace filesystem mutation family for write, edit, copy, move, delete, and patch operations.',
        primary_executor_name='filesystem_write',
        enabled=True,
        available=True,
        source_path='tools/filesystem_write',
        actions=[ToolActionRecord(action_id='write', label='Write File', allowed_roles=['ceo'])],
        metadata={},
    )

    class _StubService:
        async def startup(self) -> None:
            return None

        def update_tool_policy(
            self,
            tool_id: str,
            *,
            session_id: str,
            enabled=None,
            allowed_roles_by_action=None,
            execution_mode=None,
        ):
            captured['tool_id'] = tool_id
            captured['session_id'] = session_id
            captured['enabled'] = enabled
            captured['allowed_roles_by_action'] = allowed_roles_by_action
            captured['execution_mode'] = execution_mode
            return family.model_copy(
                update={
                    'enabled': bool(enabled),
                    'actions': [
                        family.actions[0].model_copy(
                            update={'allowed_roles': list((allowed_roles_by_action or {}).get('write') or [])}
                        )
                    ],
                }
            )

    client = TestClient(_build_app(_StubService()))
    response = client.put(
        '/api/resources/tools/filesystem/policy',
        params={'session_id': 'web:shared'},
        json={'enabled': True, 'actions': {'write': ['ceo', 'inspection']}},
    )

    assert response.status_code == 200
    assert captured == {
        'tool_id': 'filesystem',
        'session_id': 'web:shared',
        'enabled': True,
        'allowed_roles_by_action': {'write': ['ceo', 'inspection']},
        'execution_mode': None,
    }
    assert response.json()['item']['actions'][0]['allowed_roles'] == ['ceo', 'inspection']


def test_main_runtime_service_filters_visible_actions_for_shared_executor():
    family = ToolFamilyRecord(
        tool_id='filesystem',
        display_name='Filesystem',
        description='Workspace file operations.',
        primary_executor_name='filesystem_write',
        enabled=True,
        available=True,
        source_path='tools/filesystem_write',
        actions=[
            ToolActionRecord(action_id='copy', label='Copy', allowed_roles=['ceo', 'execution', 'inspection'], executor_names=['filesystem_copy']),
            ToolActionRecord(action_id='write', label='Write File', allowed_roles=['ceo', 'execution'], executor_names=['filesystem_write']),
        ],
        metadata={},
    )

    class _Registry:
        def list_tool_families(self):
            return [family]

    class _PolicyEngine:
        def evaluate_tool_action(self, *, subject, tool_id: str, action_id: str):
            _ = subject, tool_id
            return SimpleNamespace(allowed=action_id == 'copy')

    service = object.__new__(MainRuntimeService)
    service.resource_registry = _Registry()
    service.policy_engine = _PolicyEngine()
    service.list_effective_tool_names = lambda **kwargs: ['filesystem_copy', 'filesystem_write']
    service._subject = lambda **kwargs: SimpleNamespace(**kwargs)

    visible = service.list_visible_tool_families(actor_role='inspection', session_id='web:shared')

    assert len(visible) == 1
    assert [action.action_id for action in visible[0].actions] == ['copy']


def test_main_runtime_service_keeps_unavailable_tool_family_visible_for_context_lookup():
    family = ToolFamilyRecord(
        tool_id='agent_browser',
        display_name='Agent Browser',
        description='Browser automation via the upstream CLI.',
        primary_executor_name='agent_browser',
        enabled=True,
        available=False,
        callable=True,
        source_path='tools/agent_browser',
        actions=[
            ToolActionRecord(action_id='browse', label='Browse', allowed_roles=['ceo'], executor_names=['agent_browser']),
            ToolActionRecord(
                action_id='internal_only',
                label='Internal Only',
                agent_visible=False,
                allowed_roles=['ceo'],
                executor_names=['agent_browser'],
            ),
        ],
        metadata={'warnings': ['missing required bins']},
    )

    class _Registry:
        def list_tool_families(self):
            return [family]

    class _PolicyEngine:
        def evaluate_tool_action(self, *, subject, tool_id: str, action_id: str):
            _ = subject, tool_id, action_id
            return SimpleNamespace(allowed=False, reason_code='resource_disabled')

    service = object.__new__(MainRuntimeService)
    service.resource_registry = _Registry()
    service.policy_engine = _PolicyEngine()
    service.list_effective_tool_names = lambda **kwargs: []
    service._subject = lambda **kwargs: SimpleNamespace(**kwargs)

    visible = service.list_visible_tool_families(actor_role='ceo', session_id='web:shared')

    assert len(visible) == 1
    assert visible[0].tool_id == 'agent_browser'
    assert [action.action_id for action in visible[0].actions] == ['browse']


def test_main_runtime_service_normalizes_short_task_id_for_lookup_and_progress():
    captured: dict[str, str] = {}

    class _Store:
        def get_task(self, task_id: str):
            captured['store_task_id'] = task_id
            if task_id == 'task:demo':
                return SimpleNamespace(task_id=task_id)
            return None

    class _QueryService:
        def view_progress(self, task_id: str, *, mark_read: bool = True):
            captured['progress_task_id'] = task_id
            captured['mark_read'] = str(mark_read)
            return SimpleNamespace(text=f'progress:{task_id}:{mark_read}')

    service = object.__new__(MainRuntimeService)
    service.store = _Store()
    service.query_service = _QueryService()

    task = service.get_task('demo')
    progress = service.view_progress('demo', mark_read=False)

    assert task is not None
    assert captured['store_task_id'] == 'task:demo'
    assert captured['progress_task_id'] == 'task:demo'
    assert progress == 'progress:task:demo:False'


def test_main_runtime_service_normalizes_short_task_id_for_failed_node_lookup():
    captured: dict[str, str] = {}

    class _QueryService:
        def failed_node_ids(self, task_id: str):
            captured['failed_task_id'] = task_id
            return ['node:1', 'node:2']

    service = object.__new__(MainRuntimeService)
    service.query_service = _QueryService()

    result = service.failed_node_ids('demo')

    assert captured['failed_task_id'] == 'task:demo'
    assert result == '- node:1\n- node:2'


def test_main_runtime_service_node_detail_includes_matching_artifacts():
    captured: dict[str, str] = {}

    class _Store:
        def list_artifacts(self, task_id: str):
            captured['artifacts_task_id'] = task_id
            return [
                SimpleNamespace(
                    artifact_id='artifact:1',
                    task_id=task_id,
                    node_id='node:demo',
                    kind='report',
                    title='Artifact One',
                    path='D:/artifact-one.md',
                    mime_type='text/markdown',
                    preview_text='one',
                    created_at='2026-03-25T00:00:00',
                    model_dump=lambda mode='json': {
                        'artifact_id': 'artifact:1',
                        'task_id': task_id,
                        'node_id': 'node:demo',
                        'kind': 'report',
                        'title': 'Artifact One',
                        'path': 'D:/artifact-one.md',
                        'mime_type': 'text/markdown',
                        'preview_text': 'one',
                        'created_at': '2026-03-25T00:00:00',
                    },
                ),
                SimpleNamespace(
                    artifact_id='artifact:2',
                    task_id=task_id,
                    node_id='node:other',
                    kind='report',
                    title='Artifact Two',
                    path='D:/artifact-two.md',
                    mime_type='text/markdown',
                    preview_text='two',
                    created_at='2026-03-25T00:00:01',
                    model_dump=lambda mode='json': {
                        'artifact_id': 'artifact:2',
                        'task_id': task_id,
                        'node_id': 'node:other',
                        'kind': 'report',
                        'title': 'Artifact Two',
                        'path': 'D:/artifact-two.md',
                        'mime_type': 'text/markdown',
                        'preview_text': 'two',
                        'created_at': '2026-03-25T00:00:01',
                    },
                ),
            ]

    class _Task:
        task_id = 'task:demo'

    service = object.__new__(MainRuntimeService)
    service.store = _Store()
    service.get_task = lambda task_id: _Task() if task_id == 'task:demo' else None
    service.get_node_detail_payload = lambda task_id, node_id, detail_level='summary': {
        'ok': True,
        'task_id': task_id,
        'node_id': node_id,
        'item': {'task_id': task_id, 'node_id': node_id, 'status': 'failed', 'detail_level': detail_level},
    }

    result = service.node_detail('demo', 'node:demo', detail_level='full')

    assert isinstance(result, dict)
    assert captured['artifacts_task_id'] == 'task:demo'
    assert result['task_id'] == 'task:demo'
    assert result['node_id'] == 'node:demo'
    assert result['artifact_count'] == 1
    assert result['artifacts'][0]['artifact_id'] == 'artifact:1'
    assert result['artifacts'][0]['ref'] == 'artifact:artifact:1'


def test_main_runtime_service_node_detail_compacts_execution_trace_for_tool_output():
    class _Store:
        def list_artifacts(self, task_id: str):
            return []

    class _Task:
        task_id = 'task:demo'

    service = object.__new__(MainRuntimeService)
    service.store = _Store()
    service.get_task = lambda task_id: _Task() if task_id == 'task:demo' else None
    service.get_node_detail_payload = lambda task_id, node_id, detail_level='summary': {
        'ok': True,
        'task_id': task_id,
        'node_id': node_id,
        'item': {
            'task_id': task_id,
            'node_id': node_id,
            'status': 'failed',
            'detail_level': detail_level,
            'execution_trace': {
                'initial_prompt': 'full prompt',
                'tool_steps': [
                    {
                        'tool_name': 'filesystem',
                        'arguments_text': '{"path": "repo"}',
                        'output_text': 'repo summary',
                        'output_ref': 'artifact:tool-step',
                        'status': 'success',
                    }
                ],
                'stages': [
                    {
                        'stage_id': 'stage-1',
                        'stage_goal': 'inspect repository',
                        'tool_round_budget': 6,
                        'tool_rounds_used': 1,
                        'rounds': [
                            {
                                'round_id': 'round-1',
                                'tools': [
                                    {
                                        'tool_name': 'filesystem',
                                        'arguments_text': '{"path": "repo"}',
                                        'output_text': 'repo summary',
                                        'output_ref': 'artifact:tool-step',
                                        'status': 'success',
                                    },
                                    {
                                        'tool_name': 'content',
                                        'arguments_text': '{"ref": "artifact:tool-step"}',
                                        'output_text': 'file contents',
                                        'output_ref': 'artifact:content-step',
                                        'status': 'success',
                                    },
                                ],
                            }
                        ],
                    }
                ],
                'final_output': 'done',
                'acceptance_result': 'pass',
            },
        },
    }

    result = service.node_detail('demo', 'node:demo', detail_level='full')

    assert isinstance(result, dict)
    assert result['item']['execution_trace']['stages'][0]['stage_goal'] == 'inspect repository'
    assert result['item']['execution_trace']['stages'][0]['rounds'][0]['tools'][0]['tool_name'] == 'filesystem'


def test_main_runtime_service_node_detail_defaults_to_summary_payload():
    class _Store:
        def list_artifacts(self, task_id: str):
            return [
                SimpleNamespace(
                    artifact_id='artifact:1',
                    task_id=task_id,
                    node_id='node:demo',
                    kind='report',
                    title='Artifact One',
                    path='D:/artifact-one.md',
                    mime_type='text/markdown',
                    preview_text='one',
                    created_at='2026-03-25T00:00:00',
                    model_dump=lambda mode='json': {
                        'artifact_id': 'artifact:1',
                        'task_id': task_id,
                        'node_id': 'node:demo',
                        'kind': 'report',
                        'title': 'Artifact One',
                        'path': 'D:/artifact-one.md',
                        'mime_type': 'text/markdown',
                        'preview_text': 'one',
                        'created_at': '2026-03-25T00:00:00',
                    },
                )
            ]

    class _Task:
        task_id = 'task:demo'

    service = object.__new__(MainRuntimeService)
    service.store = _Store()
    service.get_task = lambda task_id: _Task() if task_id == 'task:demo' else None
    service.get_node_detail_payload = lambda task_id, node_id, detail_level='summary': {
        'ok': True,
        'task_id': task_id,
        'node_id': node_id,
        'item': {
            'task_id': task_id,
            'node_id': node_id,
            'detail_level': detail_level,
            'output_preview': 'preview output',
            'output_ref': 'artifact:artifact:out',
            'execution_trace_summary': {'stages': []},
            'execution_trace_ref': 'artifact:artifact:trace',
        },
    }

    result = service.node_detail('demo', 'node:demo')

    assert result['item']['detail_level'] == 'summary'
    assert 'artifacts_preview' in result
    assert 'artifacts' not in result


def test_task_node_detail_tool_parameters_match_resource_contract():
    tool = TaskNodeDetailTool(SimpleNamespace())

    assert tool.parameters == {
        'type': 'object',
        'properties': {
            '任务id': {'type': 'string', 'description': '目标任务 id。'},
            '节点id': {'type': 'string', 'description': '目标节点 id。'},
            'detail_level': {
                'type': 'string',
                'enum': ['summary', 'full'],
                'description': 'summary 返回轻量节点详情与 refs/工件预览；full 返回完整执行轨迹和完整工件列表。',
            },
        },
        'required': ['任务id', '节点id'],
    }


def test_runtime_service_has_single_live_task_node_detail_tool_definition():
    source = inspect.getsource(__import__("main.service.runtime_service", fromlist=["TaskNodeDetailTool"]))

    assert source.count("return 'task_node_detail'") == 1


@pytest.mark.asyncio
async def test_create_async_task_tool_uses_runtime_task_default_max_depth():
    captured: dict[str, object] = {}

    class _StubService:
        async def precheck_async_task_creation(self, **kwargs):
            captured['precheck'] = dict(kwargs)
            return {
                'decision': 'approve_new',
                'matched_task_id': '',
                'reason': 'no duplicate found',
                'decision_source': 'rule',
            }

        async def create_task(self, task: str, *, session_id: str = 'web:shared', max_depth: int | None = None, **kwargs):
            captured['task'] = task
            captured['session_id'] = session_id
            captured['max_depth'] = max_depth
            captured['kwargs'] = kwargs
            return SimpleNamespace(task_id='task:demo')

    tool = CreateAsyncTaskTool(_StubService())
    result = await tool.execute(
        '整理需求',
        core_requirement='梳理用户需求的核心目标',
        execution_policy={'mode': 'focus'},
        file_targets=[
            {'path': 'D:/Uploads/resume.docx'},
            {'path': 'D:/Uploads/jd.png', 'ref': 'artifact:artifact:jd123'},
        ],
        __g3ku_runtime={'session_key': 'web:ceo-demo', 'task_defaults': {'max_depth': 3}},
    )

    assert result.endswith('task:demo')
    assert captured['task'] == '整理需求'
    assert captured['session_id'] == 'web:ceo-demo'
    assert captured['max_depth'] == 3
    assert captured['kwargs']['metadata']['core_requirement'] == '梳理用户需求的核心目标'
    assert captured['kwargs']['metadata']['execution_policy'] == {'mode': 'focus'}
    assert captured['kwargs']['metadata']['file_targets'] == [
        {'path': 'D:/Uploads/resume.docx'},
        {'path': 'D:/Uploads/jd.png', 'ref': 'artifact:artifact:jd123'},
    ]


def test_create_async_task_tool_requires_execution_policy_param() -> None:
    tool = CreateAsyncTaskTool(SimpleNamespace())

    errors = tool.validate_params(
        {
            'task': '整理需求',
            'core_requirement': '梳理用户需求的核心目标',
        }
    )

    assert 'missing required execution_policy' in errors


"""
legacy broken draft kept only to neutralize old garbled literals during patching
    tool = CreateAsyncTaskTool(SimpleNamespace())

    errors = tool.validate_params(
        {
            'task': '鏁寸悊闇€姹?,
            'core_requirement': '姊崇悊鐢ㄦ埛闇€姹傜殑鏍稿績鐩爣',
            'execution_policy': {'mode': 'focus'},
            'file_targets': None,
        }
    )

    assert errors == []
"""


def test_create_async_task_tool_allows_null_file_targets() -> None:
    tool = CreateAsyncTaskTool(SimpleNamespace())

    errors = tool.validate_params(
        {
            'task': 'summarize uploaded files',
            'core_requirement': 'summarize the uploaded file requirements',
            'execution_policy': {'mode': 'focus'},
            'file_targets': None,
        }
    )

    assert errors == []


@pytest.mark.asyncio
async def test_create_async_task_tool_rejects_continuation_of_task_id():
    tool = CreateAsyncTaskTool(SimpleNamespace())

    with pytest.raises(ValueError, match='create_async_task_no_longer_supports_continuation'):
        await tool.execute(
            '继续完成失败任务',
            core_requirement='继续完成打开网页的自动化流程',
            execution_policy={'mode': 'focus'},
            continuation_of_task_id='task:old-1',
            __g3ku_runtime={'session_key': 'web:ceo-demo'},
        )


@pytest.mark.asyncio
async def test_create_async_task_tool_returns_duplicate_rejection_text():
    captured: dict[str, object] = {}

    class _StubService:
        async def precheck_async_task_creation(self, **kwargs):
            captured['precheck'] = dict(kwargs)
            return {
                'decision': 'reject_duplicate',
                'matched_task_id': 'task:existing-1',
                'reason': 'core_requirement exact match',
                'decision_source': 'rule',
            }

        async def create_task(self, *args, **kwargs):
            raise AssertionError('create_task should not run for duplicate rejection')

    tool = CreateAsyncTaskTool(_StubService())
    result = await tool.execute(
        '整理重点客户流失信号',
        core_requirement='整理重点客户流失信号',
        execution_policy={'mode': 'focus'},
        __g3ku_runtime={'session_key': 'web:ceo-demo'},
    )

    assert '任务未创建' in result
    assert 'task:existing-1' in result
    assert '高度重复' in result
    assert captured['precheck']['session_id'] == 'web:ceo-demo'


@pytest.mark.asyncio
async def test_create_async_task_tool_rechecks_exact_duplicate_before_create():
    class _StubService:
        async def precheck_async_task_creation(self, **kwargs):
            _ = kwargs
            return {
                'decision': 'approve_new',
                'matched_task_id': '',
                'reason': 'rule precheck found no exact duplicate',
                'decision_source': 'rule',
            }

        def revalidate_async_task_creation_before_create(self, **kwargs):
            _ = kwargs
            return {
                'decision': 'reject_duplicate',
                'matched_task_id': 'task:existing-guarded',
                'reason': 'core_requirement exact match',
                'decision_source': 'rule',
            }

        async def create_task(self, *args, **kwargs):
            raise AssertionError('create_task should not run when revalidation catches a duplicate')

    tool = CreateAsyncTaskTool(_StubService())
    result = await tool.execute(
        '整理重点客户流失信号',
        core_requirement='整理重点客户流失信号',
        execution_policy={'mode': 'focus'},
        __g3ku_runtime={'session_key': 'web:ceo-demo'},
    )

    assert '任务未创建' in result
    assert 'task:existing-guarded' in result
    assert '高度重复' in result


@pytest.mark.asyncio
async def test_create_async_task_tool_returns_append_notice_guidance():
    class _StubService:
        async def precheck_async_task_creation(self, **kwargs):
            _ = kwargs
            return {
                'decision': 'reject_use_append_notice',
                'matched_task_id': 'task:existing-2',
                'reason': 'existing task only needs the new acceptance constraint',
                'decision_source': 'llm',
            }

        async def create_task(self, *args, **kwargs):
            raise AssertionError('create_task should not run for append-notice rejection')

    tool = CreateAsyncTaskTool(_StubService())
    result = await tool.execute(
        '整理重点客户流失信号并新增董事会验收格式',
        core_requirement='整理重点客户流失信号并新增董事会验收格式',
        execution_policy={'mode': 'focus'},
        requires_final_acceptance=True,
        final_acceptance_prompt='必须按董事会模板输出',
        __g3ku_runtime={'session_key': 'web:ceo-demo'},
    )

    assert '任务未创建' in result
    assert 'task:existing-2' in result
    assert '追加通知' in result
    assert 'task_append_notice' in result


def test_create_async_task_contract_no_longer_accepts_continuation_fields() -> None:
    from main.service.create_async_task_contract import build_create_async_task_parameters

    schema = build_create_async_task_parameters()
    props = dict(schema.get('properties') or {})

    assert 'file_targets' in props
    assert props['file_targets']['type'] == ['array', 'null']
    assert props['file_targets']['items']['type'] == 'object'
    assert props['file_targets']['items']['properties']['path']['type'] == ['string', 'null']
    assert props['file_targets']['items']['properties']['ref']['type'] == ['string', 'null']
    assert 'authoritative reopen targets' in str(props['file_targets']['description'] or '').lower()
    assert 'exact reopen handles in `file_targets`' in str(props['task']['description'] or '').lower()
    assert 'continuation_of_task_id' not in props
    assert 'reuse_existing' not in props


@pytest.mark.asyncio
async def test_main_runtime_service_falls_back_core_requirement_to_task_prompt(tmp_path: Path):
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='embedded',
    )
    service.global_scheduler.enqueue_task = _noop_enqueue_task

    try:
        record = await service.create_task('整理需求', session_id='web:shared', metadata={})

        assert record.metadata['core_requirement'] == '整理需求'
        stored = service.get_task(record.task_id)
        assert stored is not None
        assert stored.metadata['core_requirement'] == '整理需求'
    finally:
        await service.close()


def test_ceo_session_task_defaults_endpoint_reads_and_updates_depth(tmp_path: Path):
    class _SessionManager:
        def __init__(self, session, path: Path):
            self._session = session
            self._path = path
            self.saved = 0

        def get_path(self, key: str) -> Path:
            assert key == self._session.key
            return self._path

        def get_or_create(self, key: str):
            assert key == self._session.key
            return self._session

        def save(self, session) -> None:
            self._session = session
            self.saved += 1

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': self._session.key}]

    session_path = tmp_path / 'sessions' / 'web_ceo_demo.jsonl'
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    original_updated_at = datetime(2026, 3, 18, 0, 0, 0)
    session = Session(key='web:ceo-demo', metadata={}, updated_at=original_updated_at)
    manager = _SessionManager(session, session_path)

    from g3ku.runtime import web_ceo_sessions
    from g3ku.runtime.api import ceo_sessions

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    ceo_sessions.get_agent = lambda: SimpleNamespace(sessions=manager)
    ceo_sessions.get_runtime_manager = lambda _agent: SimpleNamespace(get=lambda _session_id: None)
    ceo_sessions.workspace_path = lambda: tmp_path
    ceo_sessions.main_runtime_depth_limits = lambda: {'default_max_depth': 2, 'hard_max_depth': 4}
    web_ceo_sessions.main_runtime_depth_limits = lambda: {'default_max_depth': 2, 'hard_max_depth': 4}

    client = TestClient(app)

    initial = client.get('/api/ceo/sessions/web:ceo-demo/task-defaults')
    assert initial.status_code == 200
    assert initial.json()['task_defaults']['max_depth'] == 2
    assert 'task_defaults' not in manager.get_or_create('web:ceo-demo').metadata
    assert 'task_defaults_scope' not in manager.get_or_create('web:ceo-demo').metadata

    updated = client.patch('/api/ceo/sessions/web:ceo-demo/task-defaults', json={'max_depth': 9})
    assert updated.status_code == 200
    assert updated.json()['task_defaults']['max_depth'] == 4
    assert manager.saved >= 1
    assert manager.get_or_create('web:ceo-demo').metadata['task_defaults']['max_depth'] == 4
    assert manager.get_or_create('web:ceo-demo').metadata['task_defaults_scope'] == 'session'
    assert manager.get_or_create('web:ceo-demo').updated_at > original_updated_at


def test_ceo_session_task_defaults_endpoint_ignores_legacy_unscoped_depth(tmp_path: Path):
    class _SessionManager:
        def __init__(self, session, path: Path):
            self._session = session
            self._path = path
            self.saved = 0

        def get_path(self, key: str) -> Path:
            assert key == self._session.key
            return self._path

        def get_or_create(self, key: str):
            assert key == self._session.key
            return self._session

        def save(self, session) -> None:
            self._session = session
            self.saved += 1

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': self._session.key}]

    session_path = tmp_path / 'sessions' / 'web_ceo_legacy.jsonl'
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    session = Session(
        key='web:ceo-legacy',
        metadata={'task_defaults': {'max_depth': 39}},
        updated_at=datetime(2026, 3, 18, 0, 0, 0),
    )
    manager = _SessionManager(session, session_path)

    from g3ku.runtime import web_ceo_sessions
    from g3ku.runtime.api import ceo_sessions

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    ceo_sessions.get_agent = lambda: SimpleNamespace(sessions=manager)
    ceo_sessions.get_runtime_manager = lambda _agent: SimpleNamespace(get=lambda _session_id: None)
    ceo_sessions.workspace_path = lambda: tmp_path
    ceo_sessions.main_runtime_depth_limits = lambda: {'default_max_depth': 1, 'hard_max_depth': 4}
    web_ceo_sessions.main_runtime_depth_limits = lambda: {'default_max_depth': 1, 'hard_max_depth': 4}

    client = TestClient(app)

    response = client.get('/api/ceo/sessions/web:ceo-legacy/task-defaults')

    assert response.status_code == 200
    assert response.json()['task_defaults']['max_depth'] == 1
    assert 'task_defaults' not in manager.get_or_create('web:ceo-legacy').metadata
    assert 'task_defaults_scope' not in manager.get_or_create('web:ceo-legacy').metadata


def test_ceo_frontdoor_support_ignores_unscoped_task_default_override():
    assert CeoFrontDoorSupport._session_task_defaults(
        SimpleNamespace(metadata={'task_defaults': {'max_depth': 4}})
    ) == {}
    assert CeoFrontDoorSupport._session_task_defaults(
        SimpleNamespace(metadata={'task_defaults': {'max_depth': 4}, 'task_defaults_scope': 'session'})
    ) == {'max_depth': 4}


def test_main_runtime_settings_endpoint_reads_and_updates_global_depth(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    initial = client.get('/api/main-runtime/settings')
    assert initial.status_code == 200
    assert initial.json()['task_defaults']['max_depth'] == 1
    assert initial.json()['main_runtime']['hard_max_depth'] == 4

    updated = client.put('/api/main-runtime/settings', json={'max_depth': 9})
    assert updated.status_code == 200
    assert updated.json()['task_defaults']['max_depth'] == 9
    assert updated.json()['main_runtime']['hard_max_depth'] == 9
    assert captured == {'force': True, 'reason': 'admin_main_runtime_update'}

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['mainRuntime']['defaultMaxDepth'] == 9


def test_ceo_session_activate_endpoint_allows_switching_from_running_session(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)
    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    current = Session(key='web:ceo-current', metadata={'title': 'Current Session'})
    other = Session(key='web:ceo-other', metadata={'title': 'Other Session'})
    current_path = tmp_path / 'sessions' / 'web_ceo_current.jsonl'
    other_path = tmp_path / 'sessions' / 'web_ceo_other.jsonl'
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    other_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    manager = _SessionManager(
        [current, other],
        {
            current.key: current_path,
            other.key: other_path,
        },
    )

    from g3ku.runtime.api import ceo_sessions

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    ceo_sessions.get_agent = lambda: SimpleNamespace(sessions=manager)
    ceo_sessions.get_runtime_manager = lambda _agent: SimpleNamespace(
        get=lambda session_id: (
            SimpleNamespace(state=SimpleNamespace(status='running', is_running=True))
            if session_id == current.key
            else None
        )
    )
    ceo_sessions.workspace_path = lambda: tmp_path

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    response = client.post(f'/api/ceo/sessions/{other.key}/activate')

    assert response.status_code == 200
    payload = response.json()
    assert payload['active_session_id'] == other.key
    assert payload['item']['session_id'] == other.key
    assert ceo_sessions.WebCeoStateStore(tmp_path).get_active_session_id() == other.key


def test_ceo_session_list_endpoint_reports_running_state(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)
    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    current = Session(key='web:ceo-current', metadata={'title': 'Current Session'})
    other = Session(key='web:ceo-other', metadata={'title': 'Other Session'})
    current_path = tmp_path / 'sessions' / 'web_ceo_current.jsonl'
    other_path = tmp_path / 'sessions' / 'web_ceo_other.jsonl'
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    other_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    manager = _SessionManager(
        [current, other],
        {
            current.key: current_path,
            other.key: other_path,
        },
    )

    from g3ku.runtime.api import ceo_sessions

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    ceo_sessions.get_agent = lambda: SimpleNamespace(sessions=manager)
    ceo_sessions.get_runtime_manager = lambda _agent: SimpleNamespace(
        get=lambda session_id: (
            SimpleNamespace(state=SimpleNamespace(status='running', is_running=True))
            if session_id == current.key
            else None
        )
    )
    ceo_sessions.workspace_path = lambda: tmp_path

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    response = client.get('/api/ceo/sessions')

    assert response.status_code == 200
    payload = response.json()
    items = {item['session_id']: item for item in payload['items']}
    assert items[current.key]['is_running'] is True
    assert items[other.key]['is_running'] is False


def test_ceo_session_create_endpoint_allows_new_session_while_current_session_is_running(
    tmp_path: Path,
    monkeypatch,
):
    _mock_ceo_catalog_config(monkeypatch)
    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            if key not in self._sessions:
                self._sessions[key] = Session(key=key, metadata={})
                path = tmp_path / 'sessions' / f"{key.replace(':', '_')}.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
                self._paths[key] = path
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session
            self._paths.setdefault(
                session.key,
                tmp_path / 'sessions' / f"{session.key.replace(':', '_')}.jsonl",
            )

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    current = Session(key='web:ceo-current', metadata={'title': 'Current Session'})
    current_path = tmp_path / 'sessions' / 'web_ceo_current.jsonl'
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    manager = _SessionManager([current], {current.key: current_path})

    from g3ku.runtime import web_ceo_sessions
    from g3ku.runtime.api import ceo_sessions

    monkeypatch.setattr(web_ceo_sessions, 'new_web_ceo_session_id', lambda: 'web:ceo-new')

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    ceo_sessions.get_agent = lambda: SimpleNamespace(sessions=manager)
    ceo_sessions.get_runtime_manager = lambda _agent: SimpleNamespace(
        get=lambda session_id: (
            SimpleNamespace(state=SimpleNamespace(status='running', is_running=True))
            if session_id == current.key
            else None
        )
    )
    ceo_sessions.workspace_path = lambda: tmp_path

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    response = client.post('/api/ceo/sessions', json={'title': 'Parallel Session'})

    assert response.status_code == 200
    payload = response.json()
    assert payload['active_session_id'] == 'web:ceo-new'
    assert payload['item']['session_id'] == 'web:ceo-new'
    assert payload['item']['title'] == 'Parallel Session'
    assert ceo_sessions.WebCeoStateStore(tmp_path).get_active_session_id() == 'web:ceo-new'


def test_ceo_session_delete_check_reports_grouped_task_ids(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    class _TaskService:
        async def startup(self) -> None:
            return None

        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return [
                SimpleNamespace(task_id='task:done-1', title='Done 1', status='success', is_paused=False),
                SimpleNamespace(task_id='task:paused-1', title='Paused 1', status='in_progress', is_paused=True),
                SimpleNamespace(task_id='task:busy-1', title='Busy 1', status='in_progress', is_paused=False),
            ]

        def get_session_task_counts(self, session_id: str) -> dict[str, int]:
            _ = session_id
            return {
                'total': 3,
                'unfinished': 1,
                'in_progress': 1,
                'paused': 1,
                'terminal': 1,
                'deletable': 2,
            }

    current = Session(key='web:ceo-delete-check', metadata={'title': 'Delete Check'})
    current_path = tmp_path / 'sessions' / 'web_ceo_delete_check.jsonl'
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    manager = _SessionManager([current], {current.key: current_path})

    from g3ku.runtime.api import ceo_sessions

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    ceo_sessions.get_agent = lambda: SimpleNamespace(sessions=manager, main_task_service=_TaskService())
    ceo_sessions.get_runtime_manager = lambda _agent: SimpleNamespace(get=lambda _session_id: None)
    ceo_sessions.workspace_path = lambda: tmp_path

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    response = client.get(f'/api/ceo/sessions/{current.key}/delete-check')

    assert response.status_code == 200
    payload = response.json()
    assert payload['can_delete'] is True
    assert payload['related_tasks']['deletable'] == 2
    assert [item['task_id'] for item in payload['usage']['completed_tasks']] == ['task:done-1']
    assert [item['task_id'] for item in payload['usage']['paused_tasks']] == ['task:paused-1']
    assert [item['task_id'] for item in payload['usage']['in_progress_tasks']] == ['task:busy-1']
    assert [item['task_id'] for item in payload['usage']['tasks']] == ['task:busy-1']


def test_ceo_session_delete_allows_unfinished_related_tasks(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def invalidate(self, key: str) -> None:
            self._sessions.pop(key, None)
            self._paths.pop(key, None)

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    class _TaskService:
        async def startup(self) -> None:
            return None

        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return [
                SimpleNamespace(task_id='task:busy-delete', title='Busy Delete', status='in_progress', is_paused=False),
            ]

        def get_session_task_counts(self, session_id: str) -> dict[str, int]:
            _ = session_id
            return {
                'total': 1,
                'unfinished': 1,
                'in_progress': 1,
                'paused': 0,
                'terminal': 0,
                'deletable': 0,
            }

    current = Session(key='web:ceo-delete-unfinished', metadata={'title': 'Delete Unfinished'})
    other = Session(key='web:ceo-keep-after-delete', metadata={'title': 'Keep After Delete'})
    current_path = tmp_path / 'sessions' / 'web_ceo_delete_unfinished.jsonl'
    other_path = tmp_path / 'sessions' / 'web_ceo_keep_after_delete.jsonl'
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    other_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    manager = _SessionManager(
        [current, other],
        {
            current.key: current_path,
            other.key: other_path,
        },
    )
    captured: dict[str, object] = {}

    class _RuntimeManager:
        def get(self, session_id: str):
            _ = session_id
            return None

        def remove(self, session_id: str):
            captured['removed_session'] = session_id
            return None

    async def _cancel_session_tasks(session_key: str) -> int:
        captured['cancelled_session'] = session_key
        return 0

    async def _enqueue_session_boundary_flush(**kwargs):
        captured.setdefault('flush_calls', []).append(dict(kwargs))
        return {'ok': True}

    from g3ku.runtime.api import ceo_sessions

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    ceo_sessions.get_agent = lambda: SimpleNamespace(
        sessions=manager,
        main_task_service=_TaskService(),
        memory_manager=SimpleNamespace(enqueue_session_boundary_flush=_enqueue_session_boundary_flush),
        cancel_session_tasks=_cancel_session_tasks,
    )
    ceo_sessions.get_runtime_manager = lambda _agent: _RuntimeManager()
    ceo_sessions.get_web_heartbeat_service = lambda _agent: None
    ceo_sessions.workspace_path = lambda: tmp_path

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    response = client.delete(f'/api/ceo/sessions/{current.key}')

    assert response.status_code == 200
    payload = response.json()
    assert payload['deleted'] is True
    assert payload['session_id'] == current.key
    assert captured == {
        'removed_session': current.key,
        'cancelled_session': current.key,
    }


@pytest.mark.asyncio
async def test_ceo_session_delete_removes_session_owned_frontdoor_stage_archives(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def invalidate(self, key: str) -> None:
            self._sessions.pop(key, None)
            self._paths.pop(key, None)

        def list_sessions(self) -> list[dict[str, str]]:
            return [{"key": key} for key in self._sessions]

    class _TaskService:
        def __init__(self, service: MainRuntimeService) -> None:
            self._service = service
            self.artifact_store = service.artifact_store
            self.store = service.store

        async def startup(self) -> None:
            return None

        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return []

        def get_session_task_counts(self, session_id: str) -> dict[str, int]:
            _ = session_id
            return {"all": 0, "in_progress": 0}

    class _RuntimeManager:
        def get(self, session_id: str):
            _ = session_id
            return None

        def remove(self, session_id: str):
            captured["removed_session"] = session_id
            return None

    async def _cancel_session_tasks(session_key: str) -> int:
        captured["cancelled_session"] = session_key
        return 0

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    current = Session(key="web:ceo-frontdoor-archive-delete", metadata={"title": "Delete Frontdoor Archive"})
    other = Session(key="web:ceo-frontdoor-archive-keep", metadata={"title": "Keep Frontdoor Archive"})
    current_path = tmp_path / "sessions" / "web_ceo_frontdoor_archive_delete.jsonl"
    other_path = tmp_path / "sessions" / "web_ceo_frontdoor_archive_keep.jsonl"
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding="utf-8")
    other_path.write_text('{"_type":"metadata"}\n', encoding="utf-8")
    manager = _SessionManager(
        [current, other],
        {
            current.key: current_path,
            other.key: other_path,
        },
    )
    captured: dict[str, object] = {}

    try:
        await service.startup()
        current_summary, current_ref = service.content_store.summarize_for_storage(
            json.dumps({"session_id": current.key, "stages": [{"stage_index": 1}]}, ensure_ascii=False, indent=2),
            runtime={
                "task_id": _frontdoor_stage_archive_task_id(current.key),
                "session_key": current.key,
            },
            display_name="frontdoor-stage-history:current",
            source_kind="stage_history_archive",
            force=True,
        )
        other_summary, other_ref = service.content_store.summarize_for_storage(
            json.dumps({"session_id": other.key, "stages": [{"stage_index": 2}]}, ensure_ascii=False, indent=2),
            runtime={
                "task_id": _frontdoor_stage_archive_task_id(other.key),
                "session_key": other.key,
            },
            display_name="frontdoor-stage-history:other",
            source_kind="stage_history_archive",
            force=True,
        )

        assert current_summary
        assert other_summary
        current_artifact = service.get_artifact(str(current_ref).split(":", 1)[1])
        other_artifact = service.get_artifact(str(other_ref).split(":", 1)[1])
        assert current_artifact is not None
        assert other_artifact is not None

        from g3ku.runtime.api import ceo_sessions

        app = FastAPI()
        app.include_router(ceo_sessions.router, prefix="/api")
        monkeypatch.setattr(
            ceo_sessions,
            "get_agent",
            lambda: SimpleNamespace(
                sessions=manager,
                main_task_service=_TaskService(service),
                cancel_session_tasks=_cancel_session_tasks,
            ),
        )
        monkeypatch.setattr(ceo_sessions, "get_runtime_manager", lambda _agent: _RuntimeManager())
        monkeypatch.setattr(ceo_sessions, "get_web_heartbeat_service", lambda _agent: None)
        monkeypatch.setattr(ceo_sessions, "workspace_path", lambda: tmp_path)

        ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
        client = TestClient(app)

        response = client.delete(f"/api/ceo/sessions/{current.key}")

        assert response.status_code == 200
        payload = response.json()
        assert payload["deleted"] is True
        assert captured == {
            "removed_session": current.key,
            "cancelled_session": current.key,
        }
        assert service.artifact_store.list_artifacts(_frontdoor_stage_archive_task_id(current.key)) == []
        assert not Path(current_artifact.path).exists()
        remaining = service.artifact_store.list_artifacts(_frontdoor_stage_archive_task_id(other.key))
        assert len(remaining) == 1
        assert Path(other_artifact.path).exists()
    finally:
        await service.close()


def test_ceo_session_delete_stops_detached_background_tool_executions(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)
    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def invalidate(self, key: str) -> None:
            self._sessions.pop(key, None)
            self._paths.pop(key, None)

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    class _TaskService:
        async def startup(self) -> None:
            return None

        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return []

        def get_session_task_counts(self, session_id: str) -> dict[str, int]:
            _ = session_id
            return {'all': 0, 'in_progress': 0}

    current = Session(key='web:ceo-delete-me', metadata={'title': 'Delete Me'})
    other = Session(key='web:ceo-keep', metadata={'title': 'Keep Me'})
    current_path = tmp_path / 'sessions' / 'web_ceo_delete_me.jsonl'
    other_path = tmp_path / 'sessions' / 'web_ceo_keep.jsonl'
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    other_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    manager = _SessionManager(
        [current, other],
        {
            current.key: current_path,
            other.key: other_path,
        },
    )
    captured: dict[str, object] = {}

    class _Heartbeat:
        def clear_session(self, session_id: str) -> None:
            captured['heartbeat_cleared'] = session_id

    class _ToolExecutionManager:
        async def stop_session_executions(self, session_key: str, *, reason: str = 'session_deleted', **kwargs):
            _ = kwargs
            captured['stopped_session_key'] = session_key
            captured['stop_reason'] = reason
            return [
                {'execution_id': 'tool-exec:1', 'status': 'stopped'},
                {'execution_id': 'tool-exec:2', 'status': 'stopped'},
            ]

    class _RuntimeManager:
        def get(self, session_id: str):
            _ = session_id
            return None

        def remove(self, session_id: str):
            captured['removed_session'] = session_id
            return None

    async def _cancel_session_tasks(session_key: str) -> int:
        captured['cancelled_session'] = session_key
        return 0

    async def _purge_checkpointer_thread(session_key: str) -> None:
        captured['purged_checkpointer_thread'] = session_key

    from g3ku.runtime.api import ceo_sessions

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    ceo_sessions.get_agent = lambda: SimpleNamespace(
        sessions=manager,
        main_task_service=_TaskService(),
        tool_execution_manager=_ToolExecutionManager(),
        cancel_session_tasks=_cancel_session_tasks,
        purge_checkpointer_thread=_purge_checkpointer_thread,
    )
    ceo_sessions.get_runtime_manager = lambda _agent: _RuntimeManager()
    ceo_sessions.get_web_heartbeat_service = lambda _agent: _Heartbeat()
    ceo_sessions.workspace_path = lambda: tmp_path

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    response = client.delete(f'/api/ceo/sessions/{current.key}')

    assert response.status_code == 200
    payload = response.json()
    assert payload['deleted'] is True
    assert payload['stopped_background_tool_count'] == 2
    assert captured == {
        'stopped_session_key': current.key,
        'stop_reason': 'session_deleted',
        'heartbeat_cleared': current.key,
        'removed_session': current.key,
        'cancelled_session': current.key,
        'purged_checkpointer_thread': current.key,
    }


def _build_inflight_only_ceo_session_delete_client(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    from g3ku.runtime.api import ceo_sessions
    from g3ku.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    current = Session(key='web:ceo-active-current', metadata={'title': 'Current Session'})
    manager.save(current)
    target_id = 'web:ceo-inflight-only'
    captured: dict[str, str] = {}

    monkeypatch.setattr(web_ceo_sessions, 'workspace_path', lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, 'workspace_path', lambda: tmp_path)
    web_ceo_sessions.write_inflight_turn_snapshot(
        target_id,
        {
            'status': 'running',
            'started_at': datetime.now().isoformat(),
            'user_message': {'role': 'user', 'content': 'Delete the inflight-only session'},
        },
    )

    class _TaskService:
        async def startup(self) -> None:
            return None

        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return []

        def get_session_task_counts(self, session_id: str) -> dict[str, int]:
            _ = session_id
            return {'total': 0, 'unfinished': 0, 'in_progress': 0, 'paused': 0, 'terminal': 0, 'deletable': 0}

    class _RuntimeManager:
        def get(self, session_id: str):
            _ = session_id
            return None

        def remove(self, session_id: str):
            captured['removed_session'] = session_id
            return None

    async def _cancel_session_tasks(session_key: str) -> int:
        captured['cancelled_session'] = session_key
        return 0

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    monkeypatch.setattr(
        ceo_sessions,
        'get_agent',
        lambda: SimpleNamespace(
            sessions=manager,
            main_task_service=_TaskService(),
            cancel_session_tasks=_cancel_session_tasks,
        ),
    )
    monkeypatch.setattr(ceo_sessions, 'get_runtime_manager', lambda _agent: _RuntimeManager())
    monkeypatch.setattr(ceo_sessions, 'get_web_heartbeat_service', lambda _agent: None)

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)
    inflight_path = web_ceo_sessions.inflight_snapshot_path_for_session(target_id, create=False)
    return client, current, target_id, captured, inflight_path


def test_ceo_session_delete_check_accepts_inflight_only_session(tmp_path: Path, monkeypatch):
    client, _current, target_id, _captured, inflight_path = _build_inflight_only_ceo_session_delete_client(tmp_path, monkeypatch)

    response = client.get(f'/api/ceo/sessions/{target_id}/delete-check')

    assert response.status_code == 200
    payload = response.json()
    assert payload['session_id'] == target_id
    assert payload['can_delete'] is True
    assert payload['related_tasks']['total'] == 0
    assert inflight_path.exists()


def test_ceo_session_delete_accepts_inflight_only_session(tmp_path: Path, monkeypatch):
    client, current, target_id, captured, inflight_path = _build_inflight_only_ceo_session_delete_client(tmp_path, monkeypatch)

    response = client.delete(f'/api/ceo/sessions/{target_id}')

    assert response.status_code == 200
    payload = response.json()
    assert payload['deleted'] is True
    assert payload['session_id'] == target_id
    assert payload['active_session_id'] == current.key
    assert captured == {
        'removed_session': target_id,
        'cancelled_session': target_id,
    }
    assert not inflight_path.exists()


def test_ensure_runtime_session_restores_frontdoor_baseline_from_inflight_snapshot(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    from g3ku.runtime.api import ceo_sessions

    monkeypatch.setattr(web_ceo_sessions, 'workspace_path', lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, 'workspace_path', lambda: tmp_path)
    session = Session(key='web:ceo-inflight-restore', metadata={'title': 'Inflight Restore'})
    session_path = tmp_path / 'sessions' / 'web_ceo_inflight_restore.jsonl'
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')

    inflight_baseline = [
        {'role': 'system', 'content': 'SYSTEM'},
        {'role': 'user', 'content': 'restore from inflight'},
    ]
    web_ceo_sessions.write_inflight_turn_snapshot(
        session.key,
        {
            'status': 'running',
            'frontdoor_request_body_messages': list(inflight_baseline),
            'frontdoor_history_shrink_reason': 'stage_compaction',
        },
    )

    class _RuntimeManager:
        def get(self, _session_id: str):
            return None

        def get_or_create(self, **kwargs):
            return RuntimeAgentSession(
                SimpleNamespace(model='demo', reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
                session_key=kwargs['session_key'],
                channel=kwargs['channel'],
                chat_id=kwargs['chat_id'],
                memory_channel=kwargs.get('memory_channel'),
                memory_chat_id=kwargs.get('memory_chat_id'),
            )

    runtime_session = ceo_sessions._ensure_runtime_session(_RuntimeManager(), session)

    assert runtime_session._frontdoor_request_body_messages == inflight_baseline
    assert runtime_session._frontdoor_history_shrink_reason == 'stage_compaction'


def test_recreate_runtime_session_accepts_completed_continuity_only_session(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    from g3ku.runtime.api import ceo_sessions

    monkeypatch.setattr(web_ceo_sessions, 'workspace_path', lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, 'workspace_path', lambda: tmp_path)
    session = Session(key='web:ceo-continuity-only', metadata={'title': 'Continuity Restore'})
    continuity_baseline = [
        {'role': 'system', 'content': 'SYSTEM'},
        {'role': 'user', 'content': 'restore from continuity'},
    ]
    web_ceo_sessions.write_completed_continuity_snapshot(
        session.key,
        {
            'frontdoor_request_body_messages': list(continuity_baseline),
            'frontdoor_history_shrink_reason': 'stage_compaction',
            'source_reason': 'actual_request_sync',
        },
    )

    class _RuntimeManager:
        def get(self, _session_id: str):
            return None

        def get_or_create(self, **kwargs):
            return RuntimeAgentSession(
                SimpleNamespace(model='demo', reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
                session_key=kwargs['session_key'],
                channel=kwargs['channel'],
                chat_id=kwargs['chat_id'],
                memory_channel=kwargs.get('memory_channel'),
                memory_chat_id=kwargs.get('memory_chat_id'),
            )

    runtime_session = ceo_sessions._recreate_runtime_session(_RuntimeManager(), session)

    assert runtime_session is not None
    assert runtime_session._frontdoor_request_body_messages == continuity_baseline
    assert runtime_session._frontdoor_history_shrink_reason == 'stage_compaction'


def test_recreate_runtime_session_enriches_completed_continuity_only_restore_with_matching_actual_request_trace(
    tmp_path: Path,
    monkeypatch,
):
    _mock_ceo_catalog_config(monkeypatch)

    from g3ku.runtime.api import ceo_sessions

    monkeypatch.setattr(web_ceo_sessions, 'workspace_path', lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, 'workspace_path', lambda: tmp_path)
    session = Session(key='web:ceo-continuity-trace-enrichment', metadata={'title': 'Continuity Trace Enrichment'})
    continuity_baseline = [
        {'role': 'system', 'content': 'SYSTEM'},
        {'role': 'user', 'content': 'restore from continuity'},
    ]
    record = web_ceo_sessions.persist_frontdoor_actual_request(
        session.key,
        payload={
            'turn_id': 'turn-artifact',
            'request_messages': [
                {'role': 'system', 'content': 'SYSTEM'},
                {'role': 'user', 'content': 'restore from continuity'},
                {
                    'role': 'assistant',
                    'content': '## Runtime Tool Contract\nkind: frontdoor_runtime_tool_contract',
                },
            ],
            'prompt_cache_key_hash': 'family-artifact',
            'actual_request_hash': 'artifact-hash',
            'actual_request_message_count': 3,
            'actual_tool_schema_hash': 'tool-artifact',
            'provider_model': 'responses:gpt-test',
        },
    )
    web_ceo_sessions.write_completed_continuity_snapshot(
        session.key,
        {
            'frontdoor_request_body_messages': list(continuity_baseline),
            'frontdoor_history_shrink_reason': '',
            'frontdoor_actual_request_path': '',
            'frontdoor_actual_request_history': [],
            'hydrated_tool_names': ['exec'],
            'visible_tool_ids': ['exec'],
            'visible_skill_ids': ['writing-plans'],
            'provider_tool_schema_names': ['exec'],
            'capability_snapshot_exposure_revision': 'exp:continuity',
            'source_reason': 'actual_request_sync',
        },
    )

    class _RuntimeManager:
        def get(self, _session_id: str):
            return None

        def get_or_create(self, **kwargs):
            return RuntimeAgentSession(
                SimpleNamespace(model='demo', reasoning_effort=None, multi_agent_runner=None, sessions=SimpleNamespace()),
                session_key=kwargs['session_key'],
                channel=kwargs['channel'],
                chat_id=kwargs['chat_id'],
                memory_channel=kwargs.get('memory_channel'),
                memory_chat_id=kwargs.get('memory_chat_id'),
            )

    runtime_session = ceo_sessions._recreate_runtime_session(_RuntimeManager(), session)

    assert runtime_session is not None
    assert runtime_session._frontdoor_request_body_messages == continuity_baseline
    assert runtime_session._frontdoor_restore_source == 'completed_continuity'
    assert runtime_session._frontdoor_actual_request_path == record['path']
    assert runtime_session._frontdoor_actual_request_history == [record]
    assert runtime_session._frontdoor_prompt_cache_key_hash == 'family-artifact'
    assert runtime_session._frontdoor_actual_request_hash == 'artifact-hash'
    assert runtime_session._frontdoor_actual_request_message_count == 3
    assert runtime_session._frontdoor_actual_tool_schema_hash == 'tool-artifact'


def _build_channel_ceo_session_delete_client(tmp_path: Path, monkeypatch, *, persist_channel_session: bool = False):
    _mock_ceo_catalog_config(monkeypatch)

    from g3ku.runtime.api import ceo_sessions

    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path], extra_keys: list[str]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)
            self._extra_keys = list(extra_keys)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def invalidate(self, key: str) -> None:
            self._sessions.pop(key, None)
            self._paths.pop(key, None)

        def list_sessions(self) -> list[dict[str, str]]:
            keys = list(self._sessions) + [key for key in self._extra_keys if key not in self._sessions]
            return [{'key': key} for key in keys]

    class _TaskService:
        async def startup(self) -> None:
            return None

        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return [
                SimpleNamespace(task_id='task:channel-done', title='Channel Done', status='success', is_paused=False),
                SimpleNamespace(task_id='task:channel-running', title='Channel Running', status='in_progress', is_paused=False),
            ]

        def get_session_task_counts(self, session_id: str) -> dict[str, int]:
            _ = session_id
            return {
                'total': 2,
                'unfinished': 1,
                'in_progress': 1,
                'paused': 0,
                'terminal': 1,
                'deletable': 1,
            }

        async def delete_task_records_for_session(self, session_id: str) -> int:
            captured['deleted_task_session'] = session_id
            return 2

    class _RuntimeManager:
        def get(self, session_id: str):
            _ = session_id
            return None

        def remove(self, session_id: str):
            captured['removed_session'] = session_id
            return None

    class _Heartbeat:
        def clear_session(self, session_id: str) -> None:
            captured['heartbeat_cleared'] = session_id

    async def _cancel_session_tasks(session_key: str) -> int:
        captured['cancelled_session'] = session_key
        return 0

    current = Session(key='web:ceo-channel-clear-fallback', metadata={'title': 'Fallback Local'})
    current_path = tmp_path / 'sessions' / 'web_ceo_channel_clear_fallback.jsonl'
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    channel_id = 'china:qqbot:default:dm:user-42'
    channel_session = Session(
        key=channel_id,
        metadata={'title': 'QQ Channel'},
        messages=[
            {'role': 'user', 'content': 'old channel question'},
            {'role': 'assistant', 'content': 'old channel answer'},
        ],
    )
    channel_path = tmp_path / 'sessions' / 'china_qqbot_default_dm_user-42.jsonl'
    if persist_channel_session:
        channel_path.write_text(
            '\n'.join(
                [
                    json.dumps(
                        {
                            '_type': 'metadata',
                            'key': channel_id,
                            'created_at': datetime.now().isoformat(),
                            'updated_at': datetime.now().isoformat(),
                            'metadata': channel_session.metadata,
                            'last_user_turn_at': None,
                            'commit_turn_counter': 0,
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(channel_session.messages[0], ensure_ascii=False),
                    json.dumps(channel_session.messages[1], ensure_ascii=False),
                ]
            )
            + '\n',
            encoding='utf-8',
        )
        sessions = [current, channel_session]
        paths = {current.key: current_path, channel_id: channel_path}
    else:
        sessions = [current]
        paths = {current.key: current_path, channel_id: channel_path}
    manager = _SessionManager(sessions, paths, [channel_id])
    captured: dict[str, str] = {}

    monkeypatch.setattr(web_ceo_sessions, 'workspace_path', lambda: tmp_path)
    monkeypatch.setattr(ceo_sessions, 'workspace_path', lambda: tmp_path)
    monkeypatch.setattr(
        ceo_sessions,
        '_build_catalog',
        lambda _session_manager, _runtime_manager, *, active_session_id: {
            'items': [{'session_id': current.key, 'title': 'Fallback Local', 'session_family': 'local'}],
            'channel_groups': [{
                'channel_id': 'qqbot',
                'label': 'QQ Bot',
                'items': [{
                    'session_id': channel_id,
                    'title': 'QQ Channel',
                    'session_family': 'channel',
                    'session_origin': 'china',
                    'channel_id': 'qqbot',
                    'is_readonly': True,
                }],
            }],
            'active_session_family': 'channel' if active_session_id == channel_id else 'local',
        },
    )

    web_ceo_sessions.write_inflight_turn_snapshot(
        channel_id,
        {
            'status': 'running',
            'started_at': datetime.now().isoformat(),
            'user_message': {'role': 'user', 'content': 'clear this channel session'},
        },
    )
    web_ceo_sessions.write_paused_execution_context(
        channel_id,
        {
            'status': 'paused',
            'started_at': datetime.now().isoformat(),
            'interrupts': [],
        },
    )
    upload_dir = web_ceo_sessions.upload_dir_for_session(channel_id)
    (upload_dir / 'artifact.txt').write_text('channel artifact', encoding='utf-8')

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    monkeypatch.setattr(
        ceo_sessions,
        'get_agent',
        lambda: SimpleNamespace(
            sessions=manager,
            main_task_service=_TaskService(),
            cancel_session_tasks=_cancel_session_tasks,
        ),
    )
    monkeypatch.setattr(ceo_sessions, 'get_runtime_manager', lambda _agent: _RuntimeManager())
    monkeypatch.setattr(ceo_sessions, 'get_web_heartbeat_service', lambda _agent: _Heartbeat())

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(channel_id)
    client = TestClient(app)
    inflight_path = web_ceo_sessions.inflight_snapshot_path_for_session(channel_id, create=False)
    paused_path = web_ceo_sessions.paused_execution_context_path_for_session(channel_id, create=False)
    return SimpleNamespace(
        client=client,
        current=current,
        channel_id=channel_id,
        captured=captured,
        inflight_path=inflight_path,
        paused_path=paused_path,
        upload_dir=upload_dir,
        session_manager=manager,
        channel_path=channel_path,
    )


def test_channel_ceo_session_delete_check_returns_clear_payload(tmp_path: Path, monkeypatch):
    state = _build_channel_ceo_session_delete_client(tmp_path, monkeypatch)

    response = state.client.get(f'/api/ceo/sessions/{state.channel_id}/delete-check')

    assert response.status_code == 200
    payload = response.json()
    assert payload['session_id'] == state.channel_id
    assert payload['can_delete'] is True
    assert payload['related_tasks']['deletable'] == 1
    assert [item['task_id'] for item in payload['usage']['completed_tasks']] == ['task:channel-done']
    assert [item['task_id'] for item in payload['usage']['in_progress_tasks']] == ['task:channel-running']
    assert state.inflight_path.exists()
    assert state.paused_path.exists()
    assert state.upload_dir.exists()


def test_channel_ceo_session_delete_clears_context_without_deleting_channel_entry(tmp_path: Path, monkeypatch):
    state = _build_channel_ceo_session_delete_client(tmp_path, monkeypatch)

    response = state.client.request(
        'DELETE',
        f'/api/ceo/sessions/{state.channel_id}',
        json={'delete_task_records': True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['deleted'] is False
    assert payload['cleared'] is True
    assert payload['session_id'] == state.channel_id
    assert payload['deleted_task_count'] == 2
    assert payload['active_session_id'] == state.channel_id
    channel_items = payload['channel_groups'][0]['items']
    assert [item['session_id'] for item in channel_items] == [state.channel_id]
    assert state.captured == {
        'heartbeat_cleared': state.channel_id,
        'deleted_task_session': state.channel_id,
        'removed_session': state.channel_id,
        'cancelled_session': state.channel_id,
    }
    assert not state.inflight_path.exists()
    assert not state.paused_path.exists()
    assert not state.upload_dir.exists()


def test_channel_ceo_session_delete_removes_persisted_transcript_and_cached_session(tmp_path: Path, monkeypatch):
    state = _build_channel_ceo_session_delete_client(tmp_path, monkeypatch, persist_channel_session=True)

    assert state.channel_path.exists()
    assert state.channel_id in state.session_manager._sessions

    response = state.client.request(
        'DELETE',
        f'/api/ceo/sessions/{state.channel_id}',
        json={'delete_task_records': False},
    )

    assert response.status_code == 200
    assert not state.channel_path.exists()
    assert state.channel_id not in state.session_manager._sessions


def test_ceo_session_bulk_delete_check_aggregates_related_task_ids(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    from g3ku.runtime.api import ceo_sessions

    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    class _TaskService:
        async def startup(self) -> None:
            return None

        def list_tasks_for_session(self, session_id: str):
            if session_id == 'web:ceo-bulk-a':
                return [
                    SimpleNamespace(task_id='task:done-1', title='Done 1', status='success', is_paused=False),
                    SimpleNamespace(task_id='task:busy-1', title='Busy 1', status='in_progress', is_paused=False),
                ]
            if session_id == 'web:ceo-bulk-b':
                return [
                    SimpleNamespace(task_id='task:done-1', title='Done 1', status='success', is_paused=False),
                    SimpleNamespace(task_id='task:paused-2', title='Paused 2', status='in_progress', is_paused=True),
                ]
            return []

        def get_session_task_counts(self, session_id: str) -> dict[str, int]:
            if session_id == 'web:ceo-bulk-a':
                return {'total': 2, 'unfinished': 1, 'in_progress': 1, 'paused': 0, 'terminal': 1, 'deletable': 1}
            if session_id == 'web:ceo-bulk-b':
                return {'total': 2, 'unfinished': 0, 'in_progress': 0, 'paused': 1, 'terminal': 1, 'deletable': 2}
            return {'total': 0, 'unfinished': 0, 'in_progress': 0, 'paused': 0, 'terminal': 0, 'deletable': 0}

    current = Session(key='web:ceo-bulk-current', metadata={'title': 'Current'})
    first = Session(key='web:ceo-bulk-a', metadata={'title': 'Bulk A'})
    second = Session(key='web:ceo-bulk-b', metadata={'title': 'Bulk B'})
    base_dir = tmp_path / 'sessions'
    base_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for session in (current, first, second):
        path = base_dir / f"{session.key.replace(':', '_')}.jsonl"
        path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
        paths[session.key] = path
    manager = _SessionManager([current, first, second], paths)

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    monkeypatch.setattr(ceo_sessions, 'get_agent', lambda: SimpleNamespace(sessions=manager, main_task_service=_TaskService()))
    monkeypatch.setattr(ceo_sessions, 'get_runtime_manager', lambda _agent: SimpleNamespace(get=lambda _session_id: None))
    monkeypatch.setattr(ceo_sessions, 'workspace_path', lambda: tmp_path)

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    response = client.post(
        '/api/ceo/sessions/delete-check',
        json={'session_ids': [first.key, second.key]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['session_ids'] == [first.key, second.key]
    assert payload['related_tasks']['total'] == 3
    assert payload['related_tasks']['deletable'] == 2
    assert [item['task_id'] for item in payload['usage']['completed_tasks']] == ['task:done-1']
    assert [item['task_id'] for item in payload['usage']['paused_tasks']] == ['task:paused-2']
    assert [item['task_id'] for item in payload['usage']['in_progress_tasks']] == ['task:busy-1']


def test_ceo_session_bulk_delete_executes_selected_sessions_in_one_request(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    from g3ku.runtime.api import ceo_sessions

    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def invalidate(self, key: str) -> None:
            self._sessions.pop(key, None)
            self._paths.pop(key, None)

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    class _TaskService:
        async def startup(self) -> None:
            return None

        async def delete_task_records_for_session(self, session_id: str) -> int:
            return {'web:ceo-bulk-a': 1, 'web:ceo-bulk-b': 2}.get(session_id, 0)

        def list_tasks_for_session(self, session_id: str):
            _ = session_id
            return []

        def get_session_task_counts(self, session_id: str) -> dict[str, int]:
            _ = session_id
            return {'total': 0, 'unfinished': 0, 'in_progress': 0, 'paused': 0, 'terminal': 0, 'deletable': 0}

    current = Session(key='web:ceo-bulk-current', metadata={'title': 'Current'})
    first = Session(key='web:ceo-bulk-a', metadata={'title': 'Bulk A'})
    second = Session(key='web:ceo-bulk-b', metadata={'title': 'Bulk B'})
    base_dir = tmp_path / 'sessions'
    base_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for session in (current, first, second):
        path = base_dir / f"{session.key.replace(':', '_')}.jsonl"
        path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
        paths[session.key] = path
    manager = _SessionManager([current, first, second], paths)
    captured: dict[str, object] = {'removed': [], 'cancelled': []}

    class _RuntimeManager:
        def get(self, session_id: str):
            _ = session_id
            return None

        def remove(self, session_id: str):
            captured['removed'].append(session_id)
            return None

    async def _cancel_session_tasks(session_key: str) -> int:
        captured['cancelled'].append(session_key)
        return 0

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    monkeypatch.setattr(
        ceo_sessions,
        'get_agent',
        lambda: SimpleNamespace(
            sessions=manager,
            main_task_service=_TaskService(),
            cancel_session_tasks=_cancel_session_tasks,
        ),
    )
    monkeypatch.setattr(ceo_sessions, 'get_runtime_manager', lambda _agent: _RuntimeManager())
    monkeypatch.setattr(ceo_sessions, 'get_web_heartbeat_service', lambda _agent: None)
    monkeypatch.setattr(ceo_sessions, 'workspace_path', lambda: tmp_path)

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    response = client.post(
        '/api/ceo/sessions/bulk-delete',
        json={'session_ids': [first.key, second.key], 'delete_task_records': True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['deleted_count'] == 2
    assert payload['failed_count'] == 0
    assert [item['session_id'] for item in payload['results']] == [first.key, second.key]
    assert [item['deleted_task_count'] for item in payload['results']] == [1, 2]
    assert captured == {
        'removed': [first.key, second.key],
        'cancelled': [first.key, second.key],
    }


@pytest.mark.asyncio
async def test_ceo_session_delete_with_task_record_cleanup_deletes_task_disk_footprint(tmp_path: Path, monkeypatch):
    _mock_ceo_catalog_config(monkeypatch)

    from g3ku.runtime.api import ceo_sessions

    class _SessionManager:
        def __init__(self, sessions: list[Session], paths: dict[str, Path]):
            self._sessions = {session.key: session for session in sessions}
            self._paths = dict(paths)

        def get_path(self, key: str) -> Path:
            return self._paths[key]

        def get_or_create(self, key: str):
            return self._sessions[key]

        def save(self, session) -> None:
            self._sessions[session.key] = session

        def invalidate(self, key: str) -> None:
            self._sessions.pop(key, None)
            self._paths.pop(key, None)

        def list_sessions(self) -> list[dict[str, str]]:
            return [{'key': key} for key in self._sessions]

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=tmp_path / 'runtime.sqlite3',
        files_base_dir=tmp_path / 'tasks',
        artifact_dir=tmp_path / 'artifacts',
        governance_store_path=tmp_path / 'governance.sqlite3',
        execution_mode='web',
    )
    current = Session(key='web:ceo-delete-cascade', metadata={'title': 'Delete Cascade'})
    other = Session(key='web:ceo-keep-after-cascade', metadata={'title': 'Keep After Cascade'})
    current_path = tmp_path / 'sessions' / 'web_ceo_delete_cascade.jsonl'
    other_path = tmp_path / 'sessions' / 'web_ceo_keep_after_cascade.jsonl'
    current_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    other_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')
    manager = _SessionManager(
        [current, other],
        {
            current.key: current_path,
            other.key: other_path,
        },
    )
    captured: dict[str, object] = {}

    service.store.upsert_worker_status(
        worker_id='worker:test',
        role='task_worker',
        status='running',
        updated_at=now_iso(),
        payload={'execution_mode': 'worker', 'active_task_count': 0},
    )
    record = await service.create_task('delete cascade task', session_id=current.key)
    task_file_dir = service.file_store.task_dir(record.task_id)
    artifact_dir = Path(service.artifact_store._task_dir(record.task_id))
    task_temp_dir = service._task_temp_dir(record.task_id)
    event_dir = Path(service.store._event_history_dir) / service._safe_task_dir_name(record.task_id)
    task_file_dir.joinpath('node.json').write_text('A', encoding='utf-8')
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.joinpath('artifact.txt').write_text('B', encoding='utf-8')
    task_temp_dir.joinpath('temp.txt').write_text('C', encoding='utf-8')
    event_dir.mkdir(parents=True, exist_ok=True)
    event_dir.joinpath('1.json').write_text('D', encoding='utf-8')

    class _RuntimeManager:
        def get(self, session_id: str):
            _ = session_id
            return None

        def remove(self, session_id: str):
            captured['removed_session'] = session_id
            return None

    async def _cancel_session_tasks(session_key: str) -> int:
        captured['cancelled_session'] = session_key
        return 0

    app = FastAPI()
    app.include_router(ceo_sessions.router, prefix='/api')
    monkeypatch.setattr(
        ceo_sessions,
        'get_agent',
        lambda: SimpleNamespace(
            sessions=manager,
            main_task_service=service,
            cancel_session_tasks=_cancel_session_tasks,
        ),
    )
    monkeypatch.setattr(ceo_sessions, 'get_runtime_manager', lambda _agent: _RuntimeManager())
    monkeypatch.setattr(ceo_sessions, 'get_web_heartbeat_service', lambda _agent: None)
    monkeypatch.setattr(ceo_sessions, 'workspace_path', lambda: tmp_path)

    ceo_sessions.WebCeoStateStore(tmp_path).set_active_session_id(current.key)
    client = TestClient(app)

    try:
        response = client.request(
            'DELETE',
            f'/api/ceo/sessions/{current.key}',
            json={'delete_task_records': True},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload['deleted'] is True
        assert payload['deleted_task_count'] == 1
        assert captured == {
            'removed_session': current.key,
            'cancelled_session': current.key,
        }
        assert service.get_task(record.task_id) is None
        assert not task_file_dir.exists()
        assert not artifact_dir.exists()
        assert not task_temp_dir.exists()
        assert not event_dir.exists()
    finally:
        await service.close()


def test_task_rest_endpoint_normalizes_short_task_id():
    captured: dict[str, str] = {}

    class _StubService:
        async def startup(self) -> None:
            return None

        def normalize_task_id(self, task_id: str) -> str:
            captured['normalized_from'] = task_id
            return f'task:{task_id}'

        def get_task_detail_payload(self, task_id: str, *, mark_read: bool = False):
            captured['detail_task_id'] = task_id
            return {'task': {'task_id': task_id}, 'summary': {'task_id': task_id}}

    from main.api import rest as task_rest

    app = FastAPI()
    app.include_router(task_rest.router, prefix='/api')
    task_rest.get_agent = lambda: SimpleNamespace(main_task_service=_StubService())

    client = TestClient(app)
    response = client.get('/api/tasks/demo')

    assert response.status_code == 200
    assert captured == {'normalized_from': 'demo', 'detail_task_id': 'task:demo'}
    assert response.json()['task']['task_id'] == 'task:demo'


def test_task_bulk_delete_rest_endpoint_normalizes_each_task_id():
    captured: dict[str, object] = {}

    class _StubService:
        async def startup(self) -> None:
            return None

        def normalize_task_id(self, task_id: str) -> str:
            return f'task:{task_id}' if ':' not in str(task_id or '') else str(task_id or '')

        async def bulk_delete_tasks(self, task_ids: list[str]):
            captured['task_ids'] = list(task_ids)
            return {
                'items': [
                    {'task_id': task_id, 'result': 'deleted'}
                    for task_id in list(task_ids)
                ],
            }

    from main.api import rest as task_rest

    app = FastAPI()
    app.include_router(task_rest.router, prefix='/api')
    task_rest.get_agent = lambda: SimpleNamespace(main_task_service=_StubService())

    client = TestClient(app)
    response = client.post('/api/tasks/bulk-delete', json={'task_ids': ['demo', 'task:done']})

    assert response.status_code == 200
    payload = response.json()
    assert captured['task_ids'] == ['task:demo', 'task:done']
    assert [item['task_id'] for item in payload['items']] == ['task:demo', 'task:done']
    assert [item['result'] for item in payload['items']] == ['deleted', 'deleted']


def test_task_retry_rest_endpoint_is_removed():
    from main.api import rest as task_rest

    app = FastAPI()
    app.include_router(task_rest.router, prefix='/api')

    client = TestClient(app)
    response = client.post('/api/tasks/demo/retry')

    assert response.status_code == 404


def test_task_retry_rest_endpoint_returns_conflict_for_non_failed_task():
    class _StubService:
        def normalize_task_id(self, task_id: str) -> str:
            return f'task:{task_id}'

        async def retry_task(self, task_id: str):
            _ = task_id
            raise ValueError('task_not_failed')

    from main.api import rest as task_rest

    app = FastAPI()
    app.include_router(task_rest.router, prefix='/api')
    task_rest.get_agent = lambda: SimpleNamespace(main_task_service=_StubService())

    client = TestClient(app)
    response = client.post('/api/tasks/demo/retry')

    assert response.status_code == 404


def test_task_retry_rest_endpoint_returns_conflict_for_non_retryable_task():
    class _StubService:
        def normalize_task_id(self, task_id: str) -> str:
            return f'task:{task_id}'

        async def retry_task(self, task_id: str):
            _ = task_id
            raise ValueError('task_not_retryable')

    from main.api import rest as task_rest

    app = FastAPI()
    app.include_router(task_rest.router, prefix='/api')
    task_rest.get_agent = lambda: SimpleNamespace(main_task_service=_StubService())

    client = TestClient(app)
    response = client.post('/api/tasks/demo/retry')

    assert response.status_code == 404


def test_task_continue_evaluate_rest_endpoint_is_removed():
    from main.api import rest as task_rest

    app = FastAPI()
    app.include_router(task_rest.router, prefix='/api')

    client = TestClient(app)
    response = client.post('/api/tasks/demo/continue-evaluate')

    assert response.status_code == 404


def test_task_continue_rest_endpoint_is_removed():
    from main.api import rest as task_rest

    app = FastAPI()
    app.include_router(task_rest.router, prefix='/api')

    client = TestClient(app)
    response = client.post(
        '/api/tasks/demo/continue',
        json={
            'mode': 'retry_in_place',
            'continuation_instruction': 'retry safely in place',
            'reason': 'manual_retry',
        },
    )

    assert response.status_code == 404


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
            'agents': {'defaults': {'workspace': '.', 'runtime': 'langgraph', 'maxTokens': 1, 'temperature': 0.1, 'maxToolIterations': 1, 'memoryWindow': 1, 'reasoningEffort': 'low'}, 'roleIterations': {'ceo': 40, 'execution': 16, 'inspection': 16}, 'roleConcurrency': {'ceo': None, 'execution': None, 'inspection': None}, 'multiAgent': {'orchestratorModelKey': None}},
            'models': {'catalog': [{'key': 'm', 'providerModel': 'openai:gpt-4.1', 'apiKey': '', 'apiBase': None, 'extraHeaders': None, 'enabled': True, 'maxTokens': 1, 'temperature': 0.1, 'reasoningEffort': 'low', 'retryOn': [], 'description': ''}], 'roles': {'ceo': ['m'], 'execution': ['m'], 'inspection': ['m']}},
            'providers': {},
            'web': {'host': '127.0.0.1', 'port': 1},
            'tools': {'exec': {'timeout': 10}},
            'resources': {'enabled': True, 'skillsDir': 'skills', 'toolsDir': 'tools', 'manifestName': 'resource.yaml', 'reload': {'enabled': True, 'pollIntervalMs': 1000, 'debounceMs': 400, 'lazyReloadOnAccess': True, 'keepLastGoodVersion': True}, 'locks': {'lockDir': '.g3ku/resource-locks', 'logicalDeleteGuard': True, 'windowsFsLock': True}, 'statePath': '.g3ku/resources.state.json'},
            'mainRuntime': {'enabled': True, 'storePath': '.g3ku/main-runtime/runtime.sqlite3', 'filesBaseDir': '.g3ku/main-runtime/tasks', 'artifactDir': '.g3ku/main-runtime/artifacts', 'governanceStorePath': '.g3ku/main-runtime/governance.sqlite3', 'defaultMaxDepth': 1, 'hardMaxDepth': 4, 'nodeDispatchConcurrency': {'execution': 8, 'inspection': 4}},
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


def test_admin_memory_queue_and_processed_endpoints_return_paged_payloads(monkeypatch):
    class _StubMemoryManager:
        async def list_queue_page(self, *, limit: int, offset: int):
            assert limit == 2
            assert offset == 1
            return {
                'items': [
                    {
                        'request_id': 'write_2',
                        'op': 'write',
                        'status': 'processing',
                        'created_at': '2026-04-17T10:00:00+08:00',
                        'processing_started_at': '2026-04-17T10:00:02+08:00',
                        'last_error_text': '',
                    }
                ],
                'total': 3,
                'has_more': False,
            }

        async def list_processed_page(self, *, limit: int, offset: int):
            assert limit == 2
            assert offset == 1
            return {
                'items': [
                    {
                        'batch_id': 'batch_1',
                        'op': 'write',
                        'processed_at': '2026-04-17T10:00:10+08:00',
                        'usage': {
                            'input_tokens': 8,
                            'output_tokens': 3,
                            'cache_read_tokens': 2,
                        },
                    }
                ],
                'total': 4,
                'has_more': True,
            }

    monkeypatch.setattr(admin_rest, '_runtime_memory_manager', lambda: _StubMemoryManager())
    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    queue_response = client.get('/api/memory/queue?limit=2&offset=1')
    processed_response = client.get('/api/memory/processed?limit=2&offset=1')

    assert queue_response.status_code == 200
    assert queue_response.json() == {
        'ok': True,
        'items': [
            {
                'request_id': 'write_2',
                'op': 'write',
                'status': 'processing',
                'created_at': '2026-04-17T10:00:00+08:00',
                'processing_started_at': '2026-04-17T10:00:02+08:00',
                'last_error_text': '',
            }
        ],
        'total': 3,
        'has_more': False,
    }

    assert processed_response.status_code == 200
    assert processed_response.json() == {
        'ok': True,
        'items': [
            {
                'batch_id': 'batch_1',
                'op': 'write',
                'processed_at': '2026-04-17T10:00:10+08:00',
                'usage': {
                    'input_tokens': 8,
                    'output_tokens': 3,
                    'cache_read_tokens': 2,
                },
            }
        ],
        'total': 4,
        'has_more': True,
    }


def test_memory_admin_mutations_are_disabled_without_feature_flag(monkeypatch):
    class _StubMemoryManager:
        pass

    monkeypatch.delenv('G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS', raising=False)
    monkeypatch.setattr(admin_rest, '_runtime_memory_manager', lambda: _StubMemoryManager())

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post('/api/memory/admin/retry-head', json={'reason': 'manual'})

    assert response.status_code == 403
    assert response.json() == {
        'detail': {
            'code': 'memory_admin_mutation_disabled',
            'message': 'memory admin mutations are disabled',
        }
    }


def test_memory_admin_mutation_retry_head_writes_audit_record(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    (workspace / 'memory').mkdir(parents=True, exist_ok=True)

    class _StubMemoryManager:
        def __init__(self):
            self.workspace = workspace
            self._rows = [
                {
                    'request_id': 'write_1',
                    'op': 'write',
                    'status': 'processing',
                    'retry_after': '2026-04-17T22:00:00+08:00',
                    'last_error_text': 'temporary error',
                }
            ]

        def _read_queue_requests(self):
            return [dict(item) for item in self._rows]

        def _write_queue_requests(self, rows):
            self._rows = [dict(item) for item in rows]

    manager = _StubMemoryManager()
    monkeypatch.setenv('G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS', '1')
    monkeypatch.setattr(admin_rest, '_runtime_memory_manager', lambda: manager)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post(
        '/api/memory/admin/retry-head',
        json={'reason': 'manual_retry'},
        headers={'x-request-id': 'req-memory-admin-1'},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['request_id'] == 'write_1'
    assert payload['item']['retry_after_cleared'] is True
    assert manager._rows[0]['retry_after'] == ''

    audit_file = workspace / 'memory' / 'admin_audit.jsonl'
    assert audit_file.exists() is True
    lines = [line for line in audit_file.read_text(encoding='utf-8').splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['action'] == 'retry_head'
    assert record['reason'] == 'manual_retry'
    assert record['request_id'] == 'req-memory-admin-1'
    assert record['queue_head_request_id'] == 'write_1'
    assert record['result'] == 'ok'


def test_memory_admin_mutation_retry_head_rolls_back_when_audit_write_fails(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    (workspace / 'memory').mkdir(parents=True, exist_ok=True)

    class _StubMemoryManager:
        def __init__(self):
            import threading

            self.workspace = workspace
            self._io_lock = threading.RLock()
            self._rows = [
                {
                    'request_id': 'write_1',
                    'op': 'write',
                    'status': 'processing',
                    'retry_after': '2026-04-17T22:00:00+08:00',
                    'last_error_text': 'temporary error',
                }
            ]

        def _read_queue_requests(self):
            return [dict(item) for item in self._rows]

        def _write_queue_requests(self, rows):
            self._rows = [dict(item) for item in rows]

    manager = _StubMemoryManager()
    monkeypatch.setenv('G3KU_ENABLE_MEMORY_ADMIN_MUTATIONS', '1')
    monkeypatch.setattr(admin_rest, '_runtime_memory_manager', lambda: manager)

    def _raise_audit_error(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(admin_rest, '_append_memory_admin_audit_event', _raise_audit_error)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post(
        '/api/memory/admin/retry-head',
        json={'reason': 'manual_retry'},
        headers={'x-request-id': 'req-memory-admin-rollback'},
    )

    assert response.status_code == 503
    assert response.json() == {
        'detail': {
            'code': 'memory_admin_audit_failed',
            'message': 'memory admin audit write failed',
        }
    }
    assert manager._rows[0]['retry_after'] == '2026-04-17T22:00:00+08:00'


def _write_runtime_config(workspace: Path) -> None:
    (workspace / '.g3ku').mkdir(parents=True, exist_ok=True)
    (workspace / '.g3ku' / 'config.json').write_text(
        json.dumps({
            'agents': {'defaults': {'workspace': '.', 'runtime': 'langgraph', 'maxTokens': 1, 'temperature': 0.1, 'maxToolIterations': 1, 'memoryWindow': 1, 'reasoningEffort': 'low'}, 'roleIterations': {'ceo': 40, 'execution': 16, 'inspection': 16}, 'multiAgent': {'orchestratorModelKey': None}},
            'models': {'catalog': [{'key': 'm', 'providerModel': 'openai:gpt-4.1', 'apiKey': 'demo-key', 'apiBase': None, 'extraHeaders': None, 'enabled': True, 'maxTokens': 1, 'temperature': 0.1, 'reasoningEffort': 'low', 'retryOn': [], 'description': '', 'contextWindowTokens': 128000}], 'roles': {'ceo': ['m'], 'execution': ['m'], 'inspection': ['m']}},
            'providers': {'openai': {'apiKey': '', 'apiBase': None, 'extraHeaders': None}},
            'web': {'host': '127.0.0.1', 'port': 1},
            'toolSecrets': {},
            'resources': {'enabled': True, 'skillsDir': 'skills', 'toolsDir': 'tools', 'manifestName': 'resource.yaml', 'reload': {'enabled': True, 'pollIntervalMs': 1000, 'debounceMs': 400, 'lazyReloadOnAccess': True, 'keepLastGoodVersion': True}, 'locks': {'lockDir': '.g3ku/resource-locks', 'logicalDeleteGuard': True, 'windowsFsLock': True}, 'statePath': '.g3ku/resources.state.json'},
            'mainRuntime': {'enabled': True, 'storePath': '.g3ku/main-runtime/runtime.sqlite3', 'filesBaseDir': '.g3ku/main-runtime/tasks', 'artifactDir': '.g3ku/main-runtime/artifacts', 'governanceStorePath': '.g3ku/main-runtime/governance.sqlite3', 'defaultMaxDepth': 1, 'hardMaxDepth': 4, 'nodeDispatchConcurrency': {'execution': 8, 'inspection': 4}},
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
                    'wecom-app': {'enabled': False, 'accounts': {}},
                    'wecom-kf': {'enabled': False, 'accounts': {}},
                    'wechat-mp': {'enabled': False, 'accounts': {}},
                    'feishu-china': {'enabled': False, 'accounts': {}},
                },
            },
        }),
        encoding='utf-8',
    )


def test_models_endpoint_returns_role_iterations(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.get('/api/models')

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['role_iterations'] == {'ceo': 40, 'execution': 16, 'inspection': 16, 'memory': None}
    assert payload['role_concurrency'] == {'ceo': None, 'execution': None, 'inspection': None, 'memory': 1}
    assert payload['items'][0]['context_window_tokens'] == 128000
    assert payload['items'][0]['image_multimodal_enabled'] is False


def test_model_retry_count_update_persists_and_refreshes_runtime(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/models/m', json={'retryCount': 3})

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['retry_count'] == 3
    assert captured == {'force': True, 'reason': 'admin_model_update'}

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['catalog'][0]['retryCount'] == 3


def test_model_context_window_update_persists_and_reads_back(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)
    monkeypatch.setattr(
        model_manager.ModelManager.load().facade.config_service,
        'probe_draft',
        lambda draft: SimpleNamespace(success=True, status=ProbeStatus.SUCCESS, message='ok'),
    )

    original_load = model_manager.ModelManager.load

    def _patched_load():
        manager = original_load()
        monkeypatch.setattr(
            manager.facade.config_service,
            'probe_draft',
            lambda draft: SimpleNamespace(success=True, status=ProbeStatus.SUCCESS, message='ok'),
        )
        return manager

    monkeypatch.setattr(model_manager.ModelManager, 'load', staticmethod(_patched_load))

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/models/m', json={'contextWindowTokens': 196000})

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['context_window_tokens'] == 196000
    assert captured == {'force': True, 'reason': 'admin_model_update'}

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['catalog'][0]['contextWindowTokens'] == 196000


def test_model_image_multimodal_update_persists_and_reads_back(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)
    monkeypatch.setattr(
        model_manager.ModelManager.load().facade.config_service,
        'probe_draft',
        lambda draft: SimpleNamespace(success=True, status=ProbeStatus.SUCCESS, message='ok'),
    )

    original_load = model_manager.ModelManager.load

    def _patched_load():
        manager = original_load()
        monkeypatch.setattr(
            manager.facade.config_service,
            'probe_draft',
            lambda draft: SimpleNamespace(success=True, status=ProbeStatus.SUCCESS, message='ok'),
        )
        return manager

    monkeypatch.setattr(model_manager.ModelManager, 'load', staticmethod(_patched_load))

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/models/m', json={'imageMultimodalEnabled': True})

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['image_multimodal_enabled'] is True
    assert captured == {'force': True, 'reason': 'admin_model_update'}

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['catalog'][0]['imageMultimodalEnabled'] is True


def test_model_update_returns_503_when_worker_runtime_refresh_ack_fails(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        _ = force, reason
        return True

    class _StubService:
        execution_mode = 'web'

        def is_worker_online(self, **kwargs) -> bool:
            _ = kwargs
            return True

        async def request_worker_runtime_refresh(self, *, reason: str):
            raise TimeoutError(f'{reason}:worker_runtime_refresh_timeout')

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)
    monkeypatch.setattr(admin_rest, '_service', lambda: _StubService())

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/models/m', json={'retryCount': 4})

    assert response.status_code == 503
    detail = response.json()['detail']
    assert detail['code'] == 'worker_runtime_refresh_failed'
    assert detail['saved'] is True
    assert detail['web_refreshed'] is True
    assert detail['worker_refresh_acked'] is False

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['catalog'][0]['retryCount'] == 4


def test_llm_config_update_refreshes_runtime(monkeypatch):
    captured: dict[str, object] = {}

    class _StubFacade:
        def update_config_record(self, config_id: str, payload: dict):
            captured['config_id'] = config_id
            captured['payload'] = dict(payload)
            return {'config_id': config_id, 'provider_id': 'responses'}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest.ModelManager, 'load_facade', classmethod(lambda cls: _StubFacade()))
    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/llm/configs/cfg-1', json={'default_model': 'gpt-5.2'})

    assert response.status_code == 200
    assert response.json()['item']['config_id'] == 'cfg-1'
    assert captured['config_id'] == 'cfg-1'
    assert captured['payload'] == {'default_model': 'gpt-5.2'}
    assert captured['force'] is True
    assert captured['reason'] == 'admin_llm_config_update'


def test_llm_config_update_returns_async_runtime_refresh_status_after_save(monkeypatch):
    captured: dict[str, object] = {}

    class _StubFacade:
        def update_config_record(self, config_id: str, payload: dict):
            captured['config_id'] = config_id
            captured['payload'] = dict(payload)
            return {'config_id': config_id, 'provider_id': 'responses'}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    class _StubService:
        execution_mode = 'web'

        def is_worker_online(self, **kwargs):
            _ = kwargs
            return True

        def enqueue_worker_runtime_refresh(self, *, reason: str):
            return {
                'worker_refresh_requested': True,
                'worker_refresh_acked': False,
                'worker_refresh_command_id': 'command:refresh-config-1',
                'worker_refresh_status': 'pending',
                'reason': reason,
            }

    monkeypatch.setattr(admin_rest.ModelManager, 'load_facade', classmethod(lambda cls: _StubFacade()))
    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)
    monkeypatch.setattr(admin_rest, '_service', lambda: _StubService())

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/llm/configs/cfg-1', json={'default_model': 'gpt-5.2'})

    assert response.status_code == 200
    payload = response.json()
    assert payload['item']['config_id'] == 'cfg-1'
    assert payload['runtime_refresh'] == {
        'saved': True,
        'web_refreshed': True,
        'worker_refresh_requested': True,
        'worker_refresh_acked': False,
        'worker_refresh_command_id': 'command:refresh-config-1',
        'worker_refresh_status': 'pending',
        'reason': 'admin_llm_config_update',
    }


def test_llm_draft_validate_does_not_require_loading_runtime_config(monkeypatch):
    captured: dict[str, object] = {}

    class _StubFacade:
        def validate_draft(self, payload: dict):
            captured['payload'] = dict(payload)
            return {'valid': True, 'errors': [], 'normalized_preview': {'provider_id': 'custom'}}

    def _unexpected_load(_cls):
        raise AssertionError('validate draft should not load full runtime config')

    monkeypatch.setattr(admin_rest.ModelManager, 'load', classmethod(_unexpected_load))
    monkeypatch.setattr(admin_rest.ModelManager, 'load_facade', classmethod(lambda cls: _StubFacade()))

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post('/api/llm/drafts/validate', json={'provider_id': 'custom'})

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['result']['valid'] is True
    assert captured['payload'] == {'provider_id': 'custom'}


def test_llm_binding_retry_count_update_persists_without_provider_probe(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/llm/bindings/m', json={'retry_count': 4})

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['retry_count'] == 4
    assert captured == {'force': True, 'reason': 'admin_llm_binding_update'}

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['catalog'][0]['retryCount'] == 4


def test_llm_binding_image_multimodal_update_persists_without_provider_probe(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/llm/bindings/m', json={'image_multimodal_enabled': True})

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['image_multimodal_enabled'] is True
    assert captured == {'force': True, 'reason': 'admin_llm_binding_update'}

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['catalog'][0]['imageMultimodalEnabled'] is True


def test_llm_binding_update_returns_async_runtime_refresh_status_after_save(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    class _StubService:
        execution_mode = 'web'

        def is_worker_online(self, **kwargs):
            _ = kwargs
            return True

        def enqueue_worker_runtime_refresh(self, *, reason: str):
            return {
                'worker_refresh_requested': True,
                'worker_refresh_acked': False,
                'worker_refresh_command_id': 'command:refresh-1',
                'worker_refresh_status': 'pending',
                'reason': reason,
            }

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)
    monkeypatch.setattr(admin_rest, '_service', lambda: _StubService())

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/llm/bindings/m', json={'retry_count': 4})

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['retry_count'] == 4
    assert payload['runtime_refresh'] == {
        'saved': True,
        'web_refreshed': True,
        'worker_refresh_requested': True,
        'worker_refresh_acked': False,
        'worker_refresh_command_id': 'command:refresh-1',
        'worker_refresh_status': 'pending',
        'reason': 'admin_llm_binding_update',
    }
    assert captured == {'force': True, 'reason': 'admin_llm_binding_update'}


def test_llm_binding_enable_returns_async_runtime_refresh_status_after_save(monkeypatch):
    captured: dict[str, object] = {}

    class _StubManager:
        def set_model_enabled(self, model_key: str, enabled: bool):
            captured['model_key'] = model_key
            captured['enabled'] = enabled
            return {'key': model_key, 'enabled': enabled}

    class _StubService:
        execution_mode = 'web'

        def is_worker_online(self, **kwargs):
            _ = kwargs
            return True

        def enqueue_worker_runtime_refresh(self, *, reason: str):
            return {
                'worker_refresh_requested': True,
                'worker_refresh_acked': False,
                'worker_refresh_command_id': 'command:enable-1',
                'worker_refresh_status': 'pending',
                'reason': reason,
            }

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest.ModelManager, 'load', classmethod(lambda cls: _StubManager()))
    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)
    monkeypatch.setattr(admin_rest, '_service', lambda: _StubService())

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post('/api/llm/bindings/m/enable')

    assert response.status_code == 200
    payload = response.json()
    assert payload['item'] == {'key': 'm', 'enabled': True}
    assert payload['runtime_refresh']['worker_refresh_command_id'] == 'command:enable-1'
    assert payload['runtime_refresh']['reason'] == 'admin_llm_binding_enable'
    assert captured['model_key'] == 'm'
    assert captured['enabled'] is True


def test_llm_binding_disable_returns_async_runtime_refresh_status_after_save(monkeypatch):
    captured: dict[str, object] = {}

    class _StubManager:
        def set_model_enabled(self, model_key: str, enabled: bool):
            captured['model_key'] = model_key
            captured['enabled'] = enabled
            return {'key': model_key, 'enabled': enabled}

    class _StubService:
        execution_mode = 'web'

        def is_worker_online(self, **kwargs):
            _ = kwargs
            return True

        def enqueue_worker_runtime_refresh(self, *, reason: str):
            return {
                'worker_refresh_requested': True,
                'worker_refresh_acked': False,
                'worker_refresh_command_id': 'command:disable-1',
                'worker_refresh_status': 'pending',
                'reason': reason,
            }

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest.ModelManager, 'load', classmethod(lambda cls: _StubManager()))
    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)
    monkeypatch.setattr(admin_rest, '_service', lambda: _StubService())

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post('/api/llm/bindings/m/disable')

    assert response.status_code == 200
    payload = response.json()
    assert payload['item'] == {'key': 'm', 'enabled': False}
    assert payload['runtime_refresh']['worker_refresh_command_id'] == 'command:disable-1'
    assert payload['runtime_refresh']['reason'] == 'admin_llm_binding_disable'
    assert captured['model_key'] == 'm'
    assert captured['enabled'] is False


def test_llm_binding_delete_returns_async_runtime_refresh_status_after_save(monkeypatch):
    captured: dict[str, object] = {}

    class _StubManager:
        def delete_model(self, model_key: str):
            captured['model_key'] = model_key

    class _StubService:
        execution_mode = 'web'

        def is_worker_online(self, **kwargs):
            _ = kwargs
            return True

        def enqueue_worker_runtime_refresh(self, *, reason: str):
            return {
                'worker_refresh_requested': True,
                'worker_refresh_acked': False,
                'worker_refresh_command_id': 'command:delete-1',
                'worker_refresh_status': 'pending',
                'reason': reason,
            }

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest.ModelManager, 'load', classmethod(lambda cls: _StubManager()))
    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)
    monkeypatch.setattr(admin_rest, '_service', lambda: _StubService())

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.delete('/api/llm/bindings/m')

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['runtime_refresh']['worker_refresh_command_id'] == 'command:delete-1'
    assert payload['runtime_refresh']['reason'] == 'admin_llm_binding_delete'
    assert captured['model_key'] == 'm'


def test_llm_binding_create_returns_structured_duplicate_name_error(monkeypatch):
    class _StubFacade:
        def create_binding(self, config, *, draft_payload: dict, binding_payload: dict):
            _ = config, draft_payload, binding_payload
            raise ValueError('Model key already exists: primary')

    class _StubManager:
        def __init__(self):
            self.config = object()
            self.facade = _StubFacade()

    monkeypatch.setattr(admin_rest.ModelManager, 'load', classmethod(lambda cls: _StubManager()))

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post(
        '/api/llm/bindings',
        json={
            'binding': {'key': 'primary', 'config_id': '', 'enabled': True},
            'draft': {'provider_id': 'custom'},
        },
    )

    assert response.status_code == 400
    detail = response.json()['detail']
    assert detail['code'] == 'llm_binding_key_exists'
    assert detail['message'] == '配置名已存在，请使用其他配置名。'
    assert detail['data']['key'] == 'primary'


def test_llm_binding_per_key_concurrency_update_persists_without_provider_probe(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    config_path = workspace / '.g3ku' / 'config.json'
    runtime_config = json.loads(config_path.read_text(encoding='utf-8'))
    runtime_config['models']['catalog'][0]['apiKey'] = 'demo-key-1,demo-key-2'
    config_path.write_text(json.dumps(runtime_config), encoding='utf-8')
    monkeypatch.chdir(workspace)

    captured: dict[str, object] = {}

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/llm/bindings/m', json={'single_api_key_max_concurrency': [3, 5]})

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['single_api_key_max_concurrency'] == [3, 5]
    assert captured == {'force': True, 'reason': 'admin_llm_binding_update'}

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['catalog'][0]['singleApiKeyMaxConcurrency'] == [3, 5]


def test_llm_memory_binding_update_refreshes_runtime(monkeypatch):
    captured: dict[str, object] = {}

    class _StubFacade:
        def get_memory_binding(self):
            return SimpleNamespace(
                embedding_config_id='cfg-embedding-old',
                rerank_config_id='cfg-rerank-old',
            )

        def set_memory_binding(self, *, embedding_config_id: str | None, rerank_config_id: str | None):
            captured['embedding_config_id'] = embedding_config_id
            captured['rerank_config_id'] = rerank_config_id
            return SimpleNamespace(
                model_dump=lambda mode='json': {
                    'embedding_config_id': embedding_config_id,
                    'embedding_provider_model': 'dashscope:qwen3-vl-embedding',
                    'rerank_config_id': rerank_config_id,
                    'rerank_provider_model': 'dashscope:qwen3-vl-rerank',
                }
            )

    class _StubManager:
        def __init__(self):
            self.facade = _StubFacade()

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest.ModelManager, 'load', classmethod(lambda cls: _StubManager()))
    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/llm/memory',
        json={
            'embedding_config_id': 'cfg-embedding-new',
            'rerank_config_id': 'cfg-rerank-new',
        },
    )

    assert response.status_code == 200
    assert response.json()['item']['embedding_config_id'] == 'cfg-embedding-new'
    assert response.json()['item']['rerank_config_id'] == 'cfg-rerank-new'
    assert captured == {
        'embedding_config_id': 'cfg-embedding-new',
        'rerank_config_id': 'cfg-rerank-new',
        'force': True,
        'reason': 'admin_llm_memory_update',
    }


def test_llm_memory_binding_partial_update_keeps_unspecified_side(monkeypatch):
    captured: dict[str, object] = {}

    class _StubFacade:
        def get_memory_binding(self):
            return SimpleNamespace(
                embedding_config_id='cfg-embedding-old',
                rerank_config_id='cfg-rerank-old',
            )

        def set_memory_binding(self, *, embedding_config_id: str | None, rerank_config_id: str | None):
            captured['embedding_config_id'] = embedding_config_id
            captured['rerank_config_id'] = rerank_config_id
            return SimpleNamespace(
                model_dump=lambda mode='json': {
                    'embedding_config_id': embedding_config_id,
                    'embedding_provider_model': 'dashscope:qwen3-vl-embedding',
                    'rerank_config_id': rerank_config_id,
                    'rerank_provider_model': 'dashscope:qwen3-vl-rerank',
                }
            )

    class _StubManager:
        def __init__(self):
            self.facade = _StubFacade()

    async def _fake_refresh(*, force: bool = False, reason: str = 'runtime') -> bool:
        captured['force'] = force
        captured['reason'] = reason
        return True

    monkeypatch.setattr(admin_rest.ModelManager, 'load', classmethod(lambda cls: _StubManager()))
    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/llm/memory',
        json={
            'embedding_config_id': 'cfg-embedding-new',
        },
    )

    assert response.status_code == 200
    assert response.json()['item']['embedding_config_id'] == 'cfg-embedding-new'
    assert response.json()['item']['rerank_config_id'] == 'cfg-rerank-old'
    assert captured == {
        'embedding_config_id': 'cfg-embedding-new',
        'rerank_config_id': 'cfg-rerank-old',
        'force': True,
        'reason': 'admin_llm_memory_update',
    }


def test_memory_dense_index_reset_endpoint_calls_memory_manager(monkeypatch):
    calls: dict[str, object] = {}

    class _StubMemoryManager:
        async def reset_dense_index(self, *, reason: str = 'manual') -> dict[str, object]:
            calls['reason'] = reason
            return {'ok': True, 'dense_enabled': True}

    monkeypatch.setattr(admin_rest, 'get_agent', lambda: SimpleNamespace(memory_manager=_StubMemoryManager()))

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post('/api/memory/dense-index/reset', json={'reason': 'embedding_model_changed'})

    assert response.status_code == 200
    assert response.json()['item']['ok'] is True
    assert calls == {'reason': 'embedding_model_changed'}


def test_memory_dense_index_rebuild_endpoint_calls_memory_manager(monkeypatch):
    calls: dict[str, object] = {}

    class _StubMemoryManager:
        async def rebuild_dense_index(self, *, reason: str = 'manual') -> dict[str, object]:
            calls['reason'] = reason
            return {'ok': True, 'indexed': 26, 'dense_points': 26}

    monkeypatch.setattr(admin_rest, 'get_agent', lambda: SimpleNamespace(memory_manager=_StubMemoryManager()))

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post('/api/memory/dense-index/rebuild', json={'reason': 'embedding_model_changed'})

    assert response.status_code == 200
    assert response.json()['item']['indexed'] == 26
    assert calls == {'reason': 'embedding_model_changed'}


def test_memory_embedding_atomic_save_returns_binding_and_rebuild_result(monkeypatch):
    calls: dict[str, object] = {}

    class _StubFacade:
        pass

    class _StubManager:
        def __init__(self):
            self.facade = _StubFacade()

    async def _fake_atomic_save(*, facade, embedding_payload, rerank_payload=None):
        calls['facade'] = facade
        calls['embedding_payload'] = embedding_payload
        calls['rerank_payload'] = rerank_payload
        return {
            'binding': {
                'embedding_config_id': 'cfg-embedding-new',
                'embedding_provider_model': 'dashscope:multimodal-embedding-v1',
                'rerank_config_id': None,
                'rerank_provider_model': '',
            },
            'reset': {'status': 'reset'},
            'rebuild': {'status': 'ready', 'indexed': 26, 'dense_points': 26},
        }

    monkeypatch.setattr(admin_rest.ModelManager, 'load', classmethod(lambda cls: _StubManager()))
    monkeypatch.setattr(admin_rest, '_save_memory_embedding_atomically', _fake_atomic_save)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.post(
        '/api/llm/memory/embedding-atomic-save',
        json={
            'embedding': {'config_id': 'cfg-embedding-old', 'draft': {'default_model': 'multimodal-embedding-v1'}},
            'rerank': None,
        },
    )

    assert response.status_code == 200
    assert response.json()['item']['rebuild']['indexed'] == 26
    assert calls['embedding_payload'] == {'config_id': 'cfg-embedding-old', 'draft': {'default_model': 'multimodal-embedding-v1'}}
    assert calls['rerank_payload'] is None


@pytest.mark.asyncio
async def test_memory_embedding_atomic_save_rolls_back_on_rebuild_failure(monkeypatch):
    events: list[tuple[str, object]] = []

    class _StubFacade:
        def __init__(self):
            self.binding = SimpleNamespace(embedding_config_id='cfg-embedding-old', rerank_config_id='cfg-rerank-old')
            self.records = {
                'cfg-embedding-old': SimpleNamespace(config_id='cfg-embedding-old', auth={'api_key': 'k'}, headers={}),
                'cfg-rerank-old': SimpleNamespace(config_id='cfg-rerank-old', auth={'api_key': 'r'}, headers={}),
            }
            self.repository = SimpleNamespace(
                list_summaries=lambda: [
                    SimpleNamespace(config_id='cfg-embedding-old', last_probe_status='success'),
                    SimpleNamespace(config_id='cfg-rerank-old', last_probe_status='success'),
                ],
                get=lambda config_id: self.records[config_id],
                save=lambda config, last_probe_status=None: events.append(('save', getattr(config, 'config_id', None), last_probe_status)),
            )

        def get_memory_binding(self):
            return self.binding

        def _hydrate_record_secrets(self, record):
            return record

        def _sanitize_record_for_storage(self, record):
            return record

        def _store_record_secrets(self, record):
            events.append(('store_secrets', getattr(record, 'config_id', None)))

        def update_config_record(self, config_id, payload):
            events.append(('update', config_id, dict(payload)))
            self.records[config_id] = SimpleNamespace(config_id=config_id, auth={'api_key': 'new'}, headers={})
            return {'config_id': config_id}

        def create_config_record(self, payload):
            raise AssertionError('create should not be called')

        def delete_config_record(self, config_id):
            events.append(('delete', config_id))

        def set_memory_binding(self, *, embedding_config_id, rerank_config_id):
            events.append(('set_binding', embedding_config_id, rerank_config_id))
            self.binding = SimpleNamespace(embedding_config_id=embedding_config_id, rerank_config_id=rerank_config_id)
            return SimpleNamespace(model_dump=lambda mode='json': {})

    class _StubMemoryManager:
        async def reset_dense_index(self, *, reason='manual'):
            events.append(('reset_dense', reason))
            return {'status': 'reset', 'dense_enabled': True}

        async def rebuild_dense_index(self, *, reason='manual'):
            events.append(('rebuild_dense', reason))
            raise RuntimeError('dense rebuild failed')

    async def _fake_refresh_web_agent_runtime(*, force=False, reason='runtime'):
        events.append(('refresh', force, reason))
        return True

    monkeypatch.setattr(admin_rest, 'refresh_web_agent_runtime', _fake_refresh_web_agent_runtime)
    monkeypatch.setattr(admin_rest, '_runtime_memory_manager', lambda: _StubMemoryManager())

    with pytest.raises(Exception) as exc_info:
        await admin_rest._save_memory_embedding_atomically(
            facade=_StubFacade(),
            embedding_payload={'config_id': 'cfg-embedding-old', 'draft': {'default_model': 'multimodal-embedding-v1'}},
            rerank_payload=None,
        )

    detail = exc_info.value.detail
    assert exc_info.value.status_code == 503
    assert detail['code'] == 'memory_embedding_atomic_save_failed'
    assert detail['saved'] is False
    assert detail['rolled_back'] is True
    assert ('update', 'cfg-embedding-old', {'default_model': 'multimodal-embedding-v1'}) in events
    assert ('reset_dense', 'embedding_model_changed') in events
    assert ('rebuild_dense', 'embedding_model_changed') in events
    assert ('set_binding', 'cfg-embedding-old', 'cfg-rerank-old') in events
    assert ('refresh', True, 'admin_llm_memory_embedding_atomic_rollback') in events


def test_load_config_backfills_missing_role_iterations(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    config_path = workspace / '.g3ku' / 'config.json'
    payload = json.loads(config_path.read_text(encoding='utf-8'))
    payload['agents'].pop('roleIterations', None)
    config_path.write_text(json.dumps(payload), encoding='utf-8')
    monkeypatch.chdir(workspace)

    from g3ku.config.loader import load_config

    cfg = load_config()

    assert cfg.get_role_max_iterations('ceo') is None
    assert cfg.get_role_max_iterations('execution') is None
    assert cfg.get_role_max_iterations('inspection') is None
    assert cfg.get_role_max_iterations('memory') is None
    assert cfg.get_role_max_concurrency('ceo') is None
    assert cfg.get_role_max_concurrency('execution') is None
    assert cfg.get_role_max_concurrency('inspection') is None
    assert cfg.get_role_max_concurrency('memory') == 1

    saved = json.loads(config_path.read_text(encoding='utf-8'))
    assert saved['agents']['roleIterations'] == {'ceo': None, 'execution': None, 'inspection': None, 'memory': None}
    assert saved['agents']['roleConcurrency'] == {'ceo': None, 'execution': None, 'inspection': None, 'memory': 1}


def test_llm_routes_endpoint_updates_role_iterations(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/llm/routes/execution',
        json={'model_keys': ['m'], 'max_iterations': 22},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['routes']['execution'] == ['m']
    assert payload['role_iterations']['execution'] == 22

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['agents']['roleIterations']['execution'] == 22


def test_llm_routes_endpoint_updates_role_concurrency(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/llm/routes/execution',
        json={'model_keys': ['m'], 'max_concurrency': 3},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['routes']['execution'] == ['m']
    assert payload['role_concurrency']['execution'] == 3

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['agents']['roleConcurrency']['execution'] == 3


def test_llm_routes_endpoint_supports_memory_scope_and_keeps_memory_concurrency_fixed(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/llm/routes/memory',
        json={'model_keys': ['m'], 'max_iterations': 7, 'max_concurrency': 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['route']['scope'] == 'memory'
    assert payload['routes']['memory'] == ['m']
    assert payload['role_iterations']['memory'] == 7
    assert payload['role_concurrency']['memory'] == 1

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['roles']['memory'] == ['m']
    assert saved['agents']['roleIterations']['memory'] == 7
    assert saved['agents']['roleConcurrency']['memory'] == 1


def test_llm_routes_memory_scope_rejects_non_chat_binding(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        LLMConfigFacade,
        'get_binding_capability',
        lambda self, config, model_key: 'embedding' if model_key == 'm' else 'chat',
    )

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put('/api/llm/routes/memory', json={'model_keys': ['m']})

    assert response.status_code == 400
    assert 'chat-capable' in str(response.json().get('detail') or '')


def test_llm_routes_memory_scope_accepts_empty_chain(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/llm/routes/memory',
        json={'model_keys': [], 'max_iterations': 5, 'max_concurrency': 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['route']['scope'] == 'memory'
    assert payload['routes']['memory'] == []
    assert payload['role_iterations']['memory'] == 5
    assert payload['role_concurrency']['memory'] == 1

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['models']['roles']['memory'] == []
    assert saved['agents']['roleIterations']['memory'] == 5
    assert saved['agents']['roleConcurrency']['memory'] == 1


def test_llm_routes_bulk_endpoint_updates_multiple_scopes_with_single_refresh(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    refresh_calls: list[str] = []

    async def _fake_refresh(reason: str):
        refresh_calls.append(reason)

    monkeypatch.setattr(admin_rest, '_refresh_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/llm/routes',
        json={
            'updates': {
                'execution': {'model_keys': ['m'], 'max_iterations': 22},
                'inspection': {'model_keys': ['m'], 'max_concurrency': 3},
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['routes']['execution'] == ['m']
    assert payload['routes']['inspection'] == ['m']
    assert payload['role_iterations']['execution'] == 22
    assert payload['role_concurrency']['inspection'] == 3
    assert refresh_calls == ['admin_llm_route_update']

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['agents']['roleIterations']['execution'] == 22
    assert saved['agents']['roleConcurrency']['inspection'] == 3


def test_model_roles_bulk_endpoint_updates_multiple_scopes_with_single_refresh(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    refresh_calls: list[str] = []

    async def _fake_refresh(reason: str):
        refresh_calls.append(reason)

    monkeypatch.setattr(admin_rest, '_refresh_runtime', _fake_refresh)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/models/routes/batch',
        json={
            'updates': {
                'execution': {'model_keys': ['m'], 'max_iterations': 11},
                'inspection': {'model_keys': ['m'], 'max_concurrency': 2},
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['roles']['execution'] == ['m']
    assert payload['roles']['inspection'] == ['m']
    assert payload['role_iterations']['execution'] == 11
    assert payload['role_concurrency']['inspection'] == 2
    assert refresh_calls == ['admin_model_roles']

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['agents']['roleIterations']['execution'] == 11
    assert saved['agents']['roleConcurrency']['inspection'] == 2


def test_runtime_refresh_status_endpoint_returns_current_command_snapshot(monkeypatch):
    class _StubService:
        def get_task_command_status(self, command_id: str):
            assert command_id == 'command:refresh-1'
            return {
                'command_id': command_id,
                'command_type': 'refresh_runtime_config',
                'status': 'completed',
                'created_at': '2026-04-12T22:35:41+08:00',
                'claimed_at': '2026-04-12T22:36:08+08:00',
                'finished_at': '2026-04-12T22:36:08+08:00',
                'worker_id': 'worker:1',
                'error_text': '',
                'payload': {'reason': 'admin_llm_binding_update'},
                'result': {'changed': True},
            }

    monkeypatch.setattr(admin_rest, '_service', lambda: _StubService())

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.get('/api/runtime-refresh/command:refresh-1')

    assert response.status_code == 200
    payload = response.json()
    assert payload['ok'] is True
    assert payload['item']['command_id'] == 'command:refresh-1'
    assert payload['item']['status'] == 'completed'
    assert payload['item']['result'] == {'changed': True}


def test_model_role_batch_save_rejects_models_missing_context_window_tokens(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    saved['models']['catalog'][0].pop('contextWindowTokens', None)
    (workspace / '.g3ku' / 'config.json').write_text(json.dumps(saved), encoding='utf-8')

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/models/routes/batch',
        json={'updates': {'ceo': {'model_keys': ['m']}}},
    )

    assert response.status_code == 400
    assert 'context_window_tokens' in str(response.json()['detail'])


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
    assert [item['id'] for item in payload['items']] == ['qqbot', 'dingtalk', 'wecom', 'wecom-app', 'wecom-kf', 'wechat-mp', 'feishu-china']


def test_china_bridge_qqbot_template_defaults_to_single_account():
    template = china_channel_template('qqbot')

    assert template['appId'] == 'your-qq-app-id'
    assert template['clientSecret'] == 'your-qq-client-secret'
    assert template['markdownSupport'] is True
    assert 'defaultAccount' not in template
    assert 'accounts' not in template


def test_china_bridge_channel_save_updates_config_file(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    async def _probe(_channel_id: str, _payload: dict[str, object]):
        return {'status': 'success', 'checked': True, 'message': 'probe ok', 'details': []}

    monkeypatch.setattr(admin_rest, '_probe_china_channel_platform_connectivity', _probe)

    response = client.put(
        '/api/china-bridge/channels/qqbot',
        json={'enabled': True, 'config': {'appId': '123456', 'clientSecret': 'demo-secret', 'accounts': {'default': {'token': 'demo-token'}}}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['item']['enabled'] is True
    assert payload['probe_result']['status'] == 'warning'
    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['chinaBridge']['enabled'] is True
    assert saved['chinaBridge']['channels']['qqbot']['enabled'] is True
    assert saved['chinaBridge']['channels']['qqbot']['appId'] == '123456'


def test_china_bridge_single_top_level_account_counts_as_one(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    async def _probe(_channel_id: str, _payload: dict[str, object]):
        return {'status': 'success', 'checked': True, 'message': 'probe ok', 'details': []}

    monkeypatch.setattr(admin_rest, '_probe_china_channel_platform_connectivity', _probe)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    save_response = client.put(
        '/api/china-bridge/channels/qqbot',
        json={'enabled': True, 'config': {'appId': '123456', 'clientSecret': 'demo-secret'}},
    )

    assert save_response.status_code == 200
    list_response = client.get('/api/china-bridge/channels')
    assert list_response.status_code == 200
    items = {item['id']: item for item in list_response.json()['items']}
    assert items['qqbot']['account_count'] == 1


def test_china_bridge_channel_save_rejects_probe_failure_before_persisting(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    async def _probe(_channel_id: str, _payload: dict[str, object]):
        return {'status': 'error', 'checked': True, 'message': 'credentials invalid', 'details': ['qqbot rejected appId']}

    monkeypatch.setattr(admin_rest, '_probe_china_channel_platform_connectivity', _probe)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.put(
        '/api/china-bridge/channels/qqbot',
        json={'enabled': True, 'config': {'appId': '123456', 'clientSecret': 'demo-secret'}},
    )

    assert response.status_code == 400
    detail = response.json()['detail']
    assert detail['code'] == 'china_channel_probe_failed'
    assert detail['probe']['message'] == 'credentials invalid'
    saved = json.loads((workspace / '.g3ku' / 'config.json').read_text(encoding='utf-8'))
    assert saved['chinaBridge']['channels']['qqbot']['enabled'] is False
    assert saved['chinaBridge']['channels']['qqbot']['accounts'] == {}


def test_china_bridge_channel_test_reports_disabled_or_validated(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)
    get_bootstrap_security_service(workspace).setup_initial_realm(password='test-password')

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    async def _probe(_channel_id: str, _payload: dict[str, object]):
        return {'status': 'success', 'checked': True, 'message': 'probe ok', 'details': []}

    monkeypatch.setattr(admin_rest, '_probe_china_channel_platform_connectivity', _probe)

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
async def test_probe_http_json_surfaces_empty_httpx_errors():
    class _FailingClient:
        async def request(self, method, url, headers=None, json=None):
            raise httpx.ConnectError('', request=httpx.Request(method, url))

    with pytest.raises(RuntimeError, match='请求失败：ConnectError'):
        await admin_rest._probe_http_json(_FailingClient(), 'POST', admin_rest.QQBOT_ACCESS_TOKEN_URL)


@pytest.mark.asyncio
async def test_probe_qqbot_connectivity_rejects_template_placeholder_account_values():
    payload = {
        'appId': '1903529517',
        'clientSecret': 'real-secret',
        'defaultAccount': 'default',
        'accounts': {
            'default': {
                'appId': 'your-qq-app-id',
                'clientSecret': 'your-qq-client-secret',
            }
        },
    }

    with pytest.raises(RuntimeError) as excinfo:
        await admin_rest._probe_qqbot_connectivity(payload)

    message = str(excinfo.value)
    assert 'QQ Bot 账号 default' in message
    assert '模板占位值' in message
    assert 'defaultAccount / accounts' in message
    assert 'accounts.default.appId / clientSecret' in message


def test_china_bridge_channels_endpoint_hides_stale_host_pid(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    state_dir = workspace / '.g3ku' / 'china-bridge'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'status.json').write_text(
        json.dumps(
            {
                'enabled': True,
                'running': True,
                'connected': True,
                'pid': 4242,
                'last_error': '',
            }
        ),
        encoding='utf-8',
    )

    monkeypatch.setattr(admin_rest, '_process_exists', lambda pid: False)

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.get('/api/china-bridge/channels')

    assert response.status_code == 200
    payload = response.json()
    assert payload['bridge']['running'] is False
    assert payload['bridge']['connected'] is False
    assert payload['bridge']['pid'] == 4242
    assert payload['bridge']['pid_alive'] is False
    assert payload['bridge']['state_stale'] is True
    assert payload['bridge']['last_error'] == 'china bridge host process is not running'


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


def test_tool_result_inline_full_flag_is_documented_in_tool_authoring_skills():
    docs = {
        'add-tool': REPO_ROOT / 'skills' / 'add-tool' / 'SKILL.md',
        'repair-tool': REPO_ROOT / 'skills' / 'repair-tool' / 'SKILL.md',
        'update-tool': REPO_ROOT / 'skills' / 'update-tool' / 'SKILL.md',
    }
    content = {
        name: ' '.join(path.read_text(encoding='utf-8').lower().split())
        for name, path in docs.items()
    }

    missing_details: list[str] = []

    def require_groups(doc_name: str, facet: str, *groups: tuple[str, ...]) -> None:
        text = content[doc_name]
        missing = []
        for group in groups:
            missing_tokens = [token for token in group if token.lower() not in text]
            if missing_tokens:
                missing.append(' + '.join(missing_tokens))
        if missing:
            missing_details.append(f"{doc_name} {facet}: missing {', '.join(missing)}")

    require_groups('add-tool', 'default false behavior', ('tool_result_inline_full', 'default', 'false'))
    require_groups('add-tool', 'preview plus output_ref path', ('preview', 'output_ref'))
    require_groups('add-tool', 'true inline-full path', ('tool_result_inline_full', 'true'), ('full', 'inline'))

    require_groups('repair-tool', 'contract troubleshooting anchor', ('tool_result_inline_full',))
    require_groups('repair-tool', 'default false behavior', ('tool_result_inline_full', 'false'), ('preview', 'output_ref'))
    require_groups('repair-tool', 'preview expectation mismatch guidance', ('preview', 'expected'))
    require_groups('repair-tool', 'output_ref repair guidance', ('output_ref', 'missing', 'stale'))
    require_groups('repair-tool', 'true inline-full path', ('tool_result_inline_full', 'true'), ('full', 'inline'))

    require_groups('update-tool', 'contract re-check trigger', ('tool_result_inline_full', 'result-delivery', 'changes'))
    require_groups('update-tool', 'default false behavior', ('preview', 'output_ref'))
    require_groups('update-tool', 'true inline-full path', ('tool_result_inline_full', 'true'), ('full', 'inline'))
    require_groups('update-tool', 'repair guidance remains aligned with same split', ('adding', 'removing', 'output_ref'), ('avoid', 'gray', 'states'))

    assert missing_details == [], (
        'Expected tool authoring skills to cover the tool result delivery contract facets across add/repair/update guidance: '
        + '; '.join(missing_details)
    )


@pytest.mark.asyncio
async def test_admin_skill_delete_endpoint_removes_files_and_syncs_catalog(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _write_skill(workspace, name='demo_skill')

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

    captured: dict[str, object] = {}

    class _MemoryManager:
        async def sync_catalog(self, service, *, skill_ids=None, tool_ids=None):
            captured['skill_ids'] = set(skill_ids or set())
            captured['tool_ids'] = set(tool_ids or set())
            return {'created': 0, 'updated': 0, 'removed': 1}

    try:
        await service.startup()
        service.memory_manager = _MemoryManager()
        client = TestClient(_build_app(service))

        response = client.delete('/api/resources/skills/demo_skill', params={'session_id': 'web:shared'})

        assert response.status_code == 200
        payload = response.json()['item']
        assert payload['skill_id'] == 'demo_skill'
        assert payload['catalog_synced'] is True
        assert payload['catalog']['removed'] == 1
        assert captured == {'skill_ids': {'demo_skill'}, 'tool_ids': set()}
        assert not (workspace / 'skills' / 'demo_skill').exists()
        assert service.get_skill_resource('demo_skill') is None
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_admin_skill_delete_endpoint_rejects_running_task_usage(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _write_skill(workspace, name='demo_skill')

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
        visible = {
            item.skill_id
            for item in service.list_visible_skill_resources(actor_role='execution', session_id='web:shared')
        }
        assert 'demo_skill' in visible
        service.store.upsert_task(
            _running_task_record(
                task_id='task:busy-skill',
                session_id='web:shared',
                title='Busy skill task',
            )
        )
        client = TestClient(_build_app(service))

        response = client.delete('/api/resources/skills/demo_skill', params={'session_id': 'web:shared'})

        assert response.status_code == 409
        payload = response.json()['detail']
        assert payload['code'] == 'skill_in_use'
        assert payload['resource_id'] == 'demo_skill'
        assert payload['usage']['tasks'][0]['task_id'] == 'task:busy-skill'
        assert not payload['usage']['ceo_sessions']
        assert (workspace / 'skills' / 'demo_skill').exists()
        assert service.get_skill_resource('demo_skill') is not None
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_admin_tool_delete_endpoint_removes_install_dir_and_syncs_catalog(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _write_external_tool(workspace, name='external_browser')

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

    captured: dict[str, object] = {}

    class _MemoryManager:
        async def sync_catalog(self, service, *, skill_ids=None, tool_ids=None):
            captured['skill_ids'] = set(skill_ids or set())
            captured['tool_ids'] = set(tool_ids or set())
            return {'created': 0, 'updated': 0, 'removed': 1}

    try:
        await service.startup()
        service.memory_manager = _MemoryManager()
        client = TestClient(_build_app(service))

        response = client.delete('/api/resources/tools/external_browser', params={'session_id': 'web:shared'})

        assert response.status_code == 200
        payload = response.json()['item']
        assert payload['tool_id'] == 'external_browser'
        assert payload['catalog_synced'] is True
        assert payload['catalog']['removed'] == 1
        assert captured == {'skill_ids': set(), 'tool_ids': {'external_browser'}}
        assert not (workspace / 'tools' / 'external_browser').exists()
        assert not (workspace / '.g3ku' / 'external-tools' / 'external_browser').exists()
        assert service.get_tool_family('external_browser') is None
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_admin_tool_delete_endpoint_rejects_running_ceo_usage(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _write_external_tool(workspace, name='external_browser')

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

    session_id = 'web:ceo-delete-demo'
    session_path = workspace / 'sessions' / 'web_ceo_delete_demo.jsonl'
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session_path.write_text('{"_type":"metadata"}\n', encoding='utf-8')

    class _StubSessionManager:
        def __init__(self):
            self._session = Session(key=session_id, metadata={'title': '删除测试会话'})

        def get_path(self, key: str) -> Path:
            assert key == session_id
            return session_path

        def get_or_create(self, key: str):
            assert key == session_id
            return self._session

    service.bind_runtime_loop(
        SimpleNamespace(
            _active_tasks={session_id: {object()}},
            sessions=_StubSessionManager(),
        )
    )

    try:
        await service.startup()
        visible = {
            item.tool_id
            for item in service.list_visible_tool_families(actor_role='ceo', session_id=session_id)
        }
        assert 'external_browser' in visible
        client = TestClient(_build_app(service))

        response = client.delete('/api/resources/tools/external_browser', params={'session_id': 'web:shared'})

        assert response.status_code == 409
        payload = response.json()['detail']
        assert payload['code'] == 'tool_in_use'
        assert payload['resource_id'] == 'external_browser'
        assert not payload['usage']['tasks']
        assert payload['usage']['ceo_sessions'][0]['session_id'] == session_id
        assert payload['usage']['ceo_sessions'][0]['title'] == '删除测试会话'
        assert (workspace / 'tools' / 'external_browser').exists()
        assert service.get_tool_family('external_browser') is not None
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_admin_endpoints_expose_builtin_agent_browser_fields(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'agent_browser')

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
        client = TestClient(_build_app(service))

        item_response = client.get('/api/resources/tools/agent_browser')
        assert item_response.status_code == 200
        item = item_response.json()['item']
        assert item['tool_type'] == 'internal'
        assert item['callable'] is True
        assert item['install_dir'] is None
        assert item['actions'][0]['action_id'] == 'browse'
        assert item['actions'][0]['allowed_roles'] == ['ceo', 'execution']

        toolskill_response = client.get('/api/resources/tools/agent_browser/toolskill')
        assert toolskill_response.status_code == 200
        payload = toolskill_response.json()
        assert payload['tool_type'] == 'internal'
        assert payload['callable'] is True
        assert 'externaltools/agent_browser' in payload['content']
        assert '## 安装' in payload['content']
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_unavailable_builtin_tool_context_remains_visible_to_ceo(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'agent_browser', 'load_tool_context')

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
        visible_names = set(service.list_effective_tool_names(actor_role='ceo', session_id='web:shared'))
        visible_families = {
            item.tool_id
            for item in service.list_visible_tool_families(actor_role='ceo', session_id='web:shared')
        }

        assert 'agent_browser' not in visible_names
        assert 'agent_browser' in visible_families

        payload = service.load_tool_context(actor_role='ceo', session_id='web:shared', tool_id='agent_browser')
        assert payload['ok'] is True
        assert payload['tool_id'] == 'agent_browser'
        assert payload['callable'] is True
        assert payload['available'] is False
        assert len(payload['warnings']) == 1
        assert payload['warnings'][0].startswith('missing required paths: externaltools/agent_browser/')
        assert '# agent_browser' in payload['content']
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_runtime_service_can_search_visible_tool_and_skill_candidates(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _write_skill(workspace, name='rollback_helper')
    _copy_repo_tools(workspace, 'content', 'load_skill_context', 'load_tool_context')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-search')

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

        tool_payload = service.load_tool_context_v2(
            actor_role='ceo',
            session_id='web:shared',
            search_query='externalized content reference',
        )
        assert tool_payload['ok'] is True
        assert tool_payload['mode'] == 'search'
        assert tool_payload['candidates'][0]['tool_id'] in {'content', 'content_navigation'}
        assert 'description' in tool_payload['candidates'][0]['matched_fields']
        assert 'load_tool_context(tool_id="' in tool_payload['next_action_hint']

        skill_payload = service.load_skill_context_v2(
            actor_role='ceo',
            session_id='web:shared',
            search_query='rollback planning',
        )
        assert skill_payload['ok'] is True
        assert skill_payload['mode'] == 'search'
        assert skill_payload['candidates'][0]['skill_id'] == 'rollback_helper'
        assert skill_payload['candidates'][0]['matched_fields']
        assert 'load_skill_context(skill_id="' in skill_payload['next_action_hint']
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_load_skill_context_v2_returns_full_skill_body_by_default(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    skill_body = (
        '# Full Body Skill\n\n'
        'This is a long skill body used to verify that full-content mode bypasses excerpt truncation.\n\n'
        '## Steps\n'
        '1. Gather context from the workspace.\n'
        '2. Compare the existing implementation with the requested behavior.\n'
        '3. Return the entire document so nothing is silently truncated.\n\n'
        '## Notes\n'
        'The caller should still receive l0 and l1 summaries alongside the full body.\n'
    )
    _write_skill(workspace, name='full_body_skill', content=skill_body)
    _copy_repo_tools(workspace, 'load_skill_context')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-full-skill-context')

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

        payload = service.load_skill_context_v2(
            actor_role='ceo',
            session_id='web:shared',
            skill_id='full_body_skill',
        )
        assert payload['ok'] is True
        assert payload['level'] == 'l2'
        assert payload['content'] == skill_body
        assert payload['l0']
        assert payload['l1']
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_load_skill_context_v2_rejects_repair_required_skill(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    root = workspace / 'skills' / 'broken_skill'
    root.mkdir(parents=True, exist_ok=True)
    (root / 'resource.yaml').write_text(
        """schema_version: 1
kind: skill
name: broken_skill
description: Broken skill for repair-required gate tests.
trigger:
  keywords: []
  always: false
requires:
  tools: []
  bins:
    - definitely_missing_bin_for_repair_gate
  env: []
content:
  main: SKILL.md
exposure:
  agent: true
  main_runtime: true
""",
        encoding='utf-8',
    )
    (root / 'SKILL.md').write_text('# Broken Skill\n\nThis body should stay hidden until repaired.\n', encoding='utf-8')
    _copy_repo_tools(workspace, 'load_skill_context')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-repair-required-skill-gate')

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

        payload = service.load_skill_context_v2(
            actor_role='ceo',
            session_id='web:shared',
            skill_id='broken_skill',
        )
        assert payload['ok'] is False
        assert payload['error'] == 'skill_repair_required'
        assert payload['skill_id'] == 'broken_skill'
        assert payload['reference_skill'] == 'writing-skills'
        assert 'repair' in str(payload['message'] or '').lower()
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_load_tool_context_v2_returns_full_tool_body_by_default(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'content', 'load_tool_context')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-full-tool-context')

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
        expected = str((service.get_tool_toolskill('content') or {}).get('content') or '')

        payload = service.load_tool_context_v2(
            actor_role='ceo',
            session_id='web:shared',
            tool_id='content',
        )

        assert payload['ok'] is True
        assert payload['level'] == 'l2'
        assert payload['content'] == expected
        assert payload['l0']
        assert payload['l1']
        assert payload['parameters_schema']['properties']['path']['type'] == 'string'
        assert 'action' in payload['required_parameters']
        assert 'path' in payload['parameters_schema']['properties']
        assert dict(payload['example_arguments']).get('action')
        assert '## Parameter Contract' in str(payload['parameter_contract_markdown'])
        assert payload['callable_now'] is True
        assert payload['will_be_hydrated_next_turn'] is True
        assert payload['hydration_targets'] == ['content']
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_message_tool_family_is_not_discovered_after_hard_delete(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'memory_runtime', 'load_skill_context', 'load_tool_context')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-message-hard-delete')

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
        items = {item.tool_id: item for item in service.list_tool_resources()}

        assert not (REPO_ROOT / 'tools' / 'message' / 'resource.yaml').exists()
        assert 'messaging' not in items
        assert 'message' not in service.list_effective_tool_names(
            actor_role='ceo',
            session_id='web:shared',
        )
    finally:
        await service.close()
        manager.close()


def test_resource_read_endpoints_work_without_configured_models(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    ensure_startup_config_ready()
    _write_skill(workspace, name='demo_skill')
    _write_external_tool(workspace, name='external_browser')
    _copy_repo_tools(workspace, 'memory_note', 'memory_write', 'memory_runtime')

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    monkeypatch.setattr(admin_rest, 'get_agent', _raise_no_model_configured)

    client = TestClient(app)

    skills_response = client.get('/api/resources/skills')
    assert skills_response.status_code == 200
    assert any(item['skill_id'] == 'demo_skill' for item in skills_response.json()['items'])

    skill_response = client.get('/api/resources/skills/demo_skill')
    assert skill_response.status_code == 200
    assert skill_response.json()['item']['skill_id'] == 'demo_skill'
    assert any(item['file_key'] == 'skill_doc' for item in skill_response.json()['files'])

    tools_response = client.get('/api/resources/tools')
    assert tools_response.status_code == 200
    assert any(item['tool_id'] == 'external_browser' for item in tools_response.json()['items'])

    tool_response = client.get('/api/resources/tools/external_browser')
    assert tool_response.status_code == 200
    assert tool_response.json()['item']['tool_id'] == 'external_browser'

    toolskill_response = client.get('/api/resources/tools/external_browser/toolskill')
    assert toolskill_response.status_code == 200
    assert '# External Browser' in str(toolskill_response.json().get('content') or '')

    memory_toolskill_response = client.get('/api/resources/tools/memory_write/toolskill')
    assert memory_toolskill_response.status_code == 200
    memory_payload = memory_toolskill_response.json()
    assert memory_payload['tool_id'] == 'memory_write'
    assert memory_payload['family_tool_id'] == 'memory'
    assert memory_payload['toolskill_source_name'] == 'memory_write'
    assert memory_payload['available'] is False
    assert '# memory_write' in str(memory_payload.get('content') or '')


def test_resource_write_endpoints_work_without_configured_models(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    ensure_startup_config_ready()
    _write_skill(workspace, name='demo_skill')
    _write_external_tool(workspace, name='external_browser')

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    monkeypatch.setattr(admin_rest, 'get_agent', _raise_no_model_configured)

    client = TestClient(app)

    skill_policy = client.put(
        '/api/resources/skills/demo_skill/policy',
        params={'session_id': 'web:shared'},
        json={'enabled': False, 'allowed_roles': ['ceo']},
    )
    assert skill_policy.status_code == 200
    assert skill_policy.json()['item']['enabled'] is False
    assert skill_policy.json()['item']['allowed_roles'] == ['ceo']

    skill_enable = client.post('/api/resources/skills/demo_skill/enable', params={'session_id': 'web:shared'})
    assert skill_enable.status_code == 200
    assert skill_enable.json()['item']['enabled'] is True

    skill_file = client.put(
        '/api/resources/skills/demo_skill/files/skill_doc',
        params={'session_id': 'web:shared'},
        json={'content': '# Demo Skill\n\nUpdated content.\n'},
    )
    assert skill_file.status_code == 200
    assert skill_file.json()['item']['catalog_synced'] is False

    tool_policy = client.put(
        '/api/resources/tools/external_browser/policy',
        params={'session_id': 'web:shared'},
        json={'actions': {'use': ['execution']}},
    )
    assert tool_policy.status_code == 200
    assert tool_policy.json()['item']['actions'][0]['allowed_roles'] == ['execution']

    tool_disable = client.post('/api/resources/tools/external_browser/disable', params={'session_id': 'web:shared'})
    assert tool_disable.status_code == 200
    assert tool_disable.json()['item']['enabled'] is False

    tool_enable = client.post('/api/resources/tools/external_browser/enable', params={'session_id': 'web:shared'})
    assert tool_enable.status_code == 200
    assert tool_enable.json()['item']['enabled'] is True

    reload_response = client.post('/api/resources/reload', params={'session_id': 'web:shared'}, json={})
    assert reload_response.status_code == 200
    assert reload_response.json()['ok'] is True


def test_update_tool_policy_endpoint_preserves_empty_action_role_lists():
    captured: dict[str, object] = {}
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

        def update_tool_policy(self, tool_id: str, *, session_id: str, enabled=None, allowed_roles_by_action=None, execution_mode=None):
            captured['tool_id'] = tool_id
            captured['session_id'] = session_id
            captured['enabled'] = enabled
            captured['allowed_roles_by_action'] = allowed_roles_by_action
            captured['execution_mode'] = execution_mode
            return family.model_copy(
                update={
                    'enabled': family.enabled if enabled is None else bool(enabled),
                    'actions': [
                        family.actions[0].model_copy(
                            update={'allowed_roles': list((allowed_roles_by_action or {}).get('run', family.actions[0].allowed_roles))}
                        )
                    ],
                }
            )

    client = TestClient(_build_app(_StubService()))
    response = client.put(
        '/api/resources/tools/exec_runtime/policy',
        params={'session_id': 'web:shared'},
        json={'actions': {'run': []}},
    )

    assert response.status_code == 200
    assert captured == {
        'tool_id': 'exec_runtime',
        'session_id': 'web:shared',
        'enabled': None,
        'allowed_roles_by_action': {'run': []},
        'execution_mode': None,
    }
    assert response.json()['item']['actions'][0]['allowed_roles'] == []


def test_update_tool_policy_endpoint_forwards_exec_execution_mode():
    captured: dict[str, object] = {}
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

        def update_tool_policy(
            self,
            tool_id: str,
            *,
            session_id: str,
            enabled=None,
            allowed_roles_by_action=None,
            execution_mode=None,
        ):
            captured['tool_id'] = tool_id
            captured['session_id'] = session_id
            captured['enabled'] = enabled
            captured['allowed_roles_by_action'] = allowed_roles_by_action
            captured['execution_mode'] = execution_mode
            return family.model_copy(
                update={
                    'metadata': {
                        **dict(family.metadata or {}),
                        'execution_mode': execution_mode,
                    }
                }
            )

    client = TestClient(_build_app(_StubService()))
    response = client.put(
        '/api/resources/tools/exec_runtime/policy',
        params={'session_id': 'web:shared'},
        json={'execution_mode': 'full_access'},
    )

    assert response.status_code == 200
    assert captured == {
        'tool_id': 'exec_runtime',
        'session_id': 'web:shared',
        'enabled': None,
        'allowed_roles_by_action': None,
        'execution_mode': 'full_access',
    }
    assert response.json()['item']['metadata']['execution_mode'] == 'full_access'


def test_governance_store_bool_meta_round_trip(tmp_path: Path):
    from main.governance.store import GovernanceStore

    store = GovernanceStore(tmp_path / 'governance.db')
    try:
        assert store.get_bool_meta('ceo_frontdoor_regulatory_mode_enabled', default=False) is False
        store.set_bool_meta('ceo_frontdoor_regulatory_mode_enabled', True)
        assert store.get_bool_meta('ceo_frontdoor_regulatory_mode_enabled', default=False) is True
        store.set_bool_meta('ceo_frontdoor_regulatory_mode_enabled', False)
        assert store.get_bool_meta('ceo_frontdoor_regulatory_mode_enabled', default=True) is False
    finally:
        store.close()


@pytest.mark.asyncio
async def test_main_runtime_service_governance_mode_round_trip(tmp_path: Path):
    runtime_store = tmp_path / 'runtime.sqlite3'
    governance_store = tmp_path / 'governance.sqlite3'
    files_base_dir = tmp_path / 'tasks'
    artifact_dir = tmp_path / 'artifacts'

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=runtime_store,
        files_base_dir=files_base_dir,
        artifact_dir=artifact_dir,
        governance_store_path=governance_store,
    )
    try:
        assert service.get_governance_mode() == {
            'enabled': False,
            'updated_at': '',
        }
        updated = service.update_governance_mode(enabled=True)
        assert updated['enabled'] is True
        assert str(updated['updated_at']).strip()
        assert service.get_governance_mode() == updated
    finally:
        await service.close()

    restarted = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        store_path=runtime_store,
        files_base_dir=files_base_dir,
        artifact_dir=artifact_dir,
        governance_store_path=governance_store,
    )
    try:
        reloaded = restarted.get_governance_mode()
        assert reloaded['enabled'] is True
        assert reloaded['updated_at'] == updated['updated_at']
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_main_runtime_service_frontdoor_reviewable_tool_risk_map_uses_governance_mode_and_tool_risk(
    tmp_path: Path,
):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'content', 'exec', 'memory_runtime', 'memory_write')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-frontdoor-governance-risk-map')

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
        assert service.frontdoor_reviewable_tool_risk_map(
            actor_role='ceo',
            session_id='web:shared',
        ) == {}

        service.update_tool_policy(
            'exec_runtime',
            session_id='web:shared',
            allowed_roles_by_action={'run': ['ceo', 'execution']},
        )
        service.update_tool_policy(
            'memory',
            session_id='web:shared',
            allowed_roles_by_action={'write': ['ceo']},
        )
        service.update_governance_mode(enabled=True)
        reviewable = service.frontdoor_reviewable_tool_risk_map(
            actor_role='ceo',
            session_id='web:shared',
        )

        assert reviewable['exec'] == 'high'
        assert reviewable['memory_write'] == 'medium'
        assert 'content_open' not in reviewable
    finally:
        await service.close()
        manager.close()


def test_governance_mode_endpoints_round_trip():
    captured: dict[str, object] = {}

    class _StubService:
        async def startup(self) -> None:
            return None

        def get_governance_mode(self):
            return {'enabled': False, 'updated_at': ''}

        def update_governance_mode(self, *, enabled: bool):
            captured['enabled'] = enabled
            return {'enabled': bool(enabled), 'updated_at': '2026-04-21T12:34:56Z'}

    client = TestClient(_build_app(_StubService()))

    get_response = client.get('/api/resources/tools/governance-mode')
    assert get_response.status_code == 200
    assert get_response.json() == {
        'ok': True,
        'item': {
            'enabled': False,
            'updated_at': '',
        },
    }

    put_response = client.put(
        '/api/resources/tools/governance-mode',
        json={'enabled': True},
    )
    assert put_response.status_code == 200
    assert captured == {'enabled': True}
    assert put_response.json() == {
        'ok': True,
        'item': {
            'enabled': True,
            'updated_at': '2026-04-21T12:34:56Z',
        },
    }


@pytest.mark.asyncio
async def test_tool_resources_mark_core_families_and_merge_memory_runtime(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(
        workspace,
        'content',
        'memory_write',
        'memory_delete',
        'memory_note',
        'memory_runtime',
        'load_skill_context',
        'load_tool_context',
        'create_async_task_cn',
        'task_append_notice_cn',
        'task_fetch_cn',
        'task_delete_cn',
        'task_failed_nodes_cn',
        'task_node_detail_cn',
        'task_progress_cn',
        'task_stats_cn',
        'task_summary_cn',
    )

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
        items = {item.tool_id: item for item in service.list_tool_resources()}

        assert items['content'].is_core is True
        assert items['memory'].is_core is True
        assert 'messaging' not in items
        assert items['skill_access'].is_core is True
        assert items['task_runtime'].is_core is True

        skill_access_actions = {action.action_id: action for action in items['skill_access'].actions}
        assert skill_access_actions['load_context'].allowed_roles == ['ceo', 'execution', 'inspection']

        task_runtime_actions = {action.action_id: action for action in items['task_runtime'].actions}
        assert 'append_notice_cn' in task_runtime_actions
        assert 'ceo' in set(task_runtime_actions['append_notice_cn'].allowed_roles)

        memory_actions = {action.action_id: action for action in items['memory'].actions}
        assert set(memory_actions) == {'write', 'delete', 'note', 'runtime'}
        assert memory_actions['write'].allowed_roles == ['ceo']
        assert memory_actions['delete'].allowed_roles == ['ceo']
        assert memory_actions['note'].allowed_roles == ['ceo']
        assert memory_actions['write'].agent_visible is True
        assert memory_actions['write'].admin_mode == 'editable'
        assert memory_actions['delete'].agent_visible is True
        assert memory_actions['delete'].admin_mode == 'editable'
        assert memory_actions['note'].agent_visible is True
        assert memory_actions['note'].admin_mode == 'editable'
        assert memory_actions['runtime'].agent_visible is False
        assert memory_actions['runtime'].admin_mode == 'readonly_system'

        class _Registry:
            def list_tool_families(self):
                return list(items.values())

        class _PolicyEngine:
            @staticmethod
            def evaluate_tool_action(**kwargs):
                subject = kwargs.get('subject')
                tool_id = str(kwargs.get('tool_id') or '')
                action_id = str(kwargs.get('action_id') or '')
                family = items.get(tool_id)
                action = next(
                    (
                        current
                        for current in list(getattr(family, 'actions', []) or [])
                        if str(getattr(current, 'action_id', '') or '') == action_id
                    ),
                    None,
                )
                allowed_roles = set(getattr(action, 'allowed_roles', []) or [])
                return SimpleNamespace(allowed=str(getattr(subject, 'actor_role', '') or '') in allowed_roles)

        visible = set(
            list_effective_tool_names(
                subject=SimpleNamespace(actor_role='ceo'),
                supported_tool_names=[
                    'load_skill_context',
                    'load_tool_context',
                    'memory_write',
                    'memory_delete',
                    'memory_note',
                    'memory_runtime',
                ],
                resource_registry=_Registry(),
                policy_engine=_PolicyEngine(),
                mutation_allowed=True,
            )
        )
        execution_visible = set(
            list_effective_tool_names(
                subject=SimpleNamespace(actor_role='execution'),
                supported_tool_names=[
                    'load_skill_context',
                    'load_tool_context',
                    'memory_write',
                    'memory_delete',
                    'memory_note',
                    'memory_runtime',
                ],
                resource_registry=_Registry(),
                policy_engine=_PolicyEngine(),
                mutation_allowed=True,
            )
        )
        inspection_visible = set(
            list_effective_tool_names(
                subject=SimpleNamespace(actor_role='inspection'),
                supported_tool_names=[
                    'load_skill_context',
                    'load_tool_context',
                    'memory_write',
                    'memory_delete',
                    'memory_note',
                    'memory_runtime',
                ],
                resource_registry=_Registry(),
                policy_engine=_PolicyEngine(),
                mutation_allowed=True,
            )
        )
        assert visible == {
            'load_skill_context',
            'load_tool_context',
            'memory_delete',
            'memory_note',
            'memory_write',
        }
        assert execution_visible == {'load_skill_context', 'load_tool_context'}
        assert inspection_visible == {'load_skill_context', 'load_tool_context'}
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_startup_reconciles_core_tool_family_visibility_and_enablement(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'load_skill_context', 'load_tool_context', 'memory_runtime', 'memory_write')

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
        families = {item.tool_id: item for item in service.resource_registry.refresh_from_current_resources()[1]}
        broken = families['skill_access'].model_copy(
            update={
                'enabled': False,
                'actions': [
                    action.model_copy(update={'allowed_roles': ['execution', 'inspection']})
                    for action in families['skill_access'].actions
                ],
            }
        )
        service.governance_store.upsert_tool_family(broken, updated_at=datetime.now().isoformat())

        await service.startup()

        reconciled = service.get_tool_family('skill_access')
        assert reconciled is not None
        assert reconciled.is_core is True
        assert reconciled.enabled is True
        assert all('ceo' not in action.allowed_roles for action in reconciled.actions if action.agent_visible)
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_startup_backfills_legacy_empty_roles_for_implicit_tool_governance_once(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'load_skill_context', 'load_tool_context', 'memory_write')

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
        families = {item.tool_id: item for item in service.resource_registry.refresh_from_current_resources()[1]}
        legacy_empty = families['skill_access'].model_copy(
            update={
                'actions': [
                    action.model_copy(update={'allowed_roles': []})
                    for action in families['skill_access'].actions
                ],
            }
        )
        service.governance_store.upsert_tool_family(legacy_empty, updated_at=datetime.now().isoformat())
        service.governance_store.set_bool_meta('implicit_tool_role_backfill_v1_applied', False)

        await service.startup()

        healed = service.get_tool_family('skill_access')
        assert healed is not None
        assert healed.actions[0].allowed_roles == ['ceo', 'execution', 'inspection']
        assert 'load_tool_context' in service.list_effective_tool_names(actor_role='ceo', session_id='web:shared')

        cleared = service.update_tool_policy(
            'skill_access',
            session_id='web:shared',
            allowed_roles_by_action={'load_context': []},
        )
        assert cleared is not None
        assert cleared.actions[0].allowed_roles == []

        service.reload_resources(session_id='web:shared')
        after_reload = service.get_tool_family('skill_access')
        assert after_reload is not None
        assert after_reload.actions[0].allowed_roles == []
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_ensure_runtime_config_current_keeps_dynamic_task_tools_visible(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    _write_runtime_config(workspace)
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(
        workspace,
        'content',
        'memory_note',
        'memory_write',
        'memory_runtime',
        'load_skill_context',
        'load_tool_context',
        'create_async_task_cn',
        'task_append_notice_cn',
        'task_fetch_cn',
        'task_delete_cn',
        'task_failed_nodes_cn',
        'task_node_detail_cn',
        'task_progress_cn',
        'task_stats_cn',
        'task_summary_cn',
    )

    import main.service.runtime_service as runtime_service_module
    from g3ku.config.loader import load_config

    config = load_config()
    monkeypatch.setattr(runtime_service_module, 'get_runtime_config', lambda force=False: (config, 7, True))

    manager = ResourceManager(workspace, app_config=config)
    manager.reload_now(trigger='test-bind')

    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        app_config=config,
        resource_manager=manager,
        store_path=workspace / '.g3ku' / 'main-runtime' / 'runtime.sqlite3',
        files_base_dir=workspace / '.g3ku' / 'main-runtime' / 'tasks',
        artifact_dir=workspace / '.g3ku' / 'main-runtime' / 'artifacts',
        governance_store_path=workspace / '.g3ku' / 'main-runtime' / 'governance.sqlite3',
    )
    manager.bind_service_getter(lambda: {'main_task_service': service, 'app_config': config})
    manager.reload_now(trigger='test-service-bind')
    service.bind_resource_manager(manager)

    try:
        await service.startup()
        before = service.list_effective_tool_names(actor_role='ceo', session_id='web:shared')
        assert 'create_async_task' in before
        assert 'memory_write' in before

        changed = service.ensure_runtime_config_current(force=True, reason='test')

        assert changed is True
        assert 'create_async_task' in manager.tool_instances()
        after = service.list_effective_tool_names(actor_role='ceo', session_id='web:shared')
        assert 'create_async_task' in after
        assert 'task_append_notice' in after
        assert 'memory_write' in after
        assert {
            'task_list',
            'task_append_notice',
            'task_delete',
            'task_failed_nodes',
            'task_node_detail',
            'task_progress',
            'task_stats',
            'task_summary',
        }.issubset(set(after))
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_core_tool_admin_endpoints_block_disable_and_delete_but_allow_role_removal(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(
        workspace,
        'content',
        'memory_runtime',
        'load_skill_context',
        'load_tool_context',
        'create_async_task_cn',
        'task_append_notice_cn',
        'task_fetch_cn',
        'task_delete_cn',
        'task_failed_nodes_cn',
        'task_node_detail_cn',
        'task_progress_cn',
        'task_stats_cn',
        'task_summary_cn',
    )

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
        client = TestClient(_build_app(service))

        disable_response = client.post('/api/resources/tools/task_runtime/disable', params={'session_id': 'web:shared'})
        assert disable_response.status_code == 409
        assert disable_response.json()['detail']['code'] == 'core_tool_disable_forbidden'

        delete_response = client.delete('/api/resources/tools/skill_access', params={'session_id': 'web:shared'})
        assert delete_response.status_code == 409
        assert delete_response.json()['detail']['code'] == 'core_tool_delete_forbidden'

        policy_response = client.put(
            '/api/resources/tools/content/policy',
            params={'session_id': 'web:shared'},
            json={'actions': {'inspect': ['execution', 'inspection']}},
        )
        assert policy_response.status_code == 200
        assert policy_response.json()['item']['actions'][0]['allowed_roles'] == ['execution', 'inspection']
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_tool_policy_empty_roles_persist_across_reload_for_exec_runtime(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'exec', 'memory_runtime')

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
        client = TestClient(_build_app(service))

        seeded = service.update_tool_policy(
            'exec_runtime',
            session_id='web:shared',
            allowed_roles_by_action={'run': ['ceo', 'execution']},
        )
        assert seeded is not None

        before = service.get_tool_family('exec_runtime')
        assert before is not None
        assert before.actions[0].allowed_roles == ['ceo', 'execution']
        assert 'exec' in service.list_effective_tool_names(actor_role='ceo', session_id='web:shared')

        policy_response = client.put(
            '/api/resources/tools/exec_runtime/policy',
            params={'session_id': 'web:shared'},
            json={'actions': {'run': []}},
        )
        assert policy_response.status_code == 200
        assert policy_response.json()['item']['actions'][0]['allowed_roles'] == []

        reloaded = client.post('/api/resources/reload', params={'session_id': 'web:shared'}, json={})
        assert reloaded.status_code == 200
        assert reloaded.json()['ok'] is True

        detail = client.get('/api/resources/tools/exec_runtime')
        assert detail.status_code == 200
        assert detail.json()['item']['actions'][0]['allowed_roles'] == []

        assert 'exec' not in service.list_effective_tool_names(actor_role='ceo', session_id='web:shared')
        visible = service.list_visible_tool_families(actor_role='ceo', session_id='web:shared')
        assert all(item.tool_id != 'exec_runtime' for item in visible)
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_exec_execution_mode_persists_across_reload_and_is_exposed_in_tool_detail(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'exec', 'memory_runtime')

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
        client = TestClient(_build_app(service))

        policy_response = client.put(
            '/api/resources/tools/exec_runtime/policy',
            params={'session_id': 'web:shared'},
            json={'execution_mode': 'full_access'},
        )
        assert policy_response.status_code == 200
        assert policy_response.json()['item']['metadata']['execution_mode'] == 'full_access'

        reloaded = client.post('/api/resources/reload', params={'session_id': 'web:shared'}, json={})
        assert reloaded.status_code == 200
        assert reloaded.json()['ok'] is True

        detail = client.get('/api/resources/tools/exec_runtime')
        assert detail.status_code == 200
        assert detail.json()['item']['metadata']['execution_mode'] == 'full_access'
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_load_tool_context_includes_exec_runtime_policy(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_repo_tools(workspace, 'exec', 'load_tool_context')

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
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-service-bind')
    service.bind_resource_manager(manager)

    try:
        await service.startup()
        updated = service.update_tool_policy(
            'exec_runtime',
            session_id='web:shared',
            execution_mode='full_access',
        )
        assert updated is not None
        service._visible_tool_family_map = lambda **kwargs: {  # type: ignore[method-assign]
            'exec_runtime': updated,
            'exec': updated,
        }

        payload = service.load_tool_context(
            actor_role='ceo',
            session_id='web:shared',
            tool_id='exec',
        )

        assert payload['ok'] is True
        assert payload['tool_id'] == 'exec'
        assert payload['exec_runtime_policy'] == {
            'mode': 'full_access',
            'guardrails_enabled': False,
            'summary': 'exec will execute shell commands without exec-side guardrails.',
        }
        assert str(payload.get('tool_context_fingerprint') or '').startswith('tcf:')
    finally:
        await service.close()
        manager.close()


def test_china_bridge_channels_endpoint_ignores_stale_error_when_ceo_model_missing(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    config_path = workspace / '.g3ku' / 'config.json'
    payload = json.loads(config_path.read_text(encoding='utf-8'))
    payload['models']['catalog'] = []
    payload['models']['roles'] = {'ceo': [], 'execution': [], 'inspection': []}
    config_path.write_text(json.dumps(payload), encoding='utf-8')

    state_dir = workspace / '.g3ku' / 'china-bridge'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'status.json').write_text(
        json.dumps(
            {
                'enabled': True,
                'running': False,
                'connected': False,
                'pid': None,
                'last_error': 'china bridge build requires a package manager, but none of these were found: pnpm, npm',
            }
        ),
        encoding='utf-8',
    )

    app = FastAPI()
    app.include_router(admin_rest.router, prefix='/api')
    client = TestClient(app)

    response = client.get('/api/china-bridge/channels')

    assert response.status_code == 200
    data = response.json()['bridge']
    assert data['running'] is False
    assert data['connected'] is False
    assert data['startup_deferred'] is True
    assert data['startup_deferred_reason'] == 'no_model_configured'
    assert 'CEO ??' in data['startup_deferred_message']
    assert data['last_error'] is None

