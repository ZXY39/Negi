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
    auto_session: bool = False
    auto_profile: bool = True
    profile_root: str = '.g3ku/tool-data/agent_browser/profiles'
    cargo_release: bool = False
    stdout_json_preview: bool = True


class AgentBrowserTool(Tool):
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
        env = self._build_env()
        command = [*command_prefix, *prefixed_args]
        timeout_seconds = int(timeout or self._settings.timeout)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return self._error_payload(f'Failed to start agent-browser: {exc}')
        except Exception as exc:
            return self._error_payload(f'Failed to start agent-browser: {exc}')

        stdin_bytes = stdin.encode('utf-8') if stdin is not None else None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(stdin_bytes), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return json.dumps(
                {
                    'ok': False,
                    'error': f'agent-browser timed out after {timeout_seconds} seconds',
                    'command': command,
                    'cwd': str(cwd),
                },
                ensure_ascii=False,
            )
        except asyncio.CancelledError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
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
        return json.dumps(payload, ensure_ascii=False)

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

        resolved_session = str(session or '').strip()
        if not resolved_session and self._settings.auto_session:
            resolved_session = str(self._settings.default_session or '').strip()
        if resolved_session and '--session' not in result:
            result = ['--session', resolved_session, *result]

        resolved_profile = str(profile or '').strip()
        if not resolved_profile and self._settings.auto_profile and command_name not in {'install', '--help', 'help', 'version', '--version'}:
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

    @staticmethod
    def _slugify(value: str) -> str:
        normalized = re.sub(r'[^A-Za-z0-9._-]+', '-', value).strip('-._')
        return normalized or 'default'

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
