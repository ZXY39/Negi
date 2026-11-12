"""前门工具后台作业的统一生命周期。

工具调用从第一秒就是一个 job：前门最多等 ``auto_background_seconds``（默认 60s），
等不到就把 job 留在进程内继续跑，并给模型一个合成结果。只有真正后台化的调用会落
``tool_jobs`` 行；快工具不产生任何行。

本模块不做工具分档，也不改工具 schema。它只负责：

- 进程内 task 强引用与 ``wait_first``；
- 交棒后的持久化、终态计算、outbox 事件与启动恢复；
- 用户手动停止（取消 task + 取消令牌 + 立即投递 ``stopped_by_user``）。

会话级暂停/停止**不**取消后台 job（2026-11-02 操作者裁决）：作业继续跑完再报事件。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from g3ku.runtime import owned_processes
from main.protocol import now_iso

TOOL_JOB_STATUS_RUNNING = "running"
TOOL_JOB_STATUS_STOPPING = "stopping"
TOOL_JOB_STATUS_SUCCESS = "success"
TOOL_JOB_STATUS_ERROR = "error"
TOOL_JOB_STATUS_TIMEOUT = "timeout"
TOOL_JOB_STATUS_INTERRUPTED = "interrupted"
TOOL_JOB_STATUS_STOPPED_BY_USER = "stopped_by_user"

TOOL_JOB_TERMINAL_STATUSES = frozenset(
    {
        TOOL_JOB_STATUS_SUCCESS,
        TOOL_JOB_STATUS_ERROR,
        TOOL_JOB_STATUS_TIMEOUT,
        TOOL_JOB_STATUS_INTERRUPTED,
        TOOL_JOB_STATUS_STOPPED_BY_USER,
    }
)
TOOL_JOB_ACTIVE_STATUSES = (TOOL_JOB_STATUS_RUNNING, TOOL_JOB_STATUS_STOPPING)
TOOL_JOB_EVENT_REASON = "tool_job"
TOOL_JOB_RESULT_PREVIEW_CHARS = 1200
TOOL_JOB_STOP_ABANDON_GRACE_SECONDS = 5.0
TOOL_JOB_RETENTION_SECONDS = 7 * 24 * 3600

# owner_kind 分流：session 行归前门会话气泡；node 行归任务节点的后台作业管理器。
# 节点行的 session_id 列存 task_id（仅作存储键，不参与事件路由）。
TOOL_JOB_OWNER_KIND_SESSION = "session"
TOOL_JOB_OWNER_KIND_NODE = "node"

OWNED_PROCESS_JOB_PREFIX = "proc:"
OWNED_PROCESS_TOOL_NAME = "exec"
# `Popen.poll()` 非阻塞且能直接给出退出码；异步侧没有可移植的子进程退出回调
# （asyncio 的 child watcher 只认识它自己 spawn 的进程，这里是 subprocess.Popen），
# 所以退出观测用一条会自己收摊的轮询 task：没有活着的进程可看时它就退出。
OWNED_PROCESS_EXIT_POLL_SECONDS = 3.0


class ToolJobNotFoundError(RuntimeError):
    pass


class ToolJobFinishedError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolJobOutcome:
    status: str = TOOL_JOB_STATUS_SUCCESS
    result_text: str = ""
    result_preview: str = ""
    result_ref: str = ""
    log_path: str = ""
    elapsed_seconds: float = 0.0
    payload: Any = None


@dataclass
class ToolJob:
    job_id: str
    session_key: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    args_hash: str
    hint: str
    started_at: str
    started_monotonic: float
    timeout_seconds: float
    recovery_policy: str
    cancel_token: Any = None
    task: asyncio.Task[Any] | None = None
    status: str = TOOL_JOB_STATUS_RUNNING
    backgrounded: bool = False
    persisted: bool = False
    stop_requested_by_user: bool = False
    suppress_event: bool = False
    event_emitted: bool = False
    finalized: bool = False
    outcome: ToolJobOutcome | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner_kind: str = TOOL_JOB_OWNER_KIND_SESSION
    owner_task_id: str = ""
    owner_node_id: str = ""
    # exec 交棒时从 arguments 原样提取的 purpose：后台面板行优先显示它，
    # hint 只是调用摘要兜底。空串表示调用没带 purpose（非 exec 或未传）。
    purpose: str = ""
    # 范围停止（cancel_all）在取消前标记：终态事件经正常 _finalize 路径发出时
    # 也带上生命周期来源（node_paused/task_paused/…），不冒充用户手动停止。
    stop_reason_pending: str = ""


def stable_tool_args_hash(arguments: Any) -> str:
    """稳定参数指纹，用于同一会话内的 in-flight 去重。"""
    try:
        text = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        text = repr(arguments)
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def tool_job_purpose_from_arguments(arguments: Any) -> str:
    """交棒登记时从工具入参提取 purpose 原文（exec 声明该参数），没有就返回空串。

    自动交棒不经过工具的 background 分支，arguments 里的 purpose 只有这里消费：
    登记时提取一次落库，后台面板行优先显示它；hint 继续当调用摘要兜底。
    """
    if not isinstance(arguments, dict):
        return ""
    return str(arguments.get("purpose") or "").strip()


def tool_job_dedupe_key(job_id: str, status: str) -> str:
    return f"tool-job:{str(job_id or '').strip()}:{str(status or '').strip()}"


def owned_process_job_id(pid: int) -> str:
    return f"{OWNED_PROCESS_JOB_PREFIX}{int(pid)}"


def parse_owned_process_job_id(job_id: str) -> int | None:
    """`proc:<pid>` 是 exec 后台进程在气泡里的行 id；不是这个形态就返回 None。"""
    raw = str(job_id or "").strip()
    if not raw.startswith(OWNED_PROCESS_JOB_PREFIX):
        return None
    try:
        pid = int(raw[len(OWNED_PROCESS_JOB_PREFIX):])
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _elapsed_seconds_since(started_at: Any) -> float:
    try:
        started = datetime.fromisoformat(str(started_at or "").strip())
    except (TypeError, ValueError):
        return 0.0
    now = datetime.now(started.tzinfo) if started.tzinfo is not None else datetime.now()
    return round(max(0.0, (now - started).total_seconds()), 1)


def owned_process_items(session_key: str) -> list[dict[str, Any]]:
    """会话 scope 下 `exec background=true` 起的活进程，映射成气泡行。

    与 tool job 行共用 `job_id / tool_name / status / started_at / elapsed_seconds /
    hint`；`kind=process` 区分来源，`proc:` 前缀让停止接口分流回归属表。
    `list_owned` 读时顺手清掉已退出/身份不符的条目，气泡因此能自愈。
    """
    scope = owned_processes.scope_key(session_key=session_key)
    items: list[dict[str, Any]] = []
    for entry in owned_processes.list_owned(scope):
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        started_at = str(entry.get("started_at") or "")
        items.append(
            {
                "kind": "process",
                "job_id": owned_process_job_id(pid),
                "pid": pid,
                "tool_name": OWNED_PROCESS_TOOL_NAME,
                "status": TOOL_JOB_STATUS_RUNNING,
                "started_at": started_at,
                "elapsed_seconds": _elapsed_seconds_since(started_at),
                "turn_id": "",
                "tool_call_id": "",
                "hint": str(entry.get("cmd") or ""),
                "purpose": str(entry.get("label") or ""),
                "log_path": str(entry.get("log_path") or ""),
            }
        )
    items.sort(key=lambda item: (str(item.get("started_at") or ""), int(item.get("pid") or 0)))
    return items


def owned_process_event_identity(entry: dict[str, Any]) -> str:
    """事件去重用的进程身份：pid 会被系统复用，只拿 pid 会让后一个进程的事件被当成重复丢掉。"""
    pid = int(entry.get("pid") or 0)
    try:
        create_time = float(entry.get("create_time") or 0.0)
    except (TypeError, ValueError):
        create_time = 0.0
    if create_time > 0:
        return f"proc:{pid}@{create_time:.3f}"
    return f"proc:{pid}@{str(entry.get('started_at') or '')}"


def build_owned_process_event_payload(
    entry: dict[str, Any],
    *,
    session_id: str,
    finished_at: str,
    status: str,
    result_preview: str = "",
    exit_code: int | None = None,
    stopped_by: str = "",
) -> dict[str, Any]:
    """exec 后台进程的终态心跳事件正文；用户停止与自然退出共用这一份形状。

    复用 tool_job 车道（reason/status 集合不变），`kind=process` 让渲染层按
    “后台进程”而不是“后台工具作业”措辞，并带上 pid/cmd/log_path 供模型核对。
    `exit_code` 只有自然退出拿得到；被终止时进程是被外部杀掉的，退出码不是事实来源。
    """
    started_at = str(entry.get("started_at") or "")
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return {
        "event_reason": TOOL_JOB_EVENT_REASON,
        "reason": TOOL_JOB_EVENT_REASON,
        "kind": "process",
        "session_id": str(session_id or "").strip(),
        "job_id": owned_process_job_id(pid),
        "dedupe_key": tool_job_dedupe_key(owned_process_event_identity(entry), status),
        "pid": pid,
        "tool_name": OWNED_PROCESS_TOOL_NAME,
        "status": str(status or "").strip(),
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": _elapsed_seconds_since(started_at),
        "timeout_seconds": 0.0,
        "turn_id": "",
        "tool_call_id": "",
        "result_preview": _clip_preview(result_preview),
        "result_ref": "",
        "log_path": str(entry.get("log_path") or ""),
        "recovery_policy": "model_decide",
        "stopped_by": str(stopped_by or ""),
        "exit_code": exit_code,
        "cmd": str(entry.get("cmd") or ""),
        "purpose": str(entry.get("label") or ""),
    }


def _clip_preview(text: Any, limit: int = TOOL_JOB_RESULT_PREVIEW_CHARS) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 1)]}..."


def build_tool_job_event_payload(
    job: ToolJob,
    *,
    status: str,
    finished_at: str,
    elapsed_seconds: float,
    outcome: ToolJobOutcome | None = None,
    stopped_by: str = "",
) -> dict[str, Any]:
    preview_source = ""
    result_ref = ""
    log_path = ""
    if outcome is not None:
        preview_source = outcome.result_preview or outcome.result_text
        result_ref = outcome.result_ref
        log_path = outcome.log_path
    payload: dict[str, Any] = {
        "event_reason": TOOL_JOB_EVENT_REASON,
        "reason": TOOL_JOB_EVENT_REASON,
        "kind": "tool_job",
        "session_id": job.session_key,
        "job_id": job.job_id,
        "tool_name": job.tool_name,
        "status": status,
        "started_at": job.started_at,
        "finished_at": finished_at,
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds or 0.0)), 1),
        "timeout_seconds": round(max(0.0, float(job.timeout_seconds or 0.0)), 1),
        "turn_id": job.turn_id,
        "tool_call_id": job.tool_call_id,
        "result_preview": _clip_preview(preview_source),
        "result_ref": str(result_ref or ""),
        "log_path": str(log_path or ""),
        "recovery_policy": str(job.recovery_policy or "model_decide"),
        "stopped_by": str(stopped_by or ""),
    }
    if job.owner_kind == TOOL_JOB_OWNER_KIND_NODE:
        payload["owner_kind"] = TOOL_JOB_OWNER_KIND_NODE
        payload["task_id"] = job.owner_task_id
        payload["node_id"] = job.owner_node_id
        # 节点行的事件身份带 job_id：穿过"消息已消费"状态仍然有效（外层
        # deliver_node_message 按 external_event_id 全状态去重）。
        payload["external_event_id"] = tool_job_dedupe_key(job.job_id, status)
    return payload


def owned_process_task_items(task_id: str) -> list[dict[str, Any]]:
    """任务 scope 下 `exec background=true` 起的活进程，映射成任务后台面板行。

    与会话侧 `owned_process_items` 同形状，额外带 `node_id` 供"定位到节点"。
    `list_owned` 读时顺手清掉已退出/身份不符的条目，面板因此能自愈。
    """
    scope = owned_processes.scope_key(session_key="", task_id=str(task_id or "").strip())
    items: list[dict[str, Any]] = []
    for entry in owned_processes.list_owned(scope):
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        started_at = str(entry.get("started_at") or "")
        items.append(
            {
                "kind": "process",
                "job_id": owned_process_job_id(pid),
                "pid": pid,
                "tool_name": OWNED_PROCESS_TOOL_NAME,
                "status": TOOL_JOB_STATUS_RUNNING,
                "started_at": started_at,
                "elapsed_seconds": _elapsed_seconds_since(started_at),
                "turn_id": "",
                "tool_call_id": "",
                "hint": str(entry.get("cmd") or ""),
                "purpose": str(entry.get("label") or ""),
                "log_path": str(entry.get("log_path") or ""),
                "node_id": str(entry.get("node_id") or ""),
            }
        )
    items.sort(key=lambda item: (str(item.get("started_at") or ""), int(item.get("pid") or 0)))
    return items


class ToolJobRunner:
    """一个会话内的后台作业索引。job task 不注册进会话 task 集合。

    会话暂停/停止只取消会话 task，不会碰到这里；只有用户点停止、会话删除或进程
    退出才会取消 job task。
    """

    def __init__(
        self,
        *,
        session_key: str,
        store_getter: Callable[[], Any],
        heartbeat_getter: Callable[[], Any] | None = None,
        changed_callback: Callable[[list[dict[str, Any]]], Any] | None = None,
        finished_callback: Callable[[dict[str, Any]], Any] | None = None,
        stop_abandon_grace_seconds: float = TOOL_JOB_STOP_ABANDON_GRACE_SECONDS,
        owner_kind: str = TOOL_JOB_OWNER_KIND_SESSION,
        owner_task_id: str = "",
        owner_node_id: str = "",
        terminal_sink: Callable[[dict[str, Any]], Any] | None = None,
        control_signals: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._session_key = str(session_key or "").strip()
        self._store_getter = store_getter
        self._heartbeat_getter = heartbeat_getter or (lambda: None)
        self._changed_callback = changed_callback
        self._finished_callback = finished_callback
        self._stop_abandon_grace_seconds = max(0.0, float(stop_abandon_grace_seconds or 0.0))
        self._owner_kind = str(owner_kind or TOOL_JOB_OWNER_KIND_SESSION).strip() or TOOL_JOB_OWNER_KIND_SESSION
        self._owner_task_id = str(owner_task_id or "").strip()
        self._owner_node_id = str(owner_node_id or "").strip()
        # 节点终态投递边界：收到事件 payload，完成节点信箱投递后返回真值。
        # 会话 runner 不注入它，事件照旧走 heartbeat 车道。
        self._terminal_sink = terminal_sink
        # 冻结类控制信号（任务/节点暂停）：作业任务里原样上抛，绝不转工具错误。
        self._control_signals = tuple(control_signals or ())
        self._jobs: dict[str, ToolJob] = {}
        self._inflight: dict[tuple[str, str], str] = {}
        self._finalize_tasks: set[asyncio.Task[Any]] = set()
        self._owned_process_tasks: set[asyncio.Task[Any]] = set()

    def _store(self) -> Any:
        try:
            return self._store_getter()
        except Exception:
            return None

    def _heartbeat(self) -> Any:
        try:
            return self._heartbeat_getter()
        except Exception:
            return None

    def start_job(
        self,
        *,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        args_hash: str,
        hint: str,
        purpose: str = "",
        run: Callable[[], Awaitable[ToolJobOutcome]],
        timeout_seconds: float,
        recovery_policy: str = "model_decide",
        cancel_token: Any = None,
    ) -> tuple[ToolJob, bool]:
        """启动 job；同一 ``(tool, args_hash)`` 仍在跑时复用已有 job。

        返回 ``(job, reused)``。``run`` 必须在当前 asyncio context 里创建 task，
        这样工具执行读到的 ContextVar（runtime context / read roots）不会丢。
        """
        normalized_tool_name = str(tool_name or "tool").strip() or "tool"
        normalized_hash = str(args_hash or "").strip()
        dedupe_key = (normalized_tool_name, normalized_hash)
        existing_id = self._inflight.get(dedupe_key)
        if existing_id:
            existing = self._jobs.get(existing_id)
            if existing is not None and existing.status in TOOL_JOB_ACTIVE_STATUSES:
                return existing, True
            self._inflight.pop(dedupe_key, None)
        job = ToolJob(
            job_id=f"tooljob:{uuid.uuid4().hex}",
            session_key=self._session_key,
            turn_id=str(turn_id or "").strip(),
            tool_call_id=str(tool_call_id or "").strip(),
            tool_name=normalized_tool_name,
            args_hash=normalized_hash,
            hint=str(hint or "").strip(),
            purpose=str(purpose or "").strip(),
            started_at=now_iso(),
            started_monotonic=time.monotonic(),
            timeout_seconds=max(0.0, float(timeout_seconds or 0.0)),
            recovery_policy=str(recovery_policy or "model_decide").strip() or "model_decide",
            cancel_token=cancel_token,
            owner_kind=self._owner_kind,
            owner_task_id=self._owner_task_id,
            owner_node_id=self._owner_node_id,
        )
        job.task = asyncio.create_task(self._run_job(job, run), name=f"tool-job:{normalized_tool_name}")
        self._jobs[job.job_id] = job
        self._inflight[dedupe_key] = job.job_id
        job.task.add_done_callback(lambda _task, current=job: self._on_task_done(current))
        return job, False

    async def _run_job(self, job: ToolJob, run: Callable[[], Awaitable[ToolJobOutcome]]) -> ToolJobOutcome:
        try:
            outcome = await run()
            if not isinstance(outcome, ToolJobOutcome):
                outcome = ToolJobOutcome(result_text=str(outcome or ""))
            return outcome
        except asyncio.CancelledError:
            raise
        except self._control_signals:
            # 冻结类控制信号（节点/任务暂停）原样上抛：转成 error outcome 会消费
            # 前台等待里的暂停语义，摧毁同 id 幂等重放通道。
            raise
        except Exception as exc:
            logger.exception("tool job {} failed: {}", job.job_id, exc)
            text = f"Error executing {job.tool_name}: {exc}"
            return ToolJobOutcome(status=TOOL_JOB_STATUS_ERROR, result_text=text, result_preview=text)

    async def wait_first(self, job: ToolJob, timeout_seconds: float) -> tuple[bool, ToolJobOutcome | None]:
        """等 job 最多 ``timeout_seconds``。

        返回 ``(True, outcome)`` 表示在前台窗口内完成；``(False, None)`` 表示已交棒。
        前台等待被取消（会话暂停/停止）时取消 job task 并向上抛，保持现有回合语义。
        """
        task = job.task
        if task is None:
            return True, None
        wait_seconds = max(0.0, float(timeout_seconds or 0.0))
        try:
            if wait_seconds > 0:
                done, _pending = await asyncio.wait({task}, timeout=wait_seconds)
            else:
                done = set()
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            raise
        if done or task.done():
            return True, task.result()
        token = job.cancel_token
        if token is not None and getattr(token, "is_cancelled", lambda: False)():
            # 暂停与交棒撞在一起：工具已经不再属于前台，但也不该留下一个已被取消的 job。
            try:
                outcome = await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                outcome = None
            return True, outcome
        self._promote(job)
        return False, None

    def _promote(self, job: ToolJob) -> None:
        if job.backgrounded:
            return
        job.backgrounded = True
        job.status = TOOL_JOB_STATUS_RUNNING
        store = self._store()
        if store is None:
            logger.warning(
                "tool job {} backgrounded without a durable store (session={})",
                job.job_id,
                job.session_key,
            )
            self._notify_changed()
            return
        try:
            store.put_tool_job(
                job_id=job.job_id,
                session_id=job.owner_task_id if job.owner_kind == TOOL_JOB_OWNER_KIND_NODE else job.session_key,
                turn_id=job.turn_id,
                tool_call_id=job.tool_call_id,
                tool_name=job.tool_name,
                args_hash=job.args_hash,
                hint=job.hint,
                purpose=job.purpose,
                status=TOOL_JOB_STATUS_RUNNING,
                started_at=job.started_at,
                updated_at=now_iso(),
                timeout_seconds=job.timeout_seconds,
                owner_pid=os.getpid(),
                recovery_policy=job.recovery_policy,
                payload={"hint": job.hint},
                owner_kind=job.owner_kind,
                task_id=job.owner_task_id,
                node_id=job.owner_node_id,
            )
            job.persisted = True
        except Exception:
            logger.exception("failed to persist tool job {} at background handoff", job.job_id)
        self._notify_changed()

    def _on_task_done(self, job: ToolJob) -> None:
        if not job.backgrounded:
            self._drop_job(job)
            return
        try:
            task = asyncio.create_task(self._finalize(job), name=f"tool-job-finalize:{job.job_id}")
        except RuntimeError:
            return
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    def _resolve_terminal(self, job: ToolJob) -> tuple[str, ToolJobOutcome | None, str]:
        task = job.task
        outcome = job.outcome
        if task is not None and task.done() and not task.cancelled():
            try:
                exc = task.exception()
            except Exception:
                exc = None
            if exc is not None:
                return TOOL_JOB_STATUS_ERROR, outcome, str(exc)
        if job.stop_requested_by_user:
            return TOOL_JOB_STATUS_STOPPED_BY_USER, outcome, ""
        if task is not None and task.cancelled():
            return TOOL_JOB_STATUS_INTERRUPTED, outcome, "cancelled"
        if outcome is None:
            return TOOL_JOB_STATUS_ERROR, None, "no outcome"
        status = str(outcome.status or "").strip().lower()
        if status not in TOOL_JOB_TERMINAL_STATUSES:
            status = TOOL_JOB_STATUS_ERROR
        return status, outcome, ""

    async def _finalize(self, job: ToolJob) -> None:
        async with job.lock:
            if job.finalized:
                return
            job.finalized = True
            task = job.task
            if task is not None and task.done() and not task.cancelled():
                try:
                    job.outcome = task.result()
                except Exception:
                    job.outcome = None
            try:
                exc = task.exception() if task is not None and task.done() and not task.cancelled() else None
            except Exception:
                exc = None
            status, outcome, error_text = self._resolve_terminal(job)
            if exc is not None and not outcome:
                outcome = ToolJobOutcome(
                    status=TOOL_JOB_STATUS_ERROR,
                    result_text=f"Error executing {job.tool_name}: {exc}",
                    result_preview=str(exc),
                )
            job.status = status
            finished_at = now_iso()
            elapsed = max(0.0, time.monotonic() - job.started_monotonic)
            if not job.event_emitted and not job.suppress_event:
                self._emit_terminal_event(
                    job,
                    status=status,
                    outcome=outcome,
                    finished_at=finished_at,
                    elapsed_seconds=elapsed,
                    stopped_by=(
                        "user"
                        if status == TOOL_JOB_STATUS_STOPPED_BY_USER
                        else str(getattr(job, "stop_reason_pending", "") or "")
                    ),
                    error_text=error_text,
                )
            self._persist_terminal(
                job,
                status=status,
                outcome=outcome,
                finished_at=finished_at,
                error_text=error_text,
            )
            self._drop_job(job)
            if not job.suppress_event:
                self._notify_finished(
                    self._finished_item(
                        job,
                        status=status,
                        finished_at=finished_at,
                        elapsed_seconds=elapsed,
                    )
                )
            self._notify_changed()

    def _persist_terminal(
        self,
        job: ToolJob,
        *,
        status: str,
        outcome: ToolJobOutcome | None,
        finished_at: str,
        error_text: str = "",
    ) -> None:
        if not job.persisted:
            return
        store = self._store()
        if store is None:
            return
        preview = ""
        result_ref = ""
        log_path = ""
        if outcome is not None:
            preview = outcome.result_preview or outcome.result_text
            result_ref = outcome.result_ref
            log_path = outcome.log_path
        if not preview and error_text:
            preview = error_text
        try:
            store.update_tool_job(
                job.job_id,
                status=status,
                finished_at=finished_at,
                updated_at=finished_at,
                result_preview=_clip_preview(preview),
                result_ref=result_ref,
                log_path=log_path,
            )
        except Exception:
            logger.exception("failed to persist terminal state for tool job {}", job.job_id)

    def _emit_terminal_event(
        self,
        job: ToolJob,
        *,
        status: str,
        outcome: ToolJobOutcome | None,
        finished_at: str,
        elapsed_seconds: float,
        stopped_by: str = "",
        error_text: str = "",
    ) -> None:
        job.event_emitted = True
        payload = build_tool_job_event_payload(
            job,
            status=status,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            outcome=outcome,
            stopped_by=stopped_by,
        )
        if not payload.get("result_preview") and error_text:
            payload["result_preview"] = _clip_preview(error_text)
        dedupe_key = tool_job_dedupe_key(job.job_id, status)
        store = self._store()
        if job.owner_kind == TOOL_JOB_OWNER_KIND_NODE:
            # 节点行：终态先落 outbox（耐久、幂等），再经注入的 sink 投节点信箱；
            # 成功后标记 delivered。写序固定「先 outbox、后投递」，崩溃重启按
            # pending outbox 补投，不重造事件。
            storage_session = job.owner_task_id
            if store is not None and job.persisted:
                try:
                    store.put_tool_job_outbox(
                        dedupe_key=dedupe_key,
                        job_id=job.job_id,
                        session_id=storage_session,
                        created_at=finished_at,
                        payload=payload,
                        owner_kind=TOOL_JOB_OWNER_KIND_NODE,
                    )
                except Exception:
                    logger.exception("failed to persist node tool job outbox row {}", dedupe_key)
            sink = self._terminal_sink
            if callable(sink):
                try:
                    result = sink(dict(payload))
                    if inspect.isawaitable(result):
                        try:
                            task = asyncio.create_task(result, name=f"tool-job-sink:{dedupe_key}")
                        except RuntimeError:
                            task = None
                        if task is not None:
                            self._finalize_tasks.add(task)
                            task.add_done_callback(self._finalize_tasks.discard)
                            return
                    if result:
                        if store is not None:
                            try:
                                store.mark_tool_job_outbox_delivered(dedupe_key, delivered_at=now_iso())
                            except Exception:
                                logger.exception("failed to mark node tool job outbox delivered {}", dedupe_key)
                except Exception:
                    logger.exception("node tool job terminal sink failed {}", dedupe_key)
            return
        if store is not None and job.persisted:
            try:
                store.put_tool_job_outbox(
                    dedupe_key=dedupe_key,
                    job_id=job.job_id,
                    session_id=job.session_key,
                    created_at=finished_at,
                    payload=payload,
                )
            except Exception:
                logger.exception("failed to persist tool job outbox row {}", dedupe_key)
        heartbeat = self._heartbeat()
        enqueue = getattr(heartbeat, "enqueue_tool_job", None) if heartbeat is not None else None
        if callable(enqueue):
            try:
                enqueue(payload)
                if store is not None and job.persisted:
                    store.mark_tool_job_outbox_enqueue_result(
                        dedupe_key,
                        accepted=True,
                        rejected_reason="",
                        updated_at=finished_at,
                    )
            except Exception:
                logger.exception("failed to enqueue tool job heartbeat event {}", dedupe_key)

    def _drop_job(self, job: ToolJob) -> None:
        self._jobs.pop(job.job_id, None)
        dedupe_key = (job.tool_name, job.args_hash)
        if self._inflight.get(dedupe_key) == job.job_id:
            self._inflight.pop(dedupe_key, None)

    @staticmethod
    def _finished_item(
        job: ToolJob,
        *,
        status: str,
        finished_at: str,
        elapsed_seconds: float,
    ) -> dict[str, Any]:
        return {
            "kind": "tool_job",
            "job_id": job.job_id,
            "session_id": job.session_key,
            "turn_id": job.turn_id,
            "tool_call_id": job.tool_call_id,
            "tool_name": job.tool_name,
            "status": status,
            "started_at": job.started_at,
            "finished_at": finished_at,
            "elapsed_seconds": round(max(0.0, float(elapsed_seconds or 0.0)), 1),
        }

    def _notify_finished(self, item: dict[str, Any]) -> None:
        callback = self._finished_callback
        if callback is None:
            return
        try:
            result = callback(dict(item))
            if inspect.isawaitable(result):
                try:
                    asyncio.create_task(result)
                except RuntimeError:
                    pass
        except Exception:
            logger.debug("tool job finished notification failed", exc_info=True)

    def _notify_changed(self) -> None:
        callback = self._changed_callback
        if callback is None:
            return
        try:
            result = callback(self.list_jobs(self._session_key))
            if inspect.isawaitable(result):
                try:
                    asyncio.create_task(result)
                except RuntimeError:
                    pass
        except Exception:
            logger.debug("tool job change notification failed", exc_info=True)

    def list_jobs(self, session_key: str | None = None) -> list[dict[str, Any]]:
        normalized_session = str(session_key or "").strip()
        now_mono = time.monotonic()
        items: list[dict[str, Any]] = []
        for job in list(self._jobs.values()):
            if not job.backgrounded:
                continue
            if job.status not in TOOL_JOB_ACTIVE_STATUSES:
                continue
            if normalized_session and job.session_key != normalized_session:
                continue
            items.append(
                {
                    "kind": "tool_job",
                    "job_id": job.job_id,
                    "tool_name": job.tool_name,
                    "status": job.status,
                    "started_at": job.started_at,
                    "elapsed_seconds": round(max(0.0, now_mono - job.started_monotonic), 1),
                    "turn_id": job.turn_id,
                    "tool_call_id": job.tool_call_id,
                    "hint": job.hint,
                    "purpose": job.purpose,
                }
            )
        items.sort(key=lambda item: (str(item.get("started_at") or ""), str(item.get("job_id") or "")))
        return items

    async def stop_job(self, job_id: str, *, reason: str = "user_manual_stop") -> dict[str, Any]:
        normalized_job_id = str(job_id or "").strip()
        job = self._jobs.get(normalized_job_id)
        if job is None:
            store = self._store()
            row = store.get_tool_job(normalized_job_id) if store is not None else None
            if row is None:
                raise ToolJobNotFoundError(normalized_job_id)
            if str(row.get("status") or "") in TOOL_JOB_TERMINAL_STATUSES:
                raise ToolJobFinishedError(normalized_job_id)
            raise ToolJobFinishedError(normalized_job_id)
        async with job.lock:
            if job.finalized or job.stop_requested_by_user:
                if job.stop_requested_by_user:
                    return {"job_id": job.job_id, "status": TOOL_JOB_STATUS_STOPPING}
                raise ToolJobFinishedError(job.job_id)
            if job.task is not None and job.task.done():
                raise ToolJobFinishedError(job.job_id)
            job.stop_requested_by_user = True
            job.status = TOOL_JOB_STATUS_STOPPING
            if job.persisted:
                store = self._store()
                if store is not None:
                    try:
                        store.update_tool_job(
                            job.job_id,
                            status=TOOL_JOB_STATUS_STOPPING,
                            stop_requested_by_user=True,
                            updated_at=now_iso(),
                        )
                    except Exception:
                        logger.exception("failed to persist stop request for tool job {}", job.job_id)
            self._emit_terminal_event(
                job,
                status=TOOL_JOB_STATUS_STOPPED_BY_USER,
                outcome=None,
                finished_at=now_iso(),
                elapsed_seconds=max(0.0, time.monotonic() - job.started_monotonic),
                stopped_by="user",
            )
            token = job.cancel_token
            if token is not None and hasattr(token, "cancel"):
                try:
                    token.cancel(reason=reason)
                except Exception:
                    pass
            if job.task is not None and not job.task.done():
                job.task.cancel()
        self._notify_changed()
        self._schedule_stop_abandon(job)
        return {"job_id": job.job_id, "status": TOOL_JOB_STATUS_STOPPING}

    def _schedule_stop_abandon(self, job: ToolJob) -> None:
        try:
            task = asyncio.create_task(
                self._abandon_if_needed(job),
                name=f"tool-job-stop-abandon:{job.job_id}",
            )
        except RuntimeError:
            return
        self._finalize_tasks.add(task)
        task.add_done_callback(self._finalize_tasks.discard)

    async def _abandon_if_needed(self, job: ToolJob) -> None:
        await asyncio.sleep(self._stop_abandon_grace_seconds)
        async with job.lock:
            if job.finalized:
                return
            job.finalized = True
            job.status = TOOL_JOB_STATUS_STOPPED_BY_USER
            logger.warning(
                "tool job {} ignored cancellation for {:.1f}s; abandoning it after the user stop event",
                job.job_id,
                self._stop_abandon_grace_seconds,
            )
            finished_at = now_iso()
            self._persist_terminal(
                job,
                status=TOOL_JOB_STATUS_STOPPED_BY_USER,
                outcome=None,
                finished_at=finished_at,
                error_text="stopped by user; task did not unwind in the grace window",
            )
            self._drop_job(job)
            self._notify_finished(
                self._finished_item(
                    job,
                    status=TOOL_JOB_STATUS_STOPPED_BY_USER,
                    finished_at=finished_at,
                    elapsed_seconds=max(0.0, time.monotonic() - job.started_monotonic),
                )
            )
            self._notify_changed()

    async def cancel_all(
        self,
        *,
        reason: str = "session_deleted",
        suppress_event: bool = True,
        stopped_by: str = "",
    ) -> int:
        """取消本 runner 的全部在飞 job（会话删除/范围生命周期停止用）。

        `suppress_event=False`（节点范围停止传 False）时按正常终态写序结案：
        先落 outbox/事件、再收尾持久行，节点信箱经终态 sink 拿到结果。
        `stopped_by` 写进事件 payload，区分生命周期来源，不冒充用户手动停止。
        """
        self._cancel_owned_process_watches()
        jobs = list(self._jobs.values())
        if not jobs:
            return 0
        for job in jobs:
            job.suppress_event = bool(suppress_event)
            # 取消前先标记停止来源：终态若经正常 _finalize 路径发出（任务取消
            # 先于本方法收尾），payload.stopped_by 仍带生命周期原因。
            job.stop_reason_pending = str(stopped_by or reason or "")
            token = job.cancel_token
            if token is not None and hasattr(token, "cancel"):
                try:
                    token.cancel(reason=reason)
                except Exception:
                    pass
            if job.task is not None and not job.task.done():
                job.task.cancel()
        tasks = [job.task for job in jobs if job.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for job in jobs:
            if job.finalized:
                continue
            job.finalized = True
            job.status = TOOL_JOB_STATUS_INTERRUPTED
            finished_at = now_iso()
            if not job.event_emitted and not job.suppress_event:
                self._emit_terminal_event(
                    job,
                    status=TOOL_JOB_STATUS_INTERRUPTED,
                    outcome=None,
                    finished_at=finished_at,
                    elapsed_seconds=max(0.0, time.monotonic() - job.started_monotonic),
                    stopped_by=stopped_by or reason,
                    error_text=reason,
                )
            self._persist_terminal(
                job,
                status=TOOL_JOB_STATUS_INTERRUPTED,
                outcome=None,
                finished_at=finished_at,
                error_text=reason,
            )
            self._drop_job(job)
            if not job.suppress_event:
                self._notify_finished(
                    self._finished_item(
                        job,
                        status=TOOL_JOB_STATUS_INTERRUPTED,
                        finished_at=finished_at,
                        elapsed_seconds=max(0.0, time.monotonic() - job.started_monotonic),
                    )
                )
        self._notify_changed()
        return len(jobs)

    def adopt_owned_process(self, *, entry: dict[str, Any], process: Any) -> None:
        """盯一个 `exec background=true` 起的进程，它自己跑完时也给模型投一条终态事件。

        交棒给前台看门狗的调用由 job 收尾并投事件；这条登记道 detach 之后没有任何东西
        在等它——模型只拿到 pid + log_path，进程跑完把输出留在日志里，没人接着处理。
        没有可 poll 的句柄时不接管：宁可不报，也不凭“进程不在了”猜它是成功还是失败。
        """
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            return
        scope = str(entry.get("scope") or "").strip()
        if pid <= 0 or not scope:
            return
        try:
            task = asyncio.create_task(
                self._watch_owned_process_exit(entry=entry, poll=poll, scope=scope, pid=pid),
                name=f"owned-process-exit:{pid}",
            )
        except RuntimeError:
            return
        self._owned_process_tasks.add(task)
        task.add_done_callback(self._owned_process_tasks.discard)

    async def _watch_owned_process_exit(
        self,
        *,
        entry: dict[str, Any],
        poll: Callable[[], Any],
        scope: str,
        pid: int,
    ) -> None:
        while True:
            try:
                exit_code = poll()
            except Exception:
                logger.debug("owned process {} watch lost its handle", pid, exc_info=True)
                return
            if exit_code is not None:
                break
            await asyncio.sleep(OWNED_PROCESS_EXIT_POLL_SECONDS)
        try:
            code = int(exit_code)
        except (TypeError, ValueError):
            code = 0
        # 条目还在盘上＝这个进程是自己退的。被 process_kill、后台气泡停止或 scope 回收
        # 摘走时，那条道已经把日志交回给模型或已经投过事件，这里不能再报一次。
        if owned_processes.find(scope, pid) is None:
            self._notify_changed()
            return
        owned_processes.drop(scope, pid)
        payload = build_owned_process_event_payload(
            entry,
            session_id=str(entry.get("session_key") or self._session_key),
            finished_at=now_iso(),
            status=TOOL_JOB_STATUS_SUCCESS if code == 0 else TOOL_JOB_STATUS_ERROR,
            result_preview=owned_processes.read_log_tail(entry),
            exit_code=code,
        )
        heartbeat = self._heartbeat()
        enqueue = getattr(heartbeat, "enqueue_tool_job", None) if heartbeat is not None else None
        if callable(enqueue):
            try:
                enqueue(payload)
            except Exception:
                logger.exception("failed to enqueue owned process exit event {}", payload["job_id"])
        self._notify_changed()

    def _cancel_owned_process_watches(self) -> None:
        for task in list(self._owned_process_tasks):
            if not task.done():
                task.cancel()


def recover_tool_jobs(store: Any, *, now: str | None = None) -> dict[str, int]:
    """Web 启动时把上一进程遗留的 running/stopping 行收成终态并补事件。

    在 ``start_web_session_heartbeat(replay_pending_outbox=True)`` 之前调用：本函数
    只落 outbox 行，心跳启动后的 replay 负责真正投递。
    """
    if store is None:
        return {"recovered": 0, "events": 0}
    recovered_at = str(now or now_iso())
    # owner_kind 分流：节点行由节点后台管理器自己的恢复口处理，不冒充会话事件。
    active_rows = store.list_tool_jobs(
        statuses=TOOL_JOB_ACTIVE_STATUSES, limit=1000, owner_kind=TOOL_JOB_OWNER_KIND_SESSION
    )
    recovered = 0
    events = 0
    for row in active_rows:
        job_id = str(row.get("job_id") or "").strip()
        if not job_id:
            continue
        previous_status = str(row.get("status") or "").strip()
        stop_requested = bool(row.get("stop_requested_by_user"))
        existing = _find_existing_job_outbox(store, job_id)
        if existing is not None:
            # 终止写序是「先 outbox，后行状态」：事故可能停在两者之间，事件已经在
            # pending 里，重启只补行状态，不能再造第二条。
            status = str(existing.get("payload", {}).get("status") or "").strip()
            if status not in TOOL_JOB_TERMINAL_STATUSES:
                status = TOOL_JOB_STATUS_STOPPED_BY_USER if stop_requested else TOOL_JOB_STATUS_INTERRUPTED
            store.update_tool_job(
                job_id,
                status=status,
                finished_at=recovered_at,
                updated_at=recovered_at,
                result_preview=str(existing.get("payload", {}).get("result_preview") or ""),
                result_ref=str(existing.get("payload", {}).get("result_ref") or ""),
            )
            recovered += 1
            continue
        if previous_status == TOOL_JOB_STATUS_STOPPING:
            status = TOOL_JOB_STATUS_STOPPED_BY_USER if stop_requested else TOOL_JOB_STATUS_INTERRUPTED
        else:
            status = TOOL_JOB_STATUS_INTERRUPTED
        recover_text = (
            "The project process restarted while this background tool job was running. "
            "It may have stopped or may still be running; inspect the result/log before retrying."
        )
        if status == TOOL_JOB_STATUS_STOPPED_BY_USER:
            recover_text = "The user manually stopped this background tool job (用户手动停止)."
        store.update_tool_job(
            job_id,
            status=status,
            finished_at=recovered_at,
            updated_at=recovered_at,
            result_preview=recover_text,
        )
        payload = {
            "event_reason": TOOL_JOB_EVENT_REASON,
            "reason": TOOL_JOB_EVENT_REASON,
            "session_id": str(row.get("session_id") or ""),
            "job_id": job_id,
            "tool_name": str(row.get("tool_name") or "tool"),
            "status": status,
            "started_at": str(row.get("started_at") or ""),
            "finished_at": recovered_at,
            "elapsed_seconds": 0.0,
            "timeout_seconds": float(row.get("timeout_seconds") or 0.0),
            "turn_id": str(row.get("turn_id") or ""),
            "tool_call_id": str(row.get("tool_call_id") or ""),
            "result_preview": recover_text,
            "result_ref": "",
            "log_path": str(row.get("log_path") or ""),
            "recovery_policy": str(row.get("recovery_policy") or "model_decide"),
            "stopped_by": "user" if status == TOOL_JOB_STATUS_STOPPED_BY_USER else "",
        }
        dedupe_key = tool_job_dedupe_key(job_id, status)
        store.put_tool_job_outbox(
            dedupe_key=dedupe_key,
            job_id=job_id,
            session_id=str(row.get("session_id") or ""),
            created_at=recovered_at,
            payload=payload,
        )
        recovered += 1
        events += 1

    # 终止写序是「先 outbox，后行状态」：崩溃/写失败可能留下终态行却没有
    # outbox。扫最近保留窗口内的终态行，缺事件就补一条；已有任一状态的事件
    # 则跳过，避免重复。
    cutoff = _retention_cutoff_iso()
    terminal_rows = store.list_tool_jobs(
        statuses=tuple(TOOL_JOB_TERMINAL_STATUSES), limit=500, owner_kind=TOOL_JOB_OWNER_KIND_SESSION
    )
    for row in terminal_rows:
        updated_at = str(row.get("updated_at") or "")
        if updated_at and updated_at < cutoff:
            continue
        job_id = str(row.get("job_id") or "").strip()
        if not job_id or _find_existing_job_outbox(store, job_id) is not None:
            continue
        status = str(row.get("status") or "").strip()
        if status not in TOOL_JOB_TERMINAL_STATUSES:
            continue
        payload = {
            "event_reason": TOOL_JOB_EVENT_REASON,
            "reason": TOOL_JOB_EVENT_REASON,
            "session_id": str(row.get("session_id") or ""),
            "job_id": job_id,
            "tool_name": str(row.get("tool_name") or "tool"),
            "status": status,
            "started_at": str(row.get("started_at") or ""),
            "finished_at": str(row.get("finished_at") or recovered_at),
            "elapsed_seconds": 0.0,
            "timeout_seconds": float(row.get("timeout_seconds") or 0.0),
            "turn_id": str(row.get("turn_id") or ""),
            "tool_call_id": str(row.get("tool_call_id") or ""),
            "result_preview": str(row.get("result_preview") or ""),
            "result_ref": str(row.get("result_ref") or ""),
            "log_path": str(row.get("log_path") or ""),
            "recovery_policy": str(row.get("recovery_policy") or "model_decide"),
            "stopped_by": "user" if status == TOOL_JOB_STATUS_STOPPED_BY_USER else "",
        }
        dedupe_key = tool_job_dedupe_key(job_id, status)
        store.put_tool_job_outbox(
            dedupe_key=dedupe_key,
            job_id=job_id,
            session_id=str(row.get("session_id") or ""),
            created_at=recovered_at,
            payload=payload,
        )
        events += 1
    return {"recovered": recovered, "events": events}


def _retention_cutoff_iso(*, now_epoch: float | None = None) -> str:
    cutoff_epoch = float(now_epoch if now_epoch is not None else time.time()) - TOOL_JOB_RETENTION_SECONDS
    return datetime.fromtimestamp(cutoff_epoch).astimezone().replace(microsecond=0).isoformat()


def _find_existing_job_outbox(store: Any, job_id: str) -> dict[str, Any] | None:
    getter = getattr(store, "get_tool_job_outbox", None)
    if not callable(getter):
        return None
    for status in (
        TOOL_JOB_STATUS_SUCCESS,
        TOOL_JOB_STATUS_ERROR,
        TOOL_JOB_STATUS_TIMEOUT,
        TOOL_JOB_STATUS_INTERRUPTED,
        TOOL_JOB_STATUS_STOPPED_BY_USER,
    ):
        try:
            row = getter(tool_job_dedupe_key(job_id, status))
        except Exception:
            row = None
        if row:
            return row
    return None


def prune_tool_job_rows(store: Any, *, now_epoch: float | None = None) -> int:
    if store is None:
        return 0
    cutoff = _retention_cutoff_iso(now_epoch=now_epoch)
    pruner = getattr(store, "prune_tool_jobs", None)
    if not callable(pruner):
        return 0
    return int(pruner(before_iso=cutoff) or 0)


__all__ = [
    "OWNED_PROCESS_JOB_PREFIX",
    "OWNED_PROCESS_TOOL_NAME",
    "TOOL_JOB_ACTIVE_STATUSES",
    "TOOL_JOB_EVENT_REASON",
    "TOOL_JOB_OWNER_KIND_NODE",
    "TOOL_JOB_OWNER_KIND_SESSION",
    "TOOL_JOB_STATUS_ERROR",
    "TOOL_JOB_STATUS_INTERRUPTED",
    "TOOL_JOB_STATUS_RUNNING",
    "TOOL_JOB_STATUS_STOPPED_BY_USER",
    "TOOL_JOB_STATUS_STOPPING",
    "TOOL_JOB_STATUS_SUCCESS",
    "TOOL_JOB_STATUS_TIMEOUT",
    "TOOL_JOB_TERMINAL_STATUSES",
    "ToolJob",
    "ToolJobFinishedError",
    "ToolJobNotFoundError",
    "ToolJobOutcome",
    "ToolJobRunner",
    "build_owned_process_event_payload",
    "build_tool_job_event_payload",
    "owned_process_event_identity",
    "owned_process_items",
    "owned_process_job_id",
    "owned_process_task_items",
    "parse_owned_process_job_id",
    "prune_tool_job_rows",
    "recover_tool_jobs",
    "stable_tool_args_hash",
    "tool_job_dedupe_key",
]
