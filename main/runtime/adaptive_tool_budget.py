from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


# 回合闸每拍最多挪这么多格。上行限的是"一步跳多少"而不是"一格多久"：反馈比采样慢
# 得多（一次模型调用实测 p50 37 s / p90 84 s，15 s 采一拍），不限跳幅就会在上一拍的
# 后果可见之前继续放人。下行按操作员要的"缓慢"，一拍退一格。
_ENTRY_STEP_UP_SLOTS = 2
_ENTRY_STEP_DOWN_SLOTS = 1


@dataclass(slots=True)
class ToolSlotLease:
    lease_id: int
    task_id: str
    node_id: str
    tool_name: str
    tool_call_id: str
    acquired_at: str
    queued_at: str = ''


@dataclass(slots=True)
class _QueuedToolRequest:
    sequence: int
    future: asyncio.Future[ToolSlotLease]
    task_id: str
    node_id: str
    tool_name: str
    tool_call_id: str
    queued_at: str
    queued_mono: float


@dataclass(slots=True)
class _QueuedEntryRequest:
    future: asyncio.Future[None]
    role: str
    queued_mono: float


class AdaptiveToolBudgetController:
    def __init__(
        self,
        *,
        normal_limit: int = 6,
        throttled_limit: int | None = None,
        critical_limit: int | None = None,
        safe_limit: int | None = None,
        step_up: int = 1,
    ) -> None:
        self._lock = threading.RLock()
        self._target_running_tools_limit = 1
        self._running_tools_count = 0
        self._waiting_queue: deque[_QueuedToolRequest] = deque()
        self._next_sequence = 0
        self._next_lease_id = 0
        self._pressure_state = 'normal'
        self._last_transition_at = ''
        self._throttled_since = ''
        self._critical_since = ''
        self._normal_limit = max(1, int(normal_limit))
        # 磁盘治理（P1）：紧急态是独立于 pressure_state 的硬闸——不进状态白名单、
        # 不被 _reset_idle_locked 冲掉、不被压力恢复链抬起。进入时 target_limit=0
        # （新工具调用排队等待而非拒绝），退出时按当前 pressure_state 恢复。
        self._disk_emergency_active = False
        self._disk_emergency_since = ''
        # 节点回合维度：一次 resume/pickup 放出的节点数可以远大于槽位数，而每个执行器
        # 在拿到任何槽位之前就要把自己那份上下文物化出来（`_run_entry` → `run_node`）。
        # 只拧工具/模型槽位拦不住这份"存在成本"，所以另立一份按角色的回合闸。
        # 闸位不再由工具压力状态机驱动：工具槽的 normal/easing/throttled 度量的是
        # 「一次工具调用」的积压，而回合闸要的是「同时在物化上下文的执行器」的余量，
        # 两者共用会让一次工具队列抖动把节点并发钉死（09-30 实盘：一次 141 秒的
        # SQLite 读把双闸踩到 1/1，持续约 9 分钟）。现在回合闸跟随 monitor 每拍发布的
        # 余量目标（`set_entry_targets`），地板只是无监控时的起步值与空闲复位位；
        # 磁盘紧急仍是唯一能直接把回合闸踩到 0 的硬闸。
        self._entry_dims: dict[str, dict[str, Any]] = {}

    def configure_entry_ceilings(self, ceilings: dict[str, int]) -> None:
        """设定各角色回合闸的地板（正常态起步值），并按当前压力状态重算实际 limit。
        地板数值本身变了 ⇒ 当场把高水位钳到新地板（operator 改数就是改意图）；
        地板没变（同一份配置的刷新）⇒ 保留已经爬到的高水位。"""
        ready: list[_QueuedEntryRequest] = []
        with self._lock:
            for role, ceiling in dict(ceilings or {}).items():
                normalized_role = str(role or '').strip().lower()
                if not normalized_role:
                    continue
                dim = self._entry_dims.setdefault(
                    normalized_role,
                    {'ceiling': 1, 'limit': 1, 'running': 0, 'waiters': deque()},
                )
                next_ceiling = max(1, int(ceiling or 1))
                if int(dim.get('ceiling') or 1) != next_ceiling:
                    dim['limit'] = min(next_ceiling, max(1, int(dim.get('limit') or 1)))
                dim['ceiling'] = next_ceiling
                ready.extend(self._resolve_entry_dim_locked(normalized_role, dim))
        self._resolve_entry_waiters(ready)

    def _entry_limit_for_locked(self, dim: dict[str, Any]) -> int:
        if self._disk_emergency_active:
            return 0
        ceiling = max(1, int(dim.get('ceiling') or 1))
        if dim.get('target') is None:
            # 监控还没发布目标（非 worker 模式、或启动后第一拍之前）：配置值是地板，
            # 已爬到的位置不掉回来。
            return max(ceiling, int(dim.get('limit') or 1))
        # 有余量目标时，闸位就是发布进来的那个数——地板已经作为下限参与过目标计算，
        # 这里不再抬，否则一次外部内存压力永远压不住回合闸。
        return max(1, int(dim.get('limit') or 1))

    def set_entry_targets(self, targets: dict[str, Any]) -> dict[str, int]:
        """按角色接受本轮的余量目标，并把它按"变化率"落成实际闸位。

        目标本身是 monitor 用自家积压轴（事件循环 lag / 写入队列 / SQLite / 上游限流）
        和内存余量算出来的，可以高于也可以低于地板；这里只负责不许一步跳太多——一次模型
        调用实测 p50 37 s，反馈比采样慢得多，上跳不限速就会在反馈回来前过冲（凌晨那次
        90 秒爬到 27 格、Private 2.4 GB 就是这么来的）。下行按操作员要求的"缓慢"。
        返回每个角色这一拍真正落到的闸位，供观测。
        """
        applied: dict[str, int] = {}
        ready: list[_QueuedEntryRequest] = []
        with self._lock:
            for role, raw_target in dict(targets or {}).items():
                normalized_role = str(role or '').strip().lower()
                if not normalized_role:
                    continue
                dim = self._entry_dims.setdefault(
                    normalized_role,
                    {'ceiling': 1, 'limit': 1, 'running': 0, 'waiters': deque(), 'target': None},
                )
                try:
                    target = max(1, int(raw_target))
                except (TypeError, ValueError):
                    continue
                dim['target'] = target
                current = max(1, int(dim.get('limit') or 1))
                if target > current:
                    dim['limit'] = min(target, current + _ENTRY_STEP_UP_SLOTS)
                elif target < current:
                    dim['limit'] = max(target, current - _ENTRY_STEP_DOWN_SLOTS)
                applied[normalized_role] = int(dim['limit'])
                ready.extend(self._resolve_entry_dim_locked(normalized_role, dim))
        self._resolve_entry_waiters(ready)
        return applied

    def _reset_entry_dim_idle_locked(self, dim: dict[str, Any]) -> None:
        """队列排空后把闸位收回地板：下一次风暴必须从配置值起步、按余量目标与变化率
        重新抬，而不是继承上一次爬到的高水位一次性放出去（21:31 无闸事故的形态）。"""
        if int(dim.get('running') or 0) == 0 and not (dim.get('waiters') or ()):
            dim['limit'] = max(1, int(dim.get('ceiling') or 1))

    def _resolve_entry_dim_locked(self, role: str, dim: dict[str, Any]) -> list[_QueuedEntryRequest]:
        dim['limit'] = self._entry_limit_for_locked(dim)
        ready: list[_QueuedEntryRequest] = []
        waiters: deque[_QueuedEntryRequest] = dim['waiters']
        while waiters and int(dim['running']) < int(dim['limit']):
            dim['running'] = int(dim['running']) + 1
            ready.append(waiters.popleft())
        return ready

    def _resolve_entry_waiters(self, ready: list[_QueuedEntryRequest]) -> None:
        for request in ready:
            if request.future.done():
                continue
            try:
                request.future.get_loop().call_soon_threadsafe(
                    _set_future_result_if_pending, request.future, None
                )
            except Exception:
                _set_future_result_if_pending(request.future, None)

    async def acquire_entry_slot(self, *, role: str) -> None:
        """按角色取一个"节点回合"闸位：拿到才允许进入 run_node（上下文物化之前）。"""
        normalized_role = str(role or '').strip().lower() or 'execution'
        future: asyncio.Future[None] | None = None
        with self._lock:
            dim = self._entry_dims.setdefault(
                normalized_role,
                {'ceiling': 1, 'limit': 1, 'running': 0, 'waiters': deque()},
            )
            dim['limit'] = self._entry_limit_for_locked(dim)
            if int(dim['running']) < int(dim['limit']):
                dim['running'] = int(dim['running']) + 1
                return
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            dim['waiters'].append(
                _QueuedEntryRequest(future=future, role=normalized_role, queued_mono=time.perf_counter())
            )
        try:
            await future
        except Exception:
            with self._lock:
                dim = self._entry_dims.get(normalized_role)
                if dim is not None:
                    dim['waiters'] = deque(
                        item for item in dim['waiters'] if item is not None and item.future is not future
                    )
            raise

    def release_entry_slot(self, *, role: str) -> None:
        normalized_role = str(role or '').strip().lower() or 'execution'
        ready: list[_QueuedEntryRequest] = []
        with self._lock:
            dim = self._entry_dims.get(normalized_role)
            if dim is None:
                return
            dim['running'] = max(0, int(dim['running']) - 1)
            self._reset_entry_dim_idle_locked(dim)
            ready = self._resolve_entry_dim_locked(normalized_role, dim)
        self._resolve_entry_waiters(ready)

    def entry_snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {
                role: {
                    'ceiling': int(dim.get('ceiling') or 0),
                    'limit': int(dim.get('limit') or 0),
                    'running': int(dim.get('running') or 0),
                    'queued': len(dim.get('waiters') or ()),
                }
                for role, dim in self._entry_dims.items()
            }

    def _sync_entry_dims_locked(self) -> list[_QueuedEntryRequest]:
        ready: list[_QueuedEntryRequest] = []
        for role, dim in self._entry_dims.items():
            ready.extend(self._resolve_entry_dim_locked(role, dim))
        return ready

    def configure(
        self,
        *,
        normal_limit: int,
        throttled_limit: int | None = None,
        critical_limit: int | None = None,
        safe_limit: int | None = None,
        step_up: int,
    ) -> None:
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        entry_ready: list[_QueuedEntryRequest] = []
        with self._lock:
            self._normal_limit = max(1, int(normal_limit or 1))
            if self._running_tools_count <= 0 and not self._waiting_queue and not self._disk_emergency_active:
                self._reset_idle_locked()
            ready = self._drain_waiters_locked()
            entry_ready = self._sync_entry_dims_locked()
        self._resolve_waiters(ready)
        self._resolve_entry_waiters(entry_ready)

    async def acquire_tool_slot(
        self,
        *,
        task_id: str,
        node_id: str,
        tool_name: str,
        tool_call_id: str,
    ) -> ToolSlotLease:
        return await self.acquire_work_slot(
            task_id=task_id,
            node_id=node_id,
            work_kind=tool_name,
            work_id=tool_call_id,
        )

    async def acquire_work_slot(
        self,
        *,
        task_id: str,
        node_id: str,
        work_kind: str,
        work_id: str,
    ) -> ToolSlotLease:
        future: asyncio.Future[ToolSlotLease] | None = None
        with self._lock:
            if not self._waiting_queue and self._running_tools_count < self._target_running_tools_limit:
                self._running_tools_count += 1
                return self._build_lease(
                    task_id=task_id,
                    node_id=node_id,
                    tool_name=work_kind,
                    tool_call_id=work_id,
                    queued_at='',
                )
            loop = asyncio.get_running_loop()
            self._next_sequence += 1
            future = loop.create_future()
            self._waiting_queue.append(
                _QueuedToolRequest(
                    sequence=self._next_sequence,
                    future=future,
                    task_id=str(task_id or '').strip(),
                    node_id=str(node_id or '').strip(),
                    tool_name=str(work_kind or '').strip() or 'work',
                    tool_call_id=str(work_id or '').strip(),
                    queued_at=_now_iso(),
                    queued_mono=time.perf_counter(),
                )
            )
        try:
            return await future
        except Exception:
            with self._lock:
                self._waiting_queue = deque(item for item in self._waiting_queue if item.future is not future)
            raise

    def release_tool_slot(self, lease: ToolSlotLease | None) -> None:
        self.release_work_slot(lease)

    def release_work_slot(self, lease: ToolSlotLease | None) -> None:
        if lease is None:
            return
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        with self._lock:
            if self._running_tools_count > 0:
                self._running_tools_count -= 1
            if self._running_tools_count <= 0 and not self._waiting_queue and not self._disk_emergency_active:
                self._reset_idle_locked()
            ready = self._drain_waiters_locked()
        self._resolve_waiters(ready)

    def set_disk_emergency(self, active: bool, *, at: str | None = None) -> None:
        """磁盘紧急硬闸：进入 → target_limit=0（新工具调用排队等待，不拒绝）；
        退出 → 按当前 pressure_state 恢复 limit 并 drain 等待队列。"""
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        entry_ready: list[_QueuedEntryRequest] = []
        with self._lock:
            if active and not self._disk_emergency_active:
                self._disk_emergency_active = True
                self._disk_emergency_since = timestamp
                self._target_running_tools_limit = 0
                self._last_transition_at = timestamp
            elif not active and self._disk_emergency_active:
                self._disk_emergency_active = False
                self._disk_emergency_since = ''
                self._last_transition_at = timestamp
                if self._pressure_state == 'critical':
                    self._target_running_tools_limit = 1
                elif self._pressure_state == 'throttled':
                    self._target_running_tools_limit = max(int(self._running_tools_count), 1)
                else:
                    self._target_running_tools_limit = max(int(self._normal_limit), 1)
                if self._running_tools_count <= 0 and not self._waiting_queue:
                    self._reset_idle_locked()
            else:
                return
            ready = self._drain_waiters_locked()
            entry_ready = self._sync_entry_dims_locked()
        self._resolve_waiters(ready)
        self._resolve_entry_waiters(entry_ready)

    def abort_task_waiters(self, task_id: str, exc: BaseException) -> int:
        """把指定任务在排队中的工具调用以异常唤醒（防死锁）。

        磁盘紧急态下任务被自动暂停时调用：等待中的 acquire future 收到
        TaskPausedError 后沿既有 pause 流转冒泡，模型不会看到工具级错误。
        """
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return 0
        targets: list[asyncio.Future[ToolSlotLease]] = []
        with self._lock:
            kept: deque[_QueuedToolRequest] = deque()
            for item in self._waiting_queue:
                if str(item.task_id) == normalized_task_id and not item.future.done():
                    targets.append(item.future)
                    continue
                kept.append(item)
            self._waiting_queue = kept
        for future in targets:
            try:
                loop = future.get_loop()
            except Exception:
                loop = None
            if loop is not None:
                loop.call_soon_threadsafe(_set_future_exception_if_pending, future, exc)
            else:
                _set_future_exception_if_pending(future, exc)
        return len(targets)

    def throttle(self, *, at: str | None = None) -> None:
        with self._lock:
            target_limit = int(self._running_tools_count)
        self.set_budget_state('throttled', at=at, target_limit=target_limit)

    def critical(self, *, at: str | None = None) -> None:
        self.set_budget_state('critical', at=at, target_limit=1)

    def set_budget_state(self, state: str, *, at: str | None = None, target_limit: int | None = None) -> None:
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            normalized_state = str(state or 'normal').strip().lower() or 'normal'
            if normalized_state == 'recovering':
                normalized_state = 'easing'
            if normalized_state not in {'normal', 'easing', 'throttled', 'critical'}:
                normalized_state = 'normal'
            self._pressure_state = normalized_state
            if target_limit is None:
                if normalized_state == 'critical':
                    next_limit = 1
                elif normalized_state == 'throttled':
                    next_limit = int(self._running_tools_count)
                else:
                    next_limit = max(int(self._target_running_tools_limit), 1)
            else:
                next_limit = max(0, int(target_limit))
            if self._disk_emergency_active:
                # 紧急硬闸期间任何压力状态迁移都不得抬起 limit（防御性钳制）。
                next_limit = 0
            self._target_running_tools_limit = next_limit
            self._last_transition_at = timestamp
            if normalized_state in {'throttled', 'critical'} and not self._throttled_since:
                self._throttled_since = timestamp
            if normalized_state == 'critical' and not self._critical_since:
                self._critical_since = timestamp
            if normalized_state == 'normal':
                self._throttled_since = ''
                self._critical_since = ''
            elif normalized_state == 'throttled':
                self._critical_since = ''
            if (
                self._running_tools_count <= 0
                and not self._waiting_queue
                and normalized_state == 'normal'
                and not self._disk_emergency_active
            ):
                self._reset_idle_locked()
            ready = self._drain_waiters_locked()
            entry_ready = self._sync_entry_dims_locked()
        self._resolve_waiters(ready)
        self._resolve_entry_waiters(entry_ready)

    def begin_easing(self, *, at: str | None = None) -> None:
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            self._pressure_state = 'easing'
            self._last_transition_at = timestamp
            entry_ready = self._sync_entry_dims_locked()
        self._resolve_entry_waiters(entry_ready)

    def begin_recovery(self, *, at: str | None = None) -> None:
        self.begin_easing(at=at)

    def step_easing(self, *, at: str | None = None) -> bool:
        """只抬工具槽。回合闸由 monitor 每拍发布的余量目标驱动（`set_entry_targets`）——
        两轴的成本量级不同（一个槽≈几十毫秒的等待 vs 一份上下文物化≈70MB 与
        秒级 CPU），共用一次抬格会让闸跟着工具的节奏跑。"""
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        changed = False
        timestamp = str(at or _now_iso()).strip() or _now_iso()
        with self._lock:
            next_limit = max(1, int(self._running_tools_count) + 1)
            if next_limit != self._target_running_tools_limit:
                self._target_running_tools_limit = next_limit
                changed = True
            self._pressure_state = 'easing'
            self._last_transition_at = timestamp
            ready = self._drain_waiters_locked()
        self._resolve_waiters(ready)
        return changed

    def step_recovery(self, *, at: str | None = None) -> bool:
        return self.step_easing(at=at)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            oldest_wait_ms = 0.0
            if self._waiting_queue:
                oldest_wait_ms = max(0.0, (time.perf_counter() - float(self._waiting_queue[0].queued_mono or 0.0)) * 1000.0)
            return {
                'tool_pressure_state': self._pressure_state,
                'tool_pressure_target_limit': int(self._target_running_tools_limit),
                'tool_pressure_running_count': int(self._running_tools_count),
                'tool_pressure_waiting_count': int(len(self._waiting_queue)),
                'tool_queue_running_count': int(self._running_tools_count),
                'tool_queue_waiting_count': int(len(self._waiting_queue)),
                'tool_pressure_last_transition_at': self._last_transition_at,
                'tool_pressure_throttled_since': self._throttled_since,
                'tool_pressure_critical_since': self._critical_since,
                'worker_execution_state': self._pressure_state,
                'worker_execution_target_limit': int(self._target_running_tools_limit),
                'worker_execution_running_count': int(self._running_tools_count),
                'worker_execution_waiting_count': int(len(self._waiting_queue)),
                'worker_execution_oldest_wait_ms': round(oldest_wait_ms, 3),
                'disk_emergency_active': bool(self._disk_emergency_active),
                'disk_emergency_since': self._disk_emergency_since,
                # 节点回合闸：恢复风暴时的"同时在物化上下文的执行器数"，与工具槽分开的两个数。
                'entry_gate_running': {
                    role: int(dim.get('running') or 0) for role, dim in self._entry_dims.items()
                },
                'entry_gate_ceiling': {
                    role: int(dim.get('ceiling') or 1) for role, dim in self._entry_dims.items()
                },
                'entry_gate_limit': {
                    role: int(dim.get('limit') or 0) for role, dim in self._entry_dims.items()
                },
                'entry_gate_queued': {
                    role: len(dim.get('waiters') or ()) for role, dim in self._entry_dims.items()
                },
                'entry_gate_targets': {
                    role: (
                        None if dim.get('target') is None else int(dim.get('target') or 0)
                    ) for role, dim in self._entry_dims.items()
                },
            }

    def _reset_idle_locked(self) -> None:
        self._pressure_state = 'normal'
        self._target_running_tools_limit = 1
        self._throttled_since = ''
        self._critical_since = ''

    def _build_lease(
        self,
        *,
        task_id: str,
        node_id: str,
        tool_name: str,
        tool_call_id: str,
        queued_at: str,
    ) -> ToolSlotLease:
        self._next_lease_id += 1
        return ToolSlotLease(
            lease_id=self._next_lease_id,
            task_id=str(task_id or '').strip(),
            node_id=str(node_id or '').strip(),
            tool_name=str(tool_name or '').strip() or 'tool',
            tool_call_id=str(tool_call_id or '').strip(),
            acquired_at=_now_iso(),
            queued_at=str(queued_at or '').strip(),
        )

    def _drain_waiters_locked(self) -> list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]]:
        ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]] = []
        while self._waiting_queue and self._running_tools_count < self._target_running_tools_limit:
            request = self._waiting_queue.popleft()
            if request.future.cancelled():
                continue
            self._running_tools_count += 1
            ready.append(
                (
                    request.future,
                    self._build_lease(
                        task_id=request.task_id,
                        node_id=request.node_id,
                        tool_name=request.tool_name,
                        tool_call_id=request.tool_call_id,
                        queued_at=request.queued_at,
                    ),
                )
            )
        return ready

    @staticmethod
    def _resolve_waiters(ready: list[tuple[asyncio.Future[ToolSlotLease], ToolSlotLease]]) -> None:
        for future, lease in ready:
            if future.done():
                continue
            try:
                loop = future.get_loop()
            except Exception:
                loop = None
            if loop is not None:
                loop.call_soon_threadsafe(_set_future_result_if_pending, future, lease)
            else:
                _set_future_result_if_pending(future, lease)


def _set_future_result_if_pending(future: asyncio.Future[ToolSlotLease], lease: ToolSlotLease) -> None:
    if not future.done():
        future.set_result(lease)


def _set_future_exception_if_pending(future: asyncio.Future[ToolSlotLease], exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)
