from __future__ import annotations

from pathlib import Path

import pytest
from g3ku.agent.tools.filesystem_mutation import FilesystemTool

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_filesystem_allows_write_under_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = FilesystemTool(workspace=workspace)
    target = workspace / 'temp' / 'notes.txt'

    result = await tool.write(path=str(target), content='ok', runtime={})

    assert result.startswith('Successfully wrote')
    assert target.read_text(encoding='utf-8') == 'ok'


@pytest.mark.asyncio
async def test_filesystem_blocks_write_under_legacy_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = FilesystemTool(workspace=workspace)
    target = workspace / 'tmp' / 'notes.txt'

    result = await tool.write(path=str(target), content='blocked', runtime={})

    assert result.startswith('Error:')
    assert 'legacy tmp directories' in result
    assert not target.exists()


@pytest.mark.asyncio
async def test_filesystem_blocks_non_registration_writes_under_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = FilesystemTool(workspace=workspace)
    target = workspace / 'tools' / 'demo' / 'vendor' / 'tool.exe'

    result = await tool.write(path=str(target), content='binary payload', runtime={})

    assert result.startswith('Error:')
    assert 'tools/ may only contain resource.yaml, main/, and toolskills/' in result
    assert not target.exists()


@pytest.mark.asyncio
async def test_filesystem_allows_registration_writes_under_tools_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = FilesystemTool(workspace=workspace)
    target = workspace / 'tools' / 'demo' / 'main' / 'tool.py'

    result = await tool.write(path=str(target), content='print("ok")\n', runtime={})

    assert result.startswith('Successfully wrote')
    assert target.read_text(encoding='utf-8') == 'print("ok")\n'


@pytest.mark.asyncio
async def test_filesystem_allows_tool_payloads_under_externaltools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = FilesystemTool(workspace=workspace)
    target = workspace / 'externaltools' / 'demo' / 'tool.zip'

    result = await tool.write(path=str(target), content='archive payload', runtime={})

    assert result.startswith('Successfully wrote')
    assert target.read_text(encoding='utf-8') == 'archive payload'


@pytest.mark.asyncio
async def test_filesystem_allows_temp_like_write_inside_task_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    task_temp_dir = workspace / 'temp' / 'tasks' / 'task_abc'
    monkeypatch.setattr(FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = FilesystemTool(workspace=workspace)
    target = task_temp_dir / 'notes.tmp'

    result = await tool.write(
        path=str(target),
        content='ok',
        runtime={'task_temp_dir': str(task_temp_dir)},
    )

    assert result.startswith('Successfully wrote')
    assert target.read_text(encoding='utf-8') == 'ok'


@pytest.mark.asyncio
async def test_filesystem_blocks_temp_like_write_outside_task_temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    task_temp_dir = workspace / 'temp' / 'tasks' / 'task_abc'
    monkeypatch.setattr(FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = FilesystemTool(workspace=workspace)
    target = workspace / 'temp' / 'notes.tmp'

    result = await tool.write(
        path=str(target),
        content='blocked',
        runtime={'task_temp_dir': str(task_temp_dir)},
    )

    assert result.startswith('Error:')
    assert str(task_temp_dir) in result
    assert not target.exists()


@pytest.mark.asyncio
async def test_filesystem_blocks_dot_prefixed_tmp_named_write_at_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 回归：`.tmp_skills_job.txt` 这类点前缀命名此前躲过 managed-artifact 启发式，
    # 被直接写到工作区根目录。剥掉前导点后应识别为临时产物并拦截到 temp_root。
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    monkeypatch.setattr(FilesystemTool, '_system_temp_roots', staticmethod(lambda: [tmp_path / 'system-temp']))
    tool = FilesystemTool(workspace=workspace)
    target = workspace / '.tmp_skills_job.txt'

    result = await tool.write(path=str(target), content='leaked', runtime={})

    assert result.startswith('Error:')
    assert str(workspace / 'temp') in result
    assert not target.exists()


def test_looks_like_managed_artifact_covers_dot_prefixed_names(tmp_path: Path) -> None:
    assert FilesystemTool._looks_like_managed_artifact(Path('.tmp_skills_job.txt')) is True
    assert FilesystemTool._looks_like_managed_artifact(Path('.temp_cache.json')) is True
    assert FilesystemTool._looks_like_managed_artifact(Path('tmp_patch.py')) is True
    # 普通业务文件不应误判
    assert FilesystemTool._looks_like_managed_artifact(Path('attempt_counter.py')) is False
    assert FilesystemTool._looks_like_managed_artifact(Path('temperature_sensor.md')) is False
    assert FilesystemTool._looks_like_managed_artifact(Path('report.md')) is False
