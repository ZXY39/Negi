"""Shell execution tool."""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime.project_environment import apply_project_environment, resolve_project_environment
from main.governance.exec_tool_policy import (
    EXEC_TOOL_FAMILY_ID,
    EXECUTION_MODE_FULL_ACCESS,
    normalize_exec_execution_mode,
    resolve_exec_execution_mode,
)


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        workspace_root: str | None = None,
        temp_root: str | None = None,
        externaltools_root: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        enable_safety_guard: bool = False,
        path_append: str = "",
        execution_mode_default: str = "governed",
        content_store: Any = None,
        main_task_service: Any = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.workspace_root = workspace_root
        self.temp_root = temp_root
        self.externaltools_root = externaltools_root
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
        self.enable_safety_guard = enable_safety_guard
        self.path_append = path_append
        self.execution_mode_default = normalize_exec_execution_mode(execution_mode_default)
        self.content_store = content_store
        self.main_task_service = main_task_service

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output."

    @property
    def model_description(self) -> str:
        execution_mode = self._resolve_execution_mode()
        if execution_mode == EXECUTION_MODE_FULL_ACCESS:
            return "Execute shell commands without exec-side guardrails and return structured output."
        return "Execute shell commands with exec-side guardrails and return structured output."

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
        cwd = self._resolve_cwd(working_dir, runtime=runtime)
        execution_mode = self._resolve_execution_mode()
        if execution_mode != EXECUTION_MODE_FULL_ACCESS:
            readonly_error = self._enforce_read_only_command(command)
            if readonly_error:
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    stdout_text="",
                    stderr_text=readonly_error,
                    error=readonly_error,
                )
            policy_error = self._enforce_command_path_policy(command, cwd, runtime=runtime)
            if policy_error:
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    stdout_text="",
                    stderr_text=policy_error,
                    error=policy_error,
                )
            guard_error = self._guard_command(command, cwd)
            if guard_error:
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    stdout_text="",
                    stderr_text=guard_error,
                    error=guard_error,
                )

        resource_state = self._capture_resource_tree_state()
        env = self._build_subprocess_env(runtime=runtime, cwd=cwd)

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
                    stdout_text="",
                    stderr_text=f"Command timed out after {self.timeout} seconds",
                    error=f"Command timed out after {self.timeout} seconds",
                )

            stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
            stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            return self._build_payload(
                status="success" if process.returncode == 0 else "error",
                exit_code=process.returncode,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                error="" if process.returncode == 0 else f"Exit code: {process.returncode}",
            )

        except Exception as e:
            return self._build_payload(
                status="error",
                exit_code=None,
                stdout_text="",
                stderr_text=str(e),
                error=f"Error executing command: {str(e)}",
            )
        finally:
            self._notify_resource_change(resource_state, runtime=runtime, trigger="tool:exec")

    def _resolve_execution_mode(self) -> str:
        service = getattr(self, "main_task_service", None)
        family = None
        if service is not None:
            get_tool_family = getattr(service, "get_tool_family", None)
            if callable(get_tool_family):
                try:
                    family = get_tool_family(EXEC_TOOL_FAMILY_ID)
                except Exception:
                    family = None
        return resolve_exec_execution_mode(
            family=family,
            settings_payload={'execution_mode': self.execution_mode_default},
        )

    def _enforce_read_only_command(self, command: str) -> str | None:
        normalized = str(command or "").strip()
        lower = normalized.lower()
        if not lower:
            return None

        mutating_patterns = [
            r"(^|[;&|]\s*)set-content\b",
            r"(^|[;&|]\s*)add-content\b",
            r"(^|[;&|]\s*)clear-content\b",
            r"(^|[;&|]\s*)new-item\b",
            r"(^|[;&|]\s*)remove-item\b",
            r"(^|[;&|]\s*)move-item\b",
            r"(^|[;&|]\s*)copy-item\b",
            r"(^|[;&|]\s*)rename-item\b",
            r"(^|[;&|]\s*)touch\b",
            r"(^|[;&|]\s*)rm\b",
            r"(^|[;&|]\s*)mv\b",
            r"(^|[;&|]\s*)cp\b",
            r"(^|[;&|]\s*)mkdir\b",
            r"(^|[;&|]\s*)install\b",
            r"(^|[;&|]\s*)tee\b",
            r"\bsed\s+-i\b",
            r"<<\s*['\"]?\w+['\"]?\s*>",
            r"(^|[^<])>>?",
            r"\bwritefilesync\b",
            r"\bappendfilesync\b",
            r"\bwritefile\b",
            r"\bappendfile\b",
            r"\bwrite_text\b",
            r"\bwrite_bytes\b",
            r"\bmkdir\(",
            r"\bmakedirs\(",
            r"\bunlink\(",
            r"\bdelete\b",
            r"\bremove\(",
            r"open\([^)]*,\s*['\"][wa+]",
        ]
        for pattern in mutating_patterns:
            if re.search(pattern, lower):
                return self._readonly_error(command)
        return None

    @staticmethod
    def _readonly_error(command: str) -> str:
        return (
            "Error: exec is a read-only tool. The command was blocked.\n\n"
            "Detected syntax that may lead to non-read-only behavior.\n\n"
            "Recommended handling\n"
            "- If this is a read-only operation, rewrite it as a single pipeline expression, "
            "avoid intermediate variables or use a different tool.\n"
            "- If you need to create, modify, or delete files, use `filesystem_write`, "
            "`filesystem_edit`, `filesystem_delete`, or `filesystem_propose_patch`."
        )

    def _enforce_command_path_policy(self, command: str, cwd: str, *, runtime: dict[str, Any] | None = None) -> str | None:
        workspace_root = self._workspace_root()
        temp_root = self._canonical_temp_root(runtime)
        externaltools_root = self._externaltools_root()
        tools_root = self._tools_root()
        cwd_path = Path(cwd).expanduser().resolve()

        if self._is_within_any_root(cwd_path, self._legacy_temp_roots()):
            return (
                f"Error: working_dir {cwd_path} is blocked. Use {temp_root} for temporary content instead of legacy tmp directories."
            )
        normalized = self._normalize_command(command)
        if self._mentions_legacy_temp_token(normalized) or self._command_references_any_root(command, self._legacy_temp_roots()):
            return f"Error: tmp paths are blocked. Use {temp_root} for downloads, caches, logs, and other temporary content."
        if self._mentions_system_temp_token(normalized):
            return f"Error: system temp paths are blocked. Use {temp_root} for downloads, caches, logs, and other temporary content."

        if self._matches_any(
            command,
            [
                r"\bwinget\s+install\b",
                r"\bchoco\s+install\b",
                r"\bscoop\s+install\b",
                r"\bapt(?:-get)?\s+install\b",
                r"\byum\s+install\b",
                r"\bdnf\s+install\b",
                r"\bpacman\s+-S\b",
                r"\bbrew\s+install\b",
                r"\bpipx\s+install\b",
                r"\buv\s+tool\s+install\b",
                r"\bcargo\s+install\b",
                r"\bgo\s+install\b",
                r"\bnpm\s+(?:install|i)\s+-g\b",
                r"\bpnpm\s+add\s+-g\b",
                r"\byarn\s+global\s+add\b",
            ],
        ):
            return (
                f"Error: global tool installs are blocked. Install third-party tools under {externaltools_root} and keep tools/ for registration only."
            )

        managed_transfer = self._matches_any(
            command,
            [
                r"\bcurl(?:\.exe)?\b",
                r"\bwget\b",
                r"\binvoke-webrequest\b",
                r"\bstart-bitstransfer\b",
                r"\bgit\s+clone\b",
                r"\bexpand-archive\b",
                r"\bunzip\b",
                r"\b7z(?:\.exe)?\s+[ex]\b",
                r"\btar\b.*(?:\s-x|\s-xf|\s--extract\b)",
                r"\b(?:python|py)\s+-m\s+venv\b",
                r"\bvirtualenv\b",
                r"\buv\s+venv\b",
            ],
        )
        if not managed_transfer:
            return None

        if self._mentions_relative_tools_token(normalized) or self._command_references_any_root(command, [tools_root]):
            return (
                f"Error: tools/ is registration-only. Download, extract, and install real third-party tool payloads under {externaltools_root} instead."
            )

        if self._has_managed_target(command, temp_root=temp_root):
            return None

        if self._is_within_workspace(cwd_path, temp_root) or self._is_within_workspace(cwd_path, externaltools_root):
            return None

        if self._is_within_workspace(cwd_path, workspace_root):
            return (
                f"Error: download, extract, clone, and local tool setup commands must use {temp_root} or {externaltools_root}, "
                f"either via working_dir or an explicit target path."
            )

        return None

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

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

        if not self.enable_safety_guard:
            return None

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        return None

    def _build_subprocess_env(self, *, runtime: dict[str, Any], cwd: str) -> dict[str, str]:
        env = os.environ.copy()
        temp_dir = str(self._canonical_temp_root(runtime))
        externaltools_dir = str(self._externaltools_root())
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        Path(externaltools_dir).mkdir(parents=True, exist_ok=True)
        runtime_temp_dir = str(
            self._runtime_task_temp_root(runtime)
            or runtime.get("temp_dir")
            or env.get("G3KU_TMP_DIR")
            or ""
        ).strip()
        if runtime_temp_dir:
            env["G3KU_RUNTIME_TEMP_DIR"] = runtime_temp_dir
        env["G3KU_TMP_DIR"] = temp_dir
        env["G3KU_TEMP_DIR"] = temp_dir
        env["G3KU_EXTERNAL_TOOLS_DIR"] = externaltools_dir
        env["TMPDIR"] = temp_dir
        env["TMP"] = temp_dir
        env["TEMP"] = temp_dir
        return apply_project_environment(
            env,
            runtime=resolve_project_environment(
                runtime=runtime,
                shell_family='powershell' if os.name == 'nt' else None,
                workspace_root=self.workspace_root,
                process_cwd=cwd,
            ),
            shell_family='powershell' if os.name == 'nt' else None,
            workspace_root=self.workspace_root,
            process_cwd=cwd,
            path_append=self.path_append,
        )

    def _resolve_cwd(self, working_dir: str | None, *, runtime: dict[str, Any] | None = None) -> str:
        if not working_dir:
            task_temp_root = self._runtime_task_temp_root(runtime)
            if task_temp_root is not None:
                task_temp_root.mkdir(parents=True, exist_ok=True)
                return str(task_temp_root)
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

    def _temp_root(self) -> Path:
        configured = str(self.temp_root or "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return self._workspace_root() / "temp"

    def _runtime_task_temp_root(self, runtime: dict[str, Any] | None = None) -> Path | None:
        payload = runtime if isinstance(runtime, dict) else {}
        raw = str(payload.get("task_temp_dir") or "").strip()
        if not raw:
            return None
        try:
            resolved = Path(raw).expanduser().resolve()
        except Exception:
            return None
        if not self._is_within_workspace(resolved, self._workspace_root()):
            return None
        return resolved

    def _canonical_temp_root(self, runtime: dict[str, Any] | None = None) -> Path:
        return self._runtime_task_temp_root(runtime) or self._temp_root()

    def _externaltools_root(self) -> Path:
        configured = str(self.externaltools_root or "").strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return self._workspace_root() / "externaltools"

    def _tools_root(self) -> Path:
        return self._workspace_root() / "tools"

    def _legacy_temp_roots(self) -> list[Path]:
        workspace_root = self._workspace_root()
        return [
            workspace_root / "tmp",
            workspace_root / ".g3ku" / "tmp",
        ]

    @staticmethod
    def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
        try:
            path.relative_to(workspace_root)
            return True
        except ValueError:
            return False

    @classmethod
    def _is_within_any_root(cls, path: Path, roots: list[Path]) -> bool:
        return any(cls._is_within_workspace(path, root) for root in roots)

    @staticmethod
    def _system_temp_roots() -> list[Path]:
        candidates: list[Path] = []
        seen: set[str] = set()
        for raw in (
            tempfile.gettempdir(),
            os.environ.get("TMP"),
            os.environ.get("TEMP"),
            os.environ.get("TMPDIR"),
        ):
            text = str(raw or "").strip()
            if not text:
                continue
            try:
                resolved = Path(text).expanduser().resolve()
            except Exception:
                continue
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(resolved)
        return candidates

    @classmethod
    def _is_system_temp_path(cls, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        return cls._is_within_any_root(resolved, cls._system_temp_roots())

    @staticmethod
    def _normalize_command(command: str) -> str:
        return str(command or "").lower().replace("/", "\\")

    @staticmethod
    def _matches_any(command: str, patterns: list[str]) -> bool:
        return any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _mentions_relative_tools_token(normalized_command: str) -> bool:
        return bool(re.search(r"(?:^|[\s'\"`>|;(])(?:\.\s*\\)?tools(?:\\|$)", normalized_command))

    @staticmethod
    def _mentions_legacy_temp_token(normalized_command: str) -> bool:
        return bool(
            re.search(r"(?:^|[\s'\"`>|;(])(?:\.\s*\\)?tmp(?:\\|$)", normalized_command)
            or re.search(r"(?:^|[\s'\"`>|;(])(?:\.\s*\\)?\.g3ku\\tmp(?:\\|$)", normalized_command)
        )

    @staticmethod
    def _mentions_system_temp_token(normalized_command: str) -> bool:
        return bool(
            re.search(r"%temp%", normalized_command)
            or re.search(r"%tmp%", normalized_command)
            or re.search(r"\$env:temp\b", normalized_command)
            or re.search(r"\$env:tmp\b", normalized_command)
            or re.search(r"\$tmpdir\b", normalized_command)
            or re.search(r"/tmp(?:/|$)", str(normalized_command or ""))
        )

    @staticmethod
    def _mentions_relative_temp_token(normalized_command: str) -> bool:
        return bool(re.search(r"(?:^|[\s'\"`>|;(])(?:\.\s*\\)?temp(?:\\|$)", normalized_command))

    @staticmethod
    def _mentions_relative_externaltools_token(normalized_command: str) -> bool:
        return bool(re.search(r"(?:^|[\s'\"`>|;(])(?:\.\s*\\)?externaltools(?:\\|$)", normalized_command))

    def _command_references_any_root(self, command: str, roots: list[Path]) -> bool:
        for raw in self._extract_absolute_paths(command):
            try:
                path = Path(raw.strip()).expanduser().resolve()
            except Exception:
                continue
            if self._is_within_any_root(path, roots):
                return True
        return False

    def _command_references_system_temp(self, command: str, *, workspace_root: Path, temp_root: Path) -> bool:
        for raw in self._extract_absolute_paths(command):
            try:
                path = Path(raw.strip()).expanduser().resolve()
            except Exception:
                continue
            if (
                self._is_within_workspace(path, workspace_root)
                and self._is_system_temp_path(path)
                and not self._is_within_workspace(path, temp_root)
            ):
                return True
        return False

    def _has_managed_target(self, command: str, *, temp_root: Path) -> bool:
        normalized = self._normalize_command(command)
        if self._mentions_relative_externaltools_token(normalized):
            return True
        if temp_root == self._temp_root() and self._mentions_relative_temp_token(normalized):
            return True
        return self._command_references_any_root(command, [temp_root, self._externaltools_root()])

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
        stdout_text: str,
        stderr_text: str,
        error: str = "",
    ) -> str:
        combined = stdout_text.strip()
        if stderr_text.strip():
            combined = f"{combined}\nSTDERR:\n{stderr_text.strip()}".strip()
        payload = {
            "status": status,
            "exit_code": exit_code,
            "head_preview": self._preview(combined, from_tail=False),
        }
        if error:
            payload["error"] = error
        return json.dumps(payload, ensure_ascii=False)

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


