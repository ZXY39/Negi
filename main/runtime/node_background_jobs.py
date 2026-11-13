"""任务节点后台作业管理器：所有权、准入、执行索引、终态与停止协调。

前门会话的作业归 `g3ku/runtime/tool_jobs.py` 的会话 runner（session_key 所有权，
暂停/停止回合不杀已后台化工作）。任务节点侧的所有权不同：后台作业归**创建它的
节点**，节点/任务暂停或终态时必须同时停。本模块持有这份节点所有权：

- **准入**：自动交接的工具作业与 `exec background=true` 的 OS 进程都过同一个
  节点生命周期域；域被暂停/终态关闭后，新作业与新进程一律拒绝。
- **执行索引**：每个 (task_id, node_id) 一个 `ToolJobRunner`（owner_kind=node），
  in-flight 去重按 ``(tool_name, 归一化参数指纹)``，不跨节点、不跨任务复用。
- **生命周期取消域**：每代一个 `ToolCancellationToken`；暂停/终态取消当前代，
  resume 只开新域，旧域永久取消——旧后台不随 resume 复活，迟到回调只能结清旧 job。
- **停止协调**：生命周期事件（暂停/终态/删除/停机）是主入口；同步段只关闭准入
  并发起取消（可在任务锁内安全调用），等待 OS 退出、耐久结案、范围进程回收都在
  异步收尾段完成，绝不持锁等待 OS。
- **终态投递**：工具作业没有 PID，终态经 `tool_job_finished` 节点信箱消息回投；
  先落 `tool_job_outbox`（幂等、按 job_id 裁决），投递成功才标记 delivered。

"同时停"指同一个生命周期动作立即关闭准入并发起停止，停止完成有可验证回执；
停止未完成时结果里保留失败对象，不向操作者宣称全部停止成功。
"""

from __future__ import annotations

import asyncio
import inspect
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from loguru import logger

from g3ku.runtime.cancellation import ToolCancellationToken
from g3ku.runtime.tool_jobs import (
    TOOL_JOB_ACTIVE_STATUSES,
    TOOL_JOB_OWNER_KIND_NODE,
    TOOL_JOB_STATUS_INTERRUPTED,
    TOOL_JOB_STATUS_RUNNING,
    TOOL_JOB_STOP_ABANDON_GRACE_SECONDS,
    ToolJob,
    ToolJobFinishedError,
    ToolJobNotFoundError,
    ToolJobRunner,
    owned_process_task_items,
    tool_job_dedupe_key,
)
from main.errors import NodePausedError, TaskPausedError

# 停止原因写进事件 payload 的 stopped_by：区分生命周期来源，
# 不能把系统收尾都写成“用户手动停止”。
STOP_REASON_NODE_PAUSED = "node_paused"
STOP_REASON_TASK_PAUSED = "task_paused"
STOP_REASON_NODE_TERMINAL = "node_terminal"
STOP_REASON_TASK_TERMINAL = "task_terminal"
STOP_REASON_TASK_CANCELLED = "task_cancelled"
STOP_REASON_TASK_DELETED = "task_deleted"
STOP_REASON_SERVICE_SHUTDOWN = "service_shutdown"

DEFAULT_STOP_WAIT_SECONDS = 8.0

# 该调用当前是否在节点后台作业的任务上下文里执行。
# react_loop 置位、node_runner 的 spawn 相位同步读它：后台作业收尾不得把
# 父节点的新一跳改回 waiting_children（§3.3 提交边界）。
_NODE_TOOL_JOB_BACKGROUND_FLAG: ContextVar[bool] = ContextVar(
    "node_tool_job_background", default=False
)

# 节点自动交接的即时回执：保持英文，不照搬前门的"会话事件"承诺。
_NODE_BACKGROUND_RECEIPT_TEMPLATE = (
    "[BACKGROUND] job_id={job_id} tool={tool_name} status=running elapsed={elapsed_seconds}s timeout={timeout_text}\n"
    "Do not repeat this call. Its result will be delivered to this node as a "
    "tool_job_finished notice when it finishes, times out, or is stopped."
)


def node_tool_job_background_active() -> bool:
    return bool(_NODE_TOOL_JOB_BACKGROUND_FLAG.get())


def set_node_tool_job_background(active: bool) -> None:
    _NODE_TOOL_JOB_BACKGROUND_FLAG.set(bool(active))


def node_tool_job_synthetic_receipt(
    *,
    job_id: str,
    tool_name: str,
    elapsed_seconds: float,
    timeout_seconds: float,
) -> str:
    timeout_value = max(0.0, float(timeout_seconds or 0.0))
    timeout_text = f"{timeout_value:g}s" if timeout_value > 0 else "none"
    return _NODE_BACKGROUND_RECEIPT_TEMPLATE.format(
        job_id=str(job_id or ""),
        tool_name=str(tool_name or "tool"),
        elapsed_seconds=round(max(0.0, float(elapsed_seconds or 0.0)), 1),
        timeout_text=timeout_text,
    )


class NodeBackgroundAdmissionClosed(RuntimeError):  # noqa: N818 - 与 ToolJobFinishedError 同族的既定命名
    """节点生命周期域已关闭（暂停/终态/删除），拒绝新的后台作业。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason or "")


# 节点作业任务里的冻结类控制信号：暂停/取消绝不转工具错误（与 _run_call 同一裁决）。
NODE_JOB_CONTROL_SIGNALS: tuple[type[BaseException], ...] = (TaskPausedError, NodePausedError)


class NodeToolJobHandle:
    """react_loop 经 runtime_context 使用的节点作业准入/等待边界。

    与前门 `_runtime_agent_session.start_tool_job / wait_tool_job` 同形状：
    节点不用把 task_id/node_id 拼成虚假 session_key 走前门接口。
    """

    def __init__(self, manager: "NodeBackgroundJobManager", *, task_id: str, node_id: str) -> None:
        self._manager = manager
        self._task_id = str(task_id or "")
        self._node_id = str(node_id or "")

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def node_id(self) -> str:
        return self._node_id

    def start_tool_job(
        self,
        *,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        args_hash: str,
        hint: str,
        purpose: str = "",
        run: Callable[[], Awaitable[Any]],
        timeout_seconds: float,
        recovery_policy: str = "model_decide",
    ) -> tuple[ToolJob, bool]:
        return self._manager.admit_tool_job(
            task_id=self._task_id,
            node_id=self._node_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_hash=args_hash,
            hint=hint,
            purpose=purpose,
            run=run,
            timeout_seconds=timeout_seconds,
            recovery_policy=recovery_policy,
        )

    async def wait_tool_job(self, job: ToolJob, timeout_seconds: float) -> tuple[bool, Any]:
        state = self._manager._scope_for_read(self._task_id, self._node_id)
        runner = state.runner if state is not None else None
        if runner is None:
            return True, None
        return await runner.wait_first(job, timeout_seconds)


@dataclass
class _ScopeState:
    """一个 owner（节点或整任务占位）的生命周期域。"""

    task_id: str
    node_id: str  # '' 表示任务级占位（不承载 runner，只记录任务域开关）
    generation: int = 1
    closed: bool = False
    close_reason: str = ""
    close_note: str = ""
    stop_pending: bool = False
    runner: ToolJobRunner | None = None
    token: ToolCancellationToken | None = None
    stop_event: asyncio.Event | None = None
    last_stop_result: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.task_id, self.node_id)


class NodeBackgroundJobManager:
    """服务生命周期内持有节点后台作业的所有权与停止协调。"""

    def __init__(
        self,
        *,
        store_getter: Callable[[], Any],
        delivery_sink: Callable[[dict[str, Any]], Any] | None = None,
        stop_grace_seconds: float = TOOL_JOB_STOP_ABANDON_GRACE_SECONDS,
    ) -> None:
        self._store_getter = store_getter
        # 终态投递边界：payload -> 节点信箱投递，真值表示"已投/已按终态拒收"。
        self._delivery_sink = delivery_sink
        self._stop_grace_seconds = max(0.0, float(stop_grace_seconds or 0.0))
        self._scopes: dict[tuple[str, str], _ScopeState] = {}
        self._task_scopes: dict[str, set[tuple[str, str]]] = {}
        self._drain_task: asyncio.Task[Any] | None = None
        self._drain_wakeup: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 基础

    def _store(self) -> Any:
        try:
            return self._store_getter()
        except Exception:
            return None

    def _ensure_drain_primitives(self) -> None:
        if self._drain_wakeup is None:
            self._drain_wakeup = asyncio.Event()
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self._loop = loop

    def _schedule_drain(self) -> None:
        self._ensure_drain_primitives()
        if self._drain_task is not None and not self._drain_task.done():
            if self._drain_wakeup is not None:
                self._drain_wakeup.set()
            return
        loop = self._loop
        if loop is None:
            return
        try:
            self._drain_task = loop.create_task(self._drain_loop(), name="node-background-jobs-drain")
        except RuntimeError:
            return
        if self._drain_wakeup is not None:
            self._drain_wakeup.set()

    def _scope(self, task_id: str, node_id: str) -> _ScopeState:
        key = (str(task_id or ""), str(node_id or ""))
        state = self._scopes.get(key)
        if state is None:
            state = _ScopeState(task_id=key[0], node_id=key[1])
            self._scopes[key] = state
            if key[1]:
                self._task_scopes.setdefault(key[0], set()).add(key)
        return state

    def _scope_for_read(self, task_id: str, node_id: str) -> _ScopeState | None:
        return self._scopes.get((str(task_id or ""), str(node_id or "")))

    # ------------------------------------------------------------------ 准入

    def admission_reason(self, task_id: str, node_id: str) -> str:
        """后台启动准入检查：域开放返回空串，关闭返回原因（exec Popen 前调用）。"""
        task_state = self._scope_for_read(task_id, "")
        if task_state is not None and task_state.closed:
            return task_state.close_reason or STOP_REASON_TASK_PAUSED
        node_state = self._scope_for_read(task_id, node_id)
        if node_state is not None and node_state.closed:
            return node_state.close_reason or STOP_REASON_NODE_PAUSED
        return ""

    def admit_tool_job(
        self,
        *,
        task_id: str,
        node_id: str,
        turn_id: str,
        tool_call_id: str,
        tool_name: str,
        args_hash: str,
        hint: str,
        purpose: str = "",
        run: Callable[[], Awaitable[Any]],
        timeout_seconds: float,
        recovery_policy: str = "model_decide",
    ) -> tuple[ToolJob, bool]:
        """把一个工具调用登记成节点后台作业；域已关闭时拒绝。

        返回 ``(job, reused)``。``run`` 在当前 asyncio context 里被包成 task，
        ContextVar（runtime context / read roots / timeout clamp）随之继承。
        ``purpose`` 是交棒时从工具入参提取的 purpose 原文，落库后台面板行优先显示。
        """
        reason = self.admission_reason(task_id, node_id)
        if reason:
            raise NodeBackgroundAdmissionClosed(reason)
        self._ensure_drain_primitives()
        state = self._scope(task_id, node_id)
        if state.runner is None:
            state.runner = ToolJobRunner(
                session_key=str(task_id or ""),
                store_getter=self._store_getter,
                owner_kind=TOOL_JOB_OWNER_KIND_NODE,
                owner_task_id=str(task_id or ""),
                owner_node_id=str(node_id or ""),
                stop_abandon_grace_seconds=self._stop_grace_seconds,
                terminal_sink=self._delivery_sink,
                control_signals=NODE_JOB_CONTROL_SIGNALS,
            )
        if state.token is None:
            state.token = ToolCancellationToken(session_key=f"task:{task_id}")
        job, reused = state.runner.start_job(
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args_hash=args_hash,
            hint=hint,
            purpose=purpose,
            run=run,
            timeout_seconds=timeout_seconds,
            recovery_policy=recovery_policy,
            cancel_token=state.token.derive_child(reason=f"node_tool:{tool_name}"),
        )
        return job, reused

    # ------------------------------------------------------------ 生命周期事件

    def handle_lifecycle_event(
        self,
        *,
        task_id: str,
        node_id: str = "",
        action: str,
        reason: str = "",
    ) -> None:
        """消费节点/任务生命周期动作；同步段只关准入并安排异步收尾。

        action ∈ node_pause / node_resume / node_terminal / task_pause /
        task_cancel / task_resume / task_terminal / task_delete。
        """
        normalized_task = str(task_id or "").strip()
        if not normalized_task:
            return
        normalized_node = str(node_id or "").strip()
        if action in {"node_pause", "node_terminal"}:
            stop_reason = STOP_REASON_NODE_TERMINAL if action == "node_terminal" else STOP_REASON_NODE_PAUSED
            self.request_scope_stop(
                normalized_task,
                normalized_node,
                stop_reason=stop_reason,
                note=str(reason or ""),
            )
        elif action in {"task_pause", "task_cancel", "task_terminal", "task_delete"}:
            stop_reason = {
                "task_pause": STOP_REASON_TASK_PAUSED,
                "task_cancel": STOP_REASON_TASK_CANCELLED,
                "task_terminal": STOP_REASON_TASK_TERMINAL,
                "task_delete": STOP_REASON_TASK_DELETED,
            }[action]
            self.request_task_stop(normalized_task, stop_reason=stop_reason, note=str(reason or ""))
        elif action in {"node_resume", "task_resume"}:
            self.open_scope_for_resume(normalized_task, normalized_node if action == "node_resume" else None)

    def request_scope_stop(
        self,
        task_id: str,
        node_id: str,
        *,
        stop_reason: str,
        note: str = "",
    ) -> None:
        """关闭节点域准入并立即发起取消（可在任务锁内同步调用）。"""
        state = self._scope(task_id, node_id)
        if not state.closed:
            state.closed = True
            state.close_reason = str(stop_reason or STOP_REASON_NODE_PAUSED)
            state.close_note = str(note or "")
        if not state.stop_pending:
            state.stop_pending = True
            if state.stop_event is None or state.stop_event.is_set():
                state.stop_event = asyncio.Event()
        if state.token is not None:
            try:
                state.token.cancel(reason=state.close_note or state.close_reason)
            except Exception:
                logger.debug("node scope token cancel failed task={} node={}", task_id, node_id, exc_info=True)
        self._schedule_drain()

    def request_task_stop(self, task_id: str, *, stop_reason: str, note: str = "") -> None:
        """关闭全任务域（含本来停靠着、没有在飞执行器的节点）并停止全部后台。"""
        normalized_task = str(task_id or "").strip()
        if not normalized_task:
            return
        self.request_scope_stop(normalized_task, "", stop_reason=stop_reason, note=note)
        for key in list(self._task_scopes.get(normalized_task, set())):
            node_state = self._scopes.get(key)
            if node_state is not None and node_state.closed and not node_state.stop_pending:
                # 已经收尾过的节点域不再重复取消；准入保持关闭。
                continue
            self.request_scope_stop(normalized_task, key[1], stop_reason=stop_reason, note=note)

    def open_scope_for_resume(self, task_id: str, node_id: str | None = None) -> None:
        """恢复只开启新调用准入：旧域永久取消，后台作业不自动重跑。"""
        normalized_task = str(task_id or "").strip()
        if not normalized_task:
            return
        if node_id:
            state = self._scope_for_read(normalized_task, node_id)
            if state is not None:
                state.closed = False
                state.close_reason = ""
                state.close_note = ""
                state.generation += 1
                state.token = ToolCancellationToken(session_key=f"task:{normalized_task}")
            return
        task_state = self._scope_for_read(normalized_task, "")
        if task_state is not None:
            task_state.closed = False
            task_state.close_reason = ""
            task_state.close_note = ""
            task_state.generation += 1
        for key in list(self._task_scopes.get(normalized_task, set())):
            state = self._scopes.get(key)
            if state is None:
                continue
            state.closed = False
            state.close_reason = ""
            state.close_note = ""
            state.generation += 1
            state.token = ToolCancellationToken(session_key=f"task:{normalized_task}")

    # ---------------------------------------------------------------- 停止收尾

    async def _drain_loop(self) -> None:
        while True:
            if self._drain_wakeup is not None:
                await self._drain_wakeup.wait()
                self._drain_wakeup.clear()
            pending = [state for state in list(self._scopes.values()) if state.stop_pending]
            if not pending:
                continue
            for state in pending:
                try:
                    await self._finalize_scope_stop(state)
                except Exception:
                    logger.exception(
                        "node background scope stop failed task={} node={}",
                        state.task_id,
                        state.node_id,
                    )
                finally:
                    state.stop_pending = False
                    if state.stop_event is not None:
                        state.stop_event.set()

    async def _finalize_scope_stop(self, state: _ScopeState) -> None:
        confirmed = 0
        failures: list[dict[str, Any]] = []
        stopped_jobs = 0
        if state.runner is not None:
            try:
                stopped_jobs = await state.runner.cancel_all(
                    reason=state.close_note or state.close_reason,
                    suppress_event=False,
                    stopped_by=state.close_reason,
                )
            except Exception:
                logger.exception(
                    "node tool job cancel_all failed task={} node={}", state.task_id, state.node_id
                )
            if state.runner.list_jobs():
                failures.append(
                    {
                        "kind": "tool_job",
                        "detail": "tool jobs still active after cancel_all",
                    }
                )
        if state.node_id:
            reap = await asyncio.to_thread(self._reap_node_processes, state.task_id, [state.node_id])
        else:
            reap = await asyncio.to_thread(self._reap_task_processes, state.task_id)
        confirmed += len(reap.get("killed") or [])
        already_gone = len(reap.get("already_gone") or [])
        failures.extend(reap.get("failed") or [])
        state.last_stop_result = {
            "scope": "task" if not state.node_id else "node",
            "task_id": state.task_id,
            "node_id": state.node_id,
            "stop_reason": state.close_reason,
            "stopped_tool_jobs": stopped_jobs,
            "confirmed_processes": confirmed,
            "already_gone_processes": already_gone,
            "failures": failures,
            "completed": not failures,
        }

    def _reap_node_processes(self, task_id: str, node_ids: list[str]) -> dict[str, Any]:
        try:
            from g3ku.runtime import owned_processes as op

            return op.reap_scope(
                op.scope_key(session_key="", task_id=str(task_id or "")),
                node_ids=node_ids,
                include_unattributed=False,
            )
        except Exception:
            logger.exception("node process reap failed task={} nodes={}", task_id, node_ids)
            return {"killed": [], "already_gone": [], "failed": [], "items": []}

    def _reap_task_processes(self, task_id: str) -> dict[str, Any]:
        try:
            from g3ku.runtime import owned_processes as op

            return op.reap_scope(
                op.scope_key(session_key="", task_id=str(task_id or "")),
                include_unattributed=True,
            )
        except Exception:
            logger.exception("task process reap failed task={}", task_id)
            return {"killed": [], "already_gone": [], "failed": [], "items": []}

    async def close_scope_and_wait(
        self,
        task_id: str,
        node_id: str | None = None,
        *,
        stop_reason: str,
        note: str = "",
        timeout_seconds: float = DEFAULT_STOP_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """关闭范围准入、停止后台并等待收尾；删除/停机路径用。

        返回逐项结果；超时或失败时 ``completed=False`` 并携带残留对象，
        调用方据此报告"停止中/失败"，不回退任何已提交的逻辑终态。
        """
        normalized_task = str(task_id or "").strip()
        result: dict[str, Any] = {
            "task_id": normalized_task,
            "node_id": str(node_id or ""),
            "stop_reason": str(stop_reason or ""),
            "stopped_tool_jobs": 0,
            "confirmed_processes": 0,
            "already_gone_processes": 0,
            "failures": [],
            "completed": False,
        }
        if not normalized_task:
            result["completed"] = True
            return result
        if node_id:
            self.request_scope_stop(
                normalized_task, node_id, stop_reason=stop_reason, note=note
            )
            keys = [(normalized_task, str(node_id))]
        else:
            self.request_task_stop(normalized_task, stop_reason=stop_reason, note=note)
            keys = [(normalized_task, "")] + sorted(self._task_scopes.get(normalized_task, set()))
        self._schedule_drain()
        deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
        while True:
            unfinished: list[_ScopeState] = []
            for key in keys:
                state = self._scope_for_read(*key)
                if state is None:
                    continue
                if state.stop_pending or (state.stop_event is not None and not state.stop_event.is_set()):
                    unfinished.append(state)
            if not unfinished:
                break
            if time.monotonic() >= deadline:
                result["failures"].append(
                    {
                        "kind": "scope_stop_timeout",
                        "detail": f"scopes still stopping: {[s.node_id or '<task>' for s in unfinished]}",
                    }
                )
                result["stopped_tool_jobs"] = sum(
                    int(s.last_stop_result.get("stopped_tool_jobs") or 0)
                    for s in (self._scope_for_read(*key) for key in keys)
                    if s is not None
                )
                result["confirmed_processes"] = sum(
                    int(s.last_stop_result.get("confirmed_processes") or 0)
                    for s in (self._scope_for_read(*key) for key in keys)
                    if s is not None
                )
                return result
            await asyncio.sleep(0.05)
        aggregated = self._collect_stop_results(keys)
        result.update(aggregated)
        result["completed"] = not result["failures"]
        return result

    def _collect_stop_results(self, keys: list[tuple[str, str]]) -> dict[str, Any]:
        stopped_jobs = 0
        confirmed = 0
        already_gone = 0
        failures: list[dict[str, Any]] = []
        for key in keys:
            state = self._scope_for_read(*key)
            if state is None:
                continue
            last = state.last_stop_result or {}
            stopped_jobs += int(last.get("stopped_tool_jobs") or 0)
            confirmed += int(last.get("confirmed_processes") or 0)
            already_gone += int(last.get("already_gone_processes") or 0)
            failures.extend(last.get("failures") or [])
        return {
            "stopped_tool_jobs": stopped_jobs,
            "confirmed_processes": confirmed,
            "already_gone_processes": already_gone,
            "failures": failures,
        }

    async def close_all(self, *, stop_reason: str, timeout_seconds: float = DEFAULT_STOP_WAIT_SECONDS) -> dict[str, Any]:
        """服务停机：停止全部节点后台工作并等待收尾。"""
        task_ids = {key[0] for key in list(self._scopes.keys()) if key[0]}
        failures: list[dict[str, Any]] = []
        stopped_jobs = 0
        confirmed = 0
        for task_id in sorted(task_ids):
            result = await self.close_scope_and_wait(
                task_id,
                stop_reason=stop_reason,
                timeout_seconds=timeout_seconds,
            )
            stopped_jobs += int(result.get("stopped_tool_jobs") or 0)
            confirmed += int(result.get("confirmed_processes") or 0)
            failures.extend(result.get("failures") or [])
        return {
            "tasks": len(task_ids),
            "stopped_tool_jobs": stopped_jobs,
            "confirmed_processes": confirmed,
            "failures": failures,
            "completed": not failures,
        }

    def discard_task(self, task_id: str) -> None:
        """任务数据已删除：丢弃内存中的域与 runner 索引。"""
        normalized_task = str(task_id or "").strip()
        if not normalized_task:
            return
        for key in [key for key in list(self._scopes.keys()) if key[0] == normalized_task]:
            state = self._scopes.pop(key, None)
            if state is not None and state.runner is not None:
                state.runner._jobs.clear()
                state.runner._inflight.clear()
        self._task_scopes.pop(normalized_task, None)

    # ------------------------------------------------------------------ 读数

    def active_tool_jobs_for_task(self, task_id: str) -> list[dict[str, Any]]:
        normalized_task = str(task_id or "").strip()
        items: list[dict[str, Any]] = []
        for key in list(self._task_scopes.get(normalized_task, set())):
            state = self._scopes.get(key)
            if state is None or state.runner is None:
                continue
            for item in state.runner.list_jobs():
                enriched = dict(item)
                enriched["kind"] = "tool_job"
                enriched["task_id"] = normalized_task
                enriched["node_id"] = key[1]
                items.append(enriched)
        items.sort(key=lambda item: (str(item.get("started_at") or ""), str(item.get("job_id") or "")))
        return items

    def count_active_for_task(self, task_id: str) -> int:
        normalized_task = str(task_id or "").strip()
        jobs = len(self.active_tool_jobs_for_task(normalized_task))
        try:
            processes = owned_process_task_items(normalized_task)
        except Exception:
            processes = []
        return jobs + len(processes)

    def snapshot_task_background(self, task_id: str) -> dict[str, Any]:
        """任务后台面板读数：工具作业与 OS 进程合并，kind/job_id 稳定区分。"""
        normalized_task = str(task_id or "").strip()
        job_items = self.active_tool_jobs_for_task(normalized_task)
        seen_jobs = {str(item.get("job_id") or "") for item in job_items}
        store = self._store()
        if store is not None:
            try:
                rows = store.list_tool_jobs(
                    owner_kind=TOOL_JOB_OWNER_KIND_NODE,
                    task_id=normalized_task,
                    statuses=tuple(TOOL_JOB_ACTIVE_STATUSES),
                    limit=200,
                )
            except Exception:
                rows = []
            for row in rows:
                job_id = str(row.get("job_id") or "")
                if not job_id or job_id in seen_jobs:
                    continue
                job_items.append(
                    {
                        "kind": "tool_job",
                        "job_id": job_id,
                        "tool_name": str(row.get("tool_name") or "tool"),
                        "status": str(row.get("status") or TOOL_JOB_STATUS_RUNNING),
                        "started_at": str(row.get("started_at") or ""),
                        "elapsed_seconds": 0.0,
                        "turn_id": str(row.get("turn_id") or ""),
                        "tool_call_id": str(row.get("tool_call_id") or ""),
                        "hint": str(row.get("hint") or ""),
                        "purpose": str(row.get("purpose") or ""),
                        "node_id": str(row.get("node_id") or ""),
                        "orphaned": True,
                    }
                )
                seen_jobs.add(job_id)
        try:
            process_items = owned_process_task_items(normalized_task)
        except Exception:
            process_items = []
        return {
            "tool_jobs": job_items,
            "processes": process_items,
            "total": len(job_items) + len(process_items),
        }

    async def stop_tool_job(self, task_id: str, job_id: str, *, reason: str = "user_manual_stop") -> dict[str, Any]:
        """用户停止单个作业：走 runner 的手动停止车道（立即投递 stopped_by_user）。"""
        normalized_task = str(task_id or "").strip()
        normalized_job = str(job_id or "").strip()
        for key in list(self._task_scopes.get(normalized_task, set())):
            state = self._scopes.get(key)
            if state is None or state.runner is None:
                continue
            job = state.runner._jobs.get(normalized_job)
            if job is not None:
                return await state.runner.stop_job(normalized_job, reason=reason)
        store = self._store()
        if store is not None:
            try:
                row = store.get_tool_job(normalized_job)
            except Exception:
                row = None
            if row is not None and str(row.get("owner_kind") or "") == TOOL_JOB_OWNER_KIND_NODE:
                if str(row.get("status") or "") in TOOL_JOB_ACTIVE_STATUSES:
                    # 本进程没有该作业的 runner（worker 重启后 web 侧误停等）：
                    # 保留登记，明确报未执行，不假报成功。
                    raise ToolJobNotFoundError(normalized_job)
                raise ToolJobFinishedError(normalized_job)
        raise ToolJobNotFoundError(normalized_job)

    # ------------------------------------------------------------ 终态与恢复

    async def deliver_tool_job_terminal(self, payload: dict[str, Any]) -> bool:
        """把作业终态事件投给创建节点；返回 True 表示已投递或已按终态拒收。"""
        sink = self._delivery_sink
        if not callable(sink):
            return False
        try:
            result = sink(dict(payload))
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            logger.exception(
                "node tool job terminal delivery failed job={}",
                payload.get("job_id"),
            )
            return False

    async def replay_pending_outbox(self, *, limit: int = 200) -> int:
        """补投 pending 的节点作业终态事件（终态已落库但 outbox 未送达）。"""
        store = self._store()
        if store is None:
            return 0
        delivered = 0
        try:
            rows = store.list_pending_tool_job_outbox(limit=limit, owner_kind=TOOL_JOB_OWNER_KIND_NODE)
        except Exception:
            return 0
        for row in rows:
            dedupe_key = str(row.get("dedupe_key") or "")
            payload = row.get("payload")
            if not dedupe_key or not isinstance(payload, dict):
                continue
            ok = await self.deliver_tool_job_terminal(payload)
            if ok:
                try:
                    store.mark_tool_job_outbox_delivered(dedupe_key, delivered_at=_now_iso())
                except Exception:
                    logger.exception("failed to mark node tool job outbox delivered {}", dedupe_key)
                delivered += 1
        return delivered

    async def recover_after_restart(self) -> dict[str, int]:
        """启动恢复：上一进程遗留的节点作业行收成 interrupted 并补投节点信箱。

        节点后台协程不会跨 worker 重启续跑；持久账本是恢复事实，不是重启指令。
        """
        store = self._store()
        if store is None:
            return {"recovered": 0, "delivered": 0}
        recovered_at = _now_iso()
        try:
            active_rows = store.list_tool_jobs(
                statuses=tuple(TOOL_JOB_ACTIVE_STATUSES),
                limit=1000,
                owner_kind=TOOL_JOB_OWNER_KIND_NODE,
            )
        except Exception:
            return {"recovered": 0, "delivered": 0}
        recovered = 0
        for row in active_rows:
            job_id = str(row.get("job_id") or "").strip()
            if not job_id:
                continue
            status = TOOL_JOB_STATUS_INTERRUPTED
            recover_text = (
                "The project process restarted while this background tool job was running. "
                "It may have stopped or may still be running; inspect the result/log before retrying."
            )
            payload = {
                "event_reason": "tool_job",
                "reason": "tool_job",
                "kind": "tool_job",
                "owner_kind": TOOL_JOB_OWNER_KIND_NODE,
                "session_id": str(row.get("task_id") or ""),
                "task_id": str(row.get("task_id") or ""),
                "node_id": str(row.get("node_id") or ""),
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
                "stopped_by": "restart",
                "external_event_id": tool_job_dedupe_key(job_id, status),
            }
            dedupe_key = tool_job_dedupe_key(job_id, status)
            try:
                store.update_tool_job(
                    job_id,
                    status=status,
                    finished_at=recovered_at,
                    updated_at=recovered_at,
                    result_preview=recover_text,
                )
            except Exception:
                logger.exception("failed to persist node tool job recovery {}", job_id)
            try:
                store.put_tool_job_outbox(
                    dedupe_key=dedupe_key,
                    job_id=job_id,
                    session_id=str(row.get("task_id") or ""),
                    created_at=recovered_at,
                    payload=payload,
                    owner_kind=TOOL_JOB_OWNER_KIND_NODE,
                )
            except Exception:
                logger.exception("failed to persist node tool job recovery outbox {}", job_id)
            recovered += 1
        delivered = await self.replay_pending_outbox()
        return {"recovered": recovered, "delivered": delivered}


def _now_iso() -> str:
    from main.protocol import now_iso

    return now_iso()


__all__ = [
    "DEFAULT_STOP_WAIT_SECONDS",
    "NodeBackgroundAdmissionClosed",
    "NodeBackgroundJobManager",
    "STOP_REASON_NODE_PAUSED",
    "STOP_REASON_NODE_TERMINAL",
    "STOP_REASON_SERVICE_SHUTDOWN",
    "STOP_REASON_TASK_CANCELLED",
    "STOP_REASON_TASK_DELETED",
    "STOP_REASON_TASK_PAUSED",
    "STOP_REASON_TASK_TERMINAL",
    "node_tool_job_background_active",
    "node_tool_job_synthetic_receipt",
    "set_node_tool_job_background",
]
