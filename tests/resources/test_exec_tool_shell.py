from __future__ import annotations

import json
import os

import pytest

from g3ku.agent.tools.shell import ExecTool


def test_windows_shell_argv_uses_powershell(monkeypatch, tmp_path) -> None:
    system_root = tmp_path / 'Windows'
    powershell_exe = system_root / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    powershell_exe.parent.mkdir(parents=True, exist_ok=True)
    powershell_exe.write_text('', encoding='utf-8')
    monkeypatch.setenv('SystemRoot', str(system_root))

    argv = ExecTool._windows_shell_argv('pwd')

    assert argv[0] == str(powershell_exe)
    assert argv[-1] == 'pwd'


@pytest.mark.asyncio
async def test_exec_tool_runs_pwd_on_windows() -> None:
    if os.name != 'nt':
        pytest.skip('Windows-only regression test')

    tool = ExecTool()
    payload = json.loads(await tool.execute(command='pwd', __g3ku_runtime={'session_key': 'web:shared'}))

    assert payload['status'] == 'success'
    assert payload['exit_code'] == 0


@pytest.mark.asyncio
async def test_exec_tool_safety_guard_disabled_by_default() -> None:
    tool = ExecTool()
    payload = json.loads(await tool.execute(command='echo shutdown', __g3ku_runtime={'session_key': 'web:shared'}))

    assert payload['status'] == 'success'
    assert payload['exit_code'] == 0
    assert 'shutdown' in payload['head_preview'].lower()


@pytest.mark.asyncio
async def test_exec_tool_can_explicitly_enable_safety_guard() -> None:
    tool = ExecTool(enable_safety_guard=True)
    payload = json.loads(await tool.execute(command='echo shutdown', __g3ku_runtime={'session_key': 'web:shared'}))

    assert payload['status'] == 'error'
    assert payload['exit_code'] is None
    assert 'dangerous pattern detected' in payload['error'].lower()
