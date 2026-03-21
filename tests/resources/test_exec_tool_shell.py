from __future__ import annotations

import json
import os

import pytest

import g3ku.agent.tools.shell as shell_module
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


def test_exec_tool_builds_subprocess_env_with_project_python(monkeypatch) -> None:
    fake_payload = {
        'project_python': r'C:\Python314\python.exe',
        'project_python_dir': r'C:\Python314',
        'project_scripts_dir': r'C:\Python314\Scripts',
        'project_path_entries': [r'C:\Python314', r'C:\Python314\Scripts'],
        'project_virtual_env': r'C:\Python314',
        'project_python_hint': r"& 'C:\Python314\python.exe'",
        'shell_family': 'powershell',
        'process_cwd': r'D:\projects\G3KU',
        'workspace_root': r'D:\projects\G3KU',
    }
    monkeypatch.setattr(shell_module, 'resolve_project_environment', lambda **kwargs: dict(fake_payload))
    monkeypatch.setenv('PATH', r'C:\Windows\System32')
    tool = ExecTool(path_append=r'D:\extra\bin')

    env = tool._build_subprocess_env(runtime={'session_key': 'web:shared'}, cwd=r'D:\projects\G3KU')

    assert env['G3KU_PROJECT_PYTHON'] == fake_payload['project_python']
    assert env['G3KU_PROJECT_PYTHON_HINT'] == fake_payload['project_python_hint']
    assert env['VIRTUAL_ENV'] == fake_payload['project_virtual_env']
    assert env['PATH'].split(os.pathsep)[:4] == [
        r'C:\Python314',
        r'C:\Python314\Scripts',
        r'D:\extra\bin',
        r'C:\Windows\System32',
    ]


@pytest.mark.asyncio
async def test_exec_tool_makes_python_available_from_project_environment_on_windows(monkeypatch) -> None:
    if os.name != 'nt':
        pytest.skip('Windows-only regression test')

    tool = ExecTool()
    monkeypatch.setenv('PATH', '')
    payload = json.loads(
        await tool.execute(
            command='python -c "import sys; print(sys.executable)"',
            __g3ku_runtime={'session_key': 'web:shared'},
        )
    )

    assert payload['status'] == 'success'
    assert payload['exit_code'] == 0
