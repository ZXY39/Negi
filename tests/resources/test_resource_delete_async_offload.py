"""资源删除的异步车道：目录删除必须离开事件循环线程。

实盘症状：Tool 管理台点删除弹 "Request timeout"，刷新后工具已经没了。根因有两层——
前端 10s 默认超时掐断了请求，而服务端 rmtree（MCP 工具目录带 node_modules）本身就要
几十秒，且跑在 web 事件循环线程上，期间整个服务（含 websocket）一起停摆。

这里钉住第二层：`delete_*_resource_async` 的目录删除必须在线程里跑，同时返回值形状与
同步车道保持一致（调用方与前端都按同一份字段读）。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from main.service.runtime_service import MainRuntimeService


def _make_tool_dir(root: Path) -> Path:
    tool_dir = root / 'demo_tool'
    (tool_dir / 'main').mkdir(parents=True)
    (tool_dir / 'resource.yaml').write_text('name: demo_tool\n', encoding='utf-8')
    (tool_dir / 'main' / 'tool.py').write_text('def run():\n    return 1\n', encoding='utf-8')
    return tool_dir


def _bare_service(tool_dir: Path) -> tuple[MainRuntimeService, dict[str, object]]:
    service = object.__new__(MainRuntimeService)
    observed: dict[str, object] = {'delete_thread': '', 'finish_thread': ''}

    def _prepare(_tool_id: str) -> dict[str, object]:
        return {
            'target_id': 'demo_tool',
            'root': tool_dir,
            'paths': [tool_dir],
            'skipped_paths': [],
            'before_state': {},
        }

    def _finish(prepared, deleted_paths, *, session_id: str):
        observed['finish_thread'] = threading.current_thread().name
        observed['deleted_paths'] = list(deleted_paths)
        observed['session_id'] = session_id
        return {
            'tool_id': str(prepared.get('target_id') or ''),
            'path': str(prepared.get('root') or ''),
            'deleted_paths': list(deleted_paths),
            'skipped_paths': [],
            'resources': {'ok': True, 'skills': 0, 'tools': 0},
        }

    async def _sync_catalog_targets(*, tool_ids=None, skill_ids=None):
        return {'catalog_synced': False, 'tool_ids': sorted(tool_ids or []), 'skill_ids': sorted(skill_ids or [])}

    original_delete_ordered_paths = MainRuntimeService._delete_ordered_paths

    def _delete_ordered_paths(paths):
        observed['delete_thread'] = threading.current_thread().name
        return original_delete_ordered_paths(service, paths)

    service._prepare_tool_resource_delete = _prepare  # type: ignore[method-assign]
    service._finish_tool_resource_delete = _finish  # type: ignore[method-assign]
    service._sync_catalog_targets = _sync_catalog_targets  # type: ignore[method-assign]
    service._delete_ordered_paths = _delete_ordered_paths  # type: ignore[method-assign]
    return service, observed


def test_delete_tool_resource_async_removes_directory_off_the_event_loop(tmp_path: Path):
    tool_dir = _make_tool_dir(tmp_path)
    service, observed = _bare_service(tool_dir)

    item = asyncio.run(service.delete_tool_resource_async('demo_tool', session_id='web:shared'))

    assert not tool_dir.exists()
    assert observed['delete_thread'] and observed['delete_thread'] != threading.main_thread().name
    # 刷新与治理收尾仍留在事件循环线程：它们要动运行时状态，不进工作线程。
    assert observed['finish_thread'] == threading.main_thread().name
    assert item['tool_id'] == 'demo_tool'
    assert item['deleted_paths'] == [str(tool_dir)]
    assert item['catalog_synced'] is False


def test_delete_skill_resource_async_removes_directory_off_the_event_loop(tmp_path: Path):
    skill_dir = tmp_path / 'demo_skill'
    (skill_dir / 'references').mkdir(parents=True)
    (skill_dir / 'SKILL.md').write_text('# demo\n', encoding='utf-8')

    service = object.__new__(MainRuntimeService)
    observed: dict[str, str] = {}

    def _prepare(_skill_id: str) -> dict[str, object]:
        return {
            'target_id': 'demo_skill',
            'root': skill_dir,
            'paths': [skill_dir],
            'before_state': {},
        }

    def _finish(prepared, deleted_paths, *, session_id: str):
        observed['finish_thread'] = threading.current_thread().name
        return {
            'skill_id': str(prepared.get('target_id') or ''),
            'path': str(prepared.get('root') or ''),
            'deleted_paths': list(deleted_paths),
            'resources': {'ok': True},
        }

    async def _sync_catalog_targets(*, tool_ids=None, skill_ids=None):
        return {'catalog_synced': False}

    original_delete_ordered_paths = MainRuntimeService._delete_ordered_paths

    def _delete_ordered_paths(paths):
        observed['delete_thread'] = threading.current_thread().name
        return original_delete_ordered_paths(service, paths)

    service._prepare_skill_resource_delete = _prepare  # type: ignore[method-assign]
    service._finish_skill_resource_delete = _finish  # type: ignore[method-assign]
    service._sync_catalog_targets = _sync_catalog_targets  # type: ignore[method-assign]
    service._delete_ordered_paths = _delete_ordered_paths  # type: ignore[method-assign]

    item = asyncio.run(service.delete_skill_resource_async('demo_skill'))

    assert not skill_dir.exists()
    assert observed['delete_thread'] != threading.main_thread().name
    assert observed['finish_thread'] == threading.main_thread().name
    assert item['skill_id'] == 'demo_skill'
