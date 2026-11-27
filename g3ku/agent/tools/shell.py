"""Shell execution tool."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from g3ku.agent.tools import host_process_guard
from g3ku.agent.tools.base import Tool
from g3ku.deployment.data_root import data_g3ku_path, data_work_path
from g3ku.runtime import owned_processes
from g3ku.runtime.project_environment import apply_project_environment, resolve_project_environment
from g3ku.runtime.tool_watchdog import coerce_timeout_argument, resolve_tool_watchdog_config
from g3ku.utils.helpers import expand_path_anchor_tokens, model_data_root
from g3ku.utils.process_tree import kill_process_tree, kill_process_tree_checked
from g3ku.utils.subprocess_text import (
    decode_subprocess_output,
    enrich_subprocess_env_for_text,
    sniff_subprocess_encoding,
)
from main.governance.exec_tool_policy import (
    EXEC_TOOL_ID,
    EXECUTION_MODE_FULL_ACCESS,
    EXECUTION_MODE_GOVERNED,
    normalize_exec_execution_mode,
    resolve_exec_execution_mode,
)

_STREAM_READ_CHUNK_SIZE = 4096
_STREAM_CAPTURE_BYTE_LIMIT = 64 * 1024
# 全量 tee 的单文件上限：超过它停止写盘并如实标注，避免一条 `cat 大文件` 写穿磁盘。
_EXEC_FULL_OUTPUT_BYTE_LIMIT = 64 * 1024 * 1024
# exec 正文的内联额度：捕获到的正文不超过它时整篇交付给模型。额度必须低于工具结果的
# 通用外置闸门（`content.navigation.INLINE_CHAR_LIMIT` = 6000，按序列化后的载荷长度判），
# 否则 exec 自己刚凑齐的正文会被信封换成 800 字符预览。超限时 exec 不自己切割、也不自带
# ref：整篇留在载荷里，由统一信封外置成 artifact 并给出 `content_open(ref=...)` 指引。
# `stdout_truncated` 说的是 64 KB 捕获环有没有丢中段，与这里无关。
_EXEC_INLINE_CHAR_LIMIT = 5_000
# 进程退出后，输出管道尾部排空的宽限上限（秒）。正常命令在进程退出瞬间即到 EOF、
# 排空立即完成；只有当孙进程继承了 stdout/stderr 写端、管道迟迟不到 EOF 时才会等到
# 这个宽限，到点后杀进程树释放管道并返回已抢救的部分输出（带 drain_timed_out 标记）。
# 与超时路径 _collect_terminated_process_output 的 5s 排空宽限保持一致。
_POST_EXIT_DRAIN_GRACE_SECONDS = 5.0

# Windows 后台车道的 broker 回执等待上限（秒）。broker 起完目标即退出，正常毫秒级返回。
_BACKGROUND_BROKER_TIMEOUT_SECONDS = 20.0
# broker 脚本：stdin 收 JSON spec（argv/cwd/log_path/creationflags），起目标后把目标 pid
# 写回 stdout 单行 JSON 并立即退出。目标父 PID 随之指向死进程，bootstrap reaper 的
# `taskkill /T` 活进程树遍历够不着它；creationflags 由调用方传入（含 BREAKAWAY_FROM_JOB）。
_BACKGROUND_BROKER_SCRIPT = (
    "import json,subprocess,sys\n"
    "spec=json.loads(sys.stdin.read())\n"
    "log=open(spec['log_path'],'ab',buffering=0)\n"
    "p=subprocess.Popen(spec['argv'],cwd=spec.get('cwd') or None,"
    "stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,"
    "creationflags=int(spec.get('creationflags') or 0))\n"
    "sys.stdout.write(json.dumps({'pid':p.pid})+'\\n')\n"
    "sys.stdout.flush()\n"
)

_DRAIN_TIMEOUT_NOTE = (
    "命令主进程已退出，但其输出管道仍未关闭——有后台子/孙进程仍持有 stdout/stderr 写端。"
    "已等待 5s 宽限后杀掉进程树释放管道，上面的输出可能被截断。"
    "若要让进程在后台常驻（服务/抓取驱动/浏览器），请改用 `background: true`：运行时会把它"
    "detach、输出落日志文件，并返回 pid + log_path 供后续 process_kill 回收；不要手搓 "
    "`Start-Process` / `nohup` 让它继承 exec 的输出管道。"
)

_PYTHON_INLINE_SYNTAX_NOTE = (
    "这看起来是内联 `python -c` 的引号/转义失败。对任何非平凡 Python，请改用 "
    "filesystem_write 在任务级临时目录写一个 .py 文件，再用 exec 跑 `python <file>`，"
    "不要把整段脚本塞进 `-c`：嵌套引号、花括号、三引号、中文/Markdown 片段、长 JSON "
    "都极易把真正的 Python 错误混成 shell 解析错误。"
)

# 手工脱离派生写法（best-effort 文本判据）：命中只追加导流提示，不拦截命令。
# 这类进程不在归属表里，超时/取消与重启都可能把它一起收走且没有通知。
_DETACHED_SPAWN_PATTERNS = (
    r"\bdetached_process\b",
    r"\bcreate_breakaway_from_job\b",
    r"\bcreate_new_process_group\b",
    r"\bstart-process\b",
    r"\bwmic(?:\.exe)?\s+process\b[^\r\n]*\bcall\s+create\b",
    r"\bschtasks(?:\.exe)?\b[^\r\n]*\s/create\b",
    r"\bnohup\b",
    r"\bsetsid\b",
    r"\bdisown\b",
    r"\bsystemd-run\b",
)

_DETACHED_SPAWN_NOTE = (
    "检测到手工脱离派生写法（{hit}）。这样起的进程不在运行时的归属表里：运行时重启、"
    "前台调用超时或取消都可能把它一起终止，而且不会有任何通知。需要常驻的进程请改用 "
    "`background: true`（purpose 必填）：运行时会把它 detach、登记归属、输出落日志文件，"
    "并在它退出时主动叫醒你。"
)

_SWEPT_TREE_DESCENDANT_LIMIT = 8

# exec runs as a child of the Web/worker runtime. A command that terminates
# processes can therefore kill the host that is waiting for the command result
# (for example: ``Get-Process python | Stop-Process -Force``). This is a host
# integrity boundary, not an ordinary read-only/deny-pattern rule: it remains
# active in ``full_access`` mode and cannot be bypassed by command approval.
_HOST_PROCESS_TERMINATION_PATTERNS = (
    r"\bstop-process(?:\.exe)?\b",
    r"\bspps(?:\.exe)?\b",  # PowerShell alias for Stop-Process
    r"\btaskkill(?:\.exe)?\b",
    r"\bkillall(?:\.exe)?\b",
    r"\bpkill(?:\.exe)?\b",
    r"(?<![\w.-])kill(?:\.exe)?(?=\s|$)",
    r"\bwmic(?:\.exe)?\s+process\b[^\r\n]*(?:\bdelete\b|\bcall\s+terminate\b)",
    r"\binvoke-cimmethod\b[^\r\n]*(?:terminate|delete)",
    r"\b(?:os|signal)\.(?:kill|killpg|pthread_kill)\s*\(",
    r"\bterminateprocess(?:es)?\b",
    r"\.\s*(?:kill|terminate)\s*\(",
)

_HOST_PROCESS_TERMINATION_ERROR = (
    "Error: exec blocked host-process termination. The command could terminate "
    "the web runtime or a managed worker process. Use read-only process inspection, "
    "or let exec timeout/cancellation clean up the child process it started; do not "
    "terminate arbitrary processes by PID/name (for example, all python processes)."
)


@dataclass
class _BoundedStreamCapture:
    byte_limit: int = _STREAM_CAPTURE_BYTE_LIMIT
    total_bytes: int = 0
    truncated: bool = False
    _head: bytearray = field(default_factory=bytearray)
    _tail: bytearray = field(default_factory=bytearray)
    # 全量 tee：内存环只保头尾，原始字节另落一份文件，供事后登记成 artifact。
    # sink 由调用方打开与关闭；这里只负责写与停写判据。
    sink: Any = None
    sink_path: str = ""
    sink_bytes: int = 0
    sink_truncated: bool = False
    sink_failed: bool = False

    def append(self, chunk: bytes | None) -> None:
        if not chunk:
            return
        data = bytes(chunk)
        self.total_bytes += len(data)
        head_remaining = self.byte_limit - len(self._head)
        if head_remaining > 0:
            self._head.extend(data[:head_remaining])
        self._tail.extend(data)
        if len(self._tail) > self.byte_limit:
            del self._tail[:-self.byte_limit]
        self.truncated = self.total_bytes > self.byte_limit
        self._write_sink(data)

    def _write_sink(self, data: bytes) -> None:
        """写盘失败或超单文件上限都只停 tee，不影响捕获与命令本身。

        交付侧据此如实退回「中段未保留」，不谎报有全量。
        """
        if self.sink is None or self.sink_failed or self.sink_truncated:
            return
        remaining = _EXEC_FULL_OUTPUT_BYTE_LIMIT - self.sink_bytes
        if remaining <= 0:
            self.sink_truncated = True
            return
        piece = data[:remaining]
        try:
            self.sink.write(piece)
            self.sink_bytes += len(piece)
        except OSError:
            self.sink_failed = True
            return
        if len(data) > remaining:
            self.sink_truncated = True

    @classmethod
    def from_bytes(cls, data: bytes | None, *, byte_limit: int = _STREAM_CAPTURE_BYTE_LIMIT) -> "_BoundedStreamCapture":
        capture = cls(byte_limit=byte_limit)
        capture.append(data or b"")
        return capture

    def head_text(self) -> str:
        return decode_subprocess_output(bytes(self._head))

    def tail_text(self) -> str:
        return decode_subprocess_output(bytes(self._tail))


class ExecTool(Tool):
    """Tool to execute shell commands."""

    # exec 持有子进程并需要结构化收尾（杀进程树、排空管道、抢救部分输出），
    # 由工具自身消费统一 timeout_seconds 值；外层强制层对其让位。
    self_enforced_timeout = True

    def __init__(
        self,
        timeout: float | None = None,
        working_dir: str | None = None,
        workspace_root: str | None = None,
        temp_root: str | None = None,
        externaltools_root: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        enable_safety_guard: bool = True,
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
            r"(?:^|[;&|])\s*format\b",       # format (as standalone command only)
            r"\bformat-volume\b",            # PowerShell format-volume
            r"\bclear-disk\b",               # PowerShell clear-disk
            r"\b(mkfs|diskpart)\b",          # disk operations
            r"\bdd\s+if=",                   # dd
            r">\s*/dev/sd",                  # write to disk
            r"\bshred\b",                    # secure erase
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
        # 模式与授权只由运行时合同声明（前门头部 `exec_runtime_policy`、节点尾块），
        # 工具说明不写第二份权威：`tools[]` 是钉住整表，写进 mode 后它在切模式时不刷新，
        # 模型会按过期口径判断拦截边界。细节（管道、临时 .py、终止边界）住 toolskill。
        return (
            "Run shell commands directly (PowerShell/sh, no nested wrapper). background=true only when "
            "the process must outlive this call; stop it with process_kill."
        )

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
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Detach the command so it keeps running after this call returns; answers pid + "
                        "log_path immediately and then ignores timeout_seconds. Use it only when a later "
                        "call must still find this process (server, browser, crawl driver). A command that "
                        "is merely slow stays foreground. Stop it with process_kill(pid). It survives "
                        "project restarts: the runtime re-adopts it and notifies you; if it dies you are "
                        "woken to decide whether to restart it. When true, purpose is required."
                    ),
                },
                "purpose": {
                    "type": "string",
                    "description": (
                        "Required when background=true. One line (<=120 chars; the process list shows the "
                        "first 80) saying what this process serves and why it must outlive this call."
                    ),
                },
            },
            "required": ["command"]
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        payload = params if isinstance(params, dict) else {}
        if bool(payload.get("background")):
            purpose = str(payload.get("purpose") or "").strip()
            if not purpose:
                errors.append(
                    "purpose is required when background=true: one line (<=120 chars) saying "
                    "what this process serves and why it must outlive this call."
                )
            elif len(purpose) > 120:
                errors.append("purpose must be <=120 characters; the process list shows at most 80.")
        return errors

    async def execute(
        self,
        command: str,
        working_dir: str | None = None,
        background: bool = False,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = kwargs.pop("__g3ku_runtime", None) or {}
        effective_timeout = self._resolve_effective_timeout(kwargs.pop("timeout_seconds", None), runtime=runtime)
        try:
            cwd = self._resolve_cwd(working_dir, runtime=runtime)
        except ValueError as exc:
            message = str(exc)
            return self._build_payload(
                status="error",
                exit_code=None,
                stdout_text="",
                stderr_text=message,
                error=message,
            )
        execution_mode = self._resolve_execution_mode()
        if (
            execution_mode == EXECUTION_MODE_FULL_ACCESS
            and bool(runtime.get("exec_force_governed"))
        ):
            # owner 的只读档：工具档位（可能是 full_access）被策略压回 governed，
            # 于是路径策略、工作区监禁与只读形态守卫三层全部生效。
            execution_mode = EXECUTION_MODE_GOVERNED

        # Full-access exec may skip ordinary exec guardrails, but it can never
        # terminate the runtime host that owns this tool call.
        host_process_error = self._enforce_host_process_safety(command)
        if host_process_error:
            return self._build_payload(
                status="error",
                exit_code=None,
                stdout_text="",
                stderr_text=host_process_error,
                error=host_process_error,
            )

        if execution_mode != EXECUTION_MODE_FULL_ACCESS:
            # 路径监禁层：temp/系统路径策略与工作区边界，永不被白名单或审批豁免。
            policy_error = self._enforce_command_path_policy(command, cwd, runtime=runtime)
            if policy_error:
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    stdout_text="",
                    stderr_text=policy_error,
                    error=policy_error,
                )
            workspace_error = self._guard_workspace_paths(command, cwd)
            if workspace_error:
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    stdout_text="",
                    stderr_text=workspace_error,
                    error=workspace_error,
                )
            # 命令形态层：只读约束 + deny 黑名单。命中后先查白名单，再等
            # 操作者审批（超时自动拒绝），都不可得才返回错误。
            form_error = self._enforce_read_only_command(command) or self._guard_deny_patterns(command)
            if form_error:
                allowed, blocked_text = await self._resolve_form_guard_block(
                    command, cwd, form_error, runtime=runtime
                )
                if not allowed:
                    return self._build_payload(
                        status="error",
                        exit_code=None,
                        stdout_text="",
                        stderr_text=blocked_text,
                        error=blocked_text,
                    )

        env = self._build_subprocess_env(runtime=runtime, cwd=cwd)
        # 只对前台调用提示：background=true 就是官方脱离通道，不需要再导流。
        detach_note = "" if background else self._detached_spawn_note(command)

        if background:
            return self._start_background(
                command=command,
                cwd=cwd,
                env=env,
                runtime=runtime,
                purpose=str(purpose or "").strip(),
            )

        tee: tuple[Any, str] | None = None
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
                    start_new_session=True,
                )

            tee = self._open_exec_tee(runtime)
            try:
                stdout_capture, stderr_capture, drain_timed_out = await self._collect_process_output(
                    process,
                    timeout=effective_timeout,
                    tee=tee,
                )
            except asyncio.CancelledError:
                kill_process_tree(process)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                raise
            except asyncio.TimeoutError:
                kill_result = kill_process_tree_checked(process)
                # Wait for the process to fully terminate so pipes are
                # drained and file descriptors are released.
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                stdout_capture, stderr_capture = await self._collect_terminated_process_output(process, tee=tee)
                _secs_text = (
                    f"{effective_timeout:.2f}".rstrip("0").rstrip(".")
                    if effective_timeout < 1
                    else f"{effective_timeout:.0f}"
                )
                timeout_message = (
                    f"Command timed out after {_secs_text} seconds. "
                    "如需更长运行时间，请在下一次调用时显式传入更大的 \"timeout_seconds\" 参数（单位：秒，支持小数）。"
                    "命令只是慢，不等于需要常驻：加大 timeout_seconds 就行，不要用 background=true 绕开超时。"
                )
                timeout_notes = [
                    note
                    for note in (detach_note, self._swept_process_tree_note(kill_result))
                    if note
                ]
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    stdout_capture=stdout_capture,
                    stderr_capture=stderr_capture,
                    stderr_text=timeout_message,
                    error=timeout_message,
                    extra_notes=timeout_notes,
                    full_output=self._finalize_full_output(
                        tee,
                        stdout_capture=stdout_capture,
                        stderr_capture=stderr_capture,
                        runtime=runtime,
                    ),
                )

            extra_notes: list[str] = [detach_note] if detach_note else []
            if drain_timed_out:
                extra_notes.append(_DRAIN_TIMEOUT_NOTE)
            if process.returncode != 0 and self._is_python_inline_syntax_failure(command, stderr_capture):
                extra_notes.append(_PYTHON_INLINE_SYNTAX_NOTE)
            return self._build_payload(
                status="success" if process.returncode == 0 else "error",
                exit_code=process.returncode,
                stdout_capture=stdout_capture,
                stderr_capture=stderr_capture,
                error="" if process.returncode == 0 else f"Exit code: {process.returncode}",
                extra_notes=extra_notes,
                full_output=self._finalize_full_output(
                    tee,
                    stdout_capture=stdout_capture,
                    stderr_capture=stderr_capture,
                    runtime=runtime,
                ),
            )

        except Exception as e:
            return self._build_payload(
                status="error",
                exit_code=None,
                stdout_text="",
                stderr_text=str(e),
                error=f"Error executing command: {str(e)}",
                extra_notes=[detach_note] if detach_note else None,
            )
        finally:
            # 正常路径已在 _finalize_full_output 里关闭并删除；这里兜住取消/异常路径，
            # 不让临时文件留在数据根里无人回收。
            self._discard_tee(tee)
            self._notify_resource_change(runtime=runtime, trigger="tool:exec")

    async def _collect_process_output(
        self,
        process: Any,
        *,
        timeout: float,
        tee: tuple[Any, str] | None = None,
    ) -> tuple[_BoundedStreamCapture, _BoundedStreamCapture, bool]:
        sink, sink_path = tee if tee is not None else (None, "")
        if getattr(process, "stdout", None) is None and getattr(process, "stderr", None) is None:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return (
                _BoundedStreamCapture.from_bytes(stdout),
                _BoundedStreamCapture.from_bytes(stderr),
                False,
            )
        stdout_task = asyncio.create_task(self._capture_stream(process.stdout, sink=sink, sink_path=sink_path))
        stderr_task = asyncio.create_task(self._capture_stream(process.stderr, sink=sink, sink_path=sink_path))
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except BaseException:
            await self._cancel_capture_tasks(stdout_task, stderr_task)
            raise
        # 进程已退出：给尾部排空设界，避免孙进程继承输出管道写端、管道迟迟不到 EOF
        # 时无限吊住本次调用（timeout 只约束「等进程退出」那一段，约束不到这里）。
        stdout_capture, stderr_capture, drain_timed_out = await self._await_capture_tasks(
            stdout_task, stderr_task, timeout=_POST_EXIT_DRAIN_GRACE_SECONDS
        )
        if drain_timed_out:
            # 排空到点仍未 EOF：杀掉残留进程树释放管道句柄，回收 fd/僵尸进程，
            # 返回已抢救的部分输出（drain_timed_out 由调用方转成模型可见提示）。
            kill_process_tree(process)
        return stdout_capture, stderr_capture, drain_timed_out

    async def _collect_terminated_process_output(
        self,
        process: Any,
        *,
        tee: tuple[Any, str] | None = None,
    ) -> tuple[_BoundedStreamCapture, _BoundedStreamCapture]:
        sink, sink_path = tee if tee is not None else (None, "")
        if getattr(process, "stdout", None) is None and getattr(process, "stderr", None) is None:
            return _BoundedStreamCapture(), _BoundedStreamCapture()
        stdout_task = asyncio.create_task(self._capture_stream(process.stdout, sink=sink, sink_path=sink_path))
        stderr_task = asyncio.create_task(self._capture_stream(process.stderr, sink=sink, sink_path=sink_path))
        stdout_capture, stderr_capture, _drain_timed_out = await self._await_capture_tasks(
            stdout_task, stderr_task, timeout=_POST_EXIT_DRAIN_GRACE_SECONDS
        )
        return stdout_capture, stderr_capture

    async def _capture_stream(self, stream: Any, *, sink: Any = None, sink_path: str = "") -> _BoundedStreamCapture:
        capture = _BoundedStreamCapture(sink=sink, sink_path=sink_path)
        if stream is None:
            return capture
        try:
            while True:
                chunk = await stream.read(_STREAM_READ_CHUNK_SIZE)
                if not chunk:
                    break
                capture.append(chunk)
        except asyncio.CancelledError:
            return capture
        return capture

    async def _await_capture_tasks(
        self,
        stdout_task: "asyncio.Task[_BoundedStreamCapture]",
        stderr_task: "asyncio.Task[_BoundedStreamCapture]",
        *,
        timeout: float | None = None,
    ) -> tuple[_BoundedStreamCapture, _BoundedStreamCapture, bool]:
        gather = asyncio.gather(stdout_task, stderr_task)
        try:
            if timeout is None:
                stdout_capture, stderr_capture = await gather
            else:
                stdout_capture, stderr_capture = await asyncio.wait_for(gather, timeout=timeout)
            return stdout_capture, stderr_capture, False
        except asyncio.TimeoutError:
            stdout_task.cancel()
            stderr_task.cancel()
            stdout_capture, stderr_capture = await asyncio.gather(stdout_task, stderr_task)
            return stdout_capture, stderr_capture, True

    async def _cancel_capture_tasks(
        self,
        stdout_task: "asyncio.Task[_BoundedStreamCapture]",
        stderr_task: "asyncio.Task[_BoundedStreamCapture]",
    ) -> tuple[_BoundedStreamCapture, _BoundedStreamCapture]:
        stdout_task.cancel()
        stderr_task.cancel()
        return await asyncio.gather(stdout_task, stderr_task)

    def _resolve_effective_timeout(self, requested_timeout: Any, *, runtime: dict[str, Any] | None = None) -> float:
        """统一 timeout 合同：显式传参 > 构造期遗留缺省 > 运行时全局默认（600s）。"""
        explicit = coerce_timeout_argument(requested_timeout)
        if explicit is not None:
            return explicit
        if self.timeout is not None:
            legacy = coerce_timeout_argument(self.timeout)
            if legacy is not None:
                return legacy
        return resolve_tool_watchdog_config(runtime or {}).default_timeout_seconds

    @staticmethod
    def _detached_spawn_note(command: str) -> str:
        """命令里出现手工脱离派生写法时给出导流提示；只是提示，不拦截。"""
        normalized = str(command or "")
        if not normalized:
            return ""
        for pattern in _DETACHED_SPAWN_PATTERNS:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                return _DETACHED_SPAWN_NOTE.format(hit=match.group(0).strip())
        return ""

    @staticmethod
    def _swept_process_tree_note(result: Any) -> str:
        """超时/取消整树终止后，把"被一起收走的进程"与"未确认退出的残留"如实回报。"""
        descendants = [
            item for item in (getattr(result, "descendants", None) or []) if isinstance(item, dict)
        ]
        leftovers: list[int] = []
        for pid in getattr(result, "descendant_leftovers", None) or []:
            try:
                normalized = int(pid)
            except (TypeError, ValueError):
                continue
            if normalized > 0:
                leftovers.append(normalized)
        if leftovers:
            shown = ", ".join(f"pid={pid}" for pid in leftovers[:_SWEPT_TREE_DESCENDANT_LIMIT])
            more = f" 等共 {len(leftovers)} 个" if len(leftovers) > _SWEPT_TREE_DESCENDANT_LIMIT else ""
            return (
                f"警告：整树终止后以下子/孙进程仍未确认退出：{shown}{more}。"
                "它们可能仍在运行，用 process_kill 或再次终止兜底。"
            )
        if not descendants:
            return ""
        shown_items = []
        for item in descendants[:_SWEPT_TREE_DESCENDANT_LIMIT]:
            try:
                pid = int(item.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            name = str(item.get("name") or "").strip()
            shown_items.append(f"pid={pid}" + (f" ({name})" if name else ""))
        if not shown_items:
            return ""
        more = f" 等共 {len(descendants)} 个" if len(descendants) > _SWEPT_TREE_DESCENDANT_LIMIT else ""
        return (
            "本次调用超时/取消时终止了整棵进程树，其中包括已脱离父进程的子/孙进程："
            f"{', '.join(shown_items)}{more}。这些进程已不存在；若其中有需要常驻的"
            "（如 DETACHED_PROCESS / Start-Process / nohup 起的），前台里的分离写法活不过"
            "超时或取消，请改用 `background: true` 重新拉起。"
        )

    def _resolve_execution_mode(self) -> str:
        service = getattr(self, "main_task_service", None)
        record = None
        if service is not None:
            get_tool = getattr(service, "get_tool", None)
            if callable(get_tool):
                try:
                    record = get_tool(EXEC_TOOL_ID)
                except Exception:
                    record = None
        return resolve_exec_execution_mode(
            record=record,
            settings_payload={'execution_mode': self.execution_mode_default},
        )

    @staticmethod
    def _enforce_host_process_safety(command: str) -> str | None:
        """Block termination commands whose target could be the Negi host.

        The boundary is target-identity, not command shape: a termination whose
        target is statically provable as non-Negi (exact name/PID) is allowed;
        ambiguous targets (wildcards, pipeline input, dynamic expressions) and
        execution sinks stay blocked. It remains independent of ``execution_mode``
        and operator approval, and is not a general OS sandbox.
        """
        normalized = str(command or "").strip().lower()
        if not normalized:
            return None
        if not any(re.search(pattern, normalized) for pattern in _HOST_PROCESS_TERMINATION_PATTERNS):
            return None
        if host_process_guard.should_block(normalized):
            return _HOST_PROCESS_TERMINATION_ERROR
        return None

    def _enforce_read_only_command(self, command: str) -> str | None:
        normalized = str(command or "").strip()
        lower = normalized.lower()
        if not lower:
            return None

        mutating_patterns = [
            r"(^|[;&|])\s*set-content\b",
            r"(^|[;&|])\s*add-content\b",
            r"(^|[;&|])\s*clear-content\b",
            r"(^|[;&|])\s*new-item\b",
            r"(^|[;&|])\s*remove-item\b",
            r"(^|[;&|])\s*move-item\b",
            r"(^|[;&|])\s*copy-item\b",
            r"(^|[;&|])\s*rename-item\b",
            r"(^|[;&|])\s*touch\b",
            r"(^|[;&|])\s*rm\b",
            r"(^|[;&|])\s*mv\b",
            r"(^|[;&|])\s*cp\b",
            r"(^|[;&|])\s*mkdir\b",
            r"(^|[;&|])\s*install\b",
            r"(^|[;&|])\s*tee\b",
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
        # 语句起始锚点必须把换行也当成语句分隔符（MULTILINE），否则
        # "ls\nrm -rf …" 之类的分行命令能绕过只读层。
        for pattern in mutating_patterns:
            if re.search(pattern, lower, re.MULTILINE):
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

    def _guard_workspace_paths(self, command: str, cwd: str) -> str | None:
        """工作区路径监禁：永不被白名单/审批豁免（放行只豁免命令形态层）。"""
        if not self.restrict_to_workspace:
            return None
        cmd = str(command or "").strip()
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

    def _guard_deny_patterns(self, command: str) -> str | None:
        """破坏性命令黑名单：可被白名单豁免或操作者审批放行。"""
        if not self.enable_safety_guard:
            return None
        lower = str(command or "").strip().lower()
        for pattern in self.deny_patterns:
            if re.search(pattern, lower, re.MULTILINE):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"
        return None

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands.

        兼容入口（旧调用方/测试缝隙）：路径监禁 + deny 黑名单两层合判。
        execute() 主链路已改为分层调用，以便白名单/审批只豁免形态层。
        """
        return self._guard_workspace_paths(command, cwd) or self._guard_deny_patterns(command)

    def _exec_approvals_service(self) -> Any | None:
        service = getattr(self, "main_task_service", None)
        return getattr(service, "exec_approvals", None)

    def _matched_deny_pattern(self, command: str) -> str:
        lower = str(command or "").strip().lower()
        for pattern in self.deny_patterns:
            try:
                if re.search(pattern, lower, re.MULTILINE):
                    return str(pattern)
            except re.error:
                continue
        return ""

    async def _resolve_form_guard_block(
        self,
        command: str,
        cwd: str,
        guard_error: str,
        *,
        runtime: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        """命令形态层命中后的处理：白名单豁免 → 操作者审批等待 → 拒绝引导。

        审批经 governance sqlite 持久化（worker 等待、web 裁决，跨进程轮询）；
        到期未决自动拒绝；近窗连续未获批达阈值即快拒（防审批轰炸）。审批
        服务不可用时保持旧行为：直接返回原始错误文本。
        """
        runtime = runtime or {}
        approvals = self._exec_approvals_service()
        if approvals is None:
            return False, guard_error
        actor_role = str(runtime.get("actor_role") or "execution").strip().lower() or "execution"
        try:
            entry = approvals.command_allowed(command, actor_role=actor_role)
        except Exception:
            entry = None
        if entry is not None:
            return True, ""
        task_id = str(runtime.get("task_id") or "").strip()
        session_key = str(runtime.get("session_key") or "").strip()
        lane = "task" if task_id else ("ceo" if actor_role == "ceo" else "session")
        try:
            request = approvals.request_approval(
                command=command,
                guard_reason=f"{guard_error} | deny_pattern={self._matched_deny_pattern(command)}",
                actor_role=actor_role,
                lane=lane,
                context_id=task_id or session_key,
                cwd=str(cwd or ""),
            )
        except Exception:
            request = None
        if request is None:
            return False, self._guard_blocked_text(guard_error, approvals_available=True, outcome="fast_rejected")
        try:
            decision = await approvals.wait_for_decision(
                str(request.get("approval_id") or ""),
                timeout_seconds=float(request.get("wait_seconds") or 120.0),
                cancel_token=runtime.get("cancel_token"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            decision = "error"
        if decision in {"approved_once", "approved_whitelist"}:
            return True, ""
        return False, self._guard_blocked_text(guard_error, approvals_available=True, outcome=str(decision))

    @staticmethod
    def _guard_blocked_text(guard_error: str, *, approvals_available: bool, outcome: str = "") -> str:
        if not approvals_available:
            return guard_error
        lines = [guard_error]
        if outcome == "fast_rejected":
            lines.append(
                "该命令近期已连续多次审批超时/被拒，本次不再发起等待，直接拒绝。"
                "如属正当需求，请让操作者在工具管理页把命令模式加入白名单。"
            )
        elif outcome == "denied":
            lines.append("操作者已拒绝执行该命令。")
        elif outcome in {"expired", "error"}:
            lines.append(
                "等待操作者审批超时，已自动拒绝。可重新提交；操作者在工具管理页"
                "把命令模式加入白名单后无需重复审批。"
            )
        elif outcome == "cancelled":
            lines.append("审批等待被取消（任务暂停/取消）。")
        lines.append(
            "提示：文件删除/修改优先使用 filesystem_delete / filesystem_write / "
            "filesystem_edit 等专用工具，而非 shell。"
        )
        return "\n".join(lines)

    def _start_background_windows_broker(
        self,
        *,
        command: str,
        cwd: str,
        env: dict[str, str],
        log_path: str,
    ) -> tuple[int | None, str]:
        """经短命 broker 起目标，断掉 job 与父树两重连坐；返回 (目标 pid, 错误文本)。

        broker 本身留在 job 里、不登记，起完目标即退出，是一次性中继。目标带
        `CREATE_BREAKAWAY_FROM_JOB` 出生（job 已设 `BREAKAWAY_OK`，见
        `g3ku/web/windows_job.py`），停机关 job 不连坐；父 PID 指向已退出的 broker，
        `taskkill /T` 够不着。登记用目标 pid，create_time 由调用方现取。
        """
        argv = self._windows_shell_argv(command)
        target_flags = (
            getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
        spec = json.dumps(
            {"argv": argv, "cwd": cwd, "log_path": log_path, "creationflags": target_flags}
        )
        broker_argv = [sys.executable, "-u", "-c", _BACKGROUND_BROKER_SCRIPT]
        try:
            broker = subprocess.Popen(
                broker_argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return None, f"Failed to start background broker: {exc}"
        try:
            out, err = broker.communicate(
                input=spec.encode("utf-8"), timeout=_BACKGROUND_BROKER_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            broker.kill()
            broker.communicate()
            return None, "Background broker timed out before reporting the target pid."
        except Exception as exc:
            return None, f"Background broker failed: {exc}"
        pid = 0
        for line in reversed((out or b"").decode("utf-8", errors="replace").splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                pid = int(json.loads(line).get("pid") or 0)
            except Exception:
                pid = 0
            break
        if broker.returncode != 0 or pid <= 0:
            detail = (err or b"").decode("utf-8", errors="replace").strip()[-500:]
            return None, (
                f"Background broker did not start the target (exit={broker.returncode}). {detail}"
            ).strip()
        return pid, ""

    def _start_background(
        self,
        *,
        command: str,
        cwd: str,
        env: dict[str, str],
        runtime: dict[str, Any],
        purpose: str = "",
    ) -> str:
        """启动一个运行时拥有的后台进程，并登记进归属表。

        与前台车道只差三处：不接管道（stdout/stderr 直接落日志文件）、不等待
        （起后立即返回）、启动即登记。detach 之后它不再受 exec 的超时/取消
        管辖，回收权交给 `process_kill` 与 scope 生命周期。Windows 经短命 broker
        中转出生（断 job 与父树连坐，见 `_start_background_windows_broker`），
        因此没有可 poll 的句柄，退出观察走存活轮询；POSIX 用 `start_new_session`
        脱离进程组，仍保留 Popen 句柄。

        节点道的生命周期准入门（`_node_background_admission`）在 Popen 前检查
        本次调用所属域是否仍开放；Popen 后登记/adopt 前再核对一次——若域恰在
        启动瞬间被关闭（暂停/终态落地），刚起的进程立即按树终止并计入正在
        执行的停止批次，不留下一个漏网进程。
        """
        session_key = str(runtime.get("session_key") or "web:shared").strip() or "web:shared"
        task_id = str(runtime.get("task_id") or "").strip()
        node_id = str(runtime.get("node_id") or "").strip()
        admission = runtime.get("_node_background_admission")
        if callable(admission):
            closed_reason = ""
            try:
                closed_reason = str(admission() or "")
            except Exception:
                closed_reason = ""
            if closed_reason:
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    error=(
                        "Background start rejected: this node's background lane is closed "
                        f"({closed_reason}). Resume the node or wait for it to finish before "
                        "starting background work."
                    ),
                )
        scope = owned_processes.scope_key(session_key=session_key, task_id=task_id)
        log_path = owned_processes.new_log_path(scope)
        process: Any = None
        if os.name == "nt":
            target_pid, broker_error = self._start_background_windows_broker(
                command=command, cwd=cwd, env=env, log_path=str(log_path)
            )
            if broker_error or not target_pid:
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    error=broker_error or "Failed to start background process.",
                )
        else:
            popen_kwargs: dict[str, Any] = {
                "cwd": cwd,
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stderr": subprocess.STDOUT,
                "shell": True,
                "start_new_session": True,
            }
            try:
                with open(log_path, "ab", buffering=0) as log_handle:
                    popen_kwargs["stdout"] = log_handle
                    process = subprocess.Popen(command, **popen_kwargs)
            except Exception as exc:
                message = f"Failed to start background process: {exc}"
                return self._build_payload(status="error", exit_code=None, error=message)
            target_pid = int(process.pid)
        entry = owned_processes.register(
            scope=scope,
            pid=int(target_pid),
            cmd=command,
            log_path=str(log_path),
            session_key=session_key,
            task_id=task_id,
            node_id=node_id,
            actor_role=str(runtime.get("actor_role") or ""),
            owner_pid=os.getpid(),
            label=purpose,
        )
        if callable(admission):
            # Popen 后登记/adopt 前的第二次核对：此前进程快照为空不代表此刻
            # 仍然开放；关闭则立刻收掉这条刚登记的进程。
            post_reason = ""
            try:
                post_reason = str(admission() or "")
            except Exception:
                post_reason = ""
            if post_reason:
                try:
                    owned_processes.drop(scope, int(entry["pid"]))
                except Exception:
                    pass
                try:
                    kill_process_tree(int(entry["pid"]))
                except Exception:
                    pass
                return self._build_payload(
                    status="error",
                    exit_code=None,
                    error=(
                        "Background start aborted: the node's background lane closed "
                        f"({post_reason}) while the process was starting; the freshly "
                        "started process was terminated."
                    ),
                )
        self._adopt_background_process(runtime, entry=entry, process=process)
        self._notify_background_items_changed(runtime)
        extra_notes = [
            f"Background pid {entry['pid']} is owned by this "
            f"{'task' if task_id else 'session'}; tail `{log_path}` with exec, "
            f"stop it with process_kill(pid={entry['pid']})."
        ]
        if purpose:
            extra_notes.insert(0, f"Purpose: {purpose}")
        return self._build_payload(
            status="success",
            exit_code=None,
            stdout_text=f"started background pid {entry['pid']}",
            extra_fields={
                "background": True,
                "pid": entry["pid"],
                "log_path": str(log_path),
                **({"purpose": purpose} if purpose else {}),
            },
            extra_notes=extra_notes,
        )

    @staticmethod
    def _adopt_background_process(runtime: dict[str, Any], *, entry: dict[str, Any], process: Any) -> None:
        """把进程句柄交给会话，让它自然退出时也能投一条终态事件。

        只在拿到回调的车道生效（前门 CEO）；节点道不注入，进程照旧只留归属表条目。
        """
        adopt = runtime.get("_adopt_background_process")
        if not callable(adopt):
            return
        try:
            adopt(entry=dict(entry), process=process)
        except Exception:
            pass

    @staticmethod
    def _notify_background_items_changed(runtime: dict[str, Any]) -> None:
        notify = runtime.get("_notify_background_items_changed")
        if not callable(notify):
            return
        try:
            notify()
        except Exception:
            pass

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
        return enrich_subprocess_env_for_text(
            apply_project_environment(
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
        )

    def _resolve_cwd(self, working_dir: str | None, *, runtime: dict[str, Any] | None = None) -> str:
        if not working_dir:
            task_temp_root = self._runtime_task_temp_root(runtime)
            if task_temp_root is not None:
                task_temp_root.mkdir(parents=True, exist_ok=True)
                return str(task_temp_root)
            return self.working_dir or os.getcwd()
        # 锚源与 filesystem 道同一套（同一个 `_canonical_temp_root`、同一个 workspace 根），
        # 且展开发生在路径守卫之前——守卫要拿到真实路径而不是带花括号的字面串。
        workspace_root = self._workspace_root()
        expanded = expand_path_anchor_tokens(
            working_dir,
            workspace=workspace_root,
            temp_root=self._canonical_temp_root(runtime),
            data_root=model_data_root(),
        )
        return str(Path(expanded).expanduser())

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
        # 与 `_workspace_root()` 同形 resolve：`task_temp_dir` 的准入判据是词法
        # relative_to，两边归一程度不一致会把合法路径判否、静默退回项目根。
        return data_work_path("temp").resolve()

    def _is_allowed_temp_target(self, path: Path) -> bool:
        """临时目录准入：项目根内，或数据根的 `temp/` 之下。

        只放开 `temp/` 这一支，不放开整个数据根——数据根另一侧住着任务库、日志与
        配置束，把它们交给模型写路径不在本次范围内。
        """
        return self._is_within_workspace(path, self._workspace_root()) or self._is_within_workspace(
            path, self._temp_root()
        )

    def _runtime_task_temp_root(self, runtime: dict[str, Any] | None = None) -> Path | None:
        payload = runtime if isinstance(runtime, dict) else {}
        # 受限模式：会话工作目录就是 cwd 与临时/下载落点（exec 只有在 operator
        # 显式勾选后才可达；这里保证它不会把文件散落到项目根）。
        if str(payload.get("sandbox_tier") or "") == "limited":
            session_workspace = str(payload.get("session_workspace_dir") or "").strip()
            if session_workspace:
                try:
                    return Path(session_workspace).expanduser().resolve()
                except (OSError, RuntimeError, ValueError):
                    pass
        raw = str(payload.get("task_temp_dir") or "").strip()
        if not raw:
            return None
        try:
            resolved = Path(raw).expanduser().resolve()
        except Exception:
            return None
        if not self._is_allowed_temp_target(resolved):
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
            data_g3ku_path("tmp"),
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

    def _open_exec_tee(self, runtime: dict[str, Any]) -> tuple[Any, str] | None:
        """打开本次前台 exec 的全量 tee 落点；打不开返回 None（退回现状语义）。

        落数据根而不是工作区：它是运行时自有产物，不该进用户工作区或版本库。
        不复用 owned_processes 的日志目录——那里与后台进程登记表同寿命，前台产物
        会被后台条目的清理误删。从第一个 chunk 就写：等超阈值再写会丢头部。
        """
        try:
            scope = owned_processes.scope_key(
                session_key=str(runtime.get("session_key") or ""),
                task_id=str(runtime.get("task_id") or ""),
            )
            # scope 键含冒号（task:… / session:…），Windows 路径分量非法；
            # 沿用 owned_processes._scope_dir 的 sha1 目录口径。
            digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:16]
            directory = data_g3ku_path("exec-tmp", digest, create=True)
            path = directory / f"exec-{os.urandom(6).hex()}.log"
            return open(path, "wb", buffering=0), str(path)
        except Exception:
            return None

    @staticmethod
    def _discard_tee(tee: tuple[Any, str] | None) -> None:
        if tee is None:
            return
        handle, path = tee
        try:
            handle.close()
        except OSError:
            pass
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def _finalize_full_output(
        self,
        tee: tuple[Any, str] | None,
        *,
        stdout_capture: _BoundedStreamCapture | None,
        stderr_capture: _BoundedStreamCapture | None,
        runtime: dict[str, Any],
    ) -> dict[str, Any] | None:
        """关 tee；只有捕获环真丢过中段才把全量登记成 artifact。

        未超捕获上限时环里已是全量，登记只会多一份副本，所以直接删临时文件。
        登记失败同样删临时文件并返回 None——交付侧退回「中段未保留」，不谎报有全量。
        store 是流式读源文件另写 artifact，不搬源文件，所以登记后删源是安全的。
        """
        if tee is None:
            return None
        handle, path = tee
        try:
            handle.close()
        except OSError:
            pass
        try:
            captures = [item for item in (stdout_capture, stderr_capture) if item is not None]
            if not any(item.truncated for item in captures):
                return None
            if any(item.sink_failed for item in captures):
                return None
            if max((item.sink_bytes for item in captures), default=0) <= 0:
                return None
            register = getattr(self.content_store, "register_full_output_file", None)
            if not callable(register):
                return None
            head_bytes = b"".join(bytes(item._head) for item in captures)
            info = register(
                source_path=path,
                runtime=runtime,
                display_name="exec full output",
                source_kind="tool_result:exec",
                source_encoding=sniff_subprocess_encoding(head_bytes),
            )
            if not isinstance(info, dict) or not str(info.get("ref") or "").strip():
                return None
            return {
                "ref": str(info["ref"]),
                "line_count": int(info.get("line_count") or 0),
                "char_count": int(info.get("char_count") or 0),
                "size_bytes": int(info.get("size_bytes") or 0),
                "truncated": any(item.sink_truncated for item in captures),
            }
        except Exception:
            return None
        finally:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def _build_payload(
        self,
        *,
        status: str,
        exit_code: int | None,
        stdout_text: str = "",
        stderr_text: str = "",
        error: str = "",
        stdout_capture: _BoundedStreamCapture | None = None,
        stderr_capture: _BoundedStreamCapture | None = None,
        extra_notes: list[str] | None = None,
        extra_fields: dict[str, Any] | None = None,
        full_output: dict[str, Any] | None = None,
    ) -> str:
        def _merge(primary: str, fallback: str) -> str:
            normalized_primary = str(primary or "").strip()
            normalized_fallback = str(fallback or "").strip()
            if normalized_primary and normalized_fallback and normalized_fallback not in normalized_primary:
                return f"{normalized_primary}\n{normalized_fallback}".strip()
            return normalized_primary or normalized_fallback

        stdout_head = _merge(stdout_capture.head_text() if stdout_capture is not None else "", stdout_text)
        stderr_head = _merge(stderr_capture.head_text() if stderr_capture is not None else "", stderr_text)
        stdout_tail = _merge(stdout_capture.tail_text() if stdout_capture is not None else "", stdout_text)
        stderr_tail = _merge(stderr_capture.tail_text() if stderr_capture is not None else "", stderr_text)

        combined_head = stdout_head
        if stderr_head:
            combined_head = f"{combined_head}\nSTDERR:\n{stderr_head}".strip()
        combined_tail = stdout_tail
        if stderr_tail:
            combined_tail = f"{combined_tail}\nSTDERR:\n{stderr_tail}".strip()

        # 反应式指引/告警：既进 tail_preview（模型一定会看工具结果），也单列 notes 字段。
        notes = [str(note).strip() for note in (extra_notes or []) if str(note or "").strip()]
        if full_output:
            notes.append(
                "完整输出已外置（本结果 ref 背后就是全量，不是头尾摘录）："
                "用 content_search(ref=<本结果 ref>, query=…) 定位行号，再 "
                "content_open(ref=…, start_line=<行号-3>, end_line=<行号+3>) 取上下文；"
                "不要为了看中段重跑命令。"
            )

        captured_body = combined_head
        ring_truncated = (
            (stdout_capture is not None and stdout_capture.truncated)
            or (stderr_capture is not None and stderr_capture.truncated)
        )
        if ring_truncated and combined_tail:
            # 三态如实：有全量 / 全量自身被上限截断 / 真的没有全量。
            # 「中段未保留」只允许在第三种情况出现。
            if full_output and full_output.get("truncated"):
                middle_note = (
                    f"…（超出捕获上限，中段未内联；完整输出已外置，但全量文件自身只写到 "
                    f"{_EXEC_FULL_OUTPUT_BYTE_LIMIT} 字节上限，其后未保留）…"
                )
            elif full_output:
                middle_note = "…（超出捕获上限，中段未内联；完整输出已外置，按本结果 ref 复开即可读到中段）…"
            else:
                middle_note = "…（超出捕获上限，中段未保留）…"
            captured_body = f"{combined_head}\n{middle_note}\n{combined_tail}".strip()
        # 整篇交付；超限只打标，切割与外置归统一信封（它负责给 ref）。
        head_preview = captured_body
        tail_preview = ""
        if notes:
            notes_block = "\n".join(notes)
            tail_preview = notes_block

        payload = {
            "status": status,
            "exit_code": exit_code,
            "head_preview": head_preview,
            "tail_preview": tail_preview,
            "stdout_truncated": bool(stdout_capture and stdout_capture.truncated),
            "stderr_truncated": bool(stderr_capture and stderr_capture.truncated),
            "stdout_captured_bytes": int(stdout_capture.total_bytes if stdout_capture is not None else len(stdout_text.encode('utf-8'))),
            "stderr_captured_bytes": int(stderr_capture.total_bytes if stderr_capture is not None else len(stderr_text.encode('utf-8'))),
            "output_truncated": len(captured_body) > _EXEC_INLINE_CHAR_LIMIT,
        }
        if full_output:
            # 投递道据此把信封 ref 指向全量 artifact，而不是把残缺载荷再存一份。
            payload["full_output_ref"] = str(full_output.get("ref") or "")
            payload["full_output_line_count"] = int(full_output.get("line_count") or 0)
            payload["full_output_char_count"] = int(full_output.get("char_count") or 0)
            payload["full_output_size_bytes"] = int(full_output.get("size_bytes") or 0)
            payload["full_output_truncated"] = bool(full_output.get("truncated"))
        if notes:
            payload["notes"] = notes
        if extra_fields:
            payload.update({str(key): value for key, value in dict(extra_fields).items()})
        if error:
            payload["error"] = error
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _is_python_inline_syntax_failure(command: str, stderr_capture: _BoundedStreamCapture | None) -> bool:
        """识别内联 `python -c`/`py -c` 的引号/转义失败，给模型「先落 .py 再跑」的就地纠正。

        仅作反应式提示（非正确性判定）：命中后在工具结果里追加 _PYTHON_INLINE_SYNTAX_NOTE。
        误报代价极低（多一条指引），故用宽松启发式：命令里出现 python/py 且带独立 `-c`，
        且 stderr 含 SyntaxError。
        """
        cmd = str(command or "")
        if re.search(r"\b(?:python|python3|py)\b", cmd) is None:
            return False
        if re.search(r"\s-c\b", cmd) is None:
            return False
        stderr_text = ""
        if stderr_capture is not None:
            stderr_text = f"{stderr_capture.head_text()}\n{stderr_capture.tail_text()}"
        return "syntaxerror" in stderr_text.lower()

    def _notify_resource_change(
        self,
        *,
        runtime: dict[str, Any],
        trigger: str,
    ) -> None:
        service = self.main_task_service
        if service is None or not hasattr(service, "refresh_changed_resources"):
            return
        session_id = str(runtime.get("session_key") or "web:shared").strip() or "web:shared"
        try:
            service.refresh_changed_resources(None, trigger=trigger, session_id=session_id)
        except Exception:
            return


