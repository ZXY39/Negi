from __future__ import annotations

import importlib.util
import json
import os
import shutil
import textwrap
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.agent.tools.filesystem_mutation import FilesystemTool
from g3ku.agent.tools.propose_patch import parse_patch_artifact
from g3ku.content import ContentNavigationService, content_summary_and_ref, parse_content_envelope
from g3ku.resources import ResourceManager
from g3ku.resources.loader import ResourceLoader
from g3ku.resources.registry import ResourceRegistry
from g3ku.resources.tool_settings import FilesystemToolSettings
from main.api import rest as api_rest
from main.governance.resource_bridge import build_tool_families
from main.service.runtime_service import MainRuntimeService
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore

REPO_ROOT = Path(__file__).resolve().parents[2]


def _resource_app_config(*, poll_interval_ms: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        resources=SimpleNamespace(
            enabled=True,
            skills_dir='skills',
            tools_dir='tools',
            manifest_name='resource.yaml',
            state_path='.g3ku/resources.state.json',
            reload=SimpleNamespace(
                enabled=True,
                poll_interval_ms=poll_interval_ms,
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


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    last_value = None
    while time.monotonic() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    raise AssertionError(f'Condition not met within {timeout:.1f}s; last value={last_value!r}')


def _python_launcher() -> str:
    if os.name == 'nt':
        if shutil.which('py'):
            return 'py -3'
        return f'& "{Path(os.sys.executable)}"'
    return 'python3' if shutil.which('python3') else 'python'


def _copy_filesystem_split_tools(workspace: Path, *tool_names: str) -> None:
    names = tool_names or (
        'filesystem_write',
        'filesystem_edit',
        'filesystem_copy',
        'filesystem_move',
        'filesystem_delete',
        'filesystem_propose_patch',
    )
    for name in names:
        shutil.copytree(REPO_ROOT / 'tools' / name, workspace / 'tools' / name)


def _large_direct_load_payload(*, uri: str, body_label: str) -> dict[str, object]:
    body = '\n'.join(f'{body_label} line {index:03d}' for index in range(1, 321))
    return {
        'ok': True,
        'level': 'l2',
        'content': body,
        'l0': f'{body_label} short summary',
        'l1': f'{body_label} structured overview',
        'path': f'/virtual/{body_label}.md',
        'uri': uri,
    }


def _write_demo_tool(root: Path, *, name: str = 'demo_echo', guide: str = 'Demo echo guide') -> Path:
    tool_root = root / 'tools' / name
    (tool_root / 'toolskills').mkdir(parents=True, exist_ok=True)
    (tool_root / 'main').mkdir(parents=True, exist_ok=True)
    (tool_root / 'resource.yaml').write_text(
        textwrap.dedent(
            f"""\
            schema_version: 1
            kind: tool
            name: {name}
            description: Echo a short string for resource smoke tests.
            tool_result_delivery_contract: runtime_managed
            protocol: mcp
            mcp:
              transport: embedded
            requires:
              tools: []
              bins: []
              env: []
            permissions:
              network: false
              filesystem: []
            parameters:
              type: object
              properties:
                text:
                  type: string
                  description: Text to echo back.
              required:
              - text
            exposure:
              agent: true
              main_runtime: true
            toolskill:
              enabled: true
            """
        ),
        encoding='utf-8',
    )
    (tool_root / 'toolskills' / 'SKILL.md').write_text(
        f"# {name}\n\n{guide}\n",
        encoding='utf-8',
    )
    (tool_root / 'main' / 'tool.py').write_text(
        textwrap.dedent(
            """\
            async def execute(text: str, **kwargs):
                return text
            """
        ),
        encoding='utf-8',
    )
    return tool_root


def test_tool_resource_current_version_text_has_no_replacement_question_marks():
    manifests = sorted((REPO_ROOT / 'tools').rglob('resource.yaml'))
    assert manifests

    for manifest_path in manifests:
        raw = yaml.safe_load(manifest_path.read_text(encoding='utf-8')) or {}
        if not isinstance(raw, dict):
            continue
        current_version = raw.get('current_version')
        if not isinstance(current_version, dict):
            continue
        for key in ('summary', 'compare_rule', 'source_of_truth'):
            value = str(current_version.get(key) or '')
            assert '?' not in value, f'{manifest_path}:{key} contains replacement question marks: {value}'


def test_skill_dependency_on_tool_family_resolves_from_split_executors(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'skills' / 'add-tool', workspace / 'skills' / 'add-tool')
    _copy_filesystem_split_tools(workspace)

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    skill = registry.discover().skills['add-tool']

    assert skill.available is True
    assert not any('missing required tools' in warning for warning in skill.warnings)


def test_content_describe_docs_do_not_reference_removed_filesystem_read_tools() -> None:
    content_manifest = yaml.safe_load(
        (REPO_ROOT / 'tools' / 'content_describe' / 'resource.yaml').read_text(encoding='utf-8')
    ) or {}
    content_toolskill = (
        REPO_ROOT / 'tools' / 'content_describe' / 'toolskills' / 'SKILL.md'
    ).read_text(encoding='utf-8')
    path_description = (
        (
            ((content_manifest.get('parameters') or {}).get('properties') or {}).get('path') or {}
        ).get('description')
        or ''
    )
    assert 'directory' not in str(path_description).lower()
    assert 'directories are not supported' in content_toolskill.lower()
    assert '`filesystem_list`' not in content_toolskill


def test_content_navigation_manifests_use_real_artifact_ref_examples() -> None:
    for tool_name in ('content', 'content_describe', 'content_open', 'content_search'):
        manifest = yaml.safe_load((REPO_ROOT / 'tools' / tool_name / 'resource.yaml').read_text(encoding='utf-8')) or {}
        ref_description = str(
            (
                ((manifest.get('parameters') or {}).get('properties') or {}).get('ref') or {}
            ).get('description')
            or ''
        )
        assert 'artifact:artifact:' in ref_description
        assert 'artifact:artifact_xxx' not in ref_description


def _write_demo_external_tool(
    root: Path,
    *,
    name: str = 'external_browser',
    guide: str = 'External browser guide',
    install_dir: str | None = None,
) -> Path:
    tool_root = root / 'tools' / name
    (tool_root / 'toolskills').mkdir(parents=True, exist_ok=True)
    install_path = install_dir or f'.g3ku/external-tools/{name}'
    (tool_root / 'resource.yaml').write_text(
        textwrap.dedent(
            f"""\
            schema_version: 1
            kind: tool
            name: {name}
            display_name: External Browser
            description: Registered external browser automation tool.
            tool_type: external
            install_dir: {install_path}
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
            """
        ),
        encoding='utf-8',
    )
    (tool_root / 'toolskills' / 'SKILL.md').write_text(
        f"# {name}\n\n## 何时使用\n\n{guide}\n\n## 安装\n\nInstall it outside the tools directory.\n\n## 更新\n\nPull the upstream project in place.\n\n## 使用\n\nRun it from the configured install_dir.\n",
        encoding='utf-8',
    )
    return tool_root


def _write_demo_skill(
    root: Path,
    *,
    name: str = 'demo_skill',
    guide: str = 'Demo skill guide',
    allowed_roles: list[str] | None = None,
) -> Path:
    skill_root = root / 'skills' / name
    skill_root.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        'schema_version: 1',
        'kind: skill',
        f'name: {name}',
        'description: Demo skill for resource smoke tests.',
        'trigger:',
        '  keywords: []',
        'requires:',
        '  tools: []',
        '  bins: []',
        '  env: []',
    ]
    if allowed_roles:
        manifest_lines.extend(['governance:', '  allowed_roles:'])
        manifest_lines.extend([f'    - {role}' for role in allowed_roles])
    manifest_lines.extend(
        [
            'exposure:',
            '  agent: true',
            '  main_runtime: true',
            '',
        ]
    )
    (skill_root / 'resource.yaml').write_text('\n'.join(manifest_lines), encoding='utf-8')
    (skill_root / 'SKILL.md').write_text(
        f"# {name}\n\n{guide}\n",
        encoding='utf-8',
    )
    return skill_root


def _load_agent_browser_handler(workspace: Path):
    if not (REPO_ROOT / 'tools' / 'agent_browser' / 'resource.yaml').exists():
        pytest.skip('agent_browser tool resource is not present in this workspace')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)

    tool_root = workspace / 'tools' / 'agent_browser'
    (tool_root / 'main').mkdir(parents=True, exist_ok=True)
    (tool_root / 'toolskills').mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / 'tools' / 'agent_browser' / 'resource.yaml', tool_root / 'resource.yaml')
    shutil.copy2(REPO_ROOT / 'tools' / 'agent_browser' / 'main' / 'tool.py', tool_root / 'main' / 'tool.py')
    shutil.copy2(REPO_ROOT / 'tools' / 'agent_browser' / 'toolskills' / 'SKILL.md', tool_root / 'toolskills' / 'SKILL.md')

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['agent_browser']
    tool = ResourceLoader(workspace).load_tool(descriptor)

    assert tool is not None
    return tool._handler


class _VisibleToolService:
    def __init__(self):
        self.resource_manager = None

    def bind_resource_manager(self, manager: ResourceManager) -> None:
        self.resource_manager = manager

    async def startup(self) -> None:
        return None

    def load_tool_context(self, *, actor_role: str, session_id: str, tool_id: str):
        _ = actor_role, session_id
        if self.resource_manager is None:
            return {'ok': False, 'error': 'resource manager unavailable'}
        return {'ok': True, 'tool_id': tool_id, 'content': self.resource_manager.load_toolskill_body(tool_id)}


class _ResourceSyncService(_VisibleToolService):
    def capture_resource_tree_state(self) -> dict[str, dict[str, str]]:
        if self.resource_manager is None:
            return {}
        return self.resource_manager.capture_resource_tree_state()

    def refresh_resource_paths(self, paths, *, trigger: str = 'path-change', session_id: str = 'web:shared'):
        _ = session_id
        if self.resource_manager is None:
            return {'ok': False}
        snapshot = self.resource_manager.refresh_paths(list(paths or []), trigger=trigger)
        return {'ok': True, 'skills': len(snapshot.skills), 'tools': len(snapshot.tools)}

    def refresh_changed_resources(self, before_state, *, trigger: str = 'path-change', session_id: str = 'web:shared'):
        _ = session_id
        if self.resource_manager is None:
            return {'ok': False}
        snapshot = self.resource_manager.refresh_changed_tree_state(before_state, trigger=trigger)
        return {'ok': True, 'skills': len(snapshot.skills), 'tools': len(snapshot.tools)}


class _ArtifactService:
    def __init__(self, artifact_store: TaskArtifactStore):
        self.artifact_store = artifact_store
        self.content_store = ContentNavigationService(
            workspace=artifact_store._artifact_dir.parent,
            artifact_store=artifact_store,
            artifact_lookup=artifact_store,
        )

    async def startup(self) -> None:
        return None


class _MainTaskService:
    async def startup(self) -> None:
        return None

    def summary(self, session_id: str) -> str:
        return f'summary:{session_id}'

    def get_tasks(self, session_id: str, task_type: int) -> str:
        return f'list:{session_id}:{task_type}'

    async def task_append_notice(self, *, task_ids, node_ids, message: str, session_id: str) -> str:
        _ = node_ids
        joined = ','.join(list(task_ids or []))
        return f'append-notice:{session_id}:{joined}:{message}'

    def task_stats(self, **kwargs):
        _ = kwargs
        return 'task-stats'

    def task_delete_preview(self, **kwargs):
        _ = kwargs
        return 'task-delete'

    async def task_delete_confirm(self, **kwargs):
        _ = kwargs
        return 'task-delete'

    def failed_node_ids(self, task_id: str) -> str:
        return f'failed:{task_id}'

    def node_detail(self, task_id: str, node_id: str):
        return {
            'ok': True,
            'task_id': task_id,
            'node_id': node_id,
            'item': {'task_id': task_id, 'node_id': node_id, 'status': 'failed'},
            'artifact_count': 1,
            'artifacts': [
                {
                    'artifact_id': 'artifact:demo',
                    'task_id': task_id,
                    'node_id': node_id,
                    'kind': 'report',
                    'title': 'Demo Artifact',
                    'path': 'D:/demo.md',
                    'mime_type': 'text/markdown',
                    'preview_text': 'demo',
                    'created_at': '2026-03-25T00:00:00',
                    'ref': 'artifact:artifact:demo',
                }
            ],
        }

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> str:
        return f'progress:{task_id}:{mark_read}'


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f'chat backend should not be used in this test: {kwargs!r}')


@pytest.mark.asyncio
async def test_resource_refresh_is_manual_or_release_triggered_and_load_tool_context(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'load_tool_context', workspace / 'tools' / 'load_tool_context')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    service = _VisibleToolService()
    service.bind_resource_manager(manager)
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    manager.start()

    try:
        assert manager.get_tool('load_tool_context') is not None

        demo_dir = _write_demo_tool(workspace)
        time.sleep(0.45)
        assert manager.get_tool('demo_echo') is None
        manager.reload_now(trigger='test-manual-add')
        _wait_until(lambda: manager.get_tool('demo_echo') is not None)

        load_tool = manager.get_tool('load_tool_context')
        payload = json.loads(await load_tool.execute(tool_id='demo_echo'))
        assert payload['ok'] is True
        assert payload['tool_id'] == 'demo_echo'
        assert 'Demo echo guide' in payload['content']

        shutil.rmtree(demo_dir)
        time.sleep(0.45)
        assert manager.get_tool_descriptor('demo_echo') is not None
        manager.reload_now(trigger='test-manual-delete')
        _wait_until(lambda: manager.get_tool_descriptor('demo_echo') is None)
        assert manager.get_tool('demo_echo') is None

        demo_dir = _write_demo_tool(workspace, guide='Release reload guide')
        time.sleep(0.45)
        assert manager.get_tool('demo_echo') is None

        with manager.acquire_tool('load_tool_context'):
            pass
        _wait_until(lambda: manager.get_tool('demo_echo') is not None)

        with manager.acquire_tool('demo_echo'):
            assert await manager.get_tool('demo_echo').execute(text='busy') == 'busy'
            if os.name == 'nt':
                with pytest.raises((PermissionError, OSError)):
                    shutil.rmtree(demo_dir)
            else:
                pytest.skip('Windows delete guard is required for this smoke test')

        shutil.rmtree(demo_dir)
        manager.reload_now(trigger='test-manual-final-delete')
        _wait_until(lambda: manager.get_tool_descriptor('demo_echo') is None)
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_tool_runs_as_resource_tool(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace)

    target_file = workspace / 'temp' / 'target.txt'
    written_file = workspace / 'temp' / 'written.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('before value\n', encoding='utf-8')

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    service = _ArtifactService(artifact_store)

    manager = ResourceManager(workspace, app_config=_resource_app_config(poll_interval_ms=500))
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')

    try:
        write_tool = manager.get_tool('filesystem_write')
        edit_tool = manager.get_tool('filesystem_edit')
        delete_tool = manager.get_tool('filesystem_delete')
        patch_tool = manager.get_tool('filesystem_propose_patch')
        assert write_tool is not None
        assert edit_tool is not None
        assert delete_tool is not None
        assert patch_tool is not None
        assert 'Successfully wrote' in await write_tool.execute(path=str(written_file), content='hello\n')
        assert 'Successfully edited' in await edit_tool.execute(
            path=str(target_file),
            old_text='before value',
            new_text='after value',
        )

        result = json.loads(
            await patch_tool.execute(
                path=str(target_file),
                old_text='after value',
                new_text='patched value',
                summary='Patch target',
                __g3ku_runtime={
                    'task_id': 'task:test',
                    'node_id': 'node:test',
                    'session_key': 'session:test',
                },
            )
        )

        assert result['success'] is True
        assert result['summary'] == 'Patch target'
        assert target_file.read_text(encoding='utf-8') == 'after value\n'

        artifact_path = Path(result['artifact']['path'])
        assert artifact_path.exists()
        metadata, diff_text = parse_patch_artifact(artifact_path.read_text(encoding='utf-8'))
        assert Path(metadata['path']) == target_file.resolve()
        assert 'patched value' in diff_text
        deleted = json.loads(await delete_tool.execute(paths=[str(written_file)]))
        assert deleted['ok'] is True
        assert deleted['items'][0]['status'] == 'deleted'
        assert not written_file.exists()
    finally:
        manager.close()
        store.close()


def test_filesystem_split_tools_are_discoverable_and_merge_into_filesystem_family(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace)

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    snapshot = registry.discover()

    for tool_name in (
        'filesystem_write',
        'filesystem_edit',
        'filesystem_copy',
        'filesystem_move',
        'filesystem_delete',
        'filesystem_propose_patch',
    ):
        assert tool_name in snapshot.tools

    families = {item.tool_id: item for item in build_tool_families(list(snapshot.tools.values()))}
    family = families['filesystem']
    action_map = {action.action_id: action for action in family.actions}

    assert family.primary_executor_name == 'filesystem_write'
    assert set(family.metadata['sources']) == {
        'filesystem_write',
        'filesystem_edit',
        'filesystem_copy',
        'filesystem_move',
        'filesystem_delete',
        'filesystem_propose_patch',
    }
    assert 'filesystem_write' in action_map['write'].executor_names
    assert 'filesystem_edit' in action_map['edit'].executor_names
    assert 'filesystem_copy' in action_map['copy'].executor_names
    assert 'filesystem_move' in action_map['move'].executor_names
    assert 'filesystem_delete' in action_map['delete'].executor_names
    assert 'filesystem_propose_patch' in action_map['propose_patch'].executor_names


def test_content_split_tools_are_discoverable_and_merge_into_content_navigation_family(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')
    for tool_name in (
        'content_describe',
        'content_search',
        'content_open',
    ):
        shutil.copytree(REPO_ROOT / 'tools' / tool_name, workspace / 'tools' / tool_name)

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    snapshot = registry.discover()

    for tool_name in (
        'content',
        'content_describe',
        'content_search',
        'content_open',
    ):
        assert tool_name in snapshot.tools

    families = {item.tool_id: item for item in build_tool_families(list(snapshot.tools.values()))}
    family = families['content_navigation']
    action_map = {action.action_id: action for action in family.actions}

    assert set(family.metadata['sources']) == {
        'content',
        'content_describe',
        'content_search',
        'content_open',
    }
    assert 'content_describe' in action_map['describe'].executor_names
    assert 'content_search' in action_map['search'].executor_names
    assert 'content_open' in action_map['open'].executor_names


def test_tool_managed_builtin_family_defaults_to_deny_all_roles_on_first_discovery(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    snapshot = registry.discover()

    families = {item.tool_id: item for item in build_tool_families(list(snapshot.tools.values()))}
    family = families['exec_runtime']
    action_map = {action.action_id: action for action in family.actions}

    assert action_map['run'].allowed_roles == []


@pytest.mark.asyncio
async def test_content_split_tool_executes_with_legacy_content_settings(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    inside_file = workspace / 'allowed-dir' / 'inside.txt'
    outside_file = tmp_path / 'outside.txt'
    inside_file.parent.mkdir(parents=True, exist_ok=True)
    inside_file.write_text('inside\n', encoding='utf-8')
    outside_file.write_text('outside\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')
    shutil.copytree(REPO_ROOT / 'tools' / 'content_open', workspace / 'tools' / 'content_open')

    legacy_manifest = workspace / 'tools' / 'content' / 'resource.yaml'
    legacy_manifest.write_text(
        legacy_manifest.read_text(encoding='utf-8').replace('restrict_to_workspace: false', 'restrict_to_workspace: true'),
        encoding='utf-8',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content_open')
        assert tool is not None

        opened = json.loads(await tool.execute(path=str(inside_file), start_line=1, end_line=5))
        assert opened['ok'] is True
        assert opened['excerpt'] == 'inside'

        blocked = await tool.execute(path=str(outside_file), start_line=1, end_line=5)
        assert 'outside workspace' in blocked
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_tool_rejects_relative_paths(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('hello\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_write')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_write')
        assert tool is not None
        result = await tool.execute(path='target.txt', content='hello\n')
        assert 'relative path is not allowed; provide absolute path' in result
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_tool_reports_reliable_file_changes_only(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'temp' / 'target.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('before\n', encoding='utf-8')

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)

    class _RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, str]] = []

        def refresh_resource_paths(self, paths, *, trigger: str = 'path-change', session_id: str = 'web:shared'):
            _ = paths, trigger, session_id
            return {'ok': True}

        def record_node_file_change(self, task_id: str, node_id: str, *, path: str, change_type: str) -> None:
            self.calls.append((task_id, node_id, path, change_type))

    service = _RecordingService()
    tool = FilesystemTool(
        workspace=workspace,
        artifact_store=artifact_store,
        main_task_service=service,
        settings=FilesystemToolSettings(),
    )
    runtime = {
        'task_id': 'task:test',
        'node_id': 'node:test',
        'session_key': 'web:shared',
    }

    created_file = workspace / 'temp' / 'created.txt'
    result = await tool.write(path=str(created_file), content='hello\n', runtime=runtime)
    assert 'Successfully wrote' in result

    result = await tool.edit(
        path=str(target_file),
        old_text='before',
        new_text='after',
        runtime=runtime,
    )
    assert 'Successfully edited' in result

    result = json.loads(await tool.delete(paths=[str(created_file)], runtime=runtime))
    assert result['ok'] is True
    assert result['items'][0]['status'] == 'deleted'

    result = json.loads(
        tool.propose_patch(
            path=str(target_file),
            old_text='after',
            new_text='patched',
            summary='Patch target',
            runtime=runtime,
        )
    )
    assert result['success'] is True

    assert service.calls == [
        ('task:test', 'node:test', str(created_file.resolve()), 'created'),
        ('task:test', 'node:test', str(target_file.resolve()), 'modified'),
        ('task:test', 'node:test', str(created_file.resolve()), 'deleted'),
    ]
    store.close()


@pytest.mark.asyncio
async def test_filesystem_tool_rejects_artifact_refs_with_content_guidance(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_write')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_write')
        assert tool is not None
        result = await tool.execute(path='artifact:artifact:demo123', content='demo')
        assert 'content ref is not a filesystem path' in result
        assert 'use the content tool with ref=artifact:artifact:demo123' in result
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_content_search_overflow_requires_refine(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('\n'.join(['needle'] * 20) + '\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content')
        assert tool is not None
        payload = json.loads(await tool.execute(action='search', path=str(target_file), query='needle', limit=5))
        assert payload['ok'] is True
        assert payload['overflow'] is True
        assert payload['requires_refine'] is True
        assert payload['hits'] == []
        assert payload['count'] == 0
        assert payload['overflow_lower_bound'] == 6
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_content_search_split_tool_combines_ref_and_path_results_when_both_supplied(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    local_file = workspace / 'local.log'
    local_file.parent.mkdir(parents=True, exist_ok=True)
    local_file.write_text('alpha\nneedle in path\nomega\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')
    shutil.copytree(REPO_ROOT / 'tools' / 'content_search', workspace / 'tools' / 'content_search')

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    service = _ArtifactService(artifact_store)
    artifact = artifact_store.create_text_artifact(
        task_id='task:test',
        node_id='node:test',
        kind='tool_output',
        title='Artifact Search',
        content='zero\nneedle in ref\nend\n',
        extension='.log',
        mime_type='text/plain',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content_search')
        assert tool is not None

        payload = json.loads(
            await tool.execute(
                ref=f'artifact:{artifact.artifact_id}',
                path=str(local_file),
                query='needle',
                before=0,
                after=0,
            )
        )

        assert payload['ok'] is True
        assert payload['combined'] is True
        assert payload['partial'] is False
        assert payload['success_count'] == 2
        assert payload['failure_count'] == 0
        assert payload['targets']['ref']['ok'] is True
        assert payload['targets']['ref']['hits'][0]['line'] == 2
        assert payload['targets']['path']['ok'] is True
        assert payload['targets']['path']['hits'][0]['line'] == 2
    finally:
        manager.close()
        store.close()


@pytest.mark.asyncio
async def test_content_open_split_tool_returns_ref_success_and_path_error_when_both_supplied(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')
    shutil.copytree(REPO_ROOT / 'tools' / 'content_open', workspace / 'tools' / 'content_open')

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    service = _ArtifactService(artifact_store)
    artifact = artifact_store.create_text_artifact(
        task_id='task:test',
        node_id='node:test',
        kind='tool_output',
        title='Artifact Open',
        content='alpha\nneedle in ref\nomega\n',
        extension='.log',
        mime_type='text/plain',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content_open')
        assert tool is not None

        payload = json.loads(
            await tool.execute(
                ref=f'artifact:{artifact.artifact_id}',
                path='artifact:artifact:demo123',
                around_line=2,
                window=3,
            )
        )

        assert payload['ok'] is True
        assert payload['combined'] is True
        assert payload['partial'] is True
        assert payload['success_count'] == 1
        assert payload['failure_count'] == 1
        assert payload['targets']['ref']['ok'] is True
        assert 'needle in ref' in payload['targets']['ref']['excerpt']
        assert payload['targets']['path']['ok'] is False
        assert 'content ref must be passed via ref, not path' in payload['targets']['path']['error']
    finally:
        manager.close()
        store.close()


@pytest.mark.asyncio
async def test_filesystem_edit_line_range_mode(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('alpha\nbeta\ngamma\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_edit')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_edit')
        assert tool is not None
        result = await tool.execute(
            path=str(target_file),
            start_line=1,
            end_line=2,
            replacement='delta\nepsilon\n',
        )
        assert 'Successfully edited' in result
        assert target_file.read_text(encoding='utf-8') == 'delta\nepsilon\ngamma\n'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_edit_validation_failure_rolls_back(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.py'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('print("before")\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_edit')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_edit')
        assert tool is not None
        tool._handler._settings = tool._handler._settings.model_copy(
            update={
                'edit_validation_default_commands': [],
                'edit_validation_commands_by_ext': {
                    '.py': [f'{_python_launcher()} -c "import sys; sys.exit(1)"']
                },
            }
        )
        result = await tool.execute(
            path=str(target_file),
            old_text='print("before")\n',
            new_text='print("after")\n',
        )
        assert result.startswith('Error: Edit validation failed')
        assert target_file.read_text(encoding='utf-8') == 'print("before")\n'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_edit_rejects_mixed_modes(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('before\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_edit')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_edit')
        assert tool is not None
        result = await tool.execute(
            path=str(target_file),
            old_text='before',
            new_text='after',
            start_line=1,
            end_line=1,
            replacement='after\n',
        )
        assert result == 'Error: edit requires exactly one mode: text-replace or line-range'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_write_validation_failure_removes_new_file(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.py'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_write')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_write')
        assert tool is not None
        tool._handler._settings = tool._handler._settings.model_copy(
            update={
                'write_validation_default_commands': [],
                'write_validation_commands_by_ext': {
                    '.py': [f'{_python_launcher()} -c "import sys; sys.exit(1)"']
                },
            }
        )
        result = await tool.execute(path=str(target_file), content='print("after")\n')
        assert result.startswith('Error: Write validation failed')
        assert target_file.exists() is False
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_write_validation_failure_restores_existing_file(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.py'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('print("before")\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_write')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_write')
        assert tool is not None
        tool._handler._settings = tool._handler._settings.model_copy(
            update={
                'write_validation_default_commands': [],
                'write_validation_commands_by_ext': {
                    '.py': [f'{_python_launcher()} -c "import sys; sys.exit(1)"']
                },
            }
        )
        result = await tool.execute(path=str(target_file), content='print("after")\n')
        assert result.startswith('Error: Write validation failed')
        assert target_file.read_text(encoding='utf-8') == 'print("before")\n'
    finally:
        manager.close()




def test_external_tool_is_discovered_but_not_loaded_as_callable_instance(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    install_dir = workspace / '.g3ku' / 'external-tools' / 'external_browser'
    install_dir.mkdir(parents=True, exist_ok=True)
    _write_demo_external_tool(workspace, install_dir='.g3ku/external-tools/external_browser')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        descriptor = manager.get_tool_descriptor('external_browser')
        assert descriptor is not None
        assert descriptor.available is True
        assert descriptor.tool_type == 'external'
        assert descriptor.callable is False
        assert descriptor.install_dir == install_dir.resolve()
        assert manager.get_tool('external_browser') is None
        assert 'external_browser' not in manager.tool_instances()
    finally:
        manager.close()


def test_external_tool_rejects_main_dir_and_install_dir_inside_tools(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)

    with_main = _write_demo_external_tool(workspace, name='external_with_main')
    (with_main / 'main').mkdir(parents=True, exist_ok=True)

    inside_tools = _write_demo_external_tool(
        workspace,
        name='external_in_tools',
        install_dir='tools/external_in_tools/runtime',
    )
    _ = inside_tools

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    discovered = registry.discover().tools

    with_main_descriptor = discovered['external_with_main']
    assert with_main_descriptor.available is False
    assert any('must not contain main/' in err for err in with_main_descriptor.errors)

    inside_tools_descriptor = discovered['external_in_tools']
    assert inside_tools_descriptor.available is False
    assert any('outside the tools directory' in err for err in inside_tools_descriptor.errors)


@pytest.mark.asyncio
async def test_main_runtime_query_tools_load_from_resources(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    for name in ('task_summary_cn', 'task_fetch_cn', 'task_append_notice_cn', 'task_delete_cn', 'task_failed_nodes_cn', 'task_node_detail_cn', 'task_progress_cn', 'task_stats_cn'):
        shutil.copytree(REPO_ROOT / 'tools' / name, workspace / 'tools' / name)

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': _MainTaskService()})
    manager.reload_now(trigger='test-bind')

    try:
        summary_tool = manager.get_tool('task_summary')
        fetch_tool = manager.get_tool('task_list')
        append_notice_tool = manager.get_tool('task_append_notice')
        delete_tool = manager.get_tool('task_delete')
        failed_nodes_tool = manager.get_tool('task_failed_nodes')
        node_detail_tool = manager.get_tool('task_node_detail')
        progress_tool = manager.get_tool('task_progress')
        stats_tool = manager.get_tool('task_stats')

        assert summary_tool is not None
        assert fetch_tool is not None
        assert append_notice_tool is not None
        assert delete_tool is not None
        assert failed_nodes_tool is not None
        assert node_detail_tool is not None
        assert progress_tool is not None
        assert stats_tool is not None
        assert await summary_tool.execute(__g3ku_runtime={'session_key': 'web:shared'}) == 'summary:web:shared'
        assert await append_notice_tool.execute(
            __g3ku_runtime={'session_key': 'web:shared'},
            task_ids=['task:demo'],
            message='add notice',
        ) == 'append-notice:web:shared:task:demo:add notice'
        assert await fetch_tool.execute(__g3ku_runtime={'session_key': 'web:shared'}, **{'任务类型': 4}) == 'list:web:shared:4'
        assert await delete_tool.execute(mode='preview', **{'任务id列表': ['task:demo']}) == 'task-delete'
        assert await failed_nodes_tool.execute(**{'任务id': 'task:demo'}) == 'failed:task:demo'
        assert json.loads(await node_detail_tool.execute(**{'任务id': 'task:demo', '节点id': 'node:demo'})) == {
            'ok': True,
            'task_id': 'task:demo',
            'node_id': 'node:demo',
            'item': {'task_id': 'task:demo', 'node_id': 'node:demo', 'status': 'failed'},
            'artifact_count': 1,
            'artifacts': [
                {
                    'artifact_id': 'artifact:demo',
                    'task_id': 'task:demo',
                    'node_id': 'node:demo',
                    'kind': 'report',
                    'title': 'Demo Artifact',
                    'path': 'D:/demo.md',
                    'mime_type': 'text/markdown',
                    'preview_text': 'demo',
                    'created_at': '2026-03-25T00:00:00',
                    'ref': 'artifact:artifact:demo',
                }
            ],
        }
        assert await progress_tool.execute(**{'任务id': 'task:demo'}) == 'progress:task:demo:True'
        assert await stats_tool.execute(mode='id', **{'任务id列表': ['task:demo']}) == 'task-stats'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_exec_tool_reads_manifest_settings(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')

    manifest = workspace / 'tools' / 'exec' / 'resource.yaml'
    manifest.write_text(
        manifest.read_text(encoding='utf-8')
        .replace('timeout: 60', 'timeout: 7')
        .replace("path_append: ''", "path_append: 'D:/bin'")
        .replace('restrict_to_workspace: false', 'restrict_to_workspace: true'),
        encoding='utf-8',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('exec')
        assert tool is not None
        handler = tool._handler
        assert handler.timeout == 7
        assert handler.working_dir is None
        assert handler.workspace_root == str(workspace)
        assert handler.path_append == 'D:/bin'
        assert handler.restrict_to_workspace is True
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_tool_auto_refreshes_new_skill_without_full_reload(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_write')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    service = _ResourceSyncService()
    service.bind_resource_manager(manager)
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    manager.start()

    def _fail_reload(self, *, trigger: str = 'manual'):
        raise AssertionError(f'full reload should not run during targeted refresh: {trigger}')

    manager.reload_now = MethodType(_fail_reload, manager)

    try:
        tool = manager.get_tool('filesystem_write')
        assert tool is not None

        skill_root = workspace / 'skills' / 'auto_skill'
        manifest = textwrap.dedent(
            """\
            schema_version: 1
            kind: skill
            name: auto_skill
            description: Auto refreshed skill.
            trigger:
              keywords: []
            requires:
              tools: []
              bins: []
              env: []
            exposure:
              agent: true
              main_runtime: true
            """
        )
        body = '# auto_skill\n\nAuto skill guide\n'

        await tool.execute(path=str(skill_root / 'resource.yaml'), content=manifest)
        await tool.execute(path=str(skill_root / 'SKILL.md'), content=body)

        skill = manager.get_skill('auto_skill')
        assert skill is not None
        assert skill.available is True
        assert 'Auto skill guide' in manager.load_skill_body('auto_skill')
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_exec_tool_blocks_skill_file_creation_side_effects(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    service = _ResourceSyncService()
    service.bind_resource_manager(manager)
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    manager.start()

    try:
        tool = manager.get_tool('exec')
        assert tool is not None

        skill_root = workspace / 'skills' / 'exec_skill'
        manifest = textwrap.dedent(
            """\
            schema_version: 1
            kind: skill
            name: exec_skill
            description: Exec refreshed skill.
            trigger:
              keywords: []
            requires:
              tools: []
              bins: []
              env: []
            exposure:
              agent: true
              main_runtime: true
            """
        )
        body = '# exec_skill\n\nExec skill guide\n'
        script = (
            "from pathlib import Path;"
            f"root=Path(r'{skill_root}');"
            "root.mkdir(parents=True, exist_ok=True);"
            f"(root/'resource.yaml').write_text({manifest!r}, encoding='utf-8');"
            f"(root/'SKILL.md').write_text({body!r}, encoding='utf-8')"
        )
        payload = json.loads(await tool.execute(command=f'{_python_launcher()} -c "{script}"', __g3ku_runtime={'session_key': 'web:shared'}))

        assert payload['status'] == 'error'
        assert 'read-only' in str(payload.get('error') or '').lower()
        assert manager.get_skill('exec_skill') is None
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_exec_tool_externalizes_long_stdout(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    service = _ArtifactService(artifact_store)

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    try:
        exec_tool = manager.get_tool('exec')
        content_tool = manager.get_tool('content')
        assert exec_tool is not None
        assert content_tool is not None
        command = f'{_python_launcher()} -c "for i in range(90): print(\'line-%03d\' % i)"'
        payload = json.loads(await exec_tool.execute(command=command, __g3ku_runtime={'session_key': 'cli:test'}))
        assert payload['status'] == 'success'
        assert 'line-000' in payload['head_preview']
        assert 'command' not in payload
        assert 'stdout_ref' not in payload
        assert 'stderr_ref' not in payload
    finally:
        manager.close()
        store.close()


def test_content_navigation_externalizes_only_after_6000_chars(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)

    inline_text = "x" * 6000
    externalized_text = "x" * 6001

    inline = navigator.externalize_for_message(
        inline_text,
        runtime={"task_id": "task:test", "node_id": "node:inline"},
        display_name="inline-test",
        source_kind="tool_result:exec",
        compact=True,
    )
    externalized = navigator.externalize_for_message(
        externalized_text,
        runtime={"task_id": "task:test", "node_id": "node:externalized"},
        display_name="externalized-test",
        source_kind="tool_result:exec",
        compact=True,
    )

    assert parse_content_envelope(inline) is None
    envelope = parse_content_envelope(externalized)
    assert envelope is not None
    store.close()


@pytest.mark.asyncio
async def test_exec_tool_allows_paths_outside_workspace_by_default(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    outside_dir = tmp_path / 'outside'
    outside_dir.mkdir(parents=True, exist_ok=True)
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('exec')
        assert tool is not None

        outside_cwd = json.loads(
            await tool.execute(
                command='echo allowed',
                working_dir=str(outside_dir),
                __g3ku_runtime={'session_key': 'web:shared'},
            )
        )
        assert outside_cwd['status'] == 'success'

        outside_path = outside_dir / 'sample.txt'
        outside_path.write_text('allowed\n', encoding='utf-8')
        outside_path_result = json.loads(
            await tool.execute(
                command=f'type "{outside_path}"' if os.name == 'nt' else f'cat "{outside_path}"',
                __g3ku_runtime={'session_key': 'web:shared'},
            )
        )
        assert outside_path_result['status'] == 'success'
        assert 'allowed' in outside_path_result['head_preview']
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_exec_tool_blocks_mutating_commands_on_windows_and_posix(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('exec')
        assert tool is not None

        blocked_commands = [
            "Set-Content foo.txt 'x'",
            "Add-Content foo.txt 'x'",
            "Remove-Item foo.txt",
            "New-Item foo.txt -ItemType File",
            "cmd /c echo hi > foo.txt",
            "python -c \"open('foo.txt','w').write('x')\"",
            "touch foo.txt",
            "rm -f foo.txt",
            "mv a.txt b.txt",
            "cp a.txt b.txt",
            "mkdir out",
            "echo hi > foo.txt",
            "node -e \"require('fs').writeFileSync('foo.txt','x')\"",
            "sed -i 's/a/b/' foo.txt",
        ]

        for command in blocked_commands:
            payload = json.loads(await tool.execute(command=command, __g3ku_runtime={'session_key': 'web:shared'}))
            assert payload['status'] == 'error'
            assert 'read-only' in (payload.get('error') or '').lower()
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_exec_tool_blocks_paths_outside_workspace_when_restricted(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    outside_dir = tmp_path / 'outside'
    outside_dir.mkdir(parents=True, exist_ok=True)
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')

    manifest = workspace / 'tools' / 'exec' / 'resource.yaml'
    manifest.write_text(
        manifest.read_text(encoding='utf-8').replace('restrict_to_workspace: false', 'restrict_to_workspace: true'),
        encoding='utf-8',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('exec')
        assert tool is not None

        blocked_cwd = json.loads(
            await tool.execute(
                command='echo blocked',
                working_dir=str(outside_dir),
                __g3ku_runtime={'session_key': 'web:shared'},
            )
        )
        assert blocked_cwd['status'] == 'error'
        assert 'working_dir outside workspace' in blocked_cwd['error']

        outside_path = outside_dir / 'sample.txt'
        outside_path.write_text('blocked\n', encoding='utf-8')
        blocked_path = json.loads(
            await tool.execute(
                command=f'type "{outside_path}"' if os.name == 'nt' else f'cat "{outside_path}"',
                working_dir=str(workspace),
                __g3ku_runtime={'session_key': 'web:shared'},
            )
        )
        assert blocked_path['status'] == 'error'
        assert 'path outside workspace' in blocked_path['error']
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_tool_rejects_removed_read_actions(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace)

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        assert manager.get_tool('filesystem') is None
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_content_tool_reads_externalized_artifact_refs(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    service = _ArtifactService(artifact_store)
    artifact = artifact_store.create_text_artifact(
        task_id='task:test',
        node_id='node:test',
        kind='tool_output',
        title='Large stdout',
        content='alpha\nbeta\nneedle\nomega\n',
        extension='.log',
        mime_type='text/plain',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content')
        assert tool is not None
        described = json.loads(await tool.execute(action='describe', ref=f'artifact:{artifact.artifact_id}'))
        assert described['handle']['artifact_id'] == artifact.artifact_id
        searched = json.loads(await tool.execute(action='search', ref=f'artifact:{artifact.artifact_id}', query='needle'))
        assert searched['hits'][0]['line'] == 3
        opened = json.loads(await tool.execute(action='open', ref=f'artifact:{artifact.artifact_id}', around_line=3, window=3))
        assert 'needle' in opened['excerpt']
    finally:
        manager.close()
        store.close()


@pytest.mark.asyncio
async def test_content_tool_rejects_relative_paths(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'relative.log'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('alpha\nneedle\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content')
        assert tool is not None
        blocked = json.loads(await tool.execute(action='search', path='relative.log', query='needle'))
        assert blocked['ok'] is False
        assert 'relative path is not allowed; provide absolute path' in blocked['error']
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_content_tool_rejects_artifact_refs_in_path_mode(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content')
        assert tool is not None
        blocked = json.loads(await tool.execute(action='search', path='artifact:artifact:demo123', query='needle'))
        assert blocked['ok'] is False
        assert 'content ref must be passed via ref, not path' in blocked['error']
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_content_tool_reads_absolute_paths_outside_workspace_by_default(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    outside_file = tmp_path / 'outside.log'
    outside_file.write_text('alpha\nbeta\nneedle\nomega\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content')
        assert tool is not None
        searched = json.loads(await tool.execute(action='search', path=str(outside_file), query='needle'))
        assert searched['ok'] is True
        assert searched['hits'][0]['line'] == 3
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_content_tool_blocks_absolute_paths_outside_workspace_when_restricted(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    outside_file = tmp_path / 'outside.log'
    outside_file.write_text('alpha\nbeta\nneedle\nomega\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    manifest = workspace / 'tools' / 'content' / 'resource.yaml'
    manifest.write_text(
        manifest.read_text(encoding='utf-8').replace('restrict_to_workspace: false', 'restrict_to_workspace: true'),
        encoding='utf-8',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content')
        assert tool is not None
        blocked = json.loads(await tool.execute(action='search', path=str(outside_file), query='needle'))
        assert blocked['ok'] is False
        assert 'outside workspace' in blocked['error']
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_content_tool_rejects_artifact_refs_outside_acceptance_allowlist(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    service = _ArtifactService(artifact_store)
    allowed_artifact = artifact_store.create_text_artifact(
        task_id='task:test',
        node_id='node:allowed',
        kind='tool_output',
        title='Allowed',
        content='alpha\nbeta\n',
        extension='.log',
        mime_type='text/plain',
    )
    blocked_artifact = artifact_store.create_text_artifact(
        task_id='task:test',
        node_id='node:blocked',
        kind='tool_output',
        title='Blocked',
        content='gamma\ndelta\n',
        extension='.log',
        mime_type='text/plain',
    )

    runtime = {
        'task_id': 'task:test',
        'node_id': 'node:acceptance',
        'node_kind': 'acceptance',
        'enforce_content_ref_allowlist': True,
        'allowed_content_refs': [f'artifact:{allowed_artifact.artifact_id}'],
    }

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content')
        assert tool is not None

        opened = json.loads(
            await tool.execute(
                action='open',
                ref=f'artifact:{allowed_artifact.artifact_id}',
                start_line=1,
                end_line=5,
                __g3ku_runtime=runtime,
            )
        )
        assert opened['ok'] is True
        assert 'alpha' in opened['excerpt']

        blocked = json.loads(
            await tool.execute(
                action='open',
                ref=f'artifact:{blocked_artifact.artifact_id}',
                start_line=1,
                end_line=5,
                __g3ku_runtime=runtime,
            )
        )
        assert blocked['ok'] is False
        assert blocked['requested_ref'] == f'artifact:{blocked_artifact.artifact_id}'
        assert blocked['allowed_refs'] == [f'artifact:{allowed_artifact.artifact_id}']
    finally:
        manager.close()
        store.close()


@pytest.mark.asyncio
async def test_content_tool_accepts_wrapped_content_when_allowlist_uses_wrapper_or_canonical_ref(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'content', workspace / 'tools' / 'content')

    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    service = _ArtifactService(artifact_store)
    navigator = service.content_store
    inner = navigator.maybe_externalize_text(
        'alpha\nallowed\nomega\n',
        runtime={'task_id': 'task:test', 'node_id': 'node:inner'},
        display_name='inner',
        source_kind='node_output',
        force=True,
    )
    wrapped = navigator.maybe_externalize_text(
        json.dumps(inner.to_dict(), ensure_ascii=False),
        runtime={'task_id': 'task:test', 'node_id': 'node:wrapper'},
        display_name='wrapped',
        source_kind='tool_result:content',
        force=True,
    )
    assert inner is not None
    assert wrapped is not None

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('content')
        assert tool is not None

        canonical_allowed = json.loads(
            await tool.execute(
                action='open',
                ref=wrapped.ref,
                start_line=1,
                end_line=5,
                __g3ku_runtime={
                    'task_id': 'task:test',
                    'node_id': 'node:acceptance',
                    'node_kind': 'acceptance',
                    'enforce_content_ref_allowlist': True,
                    'allowed_content_refs': [inner.ref],
                },
            )
        )
        wrapper_allowed = json.loads(
            await tool.execute(
                action='open',
                ref=inner.ref,
                start_line=1,
                end_line=5,
                __g3ku_runtime={
                    'task_id': 'task:test',
                    'node_id': 'node:acceptance',
                    'node_kind': 'acceptance',
                    'enforce_content_ref_allowlist': True,
                    'allowed_content_refs': [wrapped.ref],
                },
            )
        )

        assert canonical_allowed['ok'] is True
        assert canonical_allowed['resolved_ref'] == inner.ref
        assert 'allowed' in canonical_allowed['excerpt']
        assert wrapper_allowed['ok'] is True
        assert wrapper_allowed['resolved_ref'] == inner.ref
        assert 'allowed' in wrapper_allowed['excerpt']
    finally:
        manager.close()
        store.close()


def test_content_navigation_reuses_identical_artifacts_and_tracks_origin_ref(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )

    text = ('line-001\n' * 800).strip()
    first = navigator.maybe_externalize_text(
        text,
        runtime={'task_id': 'task:test', 'node_id': 'node:one'},
        display_name='first',
        source_kind='node_input',
    )
    second = navigator.maybe_externalize_text(
        text,
        runtime={'task_id': 'task:test', 'node_id': 'node:two'},
        display_name='second',
        source_kind='react_user',
    )

    assert first is not None
    assert second is not None
    assert first.ref == second.ref
    assert 'Use content.search/open with ref=' in first.summary
    assert 'Do not pass this ref as filesystem path.' in first.summary
    assert len(store.list_artifacts('task:test')) == 1

    wrapped = navigator.maybe_externalize_text(
        json.dumps(
            {
                'ok': True,
                'ref': first.ref,
                'handle': {'ref': first.ref},
                'payload': 'x' * 1500,
            },
            ensure_ascii=False,
        ),
        runtime={'task_id': 'task:test', 'node_id': 'node:three'},
        display_name='wrapped',
        source_kind='tool_result:content',
        force=True,
    )

    assert wrapped is not None
    assert wrapped.handle is not None
    assert wrapped.handle.origin_ref == first.ref

    store.close()


def test_content_navigation_uses_singleton_runtime_frame_message_artifact(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )

    first = navigator.maybe_externalize_text(
        json.dumps([{'role': 'user', 'content': 'first'}], ensure_ascii=False, indent=2),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='runtime-frame-messages:node:test',
        source_kind='task_runtime_messages',
        force=True,
    )
    second = navigator.maybe_externalize_text(
        json.dumps([{'role': 'user', 'content': 'second'}], ensure_ascii=False, indent=2),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='runtime-frame-messages:node:test',
        source_kind='task_runtime_messages',
        force=True,
    )

    assert first is not None
    assert second is not None
    assert first.ref == second.ref
    artifacts = store.list_artifacts('task:test')
    runtime_artifacts = [item for item in artifacts if item.kind == 'task_runtime_messages']
    assert len(runtime_artifacts) == 1
    assert 'second' in Path(runtime_artifacts[0].path).read_text(encoding='utf-8')

    store.close()


def test_content_navigation_uses_singleton_execution_trace_artifact(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )

    first = navigator.maybe_externalize_text(
        json.dumps({'stage': 'first'}, ensure_ascii=False, indent=2),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='execution-trace:node:test',
        source_kind='task_execution_trace',
        force=True,
    )
    second = navigator.maybe_externalize_text(
        json.dumps({'stage': 'second'}, ensure_ascii=False, indent=2),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='execution-trace:node:test',
        source_kind='task_execution_trace',
        force=True,
    )

    assert first is not None
    assert second is not None
    assert first.ref == second.ref
    artifacts = [
        item
        for item in store.list_artifacts('task:test')
        if item.kind == 'task_execution_trace' and item.node_id == 'node:test'
    ]
    assert len(artifacts) == 1
    assert 'second' in Path(artifacts[0].path).read_text(encoding='utf-8')

    store.close()


def test_prepare_messages_for_model_uses_compact_content_refs(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )

    prepared = navigator.prepare_messages_for_model(
        [
            {
                'role': 'user',
                'content': json.dumps(
                    {
                        'task_id': 'task:test',
                        'node_id': 'node:test',
                        'node_kind': 'execution',
                        'goal': 'inspect',
                        'prompt': 'inspect',
                    },
                    ensure_ascii=False,
                ),
            },
            {
                'role': 'tool',
                'content': 'line-001\n' * 800,
            },
        ],
        runtime={'task_id': 'task:test', 'node_id': 'node:test', 'node_kind': 'execution'},
        source_prefix='react',
    )

    assert prepared[0]['content']
    raw_payload = json.loads(prepared[1]['content'])
    assert set(raw_payload.keys()) == {'type', 'summary', 'ref', 'resolved_ref', 'wrapper_ref', 'next_actions'}
    assert 'Head preview:' not in raw_payload['summary']
    assert raw_payload['ref'].startswith('artifact:')
    assert raw_payload['resolved_ref'].startswith('artifact:')
    assert raw_payload['wrapper_ref'] in {'', raw_payload['ref']}
    parsed = parse_content_envelope(prepared[1]['content'])
    assert parsed is not None
    assert parsed.handle is None

    store.close()


def test_externalize_for_message_keeps_open_excerpt_inline(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )

    file_path = tmp_path / 'demo.txt'
    file_path.write_text('\n'.join(f'line-{index:03d}' for index in range(1, 161)), encoding='utf-8')
    opened = navigator.open(path=str(file_path), start_line=40, end_line=120)

    rendered = navigator.externalize_for_message(
        json.dumps(opened, ensure_ascii=False),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='tool-open',
        source_kind='tool_result:filesystem',
        compact=True,
    )

    assert parse_content_envelope(rendered) is None
    payload = json.loads(str(rendered))
    assert payload['start_line'] == 40
    assert payload['end_line'] == 120
    assert 'line-040' in payload['excerpt']
    assert 'line-120' in payload['excerpt']

    store.close()


@pytest.mark.parametrize(
    ('source_kind', 'uri'),
    [
        ('tool_result:load_skill_context', 'g3ku://skill/full_body_skill'),
        ('tool_result:load_tool_context', 'g3ku://resource/tool/content'),
    ],
)
def test_externalize_for_message_keeps_direct_load_context_inline(tmp_path: Path, source_kind: str, uri: str):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    payload = _large_direct_load_payload(uri=uri, body_label=source_kind.replace(':', '_'))
    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='load-context',
        source_kind=source_kind,
        compact=True,
    )

    assert parse_content_envelope(rendered) is None
    parsed = json.loads(str(rendered))
    assert parsed == payload

    store.close()


@pytest.mark.parametrize(
    ('source_kind', 'uri'),
    [
        ('tool_result:load_skill_context', 'g3ku://skill/full_body_skill'),
        ('tool_result:load_tool_context', 'g3ku://resource/tool/content'),
    ],
)
def test_direct_load_tools_remain_inline_without_manifest_flag(tmp_path: Path, source_kind: str, uri: str):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    payload = _large_direct_load_payload(uri=uri, body_label=source_kind.replace(':', '_'))

    assert (
        navigator.maybe_externalize_text(
            json.dumps(payload, ensure_ascii=False),
            runtime={'task_id': 'task:test', 'node_id': 'node:test'},
            display_name='load-context',
            source_kind=source_kind,
        )
        is None
    )
    assert (
        navigator.maybe_externalize_text(
            json.dumps(payload, ensure_ascii=False),
            runtime={'task_id': 'task:test', 'node_id': 'node:test'},
            display_name='load-context',
            source_kind=source_kind,
            delivery_metadata={'tool_result_inline_full': False},
        )
        is None
    )

    store.close()


@pytest.mark.parametrize(
    'source_kind',
    [
        'tool_result:memory_note',
        'tool_result:create_async_task_cn',
        'tool_result:task_failed_nodes_cn',
        'tool_result:task_fetch_cn',
        'tool_result:task_node_detail_cn',
        'tool_result:task_progress_cn',
        'tool_result:task_summary_cn',
    ],
)
def test_externalize_for_message_keeps_named_inline_exception_tools_inline(tmp_path: Path, source_kind: str):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    payload = {
        'ok': True,
        'tool': source_kind,
        'items': [
            {
                'id': f'item-{index:03d}',
                'text': 'inline exception payload ' + ('x' * 80),
            }
            for index in range(60)
        ],
    }
    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='named-inline-exception',
        source_kind=source_kind,
        compact=True,
    )

    assert parse_content_envelope(rendered) is None
    parsed = json.loads(str(rendered))
    assert parsed == payload

    store.close()


def test_externalize_for_message_still_externalizes_large_non_direct_load_search_payload(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    payload = {
        'ok': True,
        'mode': 'search',
        'query': 'long search',
        'candidates': [
            {
                'tool_id': f'tool-{index:03d}',
                'description': 'candidate description ' + ('x' * 80),
            }
            for index in range(40)
        ],
        'next_action_hint': 'Call load_tool_context(tool_id="<tool_id>") to load details for a candidate.',
    }
    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='tool-search',
        source_kind='tool_result:load_tool_context',
        compact=True,
    )

    envelope = parse_content_envelope(rendered)
    assert envelope is not None
    assert envelope.ref.startswith('artifact:')

    store.close()


def test_externalizes_to_summary_and_ref_for_large_non_opt_in_tool_result(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    payload = {
        'ok': True,
        'stdout': '\n'.join(f'line {index:03d} ' + ('x' * 32) for index in range(180)),
    }

    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='large-tool-result',
        source_kind='tool_result:exec',
        compact=True,
    )

    envelope = parse_content_envelope(rendered)
    assert envelope is not None
    assert envelope.ref.startswith('artifact:')
    assert 'Use content.search/open with ref=' in envelope.summary

    store.close()


def test_small_non_opt_in_tool_result_stays_inline(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )
    payload = {
        'ok': True,
        'stdout': 'short output',
        'exit_code': 0,
    }

    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={'task_id': 'task:test', 'node_id': 'node:test'},
        display_name='small-tool-result',
        source_kind='tool_result:exec',
        compact=True,
    )

    assert parse_content_envelope(rendered) is None
    assert json.loads(rendered) == payload

    store.close()


def test_content_navigation_search_uses_canonical_ref_for_wrapped_content(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    inner = navigator.maybe_externalize_text("alpha\nneedle\nomega\n", runtime={"task_id": "task:test", "node_id": "node:inner"}, display_name="inner", source_kind="node_output", force=True)
    wrapped = navigator.maybe_externalize_text(json.dumps(inner.to_dict(), ensure_ascii=False), runtime={"task_id": "task:test", "node_id": "node:wrapper"}, display_name="wrapped", source_kind="tool_result:content", force=True)
    result = navigator.search(ref=wrapped.ref, query="needle")
    assert result["count"] == 1
    assert result["resolved_ref"] == inner.ref
    store.close()


def test_content_navigation_keeps_small_spawn_child_results_inline(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    payload = {
        "children": [
            {
                "goal": "child goal",
                "check_result": "未检验",
                "node_output": "summary " + ("x" * 1400),
                "node_output_summary": "summary " + ("x" * 1400),
                "node_output_ref": "artifact:artifact:child-output",
            }
        ]
    }
    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={"task_id": "task:test", "node_id": "node:spawn"},
        display_name="spawn-child-results",
        source_kind="tool_result:spawn_child_nodes",
        compact=True,
    )
    assert parse_content_envelope(rendered) is None
    assert json.loads(rendered) == payload
    store.close()


def test_content_navigation_keeps_small_search_results_inline(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    payload = {
        "ok": True,
        "ref": "artifact:artifact:search-wrapper",
        "requested_ref": "artifact:artifact:search-wrapper",
        "resolved_ref": "artifact:artifact:search-target",
        "wrapper_ref": "artifact:artifact:search-wrapper",
        "wrapper_depth": 1,
        "query": "needle",
        "hits": [
            {
                "line": 1,
                "preview": "needle " + ("y" * 1500),
            }
        ],
        "count": 1,
        "overflow": False,
        "requires_refine": False,
        "cap": 20,
        "overflow_lower_bound": None,
        "message": "",
        "suggestions": [],
    }
    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={"task_id": "task:test", "node_id": "node:search"},
        display_name="search-results",
        source_kind="tool_result:content",
        compact=True,
    )
    assert parse_content_envelope(rendered) is None
    assert json.loads(rendered) == payload
    store.close()


def test_content_navigation_externalized_tool_summary_uses_invocation_text_not_head_preview(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    payload = {
        "status": "success",
        "stdout_ref": "artifact:artifact:stdout",
        "head_preview": "line one\nline two\nline three",
        "stdout": "x" * 7000,
    }
    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={"task_id": "task:test", "node_id": "node:test"},
        display_name="tool:exec",
        source_kind="tool_result:exec",
        compact=True,
        delivery_metadata={"invocation_text": "exec(command=python gather.py, working_dir=D:\\NewProjects\\G3KU)"},
    )
    envelope = parse_content_envelope(rendered)
    assert envelope is not None
    assert "Invocation: exec(command=python gather.py, working_dir=D:\\NewProjects\\G3KU)" in envelope.summary
    assert "Head preview" not in envelope.summary
    store.close()


def test_content_navigation_wrapper_summary_flattens_to_canonical_summary(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    inner = navigator.maybe_externalize_text(
        "canonical body\n" * 400,
        runtime={"task_id": "task:test", "node_id": "node:inner"},
        display_name="inner-body",
        source_kind="node_output",
        force=True,
    )
    assert inner is not None
    wrapped = navigator.maybe_externalize_text(
        json.dumps(inner.to_dict(), ensure_ascii=False),
        runtime={"task_id": "task:test", "node_id": "node:wrapper"},
        display_name="wrapped",
        source_kind="tool_result:exec",
        force=True,
    )
    assert wrapped is not None
    assert "Origin ref:" not in wrapped.summary
    assert str(inner.ref) in wrapped.summary
    store.close()


def test_content_navigation_keeps_large_content_open_results_inline(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    payload = {
        "ok": True,
        "ref": "artifact:artifact:open-wrapper",
        "requested_ref": "artifact:artifact:open-wrapper",
        "resolved_ref": "artifact:artifact:open-canonical",
        "wrapper_ref": "artifact:artifact:open-wrapper",
        "wrapper_depth": 1,
        "start_line": 1,
        "end_line": 200,
        "excerpt": "\n".join(f"line {index}" for index in range(1, 201)),
    }
    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={"task_id": "task:test", "node_id": "node:open"},
        display_name="open-results",
        source_kind="tool_result:content_open",
        compact=True,
    )
    assert parse_content_envelope(rendered) is None
    assert json.loads(rendered) == payload
    store.close()


def test_content_navigation_keeps_large_content_search_results_inline(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    payload = {
        "ok": True,
        "ref": "artifact:artifact:search-wrapper",
        "requested_ref": "artifact:artifact:search-wrapper",
        "resolved_ref": "artifact:artifact:search-canonical",
        "wrapper_ref": "artifact:artifact:search-wrapper",
        "wrapper_depth": 1,
        "query": "needle",
        "hits": [
            {"line": index, "preview": f"needle preview {index} " + ("y" * 120)}
            for index in range(1, 16)
        ],
        "count": 15,
        "overflow": False,
        "requires_refine": False,
        "cap": 20,
        "overflow_lower_bound": None,
        "message": "",
        "suggestions": [],
    }
    rendered = navigator.externalize_for_message(
        json.dumps(payload, ensure_ascii=False),
        runtime={"task_id": "task:test", "node_id": "node:search"},
        display_name="search-results",
        source_kind="tool_result:content_search",
        compact=True,
    )
    assert parse_content_envelope(rendered) is None
    assert json.loads(rendered) == payload
    store.close()


def test_content_navigation_open_raw_view_reads_wrapper_json(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    inner = navigator.maybe_externalize_text("canonical body", runtime={"task_id": "task:test", "node_id": "node:inner"}, display_name="inner", source_kind="node_output", force=True)
    wrapped = navigator.maybe_externalize_text(json.dumps(inner.to_dict(), ensure_ascii=False), runtime={"task_id": "task:test", "node_id": "node:wrapper"}, display_name="wrapped", source_kind="tool_result:content", force=True)
    raw_result = navigator.open(ref=wrapped.ref, view="raw", start_line=1, end_line=20)
    assert raw_result["resolved_ref"] == wrapped.ref
    assert inner.ref in raw_result["excerpt"]
    store.close()


def test_content_navigation_resolves_nested_wrappers_hop_by_hop(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    inner = navigator.maybe_externalize_text("alpha\nneedle\nomega\n", runtime={"task_id": "task:test", "node_id": "node:inner"}, display_name="inner", source_kind="node_output", force=True)
    middle = navigator.maybe_externalize_text(json.dumps(inner.to_dict(), ensure_ascii=False), runtime={"task_id": "task:test", "node_id": "node:middle"}, display_name="middle", source_kind="tool_result:content", force=True)
    outer = navigator.maybe_externalize_text(json.dumps(middle.to_dict(), ensure_ascii=False), runtime={"task_id": "task:test", "node_id": "node:outer"}, display_name="outer", source_kind="tool_result:content", force=True)
    result = navigator.search(ref=outer.ref, query="needle")
    assert result["count"] == 1
    assert result["requested_ref"] == outer.ref
    assert result["resolved_ref"] == inner.ref
    assert result["wrapper_ref"] == outer.ref
    assert result["wrapper_depth"] == 2
    store.close()


def test_content_navigation_search_overflow_keeps_wrapper_metadata(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    inner = navigator.maybe_externalize_text("\n".join(["needle"] * 12), runtime={"task_id": "task:test", "node_id": "node:inner"}, display_name="inner", source_kind="node_output", force=True)
    wrapped = navigator.maybe_externalize_text(json.dumps(inner.to_dict(), ensure_ascii=False), runtime={"task_id": "task:test", "node_id": "node:wrapper"}, display_name="wrapped", source_kind="tool_result:content", force=True)
    result = navigator.search(ref=wrapped.ref, query="needle", limit=3)
    assert result["overflow"] is True
    assert result["requires_refine"] is True
    assert result["requested_ref"] == wrapped.ref
    assert result["resolved_ref"] == inner.ref
    assert result["wrapper_ref"] == wrapped.ref
    assert result["wrapper_depth"] == 1
    store.close()


def test_content_navigation_detects_wrapper_ref_cycles(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    a = artifact_store.create_text_artifact(task_id="task:test", node_id="node:a", kind="tool_result:content", title="A", content="seed-a")
    b = artifact_store.create_text_artifact(task_id="task:test", node_id="node:b", kind="tool_result:content", title="B", content="seed-b")
    Path(a.path).write_text(json.dumps({"type": "content_ref", "ref": f"artifact:{b.artifact_id}"}), encoding="utf-8")
    Path(b.path).write_text(json.dumps({"type": "content_ref", "ref": f"artifact:{a.artifact_id}"}), encoding="utf-8")
    with pytest.raises(ValueError, match="content ref cycle detected"):
        navigator.describe(ref=f"artifact:{a.artifact_id}")
    store.close()


def test_content_navigation_detects_self_referential_wrapper_cycles(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    wrapper = artifact_store.create_text_artifact(task_id="task:test", node_id="node:self", kind="tool_result:content", title="Self", content="seed-self")
    wrapper_ref = f"artifact:{wrapper.artifact_id}"
    Path(wrapper.path).write_text(json.dumps({"type": "content_ref", "ref": wrapper_ref}), encoding="utf-8")
    with pytest.raises(ValueError, match="content ref cycle detected"):
        navigator.describe(ref=wrapper_ref)
    store.close()


def test_content_navigation_populates_path_ref_metadata(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    target = tmp_path / "notes.txt"
    target.write_text("line one\nline two\n", encoding="utf-8")
    result = navigator.describe(path=str(target))
    assert result["requested_ref"] == "path:notes.txt"
    assert result["resolved_ref"] == "path:notes.txt"
    assert result["wrapper_ref"] == ""
    assert result["wrapper_depth"] == 0
    store.close()


def test_content_navigation_reads_workspace_relative_path_refs(tmp_path: Path):
    navigator = ContentNavigationService(workspace=tmp_path, allowed_dir=tmp_path)
    target = tmp_path / "g3ku" / "config" / "loader.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")

    result = navigator.read(ref="path:g3ku/config/loader.py")

    assert result["content"] == "VALUE = 1\n"
    assert result["requested_ref"] == "path:g3ku/config/loader.py"
    assert result["resolved_ref"] == "path:g3ku/config/loader.py"
    assert result["wrapper_ref"] == ""
    assert result["wrapper_depth"] == 0


def test_content_summary_and_ref_uses_canonical_ref_for_content_envelopes(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    inner = navigator.maybe_externalize_text("canonical body", runtime={"task_id": "task:test", "node_id": "node:inner"}, display_name="inner", source_kind="node_output", force=True)
    wrapped = navigator.maybe_externalize_text(json.dumps(inner.to_dict(), ensure_ascii=False), runtime={"task_id": "task:test", "node_id": "node:wrapper"}, display_name="wrapped", source_kind="tool_result:content", force=True)
    summary, ref = content_summary_and_ref(wrapped.to_dict())
    assert summary == wrapped.summary
    assert ref == inner.ref
    store.close()


def test_content_rest_routes_use_runtime_service_methods_and_preserve_legacy_artifact_excerpt_behavior(monkeypatch):
    class _StubService:
        def __init__(self):
            self.describe_calls = []
            self.search_calls = []
            self.open_calls = []
            self.artifact = SimpleNamespace(
                artifact_id="artifact:wrapper",
                task_id="task:test",
                path="",
                model_dump=lambda mode="json": {
                    "artifact_id": "artifact:wrapper",
                    "task_id": "task:test",
                    "path": "",
                },
            )

        async def startup(self):
            return None

        def normalize_task_id(self, task_id: str) -> str:
            return task_id

        def get_artifact(self, artifact_id: str):
            return self.artifact if artifact_id == self.artifact.artifact_id else None

        def describe_content(self, *, ref=None, path=None, view="canonical"):
            self.describe_calls.append({"ref": ref, "path": path, "view": view})
            return {"ok": True, "summary": "desc", "ref": ref, "requested_ref": ref, "resolved_ref": ref, "wrapper_ref": "", "wrapper_depth": 0}

        def search_content(self, *, query, ref=None, path=None, view="canonical", limit=10, before=2, after=2):
            self.search_calls.append({"query": query, "ref": ref, "path": path, "view": view, "limit": limit, "before": before, "after": after})
            return {"ok": True, "ref": ref, "requested_ref": ref, "resolved_ref": ref, "wrapper_ref": "", "wrapper_depth": 0, "query": query, "hits": [], "count": 0, "overflow": False, "requires_refine": False, "cap": limit, "overflow_lower_bound": None, "message": "", "suggestions": []}

        def open_content(self, *, ref=None, path=None, view="canonical", start_line=None, end_line=None, around_line=None, window=None):
            self.open_calls.append({"ref": ref, "path": path, "view": view, "start_line": start_line, "end_line": end_line, "around_line": around_line, "window": window})
            excerpt = "wrapper body" if view == "raw" else "canonical body"
            return {"ok": True, "ref": ref, "requested_ref": ref, "resolved_ref": ref if view == "raw" else "artifact:artifact:inner", "wrapper_ref": "" if view == "raw" else ref, "wrapper_depth": 0 if view == "raw" else 1, "start_line": 1, "end_line": 1, "excerpt": excerpt}

        def read_content(self, *, ref=None, path=None, view="canonical"):
            self.read_calls.append({"ref": ref, "path": path, "view": view})
            return {"ok": True, "ref": ref, "requested_ref": ref, "resolved_ref": "artifact:artifact:inner", "wrapper_ref": ref, "wrapper_depth": 1, "content": "canonical full body"}

    service = _StubService()
    service.read_calls = []
    monkeypatch.setattr(api_rest, "_service", lambda: service)

    app = FastAPI()
    app.include_router(api_rest.router, prefix="/api")
    client = TestClient(app)

    artifact_response = client.get("/api/tasks/task:test/artifacts/artifact:wrapper")
    assert artifact_response.status_code == 200
    assert artifact_response.json()["content"] == "wrapper body"

    describe_response = client.get("/api/content/describe", params={"ref": "artifact:artifact:wrapper"})
    assert describe_response.status_code == 200
    search_response = client.get("/api/content/search", params={"ref": "artifact:artifact:wrapper", "query": "needle"})
    assert search_response.status_code == 200
    open_response = client.get("/api/content/open", params={"ref": "artifact:artifact:wrapper"})
    assert open_response.status_code == 200
    assert open_response.json()["excerpt"] == "canonical body"
    read_response = client.get("/api/content/read", params={"ref": "artifact:artifact:wrapper"})
    assert read_response.status_code == 200
    assert read_response.json()["content"] == "canonical full body"

    assert service.open_calls[0]["view"] == "raw"
    assert service.describe_calls == [{"ref": "artifact:artifact:wrapper", "path": None, "view": "canonical"}]
    assert service.search_calls == [{"query": "needle", "ref": "artifact:artifact:wrapper", "path": None, "view": "canonical", "limit": 10, "before": 2, "after": 2}]
    assert service.open_calls[1]["view"] == "canonical"
    assert service.read_calls == [{"ref": "artifact:artifact:wrapper", "path": None, "view": "canonical"}]
@pytest.mark.xfail(reason="memory_write contract is being replaced by queued markdown memory requests")
@pytest.mark.asyncio
async def test_memory_write_uses_structured_fact_contract(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_write', workspace / 'tools' / 'memory_write')

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def upsert_structured_memory_facts(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'written': 1}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_write']
    manifest = yaml.safe_load((workspace / 'tools' / 'memory_write' / 'resource.yaml').read_text(encoding='utf-8')) or {}
    item_properties = (
        manifest.get('parameters', {})
        .get('properties', {})
        .get('facts', {})
        .get('items', {})
        .get('properties', {})
    )
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=False), 'memory_manager': manager},
    )

    payload = json.loads(
        await tool.execute(
            facts=[
                {
                    'category': 'default_setting',
                    'scope': 'global',
                    'entity': 'user',
                    'attribute': 'preferred_package_manager',
                    'value': 'pnpm',
                    'observed_at': '2026-04-08T00:00:00',
                    'time_semantics': 'durable_until_replaced',
                    'source_excerpt': '以后默认用 pnpm',
                    'qualifier': {'project': 'g3ku'},
                    'expires_at': None,
                }
            ],
            __g3ku_runtime={'session_key': 'cli:demo'},
        )
    )
    assert payload['ok'] is True
    assert manager.last_call['channel'] == 'cli'
    assert manager.last_call['chat_id'] == 'demo'
    assert manager.last_call['facts'][0]['attribute'] == 'preferred_package_manager'
    assert item_properties['merge_mode']['enum'] == ['merge']


@pytest.mark.xfail(reason="memory_write no longer parses structured fact payloads")
@pytest.mark.asyncio
async def test_memory_write_restores_json_like_value_strings_before_upsert(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_write', workspace / 'tools' / 'memory_write')

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def upsert_structured_memory_facts(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'written': 1}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_write']
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=False), 'memory_manager': manager},
    )

    payload = json.loads(
        await tool.execute(
            facts=[
                {
                    'category': 'preference',
                    'scope': 'user',
                    'entity': 'response_style',
                    'attribute': 'format',
                    'value': '{"tone":"concise","markdown":true}',
                    'observed_at': '2026-04-08T00:00:00',
                    'time_semantics': 'durable_until_replaced',
                    'source_excerpt': 'remember the response format',
                    'merge_mode': 'merge',
                }
            ],
            __g3ku_runtime={'session_key': 'cli:demo'},
        )
    )
    assert payload['ok'] is True
    assert manager.last_call['facts'][0]['value'] == {'tone': 'concise', 'markdown': True}
    assert manager.last_call['facts'][0]['merge_mode'] == 'merge'


@pytest.mark.xfail(reason="memory_write no longer parses structured fact payloads")
@pytest.mark.asyncio
async def test_memory_write_parses_json_object_string_value_before_upsert(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_write', workspace / 'tools' / 'memory_write')

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def upsert_structured_memory_facts(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'written': 1}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_write']
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=False), 'memory_manager': manager},
    )

    payload = json.loads(
        await tool.execute(
            facts=[
                {
                    'category': 'current_state',
                    'scope': 'global',
                    'entity': 'user',
                    'attribute': 'editor_preferences',
                    'value': '{"theme":"light","autosave":true,"tab_size":2}',
                    'observed_at': '2026-04-08T00:00:00',
                    'time_semantics': 'durable_until_replaced',
                    'source_excerpt': '记住我的编辑器偏好',
                }
            ],
            __g3ku_runtime={'session_key': 'cli:demo'},
        )
    )

    assert payload == {'ok': True, 'written': 1}
    assert manager.last_call is not None
    assert manager.last_call['facts'][0]['value'] == {
        'theme': 'light',
        'autosave': True,
        'tab_size': 2,
    }


@pytest.mark.xfail(reason="memory_write no longer parses structured fact payloads")
@pytest.mark.asyncio
async def test_memory_write_keeps_plain_string_value_unchanged(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_write', workspace / 'tools' / 'memory_write')

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def upsert_structured_memory_facts(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'written': 1}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_write']
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=False), 'memory_manager': manager},
    )

    payload = json.loads(
        await tool.execute(
            facts=[
                {
                    'category': 'default_setting',
                    'scope': 'global',
                    'entity': 'user',
                    'attribute': 'preferred_package_manager',
                    'value': 'pnpm',
                    'observed_at': '2026-04-08T00:00:00',
                    'time_semantics': 'durable_until_replaced',
                    'source_excerpt': '以后默认用 pnpm',
                }
            ],
            __g3ku_runtime={'session_key': 'cli:demo'},
        )
    )

    assert payload == {'ok': True, 'written': 1}
    assert manager.last_call is not None
    assert manager.last_call['facts'][0]['value'] == 'pnpm'


@pytest.mark.xfail(reason="memory_delete contract is being replaced by queued visible-text deletion")
@pytest.mark.asyncio
async def test_memory_delete_builds_and_calls_precise_delete(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_delete', workspace / 'tools' / 'memory_delete')

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def delete_structured_memory_facts(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'deleted': 2}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_delete']
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=False), 'memory_manager': manager},
    )

    payload = json.loads(
        await tool.execute(
            fact_ids=['fact-1'],
            canonical_keys=['global|identity|user|preferred_package_manager|durable_until_replaced'],
            __g3ku_runtime={'session_key': 'cli:demo'},
        )
    )
    assert payload == {'ok': True, 'deleted': 2}
    assert manager.last_call['session_key'] == 'cli:demo'
    assert manager.last_call['channel'] == 'cli'
    assert manager.last_call['chat_id'] == 'demo'
    assert manager.last_call['fact_ids'] == ['fact-1']


@pytest.mark.asyncio
async def test_memory_note_loads_note_body_by_short_ref(tmp_path: Path):
    workspace = tmp_path / "workspace"
    note_dir = workspace / "memory" / "notes"
    note_dir.mkdir(parents=True, exist_ok=True)
    (note_dir / "note_a1b2.md").write_text("# note\n详细规则\n", encoding="utf-8")

    assert importlib.util.find_spec("g3ku.agent.memory_agent_runtime") is not None
    assert importlib.util.find_spec("g3ku.agent.tools.memory_note") is not None

    from g3ku.agent.memory_agent_runtime import MemoryManager
    from g3ku.agent.tools.memory_note import MemoryNoteTool
    from g3ku.config.schema import MemoryToolsConfig

    manager = MemoryManager(workspace, MemoryToolsConfig())
    try:
        tool = MemoryNoteTool(manager=manager)
        payload = await tool.execute(ref="note_a1b2")
        assert "详细规则" in payload
    finally:
        manager.close()


def test_resource_manager_infers_memory_manager_from_main_task_service_for_memory_write(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_write', workspace / 'tools' / 'memory_write')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    service = SimpleNamespace(memory_manager=None)
    manager.bind_service_getter(lambda: {'main_task_service': service})
    manager.reload_now(trigger='test-no-memory-manager')

    try:
        unavailable_tool = manager.get_tool('memory_write')
        assert unavailable_tool is not None
        assert type(unavailable_tool).__name__ == 'RepairRequiredTool'

        service.memory_manager = object()
        manager.reload_now(trigger='test-main-task-memory-manager')

        restored_tool = manager.get_tool('memory_write')
        assert restored_tool is not None
        assert type(restored_tool).__name__ == 'EmbeddedMCPTool'
    finally:
        manager.close()


def test_resource_loader_injects_tool_secrets(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_write')

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['filesystem_write']
    loader = ResourceLoader(workspace, app_config=SimpleNamespace(tool_secrets={'filesystem_write': {'token': 'demo-secret'}}))
    runtime = loader.build_runtime_context(descriptor)

    assert runtime.tool_settings['restrict_to_workspace'] is False
    assert 'write_validation_enabled' in runtime.tool_settings
    assert runtime.tool_secrets == {'token': 'demo-secret'}


@pytest.mark.asyncio
async def test_filesystem_tool_blocks_writes_to_system_temp_outside_workspace_defaults(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    outside_file = tmp_path / 'outside.txt'
    outside_file.write_text('allowed\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_write')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_write')
        assert tool is not None
        result = await tool.execute(path=str(outside_file), content='updated\n')
        assert 'system temp directory' in result
        assert outside_file.read_text(encoding='utf-8') == 'allowed\n'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_tool_blocks_writes_outside_workspace_when_restricted(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    outside_file = tmp_path / 'outside.txt'
    outside_file.write_text('blocked\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _copy_filesystem_split_tools(workspace, 'filesystem_write')

    manifest = workspace / 'tools' / 'filesystem_write' / 'resource.yaml'
    manifest.write_text(
        manifest.read_text(encoding='utf-8').replace('restrict_to_workspace: false', 'restrict_to_workspace: true'),
        encoding='utf-8',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem_write')
        assert tool is not None
        result = await tool.execute(path=str(outside_file), content='blocked-again\n')
        assert 'outside workspace' in result or 'outside allowed directory' in result
    finally:
        manager.close()


def test_load_context_bodies_skip_release_reload(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'exec', workspace / 'tools' / 'exec')
    _write_demo_skill(workspace)

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.start()

    triggers: list[str] = []
    original_reload_now = manager.reload_now

    def _tracked_reload(self, *, trigger: str = 'manual'):
        triggers.append(trigger)
        return original_reload_now(trigger=trigger)

    manager.reload_now = MethodType(_tracked_reload, manager)

    try:
        tool_body = manager.load_toolskill_body('exec')
        skill_body = manager.load_skill_body('demo_skill')

        assert '# exec' in tool_body
        assert 'Demo skill guide' in skill_body
        assert triggers == []

        with manager.acquire_tool('exec'):
            pass
        with manager.acquire_skill('demo_skill'):
            pass

        assert triggers == ['release', 'release']
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_task_retrospective_skill_is_discovered_and_visible_only_to_ceo(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'skills' / 'task-retrospective', workspace / 'skills' / 'task-retrospective')
    shutil.copytree(REPO_ROOT / 'tools' / 'task_summary_cn', workspace / 'tools' / 'task_summary_cn')
    shutil.copytree(REPO_ROOT / 'tools' / 'task_fetch_cn', workspace / 'tools' / 'task_fetch_cn')
    shutil.copytree(REPO_ROOT / 'tools' / 'task_failed_nodes_cn', workspace / 'tools' / 'task_failed_nodes_cn')
    shutil.copytree(REPO_ROOT / 'tools' / 'task_node_detail_cn', workspace / 'tools' / 'task_node_detail_cn')
    shutil.copytree(REPO_ROOT / 'tools' / 'task_progress_cn', workspace / 'tools' / 'task_progress_cn')

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
        descriptor = manager.get_skill('task-retrospective')
        ceo_visible = [item.skill_id for item in service.list_visible_skill_resources(actor_role='ceo', session_id='web:shared')]
        execution_visible = [item.skill_id for item in service.list_visible_skill_resources(actor_role='execution', session_id='web:shared')]
        inspection_visible = [item.skill_id for item in service.list_visible_skill_resources(actor_role='inspection', session_id='web:shared')]

        assert descriptor is not None
        assert descriptor.available is True
        assert 'task-retrospective' in ceo_visible
        assert 'task-retrospective' not in execution_visible
        assert 'task-retrospective' not in inspection_visible
    finally:
        await service.close()
        manager.close()


@pytest.mark.asyncio
async def test_execution_node_messages_include_visible_skill_inventory(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    _write_demo_skill(
        workspace,
        name='demo_visible_skill',
        guide='Demo visible skill guide',
        allowed_roles=['execution'],
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
        visible = [item.skill_id for item in service.list_visible_skill_resources(actor_role='execution', session_id='web:shared')]
        assert visible == ['demo_visible_skill']

        record = await service.create_task('verify node visible skills', session_id='web:shared')
        task = service.get_task(record.task_id)
        node = service.get_node(record.root_node_id)
        assert task is not None
        assert node is not None

        messages = await service.node_runner._build_messages(task=task, node=node)
        bootstrap_payload = json.loads(messages[1]['content'])
        contract_payload = json.loads(messages[-1]['content'])

        assert 'visible_skills' not in bootstrap_payload
        assert 'execution_stage' not in bootstrap_payload
        assert contract_payload['message_type'] == 'node_runtime_tool_contract'
        assert contract_payload['candidate_skills'] == [
            {
                'skill_id': 'demo_visible_skill',
                'description': 'Demo skill for resource smoke tests.',
            }
        ]
    finally:
        await service.close()
        manager.close()


def test_tree_fingerprint_ignores_node_modules(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    tool_root = workspace / 'tools' / 'demo_node_tool'
    (tool_root / 'main').mkdir(parents=True, exist_ok=True)
    (tool_root / 'toolskills').mkdir(parents=True, exist_ok=True)
    (tool_root / 'node_modules' / 'demo-package').mkdir(parents=True, exist_ok=True)

    (tool_root / 'resource.yaml').write_text(
        textwrap.dedent(
            """\
            schema_version: 1
            kind: tool
            name: demo_node_tool
            description: Demo tool with vendored node modules.
            protocol: mcp
            mcp:
              transport: embedded
            requires:
              tools: []
              bins: []
              env: []
            permissions:
              network: false
              filesystem: []
            parameters:
              type: object
              properties: {}
              required: []
            exposure:
              agent: true
              main_runtime: true
            toolskill:
              enabled: true
            """
        ),
        encoding='utf-8',
    )
    (tool_root / 'main' / 'tool.py').write_text(
        textwrap.dedent(
            """\
            async def execute(**kwargs):
                return 'ok'
            """
        ),
        encoding='utf-8',
    )
    (tool_root / 'toolskills' / 'SKILL.md').write_text('# demo_node_tool\n', encoding='utf-8')
    node_dep = tool_root / 'node_modules' / 'demo-package' / 'index.js'
    node_dep.write_text('module.exports = 1;\n', encoding='utf-8')

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    first = registry.discover().tools['demo_node_tool'].fingerprint

    time.sleep(0.05)
    node_dep.write_text('module.exports = 2;\n', encoding='utf-8')
    second = registry.discover().tools['demo_node_tool'].fingerprint

    time.sleep(0.05)
    tool_py = tool_root / 'main' / 'tool.py'
    tool_py.write_text(
        textwrap.dedent(
            """\
            async def execute(**kwargs):
                return 'updated'
            """
        ),
        encoding='utf-8',
    )
    third = registry.discover().tools['demo_node_tool'].fingerprint

    assert second == first
    assert third != second


@pytest.mark.asyncio
async def test_agent_browser_auto_generated_profile_forces_matching_session(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    handler = _load_agent_browser_handler(workspace)
    assert handler._settings.auto_session is True

    handler._settings = handler._settings.model_copy(update={'auto_session': False})
    argv = handler._inject_global_flags(
        argv=['open', 'https://example.com'],
        session=None,
        profile=None,
        session_name=None,
    )

    expected_profile = workspace / '.g3ku' / 'tool-data' / 'agent_browser' / 'profiles' / 'g3ku-agent-browser'

    assert argv[:4] == [
        '--profile',
        str(expected_profile),
        '--session',
        'g3ku-agent-browser',
    ]
    assert expected_profile.is_dir()


@pytest.mark.asyncio
async def test_agent_browser_explicit_relative_profile_resolves_from_workspace(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    handler = _load_agent_browser_handler(workspace)

    argv = handler._inject_global_flags(
        argv=['open', 'https://example.com'],
        session=None,
        profile='.g3ku/tool-data/agent_browser/profiles/custom-profile',
        session_name=None,
    )

    expected_profile = workspace / '.g3ku' / 'tool-data' / 'agent_browser' / 'profiles' / 'custom-profile'

    assert argv[:4] == [
        '--profile',
        str(expected_profile),
        '--session',
        'g3ku-agent-browser',
    ]
    assert expected_profile.is_dir()


@pytest.mark.asyncio
async def test_agent_browser_resolves_local_cli_from_externaltools(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    handler = _load_agent_browser_handler(workspace)
    local_cli = workspace / 'externaltools' / 'agent_browser' / 'node_modules' / '.bin' / (
        'agent-browser.cmd' if os.name == 'nt' else 'agent-browser'
    )
    local_cli.parent.mkdir(parents=True, exist_ok=True)
    local_cli.write_text('@echo off\n', encoding='utf-8')

    prefix = await handler._resolve_command_prefix()

    assert prefix == [str(local_cli.resolve(strict=False))]


@pytest.mark.asyncio
async def test_agent_browser_timeout_triggers_session_cleanup(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    handler = _load_agent_browser_handler(workspace)

    run_calls: list[dict[str, object]] = []
    close_calls: list[dict[str, object]] = []

    async def _fake_resolve_command_prefix(self):
        return ['agent-browser']

    async def _fake_run_command(self, *, command_prefix, args, cwd, env, stdin, timeout_seconds, cancel_token):
        run_calls.append(
            {
                'command_prefix': list(command_prefix),
                'args': list(args),
                'cwd': str(cwd),
                'timeout_seconds': timeout_seconds,
                'cancel_token': cancel_token,
            }
        )
        return {
            'ok': False,
            'timed_out': True,
            'error': f'agent-browser timed out after {timeout_seconds} seconds',
            'command': [*command_prefix, *args],
            'cwd': str(cwd),
        }

    async def _fake_close_session(self, *, command_prefix, cwd, env, session, cancel_token):
        close_calls.append(
            {
                'command_prefix': list(command_prefix),
                'cwd': str(cwd),
                'session': session,
                'cancel_token': cancel_token,
            }
        )
        return {
            'ok': True,
            'exit_code': 0,
            'stdout': 'Browser closed',
            'stderr': '',
            'command': [*command_prefix, '--session', session, 'close'],
            'cwd': str(cwd),
        }

    handler._resolve_command_prefix = MethodType(_fake_resolve_command_prefix, handler)
    handler._run_command = MethodType(_fake_run_command, handler)
    handler._close_session = MethodType(_fake_close_session, handler)

    payload = json.loads(await handler.execute(args=['open', 'https://example.com']))
    expected_profile = workspace / '.g3ku' / 'tool-data' / 'agent_browser' / 'profiles' / 'g3ku-agent-browser'

    assert run_calls == [
        {
            'command_prefix': ['agent-browser'],
            'args': [
                '--profile',
                str(expected_profile),
                '--session',
                'g3ku-agent-browser',
                'open',
                'https://example.com',
            ],
            'cwd': str(workspace),
            'timeout_seconds': 300,
            'cancel_token': None,
        }
    ]
    assert close_calls == [
        {
            'command_prefix': ['agent-browser'],
            'cwd': str(workspace),
            'session': 'g3ku-agent-browser',
            'cancel_token': None,
        }
    ]
    assert payload['timed_out'] is True
    assert payload['session_cleanup']['ok'] is True


@pytest.mark.asyncio
async def test_agent_browser_failed_daemon_warning_retries_once_after_cleanup(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    handler = _load_agent_browser_handler(workspace)

    run_calls: list[dict[str, object]] = []
    close_calls: list[str] = []

    async def _fake_resolve_command_prefix(self):
        return ['agent-browser']

    async def _fake_run_command(self, *, command_prefix, args, cwd, env, stdin, timeout_seconds, cancel_token):
        run_calls.append(
            {
                'command_prefix': list(command_prefix),
                'args': list(args),
                'cwd': str(cwd),
                'timeout_seconds': timeout_seconds,
                'cancel_token': cancel_token,
            }
        )
        if len(run_calls) == 1:
            return {
                'ok': False,
                'exit_code': 1,
                'stdout': '',
                'stderr': "⚠ --profile ignored: daemon already running. Use 'agent-browser close' first to restart with new options.",
                'command': [*command_prefix, *args],
                'cwd': str(cwd),
            }
        return {
            'ok': True,
            'exit_code': 0,
            'stdout': '{"navigated":true}',
            'stderr': '',
            'stdout_json': {'navigated': True},
            'command': [*command_prefix, *args],
            'cwd': str(cwd),
        }

    async def _fake_close_session(self, *, command_prefix, cwd, env, session, cancel_token):
        close_calls.append(session)
        return {
            'ok': True,
            'exit_code': 0,
            'stdout': 'Browser closed',
            'stderr': '',
            'command': [*command_prefix, '--session', session, 'close'],
            'cwd': str(cwd),
        }

    handler._resolve_command_prefix = MethodType(_fake_resolve_command_prefix, handler)
    handler._run_command = MethodType(_fake_run_command, handler)
    handler._close_session = MethodType(_fake_close_session, handler)

    payload = json.loads(await handler.execute(args=['open', 'https://example.com']))
    expected_profile = workspace / '.g3ku' / 'tool-data' / 'agent_browser' / 'profiles' / 'g3ku-agent-browser'

    assert run_calls == [
        {
            'command_prefix': ['agent-browser'],
            'args': [
                '--profile',
                str(expected_profile),
                '--session',
                'g3ku-agent-browser',
                'open',
                'https://example.com',
            ],
            'cwd': str(workspace),
            'timeout_seconds': 300,
            'cancel_token': None,
        },
        {
            'command_prefix': ['agent-browser'],
            'args': [
                '--profile',
                str(expected_profile),
                '--session',
                'g3ku-agent-browser',
                'open',
                'https://example.com',
            ],
            'cwd': str(workspace),
            'timeout_seconds': 300,
            'cancel_token': None,
        },
    ]
    assert close_calls == ['g3ku-agent-browser']
    assert payload['ok'] is True
    assert payload['retried_after_session_cleanup'] is True
    assert payload['initial_attempt']['exit_code'] == 1
    assert 'daemon already running' in payload['initial_attempt']['stderr']
    assert payload['session_cleanup']['ok'] is True


@pytest.mark.asyncio
async def test_agent_browser_missing_cli_returns_install_guidance(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    handler = _load_agent_browser_handler(workspace)
    handler._settings = handler._settings.model_copy(update={'command_prefix': ['missing-g3ku-agent-browser-cli']})

    payload = json.loads(await handler.execute(args=['open', 'https://example.com']))

    assert payload['ok'] is False
    assert payload['missing_dependency'] is True
    assert payload['error'] == 'agent-browser CLI not found'
    assert payload['repo_url'] == 'https://github.com/vercel-labs/agent-browser'
    assert payload['install_root'] == str((workspace / 'externaltools' / 'agent_browser').resolve(strict=False))
    assert payload['temp_root'] == str((workspace / 'temp' / 'agent_browser').resolve(strict=False))
    assert 'load_tool_context(tool_id="agent_browser")' in payload['next_actions']


@pytest.mark.asyncio
async def test_memory_write_queues_explicit_memory_request(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_write', workspace / 'tools' / 'memory_write')

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def enqueue_write_request(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'request_id': 'write_req_1', 'status': 'queued'}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_write']
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=False), 'memory_manager': manager},
    )

    payload = json.loads(
        await tool.execute(
            content='默认创建文件时遵循项目格式要求',
            __g3ku_runtime={'session_key': 'cli:demo'},
        )
    )

    assert payload['ok'] is True
    assert payload['status'] == 'queued'
    assert manager.last_call['session_key'] == 'cli:demo'
    assert manager.last_call['decision_source'] == 'user'
    assert manager.last_call['trigger_source'] == 'memory_write_tool'
    assert manager.last_call['payload_text'] == '默认创建文件时遵循项目格式要求'


@pytest.mark.asyncio
async def test_memory_delete_queues_id_deletion_request(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_delete', workspace / 'tools' / 'memory_delete')

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def enqueue_delete_request(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'request_id': 'delete_req_1', 'status': 'queued'}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_delete']
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=False), 'memory_manager': manager},
    )

    payload = json.loads(
        await tool.execute(
            id='Ab12Z9',
            __g3ku_runtime={'session_key': 'cli:demo'},
        )
    )

    assert payload['ok'] is True
    assert payload['status'] == 'queued'
    assert manager.last_call['session_key'] == 'cli:demo'
    assert manager.last_call['decision_source'] == 'user'
    assert manager.last_call['trigger_source'] == 'memory_delete_tool'
    assert manager.last_call['payload_text'] == '["Ab12Z9"]'
