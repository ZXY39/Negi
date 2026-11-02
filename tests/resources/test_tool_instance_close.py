from __future__ import annotations

import asyncio
import inspect

import yaml

from g3ku.resources.embedded_mcp import EmbeddedMCPTool
from g3ku.resources.loader import ManifestBackedTool
from g3ku.resources.manager import ResourceManager
from g3ku.resources.registry import ResourceRegistry


def _build_descriptor(tmp_path):
    registry = ResourceRegistry(workspace=tmp_path, skills_dir=tmp_path / 'skills', tools_dir=tmp_path / 'tools')
    tool_root = tmp_path / 'tools' / 'demo_tool'
    main_root = tool_root / 'main'
    main_root.mkdir(parents=True)
    (main_root / 'tool.py').write_text('def build(runtime):\n    return None\n', encoding='utf-8')
    manifest = {
        'schema_version': 1,
        'kind': 'tool',
        'name': 'demo_tool',
        'description': 'Demo tool',
    }
    (tool_root / 'resource.yaml').write_text(yaml.safe_dump(manifest, sort_keys=False), encoding='utf-8')
    descriptor = registry.build_tool_descriptor(tool_root)
    assert descriptor is not None
    return descriptor


class _SyncCloseHandler:
    def __init__(self) -> None:
        self.closed = False

    async def execute(self, **kwargs):
        return 'ok'

    def close(self):
        self.closed = True
        return 'closed'


class _AsyncCloseHandler:
    def __init__(self) -> None:
        self.closed = False

    async def execute(self, **kwargs):
        return 'ok'

    async def close(self):
        self.closed = True


class _PlainHandler:
    async def execute(self, **kwargs):
        return 'ok'


def test_manifest_backed_tool_forwards_sync_close(tmp_path):
    handler = _SyncCloseHandler()
    tool = ManifestBackedTool(_build_descriptor(tmp_path), handler)

    assert tool.close() == 'closed'
    assert handler.closed is True


def test_manifest_backed_tool_forwards_async_close(tmp_path):
    handler = _AsyncCloseHandler()
    tool = ManifestBackedTool(_build_descriptor(tmp_path), handler)

    result = tool.close()
    assert inspect.isawaitable(result)
    asyncio.run(result)
    assert handler.closed is True


def test_manifest_backed_tool_close_is_none_without_handler_close(tmp_path):
    tool = ManifestBackedTool(_build_descriptor(tmp_path), _PlainHandler())

    assert tool.close() is None


def test_resource_manager_closes_manifest_wrapper_handlers(tmp_path):
    descriptor = _build_descriptor(tmp_path)

    sync_handler = _SyncCloseHandler()
    ResourceManager._close_tool_instance(ManifestBackedTool(descriptor, sync_handler))
    assert sync_handler.closed is True

    async_handler = _AsyncCloseHandler()
    ResourceManager._close_tool_instance(ManifestBackedTool(descriptor, async_handler))
    assert async_handler.closed is True


async def test_resource_manager_schedules_async_close_on_running_loop(tmp_path):
    handler = _AsyncCloseHandler()
    ResourceManager._close_tool_instance(ManifestBackedTool(_build_descriptor(tmp_path), handler))

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert handler.closed is True


def test_embedded_mcp_tool_close_still_forwards(tmp_path):
    handler = _SyncCloseHandler()
    tool = EmbeddedMCPTool(_build_descriptor(tmp_path), handler)

    assert tool.close() == 'closed'
    assert handler.closed is True
