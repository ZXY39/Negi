from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping


_PROJECT_ENV_KEYS = (
    'process_cwd',
    'workspace_root',
    'shell_family',
    'project_python',
    'project_python_dir',
    'project_scripts_dir',
    'project_path_entries',
    'project_virtual_env',
    'project_python_hint',
)


def _normalized_shell_family(value: str | None = None) -> str:
    family = str(value or '').strip().lower()
    if family:
        return family
    if os.name == 'nt':
        return 'powershell'
    shell = str(os.environ.get('SHELL') or '').strip().lower()
    if 'powershell' in shell or 'pwsh' in shell:
        return 'powershell'
    if 'bash' in shell:
        return 'bash'
    if 'zsh' in shell:
        return 'zsh'
    return 'sh'


def _resolved_text_path(value: str | Path | None, fallback: str = '') -> str:
    if value in {None, ''}:
        return str(fallback or '')
    try:
        return str(Path(value).expanduser().resolve())
    except Exception:
        return str(value)


def _scripts_dir_for_python(python_executable: str) -> str:
    if not python_executable:
        return ''
    python_dir = Path(python_executable).expanduser().resolve().parent
    if os.name == 'nt':
        if python_dir.name.lower() == 'scripts':
            return str(python_dir)
        return str(python_dir / 'Scripts')
    if python_dir.name.lower() == 'bin':
        return str(python_dir)
    return str(python_dir / 'bin')


def _dedupe_path_entries(entries: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in list(entries or []):
        text = str(raw or '').strip()
        if not text:
            continue
        key = os.path.normcase(os.path.normpath(text))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


def format_python_command(python_executable: str, *, shell_family: str | None = None) -> str:
    text = str(python_executable or '').strip()
    if not text:
        return 'python'
    family = _normalized_shell_family(shell_family)
    if family == 'powershell':
        return f"& '{text.replace("'", "''")}'"
    return shlex.quote(text)


def current_project_environment(
    *,
    shell_family: str | None = None,
    workspace_root: str | Path | None = None,
    process_cwd: str | Path | None = None,
) -> dict[str, Any]:
    resolved_python = _resolved_text_path(sys.executable)
    resolved_python_dir = _resolved_text_path(Path(resolved_python).parent if resolved_python else None)
    resolved_scripts_dir = _resolved_text_path(_scripts_dir_for_python(resolved_python))
    resolved_prefix = _resolved_text_path(getattr(sys, 'prefix', ''))
    resolved_base_prefix = _resolved_text_path(getattr(sys, 'base_prefix', resolved_prefix))
    project_virtual_env = ''
    if resolved_prefix and resolved_base_prefix:
        if os.path.normcase(resolved_prefix) != os.path.normcase(resolved_base_prefix):
            project_virtual_env = resolved_prefix
    resolved_shell = _normalized_shell_family(shell_family)
    return {
        'process_cwd': _resolved_text_path(process_cwd, fallback=os.getcwd()),
        'workspace_root': _resolved_text_path(workspace_root, fallback=os.getcwd()),
        'shell_family': resolved_shell,
        'project_python': resolved_python,
        'project_python_dir': resolved_python_dir,
        'project_scripts_dir': resolved_scripts_dir,
        'project_path_entries': _dedupe_path_entries([resolved_python_dir, resolved_scripts_dir]),
        'project_virtual_env': project_virtual_env,
        'project_python_hint': format_python_command(resolved_python, shell_family=resolved_shell),
    }


def resolve_project_environment(
    *,
    runtime: Mapping[str, Any] | None = None,
    shell_family: str | None = None,
    workspace_root: str | Path | None = None,
    process_cwd: str | Path | None = None,
) -> dict[str, Any]:
    payload = current_project_environment(
        shell_family=shell_family,
        workspace_root=workspace_root,
        process_cwd=process_cwd,
    )
    runtime_dict = dict(runtime or {})
    for key in _PROJECT_ENV_KEYS:
        value = runtime_dict.get(key)
        if key == 'project_path_entries':
            if isinstance(value, (list, tuple)):
                payload[key] = _dedupe_path_entries([str(item or '').strip() for item in value])
            continue
        if isinstance(value, str) and value.strip():
            payload[key] = value.strip()
    payload['project_path_entries'] = _dedupe_path_entries(
        list(payload.get('project_path_entries') or [])
        or [
            str(payload.get('project_python_dir') or '').strip(),
            str(payload.get('project_scripts_dir') or '').strip(),
        ]
    )
    if not str(payload.get('project_python_hint') or '').strip():
        payload['project_python_hint'] = format_python_command(
            str(payload.get('project_python') or '').strip(),
            shell_family=str(payload.get('shell_family') or shell_family or '').strip() or None,
        )
    return payload


def apply_project_environment(
    env: Mapping[str, str] | None = None,
    *,
    runtime: Mapping[str, Any] | None = None,
    shell_family: str | None = None,
    workspace_root: str | Path | None = None,
    process_cwd: str | Path | None = None,
    path_append: str = '',
) -> dict[str, str]:
    prepared = dict(env or {})
    payload = resolve_project_environment(
        runtime=runtime,
        shell_family=shell_family,
        workspace_root=workspace_root,
        process_cwd=process_cwd,
    )
    path_entries = list(payload.get('project_path_entries') or [])
    if path_append:
        path_entries.extend([part for part in str(path_append).split(os.pathsep) if str(part or '').strip()])
    existing_path = str(prepared.get('PATH') or '')
    if existing_path:
        path_entries.extend(existing_path.split(os.pathsep))
    merged_path = os.pathsep.join(_dedupe_path_entries(path_entries))
    if merged_path:
        prepared['PATH'] = merged_path
    project_python = str(payload.get('project_python') or '').strip()
    if project_python:
        prepared['G3KU_PROJECT_PYTHON'] = project_python
    project_python_dir = str(payload.get('project_python_dir') or '').strip()
    if project_python_dir:
        prepared['G3KU_PROJECT_PYTHON_DIR'] = project_python_dir
    project_scripts_dir = str(payload.get('project_scripts_dir') or '').strip()
    if project_scripts_dir:
        prepared['G3KU_PROJECT_SCRIPTS_DIR'] = project_scripts_dir
    project_python_hint = str(payload.get('project_python_hint') or '').strip()
    if project_python_hint:
        prepared['G3KU_PROJECT_PYTHON_HINT'] = project_python_hint
    project_virtual_env = str(payload.get('project_virtual_env') or '').strip()
    if project_virtual_env:
        prepared['VIRTUAL_ENV'] = project_virtual_env
    return prepared


__all__ = [
    'apply_project_environment',
    'current_project_environment',
    'format_python_command',
    'resolve_project_environment',
]
