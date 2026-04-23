from __future__ import annotations

import asyncio
import json
import os
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.resources.tool_settings import AgentBrowserToolSettings, runtime_tool_settings
from g3ku.runtime.cancellation import ToolCancellationRequested
from g3ku.utils.subprocess_text import decode_subprocess_output


class AgentBrowserTool(Tool):
    def __init__(self, *, workspace: Path, settings: AgentBrowserToolSettings, toolskill_path: Path | None = None) -> None:
        self._workspace = Path(workspace).resolve()
        self._settings = settings
        self._toolskill_path = toolskill_path
        self._repo_url = 'https://github.com/vercel-labs/agent-browser'
        self._pinned_version = 'v0.22.2'

    @property
    def name(self) -> str:
        return 'agent_browser'

    @property
    def description(self) -> str:
        return 'Browser automation via the official agent-browser CLI installed under externaltools/agent_browser.'

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
                    'description': 'Optional profile directory. Relative paths resolve from workspace; absolute paths must also stay inside workspace.',
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
        if mode in {'install_help', 'update_help'} and raw_args not in (None, [], ()):
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
        __g3ku_runtime: dict[str, Any] | None = None,
        **_: Any,
    ) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        cancel_token = runtime.get('cancel_token') if isinstance(runtime, dict) else None
        normalized_mode = str(mode or 'run').strip().lower() or 'run'
        if normalized_mode in {'install_help', 'update_help'}:
            return json.dumps(self._help_payload(normalized_mode), ensure_ascii=False)

        argv = [str(item) for item in list(args or []) if str(item or '').strip()]
        if not argv:
            return json.dumps({'ok': False, 'error': 'args must not be empty when mode=run'}, ensure_ascii=False)

        try:
            self._check_cancel(cancel_token)
            command_prefix = await self._resolve_command_prefix()
            if not command_prefix:
                return json.dumps(self._missing_dependency_payload(), ensure_ascii=False)

            resolved_cwd = self._resolve_cwd(cwd)
            env = self._build_process_env()
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
                cancel_token=cancel_token,
            )
            first_result.setdefault('retried_after_session_cleanup', False)
            first_result.setdefault('initial_attempt', None)
            first_result.setdefault('session_cleanup', None)

            if bool(first_result.get('cancelled')):
                return json.dumps(first_result, ensure_ascii=False)

            if bool(first_result.get('timed_out')) and active_session and bool(self._settings.cleanup_on_timeout):
                first_result['session_cleanup'] = await self._close_session(
                    command_prefix=command_prefix,
                    cwd=str(resolved_cwd),
                    env=env,
                    session=active_session,
                    cancel_token=cancel_token,
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
                    cancel_token=cancel_token,
                )
                retry_result = await self._run_command(
                    command_prefix=command_prefix,
                    args=final_args,
                    cwd=str(resolved_cwd),
                    env=env,
                    stdin=stdin,
                    timeout_seconds=effective_timeout,
                    cancel_token=cancel_token,
                )
                retry_result['retried_after_session_cleanup'] = True
                retry_result['initial_attempt'] = first_result
                retry_result['session_cleanup'] = cleanup
                return json.dumps(retry_result, ensure_ascii=False)

            return json.dumps(first_result, ensure_ascii=False)
        except (ValueError, ToolCancellationRequested) as exc:
            return json.dumps(
                self._error_payload(
                    error=str(exc),
                    cwd=str(self._workspace),
                    cancelled=isinstance(exc, ToolCancellationRequested),
                ),
                ensure_ascii=False,
            )

    def _help_payload(self, mode: str) -> dict[str, Any]:
        install_section = self._toolskill_section('安装')
        update_section = self._toolskill_section('更新')
        troubleshooting_section = self._toolskill_section('故障排查')
        install_root = self._install_root()
        browser_root = self._browser_root()
        temp_root = self._temp_root()
        verification_steps = [self._local_cli_verification_command()]
        if mode == 'install_help':
            return {
                'ok': True,
                'mode': mode,
                'repo_url': self._repo_url,
                'version': self._pinned_version,
                'summary': 'Install the official agent-browser CLI under externaltools/agent_browser and keep temporary install artifacts under temp/agent_browser.',
                'install_root': str(install_root),
                'browser_root': str(browser_root),
                'temp_root': str(temp_root),
                'recommended_steps': self._section_bullets(install_section) or [
                    f'Install agent-browser {self._pinned_version} under {install_root}.',
                    f'Install Playwright browser payloads under {browser_root}.',
                    f'Keep npm cache and other temporary artifacts under {temp_root}.',
                ],
                'verification_steps': verification_steps,
                'path_configuration_notes': troubleshooting_section or install_section,
            }
        return {
            'ok': True,
            'mode': mode,
            'repo_url': self._repo_url,
            'version': self._pinned_version,
            'summary': 'Update the local agent-browser CLI in externaltools/agent_browser, then verify the local executable path again.',
            'install_root': str(install_root),
            'browser_root': str(browser_root),
            'temp_root': str(temp_root),
            'recommended_steps': self._section_bullets(update_section) or [
                f'Update the pinned agent-browser CLI under {install_root}.',
                f'Re-run the browser install step so {browser_root} stays aligned with the local CLI version.',
            ],
            'verification_steps': verification_steps,
            'path_configuration_notes': troubleshooting_section or update_section,
        }

    async def _resolve_command_prefix(self) -> list[str]:
        explicit_prefix = [str(item) for item in list(self._settings.command_prefix or []) if str(item or '').strip()]
        if explicit_prefix:
            return self._resolve_explicit_command_prefix(explicit_prefix)
        return self._local_command_prefix()

    def _resolve_explicit_command_prefix(self, prefix: list[str]) -> list[str]:
        resolved: list[str] = []
        for index, raw in enumerate(list(prefix or [])):
            item = str(raw or '').strip()
            if not item:
                continue
            if any(sep in item for sep in ('/', '\\')):
                candidate = Path(item).expanduser()
                if not candidate.is_absolute():
                    candidate = self._workspace / candidate
                candidate = candidate.resolve(strict=False)
                self._ensure_within_workspace(candidate, label='command_prefix path')
                if not candidate.exists():
                    return []
                resolved.append(str(candidate))
                continue
            if index == 0:
                executable = shutil.which(item)
                if executable is None:
                    return []
                resolved.append(str(Path(executable).resolve(strict=False)))
                continue
            resolved.append(item)
        return resolved

    def _local_command_prefix(self) -> list[str]:
        install_root = self._install_root()
        cmd_candidate = install_root / 'node_modules' / '.bin' / ('agent-browser.cmd' if os.name == 'nt' else 'agent-browser')
        if cmd_candidate.exists():
            return [str(cmd_candidate.resolve(strict=False))]
        script_candidate = install_root / 'node_modules' / 'agent-browser' / 'bin' / 'agent-browser.js'
        node_executable = shutil.which('node')
        if node_executable is not None and script_candidate.exists():
            return [str(Path(node_executable).resolve(strict=False)), str(script_candidate.resolve(strict=False))]
        return []

    def _resolve_cwd(self, cwd: str | None) -> Path:
        if not str(cwd or '').strip():
            return self._workspace
        candidate = Path(str(cwd)).expanduser()
        if not candidate.is_absolute():
            raise ValueError('cwd must be an absolute path inside workspace')
        resolved = candidate.resolve(strict=False)
        self._ensure_within_workspace(resolved, label='cwd')
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
            target = self._workspace_path(
                str(self._settings.profile_root or '.g3ku/tool-data/agent_browser/profiles'),
                label='profile_root',
            ) / str(self._settings.default_session_name or 'g3ku-agent-browser')
        else:
            candidate = Path(raw).expanduser()
            target = candidate if candidate.is_absolute() else (self._workspace / candidate)
            target = target.resolve(strict=False)
            self._ensure_within_workspace(target, label='profile')
        target.mkdir(parents=True, exist_ok=True)
        return target.resolve(strict=False)

    def _build_process_env(self) -> dict[str, str]:
        env = os.environ.copy()
        install_root = self._install_root()
        agent_browser_home = self._agent_browser_home()
        browser_root = self._browser_root()
        temp_root = self._temp_root()
        install_root.mkdir(parents=True, exist_ok=True)
        agent_browser_home.mkdir(parents=True, exist_ok=True)
        browser_root.mkdir(parents=True, exist_ok=True)
        temp_root.mkdir(parents=True, exist_ok=True)
        env['AGENT_BROWSER_HOME'] = str(agent_browser_home)
        env['PLAYWRIGHT_BROWSERS_PATH'] = str(browser_root)
        browser_executable = self._browser_executable_path()
        if browser_executable is not None:
            env['AGENT_BROWSER_EXECUTABLE_PATH'] = str(browser_executable)
        env['TMPDIR'] = str(temp_root)
        env['TMP'] = str(temp_root)
        env['TEMP'] = str(temp_root)
        env['G3KU_AGENT_BROWSER_INSTALL_ROOT'] = str(install_root)
        env['G3KU_AGENT_BROWSER_HOME'] = str(agent_browser_home)
        env['G3KU_AGENT_BROWSER_BROWSER_ROOT'] = str(browser_root)
        if browser_executable is not None:
            env['G3KU_AGENT_BROWSER_EXECUTABLE_PATH'] = str(browser_executable)
        env['G3KU_AGENT_BROWSER_TEMP_ROOT'] = str(temp_root)
        return env

    def _install_root(self) -> Path:
        return self._workspace_path(str(self._settings.install_root or 'externaltools/agent_browser'), label='install_root')

    def _browser_root(self) -> Path:
        return self._workspace_path(
            str(self._settings.browser_root or 'externaltools/agent_browser/browsers'),
            label='browser_root',
        )

    def _agent_browser_home(self) -> Path:
        return self._install_root() / 'home'

    def _browser_executable_path(self) -> Path | None:
        browser_root = self._browser_root()
        if not browser_root.exists():
            return None
        patterns = ['chrome.exe'] if os.name == 'nt' else ['chrome', 'Google Chrome for Testing', 'Chromium']
        for pattern in patterns:
            matches = sorted(browser_root.rglob(pattern))
            if matches:
                return matches[0].resolve(strict=False)
        return None

    def _temp_root(self) -> Path:
        return self._workspace_path(str(self._settings.temp_root or 'temp/agent_browser'), label='temp_root')

    def _workspace_path(self, raw: str, *, label: str) -> Path:
        candidate = Path(str(raw or '').strip()).expanduser()
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        resolved = candidate.resolve(strict=False)
        self._ensure_within_workspace(resolved, label=label)
        return resolved

    def _ensure_within_workspace(self, path: Path, *, label: str) -> None:
        try:
            path.relative_to(self._workspace)
        except ValueError as exc:
            raise ValueError(f'{label} must stay inside workspace') from exc

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
        cancel_token: Any | None,
    ) -> dict[str, Any]:
        process: asyncio.subprocess.Process | None = None
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
            return self._error_payload(error=str(exc), command=[*list(command_prefix or []), *list(args or [])], cwd=cwd)

        try:
            stdout, stderr = await self._communicate_process(
                process=process,
                stdin=stdin,
                timeout_seconds=timeout_seconds,
                cancel_token=cancel_token,
            )
        except ToolCancellationRequested as exc:
            return self._error_payload(
                error=str(exc),
                command=[*list(command_prefix or []), *list(args or [])],
                cwd=cwd,
                cancelled=True,
            )
        except asyncio.TimeoutError:
            return {
                'ok': False,
                'command': [*list(command_prefix or []), *list(args or [])],
                'cwd': cwd,
                'stdout': '',
                'stderr': f'agent-browser timed out after {timeout_seconds} seconds',
                'stdout_json': None,
                'exit_code': None,
                'timed_out': True,
                'cancelled': False,
                'retried_after_session_cleanup': False,
                'initial_attempt': None,
                'session_cleanup': None,
                'error': f'agent-browser timed out after {timeout_seconds} seconds',
            }
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise

        stdout_text = decode_subprocess_output(stdout)
        stderr_text = decode_subprocess_output(stderr)
        return {
            'ok': process.returncode == 0,
            'command': [*list(command_prefix or []), *list(args or [])],
            'cwd': cwd,
            'stdout': stdout_text,
            'stderr': stderr_text,
            'stdout_json': self._parse_json(stdout_text),
            'exit_code': process.returncode,
            'timed_out': False,
            'cancelled': False,
            'retried_after_session_cleanup': False,
            'initial_attempt': None,
            'session_cleanup': None,
        }

    async def _communicate_process(
        self,
        *,
        process: asyncio.subprocess.Process,
        stdin: str | None,
        timeout_seconds: int,
        cancel_token: Any | None,
    ) -> tuple[bytes, bytes]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        communicate_task = asyncio.create_task(
            process.communicate(None if stdin is None else str(stdin).encode('utf-8'))
        )
        try:
            while True:
                self._check_cancel(cancel_token)
                remaining = float(timeout_seconds) - (loop.time() - started)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    return await asyncio.wait_for(asyncio.shield(communicate_task), timeout=min(0.25, remaining))
                except asyncio.TimeoutError:
                    if communicate_task.done():
                        return communicate_task.result()
                    continue
        except ToolCancellationRequested:
            await self._terminate_process(process)
            communicate_task.cancel()
            with suppress(asyncio.CancelledError):
                await communicate_task
            raise
        except asyncio.TimeoutError:
            await self._terminate_process(process)
            communicate_task.cancel()
            with suppress(asyncio.CancelledError):
                await communicate_task
            raise
        except asyncio.CancelledError:
            await self._terminate_process(process)
            communicate_task.cancel()
            with suppress(asyncio.CancelledError):
                await communicate_task
            raise

    async def _terminate_process(self, process: asyncio.subprocess.Process | None) -> None:
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        except Exception:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            return
        except Exception:
            pass
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5.0)

    async def _close_session(
        self,
        *,
        command_prefix: list[str],
        cwd: str,
        env: dict[str, str],
        session: str,
        cancel_token: Any | None,
    ) -> dict[str, Any]:
        return await self._run_command(
            command_prefix=command_prefix,
            args=['--session', str(session or '').strip(), 'close'],
            cwd=cwd,
            env=env,
            stdin=None,
            timeout_seconds=max(1, int(self._settings.default_timeout_seconds or 300)),
            cancel_token=cancel_token,
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
            'version': self._pinned_version,
            'install_root': str(self._install_root()),
            'browser_root': str(self._browser_root()),
            'temp_root': str(self._temp_root()),
            'command': [],
            'cwd': str(self._workspace),
            'stdout': '',
            'stderr': '',
            'stdout_json': None,
            'exit_code': None,
            'timed_out': False,
            'cancelled': False,
            'retried_after_session_cleanup': False,
            'initial_attempt': None,
            'session_cleanup': None,
            'summary': install_help.get('summary', ''),
            'next_actions': [
                'load_tool_context(tool_id="agent_browser")',
                'Install the local CLI under externaltools/agent_browser and verify the local executable path.',
            ],
        }

    def _error_payload(
        self,
        *,
        error: str,
        command: list[str] | None = None,
        cwd: str | None = None,
        cancelled: bool = False,
    ) -> dict[str, Any]:
        return {
            'ok': False,
            'error': str(error or '').strip(),
            'cancelled': bool(cancelled),
            'command': list(command or []),
            'cwd': str(cwd or self._workspace),
            'stdout': '',
            'stderr': str(error or '').strip(),
            'stdout_json': None,
            'exit_code': None,
            'timed_out': False,
            'retried_after_session_cleanup': False,
            'initial_attempt': None,
            'session_cleanup': None,
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

    def _local_cli_verification_command(self) -> str:
        local_cmd = self._install_root() / 'node_modules' / '.bin' / ('agent-browser.cmd' if os.name == 'nt' else 'agent-browser')
        return f'"{local_cmd}" --help'

    @staticmethod
    def _check_cancel(cancel_token: Any | None) -> None:
        if cancel_token is None or not hasattr(cancel_token, 'raise_if_cancelled'):
            return
        cancel_token.raise_if_cancelled(default_message='用户已请求暂停，正在安全停止...')


def build(runtime):
    settings = runtime_tool_settings(runtime, AgentBrowserToolSettings, tool_name='agent_browser')
    descriptor = getattr(runtime, 'resource_descriptor', None)
    toolskill_path = getattr(descriptor, 'toolskills_main_path', None) if descriptor is not None else None
    return AgentBrowserTool(
        workspace=Path(runtime.workspace),
        settings=settings,
        toolskill_path=Path(toolskill_path) if toolskill_path is not None else None,
    )
