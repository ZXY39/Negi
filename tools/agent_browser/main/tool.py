from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import urllib.request
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.config.schema import Base
from g3ku.resources.tool_settings import runtime_tool_settings


class AgentBrowserSettings(Base):
    executable: str = ''
    repo_root: str = 'main/agent-browser'
    timeout: int = 300
    bootstrap_on_first_use: bool = True
    bootstrap_timeout: int = 240
    bootstrap_from_release: bool = True
    allow_cargo_run: bool = True
    allow_global_fallback: bool = False
    default_session: str = 'g3ku-agent-browser'
    auto_session: bool = True
    auto_profile: bool = True
    profile_root: str = '.g3ku/tool-data/agent_browser/profiles'
    cargo_release: bool = False
    stdout_json_preview: bool = True


class AgentBrowserTool(Tool):
    _DAEMON_RESTART_WARNING = 'ignored: daemon already running'
    _SESSION_CLEANUP_TIMEOUT_SECONDS = 15

    def __init__(self, workspace: Path, source_root: Path, settings: AgentBrowserSettings):
        self._workspace = Path(workspace)
        self._source_root = Path(source_root)
        self._settings = settings

    @property
    def name(self) -> str:
        return 'agent_browser'

    @property
    def description(self) -> str:
        return 'Run the bundled agent-browser CLI for browser automation tasks.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'args': {'type': 'array', 'items': {'type': 'string'}},
                'working_dir': {'type': 'string'},
                'stdin': {'type': 'string'},
                'timeout': {'type': 'integer', 'minimum': 1},
                'session': {'type': 'string'},
                'profile': {'type': 'string'},
                'session_name': {'type': 'string'},
            },
            'required': ['args'],
        }

    async def execute(
        self,
        args: list[str],
        working_dir: str | None = None,
        stdin: str | None = None,
        timeout: int | None = None,
        session: str | None = None,
        profile: str | None = None,
        session_name: str | None = None,
        **kwargs: Any,
    ) -> str:
        argv = [str(item) for item in (args or [])]
        if not argv:
            return self._error_payload('`args` must contain at least one CLI argument.')

        cwd = self._resolve_working_dir(working_dir)
        if cwd is None:
            return self._error_payload('`working_dir` resolves outside the workspace or does not exist.')

        command_prefix = await self._resolve_command_prefix()
        if command_prefix is None:
            return self._error_payload(
                'Unable to locate the repository-pinned `agent-browser`. Ensure `tools/agent_browser/main/agent-browser` exists, run `corepack pnpm install --frozen-lockfile` there to fetch the matching binary, or configure `settings.executable`.'
            )

        prefixed_args = self._inject_global_flags(
            argv=argv,
            session=session,
            profile=profile,
            session_name=session_name,
        )
        resolved_session = self._extract_flag_value(prefixed_args, '--session')
        command_name = str(argv[0] or '').strip().lower()
        can_cleanup_session = bool(resolved_session) and command_name not in {'close', 'quit', 'exit'}
        env = self._build_env()
        timeout_seconds = int(timeout or self._settings.timeout)

        try:
            payload = await self._run_command(
                command_prefix=command_prefix,
                args=prefixed_args,
                cwd=cwd,
                env=env,
                stdin=stdin,
                timeout_seconds=timeout_seconds,
            )
        except FileNotFoundError as exc:
            return self._error_payload(f'Failed to start agent-browser: {exc}')
        except Exception as exc:
            return self._error_payload(f'Failed to start agent-browser: {exc}')

        if payload.get('timed_out'):
            if can_cleanup_session:
                payload['session_cleanup'] = await self._close_session(
                    command_prefix=command_prefix,
                    cwd=cwd,
                    env=env,
                    session=resolved_session,
                )
            return json.dumps(payload, ensure_ascii=False)

        if can_cleanup_session and self._should_retry_after_session_cleanup(payload):
            initial_attempt = {
                'exit_code': payload.get('exit_code'),
                'stderr': payload.get('stderr', ''),
            }
            cleanup_payload = await self._close_session(
                command_prefix=command_prefix,
                cwd=cwd,
                env=env,
                session=resolved_session,
            )
            try:
                payload = await self._run_command(
                    command_prefix=command_prefix,
                    args=prefixed_args,
                    cwd=cwd,
                    env=env,
                    stdin=stdin,
                    timeout_seconds=timeout_seconds,
                )
            except FileNotFoundError as exc:
                return self._error_payload(f'Failed to start agent-browser: {exc}')
            except Exception as exc:
                return self._error_payload(f'Failed to start agent-browser: {exc}')
            payload['retried_after_session_cleanup'] = True
            payload['initial_attempt'] = initial_attempt
            payload['session_cleanup'] = cleanup_payload
        return json.dumps(payload, ensure_ascii=False)

    async def _run_command(
        self,
        *,
        command_prefix: list[str],
        args: list[str],
        cwd: Path,
        env: dict[str, str],
        stdin: str | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        command = [*command_prefix, *args]
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdin_bytes = stdin.encode('utf-8') if stdin is not None else None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin_bytes), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            await self._terminate_process(process)
            return {
                'ok': False,
                'timed_out': True,
                'error': f'agent-browser timed out after {timeout_seconds} seconds',
                'command': command,
                'cwd': str(cwd),
            }
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise

        stdout_text = stdout.decode('utf-8', errors='replace')
        stderr_text = stderr.decode('utf-8', errors='replace')
        payload: dict[str, Any] = {
            'ok': process.returncode == 0,
            'command': command,
            'cwd': str(cwd),
            'exit_code': process.returncode,
            'stdout': stdout_text,
            'stderr': stderr_text,
        }
        if self._settings.stdout_json_preview:
            stdout_json = self._try_parse_json(stdout_text)
            if stdout_json is not None:
                payload['stdout_json'] = stdout_json
        return payload

    async def _close_session(
        self,
        *,
        command_prefix: list[str],
        cwd: Path,
        env: dict[str, str],
        session: str,
    ) -> dict[str, Any]:
        try:
            return await self._run_command(
                command_prefix=command_prefix,
                args=['--session', session, 'close'],
                cwd=cwd,
                env=env,
                stdin=None,
                timeout_seconds=self._SESSION_CLEANUP_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return {
                'ok': False,
                'error': f'Failed to close agent-browser session {session!r}: {exc}',
                'command': [*command_prefix, '--session', session, 'close'],
                'cwd': str(cwd),
            }

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass

    async def _resolve_command_prefix(self) -> list[str] | None:
        explicit = str(self._settings.executable or '').strip()
        if explicit:
            explicit_path = Path(explicit)
            if explicit_path.exists():
                return [str(explicit_path)]
            resolved = shutil.which(explicit)
            if resolved:
                return [resolved]

        if not self._source_root.exists():
            return None

        bundled = self._bundled_binary_path()
        if bundled is not None and bundled.exists():
            return [str(bundled)]

        if self._settings.bootstrap_on_first_use:
            await self._bootstrap_repo_binary()
            bundled = self._bundled_binary_path()
            if bundled is not None and bundled.exists():
                return [str(bundled)]

        if self._settings.allow_cargo_run:
            cargo = shutil.which('cargo')
            manifest = self._source_root / 'cli' / 'Cargo.toml'
            if cargo and manifest.exists():
                prefix = [cargo, 'run']
                if self._settings.cargo_release:
                    prefix.append('--release')
                prefix.extend(['--manifest-path', str(manifest), '--'])
                return prefix

        if self._settings.allow_global_fallback:
            on_path = shutil.which('agent-browser')
            if on_path:
                return [on_path]
        return None

    async def _bootstrap_repo_binary(self) -> None:
        package_json = self._source_root / 'package.json'
        if not self._settings.bootstrap_from_release or not package_json.exists():
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self._download_repo_binary), timeout=int(self._settings.bootstrap_timeout))
        except asyncio.TimeoutError:
            return

    def _download_repo_binary(self) -> None:
        target = self._bundled_binary_path()
        if target is None or target.exists():
            return
        package_json = self._source_root / 'package.json'
        try:
            package_data = json.loads(package_json.read_text(encoding='utf-8'))
        except Exception:
            return
        version = str(package_data.get('version') or '').strip()
        if not version:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        download_url = f'https://github.com/vercel-labs/agent-browser/releases/download/v{version}/{target.name}'
        try:
            with urllib.request.urlopen(download_url, timeout=int(self._settings.bootstrap_timeout)) as response:
                target.write_bytes(response.read())
            if platform.system().lower() != 'windows':
                target.chmod(0o755)
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass

    def _bundled_binary_path(self) -> Path | None:
        system = platform.system().lower()
        machine = platform.machine().lower()
        if machine in {'amd64', 'x86_64', 'x64'}:
            arch = 'x64'
        elif machine in {'arm64', 'aarch64'}:
            arch = 'arm64'
        else:
            return None

        if system == 'windows':
            name = f'agent-browser-win32-{arch}.exe'
        elif system == 'darwin':
            name = f'agent-browser-darwin-{arch}'
        elif system == 'linux':
            name = f'agent-browser-linux-{arch}'
        else:
            return None
        return self._source_root / 'bin' / name

    def _resolve_working_dir(self, working_dir: str | None) -> Path | None:
        if not working_dir:
            return self._workspace
        candidate = Path(working_dir)
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        candidate = candidate.resolve()
        workspace = self._workspace.resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError:
            return None
        if not candidate.exists() or not candidate.is_dir():
            return None
        return candidate

    def _inject_global_flags(
        self,
        *,
        argv: list[str],
        session: str | None,
        profile: str | None,
        session_name: str | None,
    ) -> list[str]:
        result = list(argv)
        command_name = result[0] if result else ''
        auto_profile_applies = (
            not str(profile or '').strip()
            and self._settings.auto_profile
            and command_name not in {'install', '--help', 'help', 'version', '--version'}
        )

        resolved_session = str(session or '').strip()
        if not resolved_session and (self._settings.auto_session or auto_profile_applies):
            resolved_session = str(self._settings.default_session or '').strip()
        if resolved_session and '--session' not in result:
            result = ['--session', resolved_session, *result]

        resolved_profile = self._resolve_profile_path(profile)
        if auto_profile_applies:
            resolved_profile = str(self._default_profile_path(resolved_session))
        if resolved_profile and '--profile' not in result:
            result = ['--profile', resolved_profile, *result]

        resolved_session_name = str(session_name or '').strip()
        if resolved_session_name and '--session-name' not in result:
            result = ['--session-name', resolved_session_name, *result]
        return result

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        temp_dir = str(env.get('G3KU_TMP_DIR') or '').strip()
        if temp_dir:
            env['G3KU_TMP_DIR'] = temp_dir
            env['TMPDIR'] = temp_dir
            env['TMP'] = temp_dir
            env['TEMP'] = temp_dir
        return env

    def _default_profile_path(self, session: str | None) -> Path:
        root = Path(self._settings.profile_root)
        if not root.is_absolute():
            root = self._workspace / root
        session_value = str(session or self._settings.default_session or '').strip() or 'default'
        profile_path = root / self._slugify(session_value)
        profile_path.mkdir(parents=True, exist_ok=True)
        return profile_path

    def _resolve_profile_path(self, profile: str | None) -> str:
        value = str(profile or '').strip()
        if not value:
            return ''
        profile_path = Path(value).expanduser()
        if not profile_path.is_absolute():
            profile_path = self._workspace / profile_path
        profile_path.mkdir(parents=True, exist_ok=True)
        return str(profile_path.resolve())

    @staticmethod
    def _slugify(value: str) -> str:
        normalized = re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('-._')
        return normalized or 'default'

    @staticmethod
    def _extract_flag_value(argv: list[str], flag: str) -> str:
        for index, item in enumerate(argv):
            if item == flag and index + 1 < len(argv):
                return str(argv[index + 1]).strip()
        return ''

    def _should_retry_after_session_cleanup(self, payload: dict[str, Any]) -> bool:
        if payload.get('ok') or payload.get('timed_out'):
            return False
        stderr_text = str(payload.get('stderr') or '')
        return self._DAEMON_RESTART_WARNING in stderr_text

    @staticmethod
    def _try_parse_json(payload: str) -> Any | None:
        text = str(payload or '').strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    @staticmethod
    def _error_payload(message: str) -> str:
        return json.dumps({'ok': False, 'error': message}, ensure_ascii=False)


def build(runtime):
    settings = runtime_tool_settings(runtime, AgentBrowserSettings, tool_name='agent_browser')
    source_root = Path(runtime.resource_root) / str(settings.repo_root or 'main/agent-browser')
    return AgentBrowserTool(workspace=Path(runtime.workspace), source_root=source_root, settings=settings)
