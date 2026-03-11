from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any


class AgentBrowserClient:
    """Thin async wrapper around the external `agent-browser` CLI."""

    def __init__(self, defaults: dict[str, Any] | None = None):
        cfg = defaults or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.command = str(cfg.get("command") or "agent-browser").strip() or "agent-browser"
        self.npm_command = str(cfg.get("npm_command") or "npm").strip() or "npm"
        self.node_command = str(cfg.get("node_command") or "node").strip() or "node"
        self.required_min_version = str(cfg.get("required_min_version") or "0.16.3").strip() or "0.16.3"
        self.install_spec = str(cfg.get("install_spec") or "agent-browser@latest").strip() or "agent-browser@latest"
        self.auto_install = bool(cfg.get("auto_install", True))
        self.auto_upgrade_if_below_min_version = bool(cfg.get("auto_upgrade_if_below_min_version", True))
        self.auto_install_browser = bool(cfg.get("auto_install_browser", True))
        self.browser_install_args = [str(v) for v in (cfg.get("browser_install_args") or ["install"]) if str(v).strip()]
        if not self.browser_install_args:
            self.browser_install_args = ["install"]
        self.default_headless = bool(cfg.get("default_headless", False))
        self.command_timeout_s = self._clamp_int(cfg.get("command_timeout_s", 120), 10, 3600, 120)
        self.install_timeout_s = self._clamp_int(cfg.get("install_timeout_s", 900), 30, 7200, 900)
        self.session_env_key = str(cfg.get("session_env_key") or "AGENT_BROWSER_SESSION").strip() or "AGENT_BROWSER_SESSION"
        self.max_stdout_chars = self._clamp_int(cfg.get("max_stdout_chars", 120000), 2000, 2_000_000, 120000)
        self.max_stderr_chars = self._clamp_int(cfg.get("max_stderr_chars", 120000), 2000, 2_000_000, 120000)
        self.extra_env = {str(k): str(v) for k, v in dict(cfg.get("extra_env") or {}).items()}
        self.allow_file_access = bool(cfg.get("allow_file_access", False))
        self.default_color_scheme = str(cfg.get("default_color_scheme") or "").strip() or None
        self.default_download_path = str(cfg.get("default_download_path") or "").strip()

        self._install_lock = asyncio.Lock()
        self._resolved_command: str | None = None
        self._resolved_npm: str | None = None
        self._resolved_node: str | None = None
        self._cached_version: str | None = None
        self._browser_runtime_checked = False

    @staticmethod
    def _clamp_int(value: Any, lo: int, hi: int, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = fallback
        return max(lo, min(parsed, hi))

    def _preview(self, value: Any, *, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + f"...(truncated {len(text) - limit} chars)"

    async def _emit_progress(self, callback, text: str, *, kind: str, data: dict[str, Any] | None = None) -> None:
        if not callback:
            return
        try:
            result = callback(text, event_kind=kind, event_data=data)
        except TypeError:
            result = callback(text)
        if asyncio.iscoroutine(result):
            await result

    def _ok(self, *, stage: str, **kwargs: Any) -> dict[str, Any]:
        payload = {"success": True, "stage": stage, **kwargs}
        payload.setdefault("error", None)
        payload.setdefault("hint", None)
        payload.setdefault("retryable", False)
        return payload

    def _fail(self, *, stage: str, error: str, hint: str, retryable: bool = False, **kwargs: Any) -> dict[str, Any]:
        payload = {"success": False, "stage": stage, "error": error, "hint": hint, "retryable": retryable, **kwargs}
        payload.setdefault("data", None)
        return payload

    def _resolve_program(self, program: str) -> str | None:
        raw = str(program or "").strip()
        if not raw:
            return None
        p = Path(raw)
        if p.is_absolute() or p.parent != Path('.'):
            return str(p) if p.exists() else None
        found = shutil.which(raw)
        return found or None

    async def _run_exec(self, argv: list[str], *, timeout_s: int, env: dict[str, str] | None = None) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=float(timeout_s))
        except asyncio.CancelledError:
            process.kill()
            try:
                await process.wait()
            except Exception:
                pass
            raise
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.wait()
            except Exception:
                pass
            return {
                "success": False,
                "exit_code": None,
                "stdout": "",
                "stderr": f"command timed out after {timeout_s}s",
            }
        return {
            "success": process.returncode == 0,
            "exit_code": process.returncode,
            "stdout": (stdout or b"").decode("utf-8", errors="replace"),
            "stderr": (stderr or b"").decode("utf-8", errors="replace"),
        }

    @staticmethod
    def _parse_version(text: str) -> str | None:
        match = re.search(r"(\d+\.\d+\.\d+)", text or "")
        return match.group(1) if match else None

    @staticmethod
    def _version_tuple(value: str) -> tuple[int, int, int]:
        parts = [int(p) for p in str(value).split('.')[:3] if p.isdigit() or p.isdecimal()]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])

    async def find_executable(self) -> str | None:
        if self._resolved_command:
            return self._resolved_command
        self._resolved_command = self._resolve_program(self.command)
        return self._resolved_command

    async def get_version(self, executable: str) -> str | None:
        result = await self._run_exec([executable, "--version"], timeout_s=30, env=os.environ.copy())
        if not result["success"]:
            return None
        version = self._parse_version(result["stdout"] or result["stderr"])
        if version:
            self._cached_version = version
        return version

    async def ensure_installed(self, *, on_progress=None) -> dict[str, Any]:
        async with self._install_lock:
            await self._emit_progress(on_progress, "Checking agent-browser availability...", kind="browser_runtime_bootstrap")
            executable = await self.find_executable()
            if executable:
                version = await self.get_version(executable)
                if version and self._version_tuple(version) < self._version_tuple(self.required_min_version):
                    if not self.auto_upgrade_if_below_min_version:
                        return self._fail(
                            stage="check_version",
                            error=f"agent-browser version {version} is below required minimum {self.required_min_version}",
                            hint=f"Upgrade with `{self.npm_command} install -g {self.install_spec}`.",
                            retryable=False,
                            version=version,
                        )
                    npm_executable = self._resolved_npm or self._resolve_program(self.npm_command)
                    if not npm_executable:
                        return self._fail(
                            stage="upgrade_package",
                            error="npm is not available",
                            hint="Install Node.js with npm, then retry.",
                            retryable=False,
                        )
                    await self._emit_progress(on_progress, f"Upgrading agent-browser to {self.install_spec}...", kind="browser_runtime_bootstrap")
                    upgraded = await self._run_exec([npm_executable, "install", "-g", self.install_spec], timeout_s=self.install_timeout_s, env=os.environ.copy())
                    if not upgraded["success"]:
                        return self._fail(
                            stage="upgrade_package",
                            error=self._preview(upgraded["stderr"] or upgraded["stdout"] or f"exit code {upgraded['exit_code']}", limit=self.max_stderr_chars),
                            hint=f"Manually run `{self.npm_command} install -g {self.install_spec}`.",
                            retryable=True,
                            stdout_raw=self._preview(upgraded["stdout"], limit=self.max_stdout_chars),
                            stderr=self._preview(upgraded["stderr"], limit=self.max_stderr_chars),
                            exit_code=upgraded["exit_code"],
                        )
                    self._resolved_command = None
                    executable = await self.find_executable()
                    version = await self.get_version(executable or self.command)
                return self._ok(stage="check_version", executable=executable, version=version)

            if not self.auto_install:
                return self._fail(
                    stage="find_command",
                    error="agent-browser command not found",
                    hint=f"Install it with `{self.npm_command} install -g {self.install_spec}`.",
                    retryable=False,
                )

            npm_executable = self._resolved_npm or self._resolve_program(self.npm_command)
            node_executable = self._resolved_node or self._resolve_program(self.node_command)
            self._resolved_npm = npm_executable
            self._resolved_node = node_executable
            if not npm_executable or not node_executable:
                return self._fail(
                    stage="find_command",
                    error="agent-browser is missing and Node.js/npm is unavailable for auto-install",
                    hint="Install Node.js 20+ with npm, then retry.",
                    retryable=False,
                )

            await self._emit_progress(on_progress, f"Installing {self.install_spec}...", kind="browser_runtime_bootstrap")
            installed = await self._run_exec([npm_executable, "install", "-g", self.install_spec], timeout_s=self.install_timeout_s, env=os.environ.copy())
            if not installed["success"]:
                return self._fail(
                    stage="install_package",
                    error=self._preview(installed["stderr"] or installed["stdout"] or f"exit code {installed['exit_code']}", limit=self.max_stderr_chars),
                    hint=f"Manually run `{self.npm_command} install -g {self.install_spec}`.",
                    retryable=True,
                    stdout_raw=self._preview(installed["stdout"], limit=self.max_stdout_chars),
                    stderr=self._preview(installed["stderr"], limit=self.max_stderr_chars),
                    exit_code=installed["exit_code"],
                )

            self._resolved_command = None
            executable = await self.find_executable()
            if not executable:
                return self._fail(
                    stage="find_command",
                    error="agent-browser install completed but executable is still not on PATH",
                    hint="Restart the shell or ensure the global npm bin directory is on PATH.",
                    retryable=False,
                )
            version = await self.get_version(executable)
            return self._ok(stage="install_package", executable=executable, version=version)

    async def ensure_browser_runtime(self, executable: str, *, on_progress=None) -> dict[str, Any]:
        async with self._install_lock:
            if self._browser_runtime_checked or not self.auto_install_browser:
                return self._ok(stage="install_browser_runtime", executable=executable, version=self._cached_version)
            install_cmd = f"{self.command} {' '.join(self.browser_install_args)}"
            await self._emit_progress(on_progress, f"Ensuring browser runtime via `{install_cmd}`...", kind="browser_runtime_bootstrap")
            result = await self._run_exec([executable, *self.browser_install_args], timeout_s=self.install_timeout_s, env=os.environ.copy())
            if not result["success"]:
                return self._fail(
                    stage="install_browser_runtime",
                    error=self._preview(result["stderr"] or result["stdout"] or f"exit code {result['exit_code']}", limit=self.max_stderr_chars),
                    hint=f"Run `{self.command} {' '.join(self.browser_install_args)}` manually to install the browser runtime.",
                    retryable=True,
                    stdout_raw=self._preview(result["stdout"], limit=self.max_stdout_chars),
                    stderr=self._preview(result["stderr"], limit=self.max_stderr_chars),
                    exit_code=result["exit_code"],
                )
            self._browser_runtime_checked = True
            return self._ok(
                stage="install_browser_runtime",
                executable=executable,
                version=self._cached_version,
                stdout_raw=self._preview(result["stdout"], limit=self.max_stdout_chars),
                stderr=self._preview(result["stderr"], limit=self.max_stderr_chars),
                exit_code=result["exit_code"],
            )

    async def ensure_ready(self, *, on_progress=None) -> dict[str, Any]:
        install_status = await self.ensure_installed(on_progress=on_progress)
        if not install_status.get("success"):
            return install_status
        executable = str(install_status.get("executable") or await self.find_executable() or "")
        runtime_status = await self.ensure_browser_runtime(executable, on_progress=on_progress)
        if not runtime_status.get("success"):
            runtime_status.setdefault("version", install_status.get("version"))
            return runtime_status
        return self._ok(
            stage="ready",
            executable=executable,
            version=install_status.get("version") or self._cached_version,
            bootstrap={
                "install": install_status,
                "runtime": runtime_status,
            },
        )

    async def run(
        self,
        command: str,
        args: list[str],
        *,
        session: str,
        headless: bool | None,
        timeout_s: int | None,
        extra_env: dict[str, str] | None = None,
        on_progress=None,
    ) -> dict[str, Any]:
        ready = await self.ensure_ready(on_progress=on_progress)
        if not ready.get("success"):
            return ready

        executable = str(ready.get("executable") or "")
        effective_headless = self.default_headless if headless is None else bool(headless)
        command_timeout_s = self._clamp_int(timeout_s or self.command_timeout_s, 10, 3600, self.command_timeout_s)

        command_parts = [part for part in str(command or "").strip().split() if part]
        if not command_parts:
            return self._fail(
                stage="run_command",
                error="command is required",
                hint="Provide an agent-browser command such as `open`, `snapshot`, `click`, or `get`.",
                retryable=False,
            )

        argv: list[str] = [executable, "--json"]
        if not effective_headless:
            argv.append("--headed")
        if self.allow_file_access:
            argv.extend(["--allow-file-access", "true"])
        if self.default_color_scheme:
            argv.extend(["--color-scheme", self.default_color_scheme])
        effective_download_path = self.default_download_path or str((extra_env or {}).get("G3KU_TMP_DIR") or "").strip()
        if effective_download_path:
            argv.extend(["--download-path", effective_download_path])
        argv.extend(command_parts)
        argv.extend(str(v) for v in (args or []))

        env = os.environ.copy()
        env[self.session_env_key] = str(session or "default")
        env["AGENT_BROWSER_JSON"] = "1"
        env["AGENT_BROWSER_HEADED"] = "0" if effective_headless else "1"
        env.update(self.extra_env)
        if extra_env:
            env.update({str(k): str(v) for k, v in extra_env.items()})

        await self._emit_progress(on_progress, f"Running agent-browser {command}...", kind="browser_command_status", data={"command": command, "args": list(args or []), "headless": effective_headless})
        result = await self._run_exec(argv, timeout_s=command_timeout_s, env=env)
        stdout = self._preview(result["stdout"], limit=self.max_stdout_chars)
        stderr = self._preview(result["stderr"], limit=self.max_stderr_chars)
        if not result["success"]:
            return self._fail(
                stage="run_command",
                error=self._preview(result["stderr"] or result["stdout"] or f"exit code {result['exit_code']}", limit=self.max_stderr_chars),
                hint="Inspect stderr and stdout_raw, then retry with a corrected command or arguments.",
                retryable=True,
                command=command,
                args=list(args or []),
                session=session,
                headless=effective_headless,
                version=ready.get("version"),
                stdout_raw=stdout,
                stderr=stderr,
                exit_code=result["exit_code"],
                bootstrap=ready.get("bootstrap"),
            )

        raw_stdout = str(result["stdout"] or "").strip()
        parsed = None
        try:
            parsed = json.loads(raw_stdout) if raw_stdout else None
        except Exception:
            parsed = None
        if not isinstance(parsed, dict):
            return self._fail(
                stage="parse_json",
                error="agent-browser command succeeded but stdout was not valid JSON",
                hint="Re-run the command with simpler arguments or inspect stdout_raw for unexpected wrapper output.",
                retryable=True,
                command=command,
                args=list(args or []),
                session=session,
                headless=effective_headless,
                version=ready.get("version"),
                stdout_raw=stdout,
                stderr=stderr,
                exit_code=result["exit_code"],
                bootstrap=ready.get("bootstrap"),
            )

        return {
            "success": bool(parsed.get("success", True)),
            "stage": "run_command",
            "command": command,
            "args": list(args or []),
            "session": session,
            "headless": effective_headless,
            "version": ready.get("version"),
            "bootstrap": ready.get("bootstrap"),
            "data": parsed.get("data"),
            "error": parsed.get("error"),
            "hint": None,
            "retryable": not bool(parsed.get("success", True)),
            "stdout_raw": stdout,
            "stderr": stderr,
            "exit_code": result["exit_code"],
        }

