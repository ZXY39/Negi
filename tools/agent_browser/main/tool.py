from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.resources.tool_settings import AgentBrowserToolSettings, runtime_tool_settings


class AgentBrowserTool(Tool):
    def __init__(self, *, workspace: Path, settings: AgentBrowserToolSettings, toolskill_path: Path | None = None) -> None:
        self._workspace = Path(workspace).resolve()
        self._settings = settings
        self._toolskill_path = toolskill_path
        self._repo_url = 'https://github.com/vercel-labs/agent-browser'

    @property
    def name(self) -> str:
        return 'agent_browser'

    @property
    def description(self) -> str:
        return 'Browser automation via the official agent-browser CLI, with install and update guidance.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'mode': {
                    'type': 'string',
                    'enum': ['run', 'install_help', 'update_help'],
                    'description': 'Execution mode. Use run for browser commands and help modes for guidance.',
                },
                'args': {
                    'type': 'array',
                    'description': 'CLI arguments forwarded to agent-browser when mode=run.',
                    'items': {'type': 'string', 'description': 'A single CLI argument.'},
                },
                'cwd': {
                    'type': 'string',
                    'description': 'Optional absolute working directory. Defaults to workspace and must remain inside workspace.',
                },
                'profile': {
                    'type': 'string',
                    'description': 'Optional profile directory. Relative paths resolve from workspace.',
                },
                'session_name': {
                    'type': 'string',
                    'description': 'Optional browser session name. Defaults to the configured g3ku browser session.',
                },
                'stdin': {
                    'type': 'string',
                    'description': 'Optional stdin text passed to the process.',
                },
                'timeout_seconds': {
                    'type': 'integer',
                    'description': 'Optional timeout override in seconds.',
                },
            },
            'required': [],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        mode = str((params or {}).get('mode') or 'run').strip().lower() or 'run'
        raw_args = (params or {}).get('args')
        args = [str(item) for item in list(raw_args or []) if str(item or '').strip()] if isinstance(raw_args, list) else []
        if mode == 'run' and not args:
            errors.append('args must not be empty when mode=run')
        if mode in {'install_help', 'update_help'} and raw_args not in (None, [], ()):  # keep help modes side-effect free
            errors.append('args must be omitted when mode=install_help or mode=update_help')
        return errors

    async def execute(
        self,
        mode: str = 'run',
        args: list[str] | None = None,
        cwd: str | None = None,
        profile: str | None = None,
        session_name: str | None = None,
        stdin: str | None = None,
        timeout_seconds: int | None = None,
        **_: Any,
    ) -> str:
        normalized_mode = str(mode or 'run').strip().lower() or 'run'
        if normalized_mode in {'install_help', 'update_help'}:
            return json.dumps(self._help_payload(normalized_mode), ensure_ascii=False)

        argv = [str(item) for item in list(args or []) if str(item or '').strip()]
        if not argv:
            return json.dumps({'ok': False, 'error': 'args must not be empty when mode=run'}, ensure_ascii=False)

        command_prefix = await self._resolve_command_prefix()
        if not command_prefix:
            return json.dumps(self._missing_dependency_payload(), ensure_ascii=False)

        try:
            resolved_cwd = self._resolve_cwd(cwd)
        except ValueError as exc:
            return json.dumps(
                {
                    'ok': False,
                    'error': str(exc),
                    'command': [],
                    'cwd': str(self._workspace),
                    'stdout': '',
                    'stderr': '',
                    'stdout_json': None,
                    'exit_code': None,
                    'timed_out': False,
                    'retried_after_session_cleanup': False,
                    'initial_attempt': None,
                    'session_cleanup': None,
                },
                ensure_ascii=False,
            )
        env = os.environ.copy()
        final_args = self._inject_global_flags(argv=argv, session=None, profile=profile, session_name=session_name)
        active_session = self._session_from_args(final_args)
        effective_timeout = max(1, int(timeout_seconds or self._settings.default_timeout_seconds or 300))

        first_result = await self._run_command(
            command_prefix=command_prefix,
            args=final_args,
            cwd=str(resolved_cwd),
            env=env,
            stdin=stdin,
            timeout_seconds=effective_timeout,
        )
        first_result.setdefault('retried_after_session_cleanup', False)
        first_result.setdefault('initial_attempt', None)
        first_result.setdefault('session_cleanup', None)

        if bool(first_result.get('timed_out')) and active_session and bool(self._settings.cleanup_on_timeout):
            first_result['session_cleanup'] = await self._close_session(
                command_prefix=command_prefix,
                cwd=str(resolved_cwd),
                env=env,
                session=active_session,
            )
            return json.dumps(first_result, ensure_ascii=False)

        if (
            not bool(first_result.get('ok'))
            and active_session
            and bool(self._settings.retry_after_session_cleanup)
            and self._is_daemon_profile_conflict(first_result)
        ):
            cleanup = await self._close_session(
                command_prefix=command_prefix,
                cwd=str(resolved_cwd),
                env=env,
                session=active_session,
            )
            retry_result = await self._run_command(
                command_prefix=command_prefix,
                args=final_args,
                cwd=str(resolved_cwd),
                env=env,
                stdin=stdin,
                timeout_seconds=effective_timeout,
            )
            retry_result['retried_after_session_cleanup'] = True
            retry_result['initial_attempt'] = first_result
            retry_result['session_cleanup'] = cleanup
            return json.dumps(retry_result, ensure_ascii=False)

        return json.dumps(first_result, ensure_ascii=False)

    def _help_payload(self, mode: str) -> dict[str, Any]:
        install_section = self._toolskill_section('安装')
        update_section = self._toolskill_section('更新')
        troubleshooting_section = self._toolskill_section('故障排查')
        if mode == 'install_help':
            return {
                'ok': True,
                'mode': mode,
                'repo_url': self._repo_url,
                'summary': 'Install the official agent-browser CLI and ensure it is available on PATH before using this tool.',
                'recommended_steps': self._section_bullets(install_section) or [
                    'Follow the upstream installation instructions from the official repository.',
                    'Ensure agent-browser is available on PATH.',
                    'Use exec to verify the command location after installation.',
                ],
                'verification_steps': [
                    'Windows: exec(command="where agent-browser")',
                    'macOS/Linux: exec(command="which agent-browser")',
                ],
                'path_configuration_notes': troubleshooting_section or install_section,
            }
        return {
            'ok': True,
            'mode': mode,
            'repo_url': self._repo_url,
            'summary': 'Update the upstream agent-browser CLI using the official upgrade path, then verify PATH resolution again.',
            'recommended_steps': self._section_bullets(update_section) or [
                'Update the CLI using the upstream repository instructions.',
                'Re-run PATH verification after the upgrade.',
            ],
            'verification_steps': [
                'Windows: exec(command="where agent-browser")',
                'macOS/Linux: exec(command="which agent-browser")',
            ],
            'path_configuration_notes': troubleshooting_section or update_section,
        }

    async def _resolve_command_prefix(self) -> list[str]:
        prefix = [str(item) for item in list(self._settings.command_prefix or []) if str(item or '').strip()]
        if not prefix:
            return []
        executable = prefix[0]
        if any(sep in executable for sep in ('/', '\\')):
            candidate = Path(executable).expanduser()
            if not candidate.is_absolute():
                return []
            if not candidate.exists():
                return []
            prefix[0] = str(candidate)
            return prefix
        if shutil.which(executable) is None:
            return []
        return prefix

    def _resolve_cwd(self, cwd: str | None) -> Path:
        if not str(cwd or '').strip():
            return self._workspace
        candidate = Path(str(cwd)).expanduser()
        if not candidate.is_absolute():
            raise ValueError('cwd must be an absolute path inside workspace')
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self._workspace)
        except ValueError as exc:
            raise ValueError('cwd must stay inside workspace') from exc
        return resolved

    def _inject_global_flags(
        self,
        *,
        argv: list[str],
        session: str | None,
        profile: str | None,
        session_name: str | None,
    ) -> list[str]:
        effective_profile = self._resolve_profile_dir(profile)
        effective_session = str(session_name or session or self._settings.default_session_name or 'g3ku-agent-browser').strip() or 'g3ku-agent-browser'
        next_argv = list(argv or [])
        if '--session' not in next_argv:
            next_argv = ['--session', effective_session, *next_argv]
        if '--profile' not in next_argv:
            next_argv = ['--profile', str(effective_profile), *next_argv]
        return next_argv

    def _resolve_profile_dir(self, profile: str | None) -> Path:
        raw = str(profile or '').strip()
        if not raw:
            target = self._workspace / str(self._settings.profile_root or '.g3ku/tool-data/agent_browser/profiles') / str(self._settings.default_session_name or 'g3ku-agent-browser')
        else:
            candidate = Path(raw).expanduser()
            target = candidate if candidate.is_absolute() else (self._workspace / candidate)
        target.mkdir(parents=True, exist_ok=True)
        return target.resolve()

    @staticmethod
    def _session_from_args(args: list[str]) -> str:
        for index, value in enumerate(list(args or [])):
            if str(value) == '--session' and index + 1 < len(args):
                return str(args[index + 1] or '').strip()
        return ''

    async def _run_command(
        self,
        *,
        command_prefix: list[str],
        args: list[str],
        cwd: str,
        env: dict[str, str],
        stdin: str | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        try:
            process = await asyncio.create_subprocess_exec(
                *list(command_prefix or []),
                *list(args or []),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                cwd=cwd,
                env=env,
            )
        except FileNotFoundError:
            payload = self._missing_dependency_payload()
            payload.update({'command': [*list(command_prefix or []), *list(args or [])], 'cwd': cwd})
            return payload
        except Exception as exc:
            return {
                'ok': False,
                'command': [*list(command_prefix or []), *list(args or [])],
                'cwd': cwd,
                'stdout': '',
                'stderr': str(exc),
                'stdout_json': None,
                'exit_code': None,
                'timed_out': False,
                'retried_after_session_cleanup': False,
                'initial_attempt': None,
                'session_cleanup': None,
            }
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(None if stdin is None else str(stdin).encode('utf-8')),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return {
                'ok': False,
                'command': [*list(command_prefix or []), *list(args or [])],
                'cwd': cwd,
                'stdout': '',
                'stderr': f'agent-browser timed out after {timeout_seconds} seconds',
                'stdout_json': None,
                'exit_code': None,
                'timed_out': True,
                'retried_after_session_cleanup': False,
                'initial_attempt': None,
                'session_cleanup': None,
                'error': f'agent-browser timed out after {timeout_seconds} seconds',
            }
        stdout_text = stdout.decode('utf-8', errors='replace') if stdout else ''
        stderr_text = stderr.decode('utf-8', errors='replace') if stderr else ''
        return {
            'ok': process.returncode == 0,
            'command': [*list(command_prefix or []), *list(args or [])],
            'cwd': cwd,
            'stdout': stdout_text,
            'stderr': stderr_text,
            'stdout_json': self._parse_json(stdout_text),
            'exit_code': process.returncode,
            'timed_out': False,
            'retried_after_session_cleanup': False,
            'initial_attempt': None,
            'session_cleanup': None,
        }

    async def _close_session(
        self,
        *,
        command_prefix: list[str],
        cwd: str,
        env: dict[str, str],
        session: str,
    ) -> dict[str, Any]:
        return await self._run_command(
            command_prefix=command_prefix,
            args=['--session', str(session or '').strip(), 'close'],
            cwd=cwd,
            env=env,
            stdin=None,
            timeout_seconds=max(1, int(self._settings.default_timeout_seconds or 300)),
        )

    @staticmethod
    def _parse_json(text: str) -> Any:
        raw = str(text or '').strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    def _is_daemon_profile_conflict(payload: dict[str, Any]) -> bool:
        stderr_text = str(payload.get('stderr') or '').lower()
        return '--profile ignored' in stderr_text or 'daemon already running' in stderr_text

    def _missing_dependency_payload(self) -> dict[str, Any]:
        install_help = self._help_payload('install_help')
        return {
            'ok': False,
            'missing_dependency': True,
            'error': 'agent-browser CLI not found',
            'repo_url': self._repo_url,
            'command': [],
            'cwd': str(self._workspace),
            'stdout': '',
            'stderr': '',
            'stdout_json': None,
            'exit_code': None,
            'timed_out': False,
            'retried_after_session_cleanup': False,
            'initial_attempt': None,
            'session_cleanup': None,
            'summary': install_help.get('summary', ''),
            'next_actions': [
                'load_tool_context(tool_id="agent_browser")',
                'Use exec to install agent-browser or verify that it is available on PATH.',
            ],
        }

    def _toolskill_section(self, heading: str) -> str:
        if self._toolskill_path is None or not self._toolskill_path.exists():
            return ''
        text = self._toolskill_path.read_text(encoding='utf-8').strip()
        if not text:
            return ''
        marker = f'## {heading}'
        if marker not in text:
            return ''
        suffix = text.split(marker, 1)[1]
        parts = suffix.split('\n## ', 1)
        return parts[0].strip()

    @staticmethod
    def _section_bullets(text: str) -> list[str]:
        items = []
        for raw in str(text or '').splitlines():
            line = raw.strip()
            if line.startswith('- '):
                items.append(line[2:].strip())
        return items


def build(runtime):
    settings = runtime_tool_settings(runtime, AgentBrowserToolSettings, tool_name='agent_browser')
    descriptor = getattr(runtime, 'resource_descriptor', None)
    toolskill_path = getattr(descriptor, 'toolskills_main_path', None) if descriptor is not None else None
    return AgentBrowserTool(
        workspace=Path(runtime.workspace),
        settings=settings,
        toolskill_path=Path(toolskill_path) if toolskill_path is not None else None,
    )
