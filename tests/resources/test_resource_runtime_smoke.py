from __future__ import annotations

import json
import os
import shutil
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from types import MethodType

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.agent.tools.propose_patch import parse_patch_artifact
from g3ku.content import ContentNavigationService, content_summary_and_ref, parse_content_envelope
from g3ku.resources import ResourceManager
from g3ku.resources.loader import ResourceLoader
from g3ku.resources.registry import ResourceRegistry
from g3ku.resources.tool_settings import FilesystemToolSettings
from main.api import rest as api_rest
from main.models import TaskArtifactRecord
from main.service.runtime_service import MainRuntimeService
from main.storage.sqlite_store import SQLiteTaskStore
from main.storage.artifact_store import TaskArtifactStore
from tools.filesystem.main.tool import FilesystemTool

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
        return 'py -3'
    return 'python3' if shutil.which('python3') else 'python'


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
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

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
        tool = manager.get_tool('filesystem')
        assert tool is not None
        assert 'target.txt' in await tool.execute(action='list', path=str(target_file.parent))
        described = json.loads(await tool.execute(action='describe', path=str(target_file)))
        assert described['handle']['line_count'] == 1
        opened = json.loads(await tool.execute(action='open', path=str(target_file), start_line=1, end_line=5))
        assert opened['excerpt'] == 'before value'
        assert 'Successfully wrote' in await tool.execute(action='write', path=str(written_file), content='hello\n')
        written = json.loads(await tool.execute(action='head', path=str(written_file), lines=5))
        assert written['excerpt'] == 'hello'
        assert 'Successfully edited' in await tool.execute(
            action='edit',
            path=str(target_file),
            old_text='before value',
            new_text='after value',
        )
        searched = json.loads(await tool.execute(action='search', path=str(target_file), query='after'))
        assert searched['scope_type'] == 'file'
        assert searched['hits'][0]['line'] == 1

        result = json.loads(
            await tool.execute(
                action='propose_patch',
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
        assert 'Successfully deleted' in await tool.execute(action='delete', path=str(written_file))
        assert not written_file.exists()
    finally:
        manager.close()
        store.close()


@pytest.mark.asyncio
async def test_filesystem_tool_rejects_relative_paths(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('hello\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        result = await tool.execute(action='head', path='target.txt', lines=5)
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
    result = await tool.execute(action='write', path=str(created_file), content='hello\n', _FilesystemTool__g3ku_runtime=runtime)
    assert 'Successfully wrote' in result

    result = await tool.execute(
        action='edit',
        path=str(target_file),
        old_text='before',
        new_text='after',
        _FilesystemTool__g3ku_runtime=runtime,
    )
    assert 'Successfully edited' in result

    result = await tool.execute(action='delete', path=str(created_file), _FilesystemTool__g3ku_runtime=runtime)
    assert 'Successfully deleted' in result

    result = json.loads(
        await tool.execute(
            action='propose_patch',
            path=str(target_file),
            old_text='after',
            new_text='patched',
            summary='Patch target',
            _FilesystemTool__g3ku_runtime=runtime,
        )
    )
    assert result['success'] is True

    assert service.calls == [
        ('task:test', 'node:test', str(created_file.resolve()), 'created'),
        ('task:test', 'node:test', str(target_file.resolve()), 'modified'),
    ]
    store.close()


@pytest.mark.asyncio
async def test_filesystem_tool_rejects_artifact_refs_with_content_guidance(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        result = await tool.execute(action='head', path='artifact:artifact:demo123', lines=5)
        assert 'content ref is not a filesystem path' in result
        assert 'use the content tool with ref=artifact:artifact:demo123' in result
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_search_overflow_requires_refine(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_dir = workspace / 'src'
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / 'target.txt'
    target_file.write_text('\n'.join(['needle'] * 20) + '\n', encoding='utf-8')
    for index in range(6):
        (target_dir / f'module_{index}.txt').write_text('needle\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        file_payload = json.loads(await tool.execute(action='search', path=str(target_file), query='needle', limit=5))
        assert file_payload['ok'] is True
        assert file_payload['overflow'] is True
        assert file_payload['requires_refine'] is True
        assert file_payload['hits'] == []
        assert file_payload['count'] == 0
        assert file_payload['overflow_lower_bound'] == 6
        assert file_payload['scope_type'] == 'file'

        dir_payload = json.loads(await tool.execute(action='search', path=str(target_dir), query='needle', limit=5))
        assert dir_payload['ok'] is True
        assert dir_payload['scope_type'] == 'directory'
        if shutil.which('rg'):
            assert dir_payload['overflow'] is True
            assert dir_payload['requires_refine'] is True
            assert dir_payload['hits'] == []
            assert dir_payload['count'] == 0
            assert dir_payload['overflow_lower_bound'] == 6
            assert dir_payload['backend'] == 'rg'
        else:
            assert dir_payload['requires_refine'] is True
            assert dir_payload['backend'] == 'unavailable'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_search_refines_when_directory_backend_is_unavailable(tmp_path: Path, monkeypatch):
    workspace = tmp_path / 'workspace'
    target_dir = workspace / 'src'
    target_dir.mkdir(parents=True, exist_ok=True)
    for index in range(5):
        (target_dir / f'module_{index}.txt').write_text('needle\n', encoding='utf-8')
    tool = FilesystemTool(
        workspace=workspace,
        settings=FilesystemToolSettings(
            search_max_files=2,
            search_timeout_seconds=10.0,
        ),
    )
    monkeypatch.setattr(shutil, 'which', lambda _name: None)

    payload = json.loads(await tool.execute(action='search', path=str(target_dir), query='needle', limit=10))

    assert payload['ok'] is True
    assert payload['requires_refine'] is True
    assert payload['timed_out'] is False
    assert payload['scope_type'] == 'directory'
    assert payload['backend'] == 'unavailable'
    assert 'backend is unavailable' in str(payload['message']).lower()


@pytest.mark.asyncio
async def test_filesystem_search_file_includes_search_diagnostics(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('needle\n', encoding='utf-8')
    tool = FilesystemTool(workspace=workspace, settings=FilesystemToolSettings())
    payload = json.loads(await tool.execute(action='search', path=str(target_file), query='needle', limit=5))
    assert payload['ok'] is True
    assert payload['backend'] == 'file'
    assert payload['timed_out'] is False
    assert int(payload['scanned_files']) == 1
    assert int(payload['scanned_bytes']) >= len('needle\n'.encode('utf-8'))


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
async def test_filesystem_edit_line_range_mode(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    target_file = workspace / 'target.txt'
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text('alpha\nbeta\ngamma\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        result = await tool.execute(
            action='edit',
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
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
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
            action='edit',
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
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        result = await tool.execute(
            action='edit',
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
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        tool._handler._settings = tool._handler._settings.model_copy(
            update={
                'write_validation_default_commands': [],
                'write_validation_commands_by_ext': {
                    '.py': [f'{_python_launcher()} -c "import sys; sys.exit(1)"']
                },
            }
        )
        result = await tool.execute(action='write', path=str(target_file), content='print("after")\n')
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
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        tool._handler._settings = tool._handler._settings.model_copy(
            update={
                'write_validation_default_commands': [],
                'write_validation_commands_by_ext': {
                    '.py': [f'{_python_launcher()} -c "import sys; sys.exit(1)"']
                },
            }
        )
        result = await tool.execute(action='write', path=str(target_file), content='print("after")\n')
        assert result.startswith('Error: Write validation failed')
        assert target_file.read_text(encoding='utf-8') == 'print("before")\n'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_search_recurses_directories(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    search_root = workspace / 'search-root'
    nested = search_root / 'nested'
    nested.mkdir(parents=True, exist_ok=True)
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    (search_root / 'alpha.txt').write_text('first hit\n', encoding='utf-8')
    (nested / 'beta.txt').write_text('second HIT\n', encoding='utf-8')
    (nested / 'binary.bin').write_bytes(b'\x00\xffhit')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        payload = json.loads(await tool.execute(action='search', path=str(search_root), query='hit', limit=10))
        assert payload['ok'] is True
        assert payload['scope_type'] == 'directory'
        assert payload['path'] == str(search_root.resolve())
        assert payload['count'] == 2
        assert all(item['path'].endswith('.txt') for item in payload['hits'])
        assert all('hit' in item['preview'].lower() for item in payload['hits'])
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
    for name in ('task_summary_cn', 'task_fetch_cn', 'task_failed_nodes_cn', 'task_node_detail_cn', 'task_progress_cn'):
        shutil.copytree(REPO_ROOT / 'tools' / name, workspace / 'tools' / name)

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': _MainTaskService()})
    manager.reload_now(trigger='test-bind')

    try:
        summary_tool = manager.get_tool('task_summary')
        fetch_tool = manager.get_tool('task_list')
        failed_nodes_tool = manager.get_tool('task_failed_nodes')
        node_detail_tool = manager.get_tool('task_node_detail')
        progress_tool = manager.get_tool('task_progress')

        assert summary_tool is not None
        assert fetch_tool is not None
        assert failed_nodes_tool is not None
        assert node_detail_tool is not None
        assert progress_tool is not None
        assert await summary_tool.execute(__g3ku_runtime={'session_key': 'web:shared'}) == 'summary:web:shared'
        assert await fetch_tool.execute(__g3ku_runtime={'session_key': 'web:shared'}, **{'任务类型': 4}) == 'list:web:shared:4'
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
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

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
        tool = manager.get_tool('filesystem')
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

        await tool.execute(action='write', path=str(skill_root / 'resource.yaml'), content=manifest)
        await tool.execute(action='write', path=str(skill_root / 'SKILL.md'), content=body)

        skill = manager.get_skill('auto_skill')
        assert skill is not None
        assert skill.available is True
        assert 'Auto skill guide' in manager.load_skill_body('auto_skill')
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_exec_tool_auto_refreshes_new_skill_without_full_reload(tmp_path: Path):
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

    def _fail_reload(self, *, trigger: str = 'manual'):
        raise AssertionError(f'full reload should not run during targeted refresh: {trigger}')

    manager.reload_now = MethodType(_fail_reload, manager)

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
        payload = json.loads(
            await tool.execute(
                command=f'{_python_launcher()} -c "{script}"',
                __g3ku_runtime={'session_key': 'web:shared'},
            )
        )

        assert payload['status'] == 'success'
        skill = manager.get_skill('exec_skill')
        assert skill is not None
        assert skill.available is True
        assert 'Exec skill guide' in manager.load_skill_body('exec_skill')
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
        assert payload['stdout_ref'].startswith('artifact:')
        assert 'line_count' not in payload
        assert 'tail_preview' not in payload
        assert 'next_actions' not in payload
        assert 'line-000' in payload['head_preview']

        hits = json.loads(await content_tool.execute(action='search', ref=payload['stdout_ref'], query='line-044', limit=1))
        excerpt = json.loads(
            await content_tool.execute(
                action='open',
                ref=payload['stdout_ref'],
                around_line=hits['hits'][0]['line'],
                window=5,
            )
        )
        assert 'line-044' in excerpt['excerpt']
    finally:
        manager.close()
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


def test_content_navigation_reuses_identical_artifacts_and_tracks_origin_ref(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / 'runtime.sqlite3')
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / 'artifacts', store=store)
    navigator = ContentNavigationService(
        workspace=tmp_path,
        artifact_store=artifact_store,
        artifact_lookup=artifact_store,
    )

    text = ('line-001\n' * 200).strip()
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
                'content': 'line-001\n' * 400,
            },
        ],
        runtime={'task_id': 'task:test', 'node_id': 'node:test', 'node_kind': 'execution'},
        source_prefix='react',
    )

    assert prepared[0]['content']
    raw_payload = json.loads(prepared[1]['content'])
    assert set(raw_payload.keys()) == {'type', 'summary', 'ref', 'next_actions'}
    assert 'Head preview:' not in raw_payload['summary']
    assert raw_payload['ref'].startswith('artifact:')
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
    'source_kind',
    [
        'tool_result:memory_search',
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


def test_content_summary_and_ref_keeps_wrapper_ref_for_content_envelopes(tmp_path: Path):
    store = SQLiteTaskStore(tmp_path / "runtime.sqlite3")
    artifact_store = TaskArtifactStore(artifact_dir=tmp_path / "artifacts", store=store)
    navigator = ContentNavigationService(workspace=tmp_path, artifact_store=artifact_store, artifact_lookup=artifact_store)
    inner = navigator.maybe_externalize_text("canonical body", runtime={"task_id": "task:test", "node_id": "node:inner"}, display_name="inner", source_kind="node_output", force=True)
    wrapped = navigator.maybe_externalize_text(json.dumps(inner.to_dict(), ensure_ascii=False), runtime={"task_id": "task:test", "node_id": "node:wrapper"}, display_name="wrapped", source_kind="tool_result:content", force=True)
    summary, ref = content_summary_and_ref(wrapped.to_dict())
    assert summary == wrapped.summary
    assert ref == wrapped.ref
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

    service = _StubService()
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

    assert service.open_calls[0]["view"] == "raw"
    assert service.describe_calls == [{"ref": "artifact:artifact:wrapper", "path": None, "view": "canonical"}]
    assert service.search_calls == [{"query": "needle", "ref": "artifact:artifact:wrapper", "path": None, "view": "canonical", "limit": 10, "before": 2, "after": 2}]
    assert service.open_calls[1]["view"] == "canonical"


@pytest.mark.asyncio
async def test_memory_search_reads_manifest_settings(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_search', workspace / 'tools' / 'memory_search')

    manifest = workspace / 'tools' / 'memory_search' / 'resource.yaml'
    manifest.write_text(
        manifest.read_text(encoding='utf-8').replace('default_limit: 8', 'default_limit: 11'),
        encoding='utf-8',
    )

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def search_tool_view(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'limit': kwargs['limit']}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_search']
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=True), 'memory_manager': manager},
    )

    payload = json.loads(await tool.execute(query='remember this', __g3ku_runtime={'session_key': 'cli:demo'}))
    assert payload == {'ok': True, 'limit': 11}
    assert manager.last_call['limit'] == 11


@pytest.mark.asyncio
async def test_memory_write_builds_without_rag_store_and_writes_explicit_items(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'memory_write', workspace / 'tools' / 'memory_write')

    class _FakeMemoryManager:
        def __init__(self):
            self.last_call = None

        async def write_explicit_memory_items(self, **kwargs):
            self.last_call = kwargs
            return {'ok': True, 'written': [{'record_id': 'rec-1', 'key': 'preferred_package_manager', 'kind': 'default'}], 'deleted': [], 'searchable': True}

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['memory_write']
    manager = _FakeMemoryManager()
    tool = ResourceLoader(workspace).load_tool(
        descriptor,
        services={'loop': SimpleNamespace(_store_enabled=False), 'memory_manager': manager},
    )

    payload = json.loads(
        await tool.execute(
            items=[
                {
                    'kind': 'default',
                    'key': 'preferred_package_manager',
                    'value': 'pnpm',
                    'statement': 'Default to pnpm for package management.',
                    'source_excerpt': '以后默认用 pnpm',
                }
            ],
            __g3ku_runtime={'session_key': 'cli:demo'},
        )
    )
    assert payload['ok'] is True
    assert payload['searchable'] is True
    assert manager.last_call['channel'] == 'cli'
    assert manager.last_call['chat_id'] == 'demo'
    assert manager.last_call['items'][0]['key'] == 'preferred_package_manager'


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
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['filesystem']
    loader = ResourceLoader(workspace, app_config=SimpleNamespace(tool_secrets={'filesystem': {'token': 'demo-secret'}}))
    runtime = loader.build_runtime_context(descriptor)

    assert runtime.tool_settings['restrict_to_workspace'] is False
    assert 'search_timeout_seconds' in runtime.tool_settings
    assert 'search_max_files' in runtime.tool_settings
    assert 'search_max_bytes' in runtime.tool_settings
    assert runtime.tool_secrets == {'token': 'demo-secret'}


@pytest.mark.asyncio
async def test_filesystem_tool_reads_absolute_paths_outside_workspace_by_default(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    outside_file = tmp_path / 'outside.txt'
    outside_file.write_text('allowed\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        result = json.loads(await tool.execute(action='head', path=str(outside_file), lines=5))
        assert result['ok'] is True
        assert result['excerpt'] == 'allowed'
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_filesystem_tool_blocks_paths_outside_workspace_when_restricted(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    outside_file = tmp_path / 'outside.txt'
    outside_file.write_text('blocked\n', encoding='utf-8')
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    manifest = workspace / 'tools' / 'filesystem' / 'resource.yaml'
    manifest.write_text(
        manifest.read_text(encoding='utf-8').replace('restrict_to_workspace: false', 'restrict_to_workspace: true'),
        encoding='utf-8',
    )

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.reload_now(trigger='test-bind')
    try:
        tool = manager.get_tool('filesystem')
        assert tool is not None
        result = await tool.execute(action='head', path=str(outside_file), lines=5)
        assert 'outside workspace' in result
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
        payload = json.loads(messages[1]['content'])

        assert payload['visible_skills'] == [
            {
                'skill_id': 'demo_visible_skill',
                'display_name': 'demo_visible_skill',
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
