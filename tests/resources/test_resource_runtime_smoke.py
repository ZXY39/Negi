from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from types import MethodType

import pytest
import yaml

from g3ku.agent.tools.propose_patch import parse_patch_artifact
from g3ku.content import ContentNavigationService
from g3ku.resources import ResourceManager
from g3ku.resources.loader import ResourceLoader
from g3ku.resources.registry import ResourceRegistry
from main.models import TaskArtifactRecord
from main.storage.sqlite_store import SQLiteTaskStore
from main.storage.artifact_store import TaskArtifactStore

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


def _write_demo_skill(root: Path, *, name: str = 'demo_skill', guide: str = 'Demo skill guide') -> Path:
    skill_root = root / 'skills' / name
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / 'resource.yaml').write_text(
        textwrap.dedent(
            f"""\
            schema_version: 1
            kind: skill
            name: {name}
            description: Demo skill for resource smoke tests.
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
        ),
        encoding='utf-8',
    )
    (skill_root / 'SKILL.md').write_text(
        f"# {name}\n\n{guide}\n",
        encoding='utf-8',
    )
    return skill_root


def _load_agent_browser_handler(workspace: Path):
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)

    tool_root = workspace / 'tools' / 'agent_browser'
    (tool_root / 'main').mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / 'tools' / 'agent_browser' / 'resource.yaml', tool_root / 'resource.yaml')
    shutil.copy2(REPO_ROOT / 'tools' / 'agent_browser' / 'main' / 'tool.py', tool_root / 'main' / 'tool.py')

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

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> str:
        return f'progress:{task_id}:{mark_read}'


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

    target_file = workspace / 'target.txt'
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
        assert 'target.txt' in await tool.execute(action='list', path='.')
        described = json.loads(await tool.execute(action='describe', path='target.txt'))
        assert described['handle']['line_count'] == 1
        opened = json.loads(await tool.execute(action='open', path='target.txt', start_line=1, end_line=5))
        assert opened['excerpt'] == 'before value'
        assert 'Successfully wrote' in await tool.execute(action='write', path='written.txt', content='hello\n')
        written = json.loads(await tool.execute(action='head', path='written.txt', lines=5))
        assert written['excerpt'] == 'hello'
        assert 'Successfully edited' in await tool.execute(
            action='edit',
            path='target.txt',
            old_text='before value',
            new_text='after value',
        )
        searched = json.loads(await tool.execute(action='search', path='target.txt', query='after'))
        assert searched['hits'][0]['line'] == 1

        result = json.loads(
            await tool.execute(
                action='propose_patch',
                path='target.txt',
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
        assert 'Successfully deleted' in await tool.execute(action='delete', path='written.txt')
        assert not (workspace / 'written.txt').exists()
    finally:
        manager.close()
        store.close()


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
    for name in ('task_summary_cn', 'task_fetch_cn', 'task_progress_cn'):
        shutil.copytree(REPO_ROOT / 'tools' / name, workspace / 'tools' / name)

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    manager.bind_service_getter(lambda: {'main_task_service': _MainTaskService()})
    manager.reload_now(trigger='test-bind')

    try:
        summary_tool = manager.get_tool('task_summary')
        fetch_tool = manager.get_tool('task_list')
        progress_tool = manager.get_tool('task_progress')

        assert summary_tool is not None
        assert fetch_tool is not None
        assert progress_tool is not None
        assert await summary_tool.execute(__g3ku_runtime={'session_key': 'web:shared'}) == 'summary:web:shared'
        assert await fetch_tool.execute(__g3ku_runtime={'session_key': 'web:shared'}, **{'任务类型': 4}) == 'list:web:shared:4'
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
        assert handler.path_append == 'D:/bin'
        assert handler.restrict_to_workspace is True
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
        command = f'"{sys.executable}" -c "for i in range(90): print(\'line-%03d\' % i)"'
        payload = json.loads(await exec_tool.execute(command=command, __g3ku_runtime={'session_key': 'cli:test'}))
        assert payload['status'] == 'success'
        assert payload['stdout_ref'].startswith('artifact:')
        assert payload['line_count'] >= 90

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


def test_resource_loader_injects_tool_secrets(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'filesystem', workspace / 'tools' / 'filesystem')

    registry = ResourceRegistry(workspace, skills_dir=workspace / 'skills', tools_dir=workspace / 'tools')
    descriptor = registry.discover().tools['filesystem']
    loader = ResourceLoader(workspace, app_config=SimpleNamespace(tool_secrets={'filesystem': {'token': 'demo-secret'}}))
    runtime = loader.build_runtime_context(descriptor)

    assert runtime.tool_settings == {'restrict_to_workspace': False}
    assert runtime.tool_secrets == {'token': 'demo-secret'}


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
async def test_agent_browser_timeout_triggers_session_cleanup(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    handler = _load_agent_browser_handler(workspace)

    run_calls: list[dict[str, object]] = []
    close_calls: list[dict[str, object]] = []

    async def _fake_resolve_command_prefix(self):
        return ['agent-browser']

    async def _fake_run_command(self, *, command_prefix, args, cwd, env, stdin, timeout_seconds):
        run_calls.append(
            {
                'command_prefix': list(command_prefix),
                'args': list(args),
                'cwd': str(cwd),
                'timeout_seconds': timeout_seconds,
            }
        )
        return {
            'ok': False,
            'timed_out': True,
            'error': f'agent-browser timed out after {timeout_seconds} seconds',
            'command': [*command_prefix, *args],
            'cwd': str(cwd),
        }

    async def _fake_close_session(self, *, command_prefix, cwd, env, session):
        close_calls.append(
            {
                'command_prefix': list(command_prefix),
                'cwd': str(cwd),
                'session': session,
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
        }
    ]
    assert close_calls == [
        {
            'command_prefix': ['agent-browser'],
            'cwd': str(workspace),
            'session': 'g3ku-agent-browser',
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

    async def _fake_run_command(self, *, command_prefix, args, cwd, env, stdin, timeout_seconds):
        run_calls.append(
            {
                'command_prefix': list(command_prefix),
                'args': list(args),
                'cwd': str(cwd),
                'timeout_seconds': timeout_seconds,
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

    async def _fake_close_session(self, *, command_prefix, cwd, env, session):
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
        },
    ]
    assert close_calls == ['g3ku-agent-browser']
    assert payload['ok'] is True
    assert payload['retried_after_session_cleanup'] is True
    assert payload['initial_attempt']['exit_code'] == 1
    assert 'daemon already running' in payload['initial_attempt']['stderr']
    assert payload['session_cleanup']['ok'] is True
