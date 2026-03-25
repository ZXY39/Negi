from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_filesystem_module():
    module_path = REPO_ROOT / 'tools' / 'filesystem' / 'main' / 'tool.py'
    module_name = f'test_filesystem_tool_{uuid4().hex}'
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_filesystem_allows_write_under_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_filesystem_module()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(module.FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = module.FilesystemTool(workspace=workspace)
    target = workspace / 'temp' / 'notes.txt'

    result = await tool.execute(action='write', path=str(target), content='ok')

    assert result.startswith('Successfully wrote')
    assert target.read_text(encoding='utf-8') == 'ok'


@pytest.mark.asyncio
async def test_filesystem_blocks_write_under_legacy_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_filesystem_module()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(module.FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = module.FilesystemTool(workspace=workspace)
    target = workspace / 'tmp' / 'notes.txt'

    result = await tool.execute(action='write', path=str(target), content='blocked')

    assert result.startswith('Error:')
    assert 'legacy tmp directories' in result
    assert not target.exists()


@pytest.mark.asyncio
async def test_filesystem_blocks_non_registration_writes_under_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_filesystem_module()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(module.FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = module.FilesystemTool(workspace=workspace)
    target = workspace / 'tools' / 'demo' / 'vendor' / 'tool.exe'

    result = await tool.execute(action='write', path=str(target), content='binary payload')

    assert result.startswith('Error:')
    assert 'tools/ may only contain resource.yaml, main/, and toolskills/' in result
    assert not target.exists()


@pytest.mark.asyncio
async def test_filesystem_allows_registration_writes_under_tools_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_filesystem_module()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(module.FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = module.FilesystemTool(workspace=workspace)
    target = workspace / 'tools' / 'demo' / 'main' / 'tool.py'

    result = await tool.execute(action='write', path=str(target), content='print("ok")\n')

    assert result.startswith('Successfully wrote')
    assert target.read_text(encoding='utf-8') == 'print("ok")\n'


@pytest.mark.asyncio
async def test_filesystem_allows_tool_payloads_under_externaltools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_filesystem_module()
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(module.FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = module.FilesystemTool(workspace=workspace)
    target = workspace / 'externaltools' / 'demo' / 'tool.zip'

    result = await tool.execute(action='write', path=str(target), content='archive payload')

    assert result.startswith('Successfully wrote')
    assert target.read_text(encoding='utf-8') == 'archive payload'
