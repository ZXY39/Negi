"""Shell execution tool."""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        workspace_root: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        path_append: str = "",
        content_store: Any = None,
        main_task_service: Any = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.workspace_root = workspace_root
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",          # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",              # del /f, del /q
            r"\brmdir\s+/s\b",               # rmdir /s
            r"(?:^|[;&|]\s*)format\b",       # format (as standalone command only)
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",          # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.path_append = path_append
        self.content_store = content_store
        self.main_task_service = main_task_service

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }
    
    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        runtime = kwargs.pop("__g3ku_runtime", None) or {}
        cwd = self._resolve_cwd(working_dir)
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return self._build_payload(
                status="error",
                exit_code=None,
                command=command,
                stdout_text="",
                stderr_text=guard_error,
                runtime=runtime,
                error=guard_error,
            )

        resource_state = self._capture_resource_tree_state()
        env = os.environ.copy()
        temp_dir = str(runtime.get("temp_dir") or env.get("G3KU_TMP_DIR") or "").strip()
        if temp_dir:
            env["G3KU_TMP_DIR"] = temp_dir
            env["TMPDIR"] = temp_dir
            env["TMP"] = temp_dir
            env["TEMP"] = temp_dir
        if self.path_append:
            env["PATH"] = env.get("PATH", "") + os.pathsep + self.path_append

        try:
            if os.name == "nt":
                process = await asyncio.create_subprocess_exec(
                    *self._windows_shell_argv(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout
                )
            except asyncio.CancelledError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                raise
            except asyncio.TimeoutError:
                process.kill()
                # Wait for the process to fully terminate so pipes are
                # drained and file descriptors are released.
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    command=command,
                    stdout_text="",
                    stderr_text=f"Command timed out after {self.timeout} seconds",
                    runtime=runtime,
                    error=f"Command timed out after {self.timeout} seconds",
                )

            stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            return self._build_payload(
                status="success" if process.returncode == 0 else "error",
                exit_code=process.returncode,
                command=command,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                runtime=runtime,
                error="" if process.returncode == 0 else f"Exit code: {process.returncode}",
            )

        except Exception as e:
            return self._build_payload(
                status="error",
                exit_code=None,
                command=command,
                stdout_text="",
                stderr_text=str(e),
                runtime=runtime,
                error=f"Error executing command: {str(e)}",
            )
        finally:
            self._notify_resource_change(resource_state, runtime=runtime, trigger="tool:exec")

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            workspace_root = self._workspace_root()
            cwd_path = Path(cwd).expanduser().resolve()
            if not self._is_within_workspace(cwd_path, workspace_root):
                return "Error: Command blocked by safety guard (working_dir outside workspace)"

            for raw in self._extract_absolute_paths(cmd):
                try:
                    p = Path(raw.strip()).expanduser().resolve()
                except Exception:
                    continue
                if p.is_absolute() and not self._is_within_workspace(p, workspace_root):
                    return "Error: Command blocked by safety guard (path outside workspace)"

        return None

    def _resolve_cwd(self, working_dir: str | None) -> str:
        if not working_dir:
            return self.working_dir or os.getcwd()
        return str(Path(working_dir).expanduser())

    @staticmethod
    def _windows_shell_argv(command: str) -> list[str]:
        return [
            ExecTool._windows_powershell_executable(),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]

    @staticmethod
    def _windows_powershell_executable() -> str:
        system_root = str(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or "").strip()
        if system_root:
            candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            if candidate.exists():
                return str(candidate)
        return "powershell.exe"

    def _workspace_root(self) -> Path:
        return Path(self.workspace_root or self.working_dir or os.getcwd()).expanduser().resolve()

    @staticmethod
    def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
        try:
            path.relative_to(workspace_root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_absolute_paths(command: str) -> list[str]:
        win_paths = re.findall(r"(?<![A-Za-z0-9_])[A-Za-z]:(?:\\|/)[^\s\"'|><;]+", command)   # Windows: C:\... or C:/...
        posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", command) # POSIX: /absolute only
        return win_paths + posix_paths

    def _build_payload(
        self,
        *,
        status: str,
        exit_code: int | None,
        command: str,
        stdout_text: str,
        stderr_text: str,
        runtime: dict[str, Any],
        error: str = "",
    ) -> str:
        combined = stdout_text.strip()
        if stderr_text.strip():
            combined = f"{combined}\nSTDERR:\n{stderr_text.strip()}".strip()
        payload = {
            "status": status,
            "exit_code": exit_code,
            "command": command,
            "stdout_ref": self._persist_ref(stdout_text, runtime=runtime, display_name="exec stdout", source_kind="exec_stdout"),
            "stderr_ref": self._persist_ref(stderr_text, runtime=runtime, display_name="exec stderr", source_kind="exec_stderr"),
            "head_preview": self._preview(combined, from_tail=False),
        }
        if error:
            payload["error"] = error
        return json.dumps(payload, ensure_ascii=False)

    def _persist_ref(
        self,
        text: str,
        *,
        runtime: dict[str, Any],
        display_name: str,
        source_kind: str,
    ) -> str:
        if self.content_store is None or not str(text or "").strip():
            return ""
        envelope = self.content_store.maybe_externalize_text(
            text,
            runtime=runtime,
            display_name=display_name,
            source_kind=source_kind,
            force=True,
        )
        return str(envelope.ref or "") if envelope is not None else ""

    def _capture_resource_tree_state(self) -> dict[str, dict[str, str]]:
        service = self.main_task_service
        if service is None or not hasattr(service, "capture_resource_tree_state"):
            return {}
        try:
            return service.capture_resource_tree_state()
        except Exception:
            return {}

    def _notify_resource_change(
        self,
        before_state: dict[str, dict[str, str]] | None,
        *,
        runtime: dict[str, Any],
        trigger: str,
    ) -> None:
        service = self.main_task_service
        if service is None or not hasattr(service, "refresh_changed_resources"):
            return
        session_id = str(runtime.get("session_key") or "web:shared").strip() or "web:shared"
        try:
            service.refresh_changed_resources(before_state, trigger=trigger, session_id=session_id)
        except Exception:
            return

    @staticmethod
    def _preview(text: str, *, from_tail: bool) -> str:
        if not text:
            return ""
        lines = text.splitlines()
        selected = lines[-6:] if from_tail else lines[:6]
        preview = "\n".join(selected).strip()
        if len(preview) <= 240:
            return preview
        return preview[:240].rstrip() + "..."


