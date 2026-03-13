from __future__ import annotations

import json
import os
import shutil
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.propose_patch import parse_patch_artifact
from g3ku.org_graph.storage.artifact_store import ArtifactStore
from g3ku.org_graph.storage.project_store import ProjectStore
from g3ku.resources import ResourceManager

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
            config_namespace: ''
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
              org_graph: true
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


class _VisibleToolService:
    def __init__(self):
        self.resource_manager = None

    def bind_resource_manager(self, manager: ResourceManager) -> None:
        self.resource_manager = manager

    async def startup(self) -> None:
        return None

    def list_effective_tool_names(self, *, actor_role: str, session_id: str):
        _ = actor_role, session_id
        return sorted(self.resource_manager.tool_instances().keys()) if self.resource_manager is not None else []


class _ArtifactService:
    def __init__(self, artifact_store: ArtifactStore):
        self.artifact_store = artifact_store

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
async def test_resource_hot_reload_delete_lock_and_load_tool_context(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'load_tool_context', workspace / 'tools' / 'load_tool_context')

    manager = ResourceManager(workspace, app_config=_resource_app_config())
    service = _VisibleToolService()
    service.bind_resource_manager(manager)
    manager.bind_service_getter(lambda: {'org_graph_service': service})
    manager.reload_now(trigger='test-bind')
    manager.start()

    try:
        _wait_until(lambda: manager.get_tool('load_tool_context') is not None)

        demo_dir = _write_demo_tool(workspace)
        _wait_until(lambda: manager.get_tool('demo_echo') is not None)

        load_tool = manager.get_tool('load_tool_context')
        payload = json.loads(await load_tool.execute(tool_id='demo_echo'))
        assert payload['ok'] is True
        assert payload['tool_id'] == 'demo_echo'
        assert 'Demo echo guide' in payload['content']

        shutil.rmtree(demo_dir)
        _wait_until(lambda: manager.get_tool_descriptor('demo_echo') is None)
        assert manager.get_tool('demo_echo') is None

        demo_dir = _write_demo_tool(workspace, guide='Busy tool guide')
        _wait_until(lambda: manager.get_tool('demo_echo') is not None)

        with manager.acquire_tool('demo_echo'):
            assert await manager.get_tool('demo_echo').execute(text='busy') == 'busy'
            if os.name == 'nt':
                with pytest.raises((PermissionError, OSError)):
                    shutil.rmtree(demo_dir)
            else:
                pytest.skip('Windows delete guard is required for this smoke test')

        shutil.rmtree(demo_dir)
        _wait_until(lambda: manager.get_tool_descriptor('demo_echo') is None)
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_propose_file_patch_runs_as_resource_tool(tmp_path: Path):
    workspace = tmp_path / 'workspace'
    (workspace / 'skills').mkdir(parents=True, exist_ok=True)
    (workspace / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copytree(REPO_ROOT / 'tools' / 'propose_file_patch', workspace / 'tools' / 'propose_file_patch')

    target_file = workspace / 'target.txt'
    target_file.write_text('before value\n', encoding='utf-8')

    project_store = ProjectStore(tmp_path / 'projects.sqlite3')
    artifact_store = ArtifactStore(
        artifact_dir=tmp_path / 'artifacts',
        project_store=project_store,
    )
    service = _ArtifactService(artifact_store)

    manager = ResourceManager(workspace, app_config=_resource_app_config(poll_interval_ms=500))
    manager.bind_service_getter(lambda: {'org_graph_service': service})
    manager.reload_now(trigger='test-bind')

    try:
        tool = manager.get_tool('propose_file_patch')
        assert tool is not None

        result = json.loads(
            await tool.execute(
                path='target.txt',
                old_text='before value',
                new_text='after value',
                summary='Patch target',
                __g3ku_runtime={
                    'project_id': 'project:test',
                    'unit_id': 'unit:test',
                    'session_key': 'session:test',
                },
            )
        )

        assert result['success'] is True
        assert result['summary'] == 'Patch target'
        assert target_file.read_text(encoding='utf-8') == 'before value\n'

        artifact_path = Path(result['artifact']['path'])
        assert artifact_path.exists()
        metadata, diff_text = parse_patch_artifact(artifact_path.read_text(encoding='utf-8'))
        assert Path(metadata['path']) == target_file.resolve()
        assert 'after value' in diff_text
    finally:
        manager.close()


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
        summary_tool = manager.get_tool('任务汇总工具')
        fetch_tool = manager.get_tool('获取任务')
        progress_tool = manager.get_tool('查看任务进度工具')

        assert summary_tool is not None
        assert fetch_tool is not None
        assert progress_tool is not None
        assert await summary_tool.execute(__g3ku_runtime={'session_key': 'web:shared'}) == 'summary:web:shared'
        assert await fetch_tool.execute(__g3ku_runtime={'session_key': 'web:shared'}, **{'任务类型': 4}) == 'list:web:shared:4'
        assert await progress_tool.execute(**{'任务id': 'task:demo'}) == 'progress:task:demo:True'
    finally:
        manager.close()
