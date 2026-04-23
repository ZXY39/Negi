from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import MethodType

import pytest

import g3ku.agent.tools.filesystem_mutation as filesystem_mutation_module
from g3ku.agent.tools.filesystem_mutation import FilesystemTool


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_agent_browser_module():
    module_path = REPO_ROOT / 'tools' / 'agent_browser' / 'main' / 'tool.py'
    spec = importlib.util.spec_from_file_location('test_agent_browser_tool_module', module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_filesystem_validation_command_decodes_legacy_codepage_output_and_sets_pythonioencoding(monkeypatch, tmp_path) -> None:
    class _StubProcess:
        returncode = 1

        async def communicate(self):
            return (b'', '站点经验\n'.encode('gbk'))

        async def wait(self):
            return 1

        def kill(self):
            return None

    captured: dict[str, object] = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured['env'] = dict(kwargs.get('env') or {})
        return _StubProcess()

    monkeypatch.setattr(filesystem_mutation_module.os, 'name', 'nt', raising=False)
    monkeypatch.setattr(filesystem_mutation_module.asyncio, 'create_subprocess_exec', _fake_create_subprocess_exec)

    tool = FilesystemTool(workspace=tmp_path)
    result = await tool._run_validation_command(
        command='python -c "pass"',
        cwd=str(tmp_path),
        timeout_seconds=5,
    )

    assert result['ok'] is False
    assert '站点经验' in result['stderr']
    assert captured['env']['PYTHONIOENCODING'] == 'utf-8'


@pytest.mark.asyncio
async def test_agent_browser_run_command_decodes_legacy_codepage_output(monkeypatch, tmp_path) -> None:
    module = _load_agent_browser_module()

    class _StubProcess:
        returncode = 1

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _StubProcess()

    async def _fake_communicate_process(self, *, process, stdin, timeout_seconds, cancel_token):
        return (b'', '站点经验\n'.encode('gbk'))

    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', _fake_create_subprocess_exec)

    handler = module.AgentBrowserTool(workspace=tmp_path, settings=module.AgentBrowserToolSettings())
    handler._communicate_process = MethodType(_fake_communicate_process, handler)

    result = await handler._run_command(
        command_prefix=['agent-browser'],
        args=['open', 'https://example.com'],
        cwd=str(tmp_path),
        env={},
        stdin=None,
        timeout_seconds=5,
        cancel_token=None,
    )

    assert result['ok'] is False
    assert '站点经验' in result['stderr']
