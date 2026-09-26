from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from loguru import logger

from main.runtime.model_key_concurrency import ModelKeyConcurrencyController, ModelKeyPermitLease
from main.runtime.model_load_balancer import ModelLoadBalancer
from main.runtime.model_route import (
    LEASE_OUTCOME_BUILD_FAILED,
    LEASE_OUTCOME_CANCELLED,
    LEASE_OUTCOME_SUCCESS,
    ModelRouteLease,
    ModelRoutePlan,
    RouteCandidateFilters,
)

# 有界扫描宽度：队头成员拿不出 permit 时，向后看这么多条请求，避免整条队列被一个
# 暂时不可用的模型钉死。
MAX_SCAN_REQUESTS = 8
# 防饿死上界：一条请求被越过这么多此后必须等它自己可用，否则低负载车道的请求会把
# 高负载车道的请求无限期推后。
SKIP_AGING_LIMIT = 4


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class NodeTurnLease:
    lease_id: int
    task_id: str
    node_id: str
    model_ref: str
    key_index: int
    acquired_at: str
    initial_model_permit: ModelKeyPermitLease | None = None
    queued_at: str = ""
    route_index: int = 0
    group_key: str = ""
    route_plan: ModelRoutePlan | None = None
    route_lease: ModelRouteLease | None = None
    # 准入阶段已经算好的候选硬性要求，chat_backend 的后续选择沿用同一份。
    route_filters: RouteCandidateFilters | None = None

    @property
    def selected_model_ref(self) -> str:
        """本次回合实际绑定的模型：优先取 balancer 的选择结果。"""
        if self.route_lease is not None:
            return str(self.route_lease.model_key or "")
        return str(self.model_ref or "")


@dataclass(slots=True)
class _QueuedNodeTurnRequest:
    future: asyncio.Future[NodeTurnLease]
    task_id: str
    node_id: str
    model_ref: str
    queued_at: str
    queued_mono: float
    route_plan: ModelRoutePlan | None = None
    filters: RouteCandidateFilters | None = None
    skipped: int = 0


class NodeTurnController:
    def __init__(
        self,
        *,
        model_concurrency_controller: ModelKeyConcurrencyController,
        balancer: ModelLoadBalancer | None = None,
        gate_supplier: Callable[[], bool] | None = None,
        freeze_supplier: Callable[[str], bool] | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self._model_concurrency_controller = model_concurrency_controller
        self._balancer = balancer
        self._gate_supplier = gate_supplier if callable(gate_supplier) else (lambda: True)
        self._freeze_supplier = freeze_supplier if callable(freeze_supplier) else (lambda _task_id: False)
        self._poll_interval_seconds = max(0.05, float(poll_interval_seconds or 0.1))
        self._lock = threading.RLock()
        self._queue: deque[_QueuedNodeTurnRequest] = deque()
        self._frozen_queues: dict[str, deque[_QueuedNodeTurnRequest]] = {}
        self._running_leases: dict[int, NodeTurnLease] = {}
        self._next_lease_id = 0
        self._wake_event: asyncio.Event | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    def configure(
        self,
        *,
        gate_supplier: Callable[[], bool] | None = None,
        freeze_supplier: Callable[[str], bool] | None = None,
        balancer: ModelLoadBalancer | None = None,
    ) -> None:
        with self._lock:
            if callable(gate_supplier):
                self._gate_supplier = gate_supplier
            if callable(freeze_supplier):
                self._freeze_supplier = freeze_supplier
            if balancer is not None:
                self._balancer = balancer
        self.poke()

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            oldest_wait_ms = 0.0
            queued_items = list(self._queue)
            for queue in self._frozen_queues.values():
                queued_items.extend(list(queue or []))
            if queued_items:
                oldest_queued_mono = min(float(item.queued_mono or 0.0) for item in queued_items)
                oldest_wait_ms = max(0.0, (time.perf_counter() - oldest_queued_mono) * 1000.0)
            return {
                "node_queue_running_count": int(len(self._running_leases)),
                "node_queue_waiting_count": int(len(self._queue) + sum(len(queue) for queue in self._frozen_queues.values())),
                "node_queue_frozen_count": int(sum(len(queue) for queue in self._frozen_queues.values())),
                "node_queue_oldest_wait_ms": round(oldest_wait_ms, 3),
            }

    async def acquire_turn(
        self,
        *,
        task_id: str,
        node_id: str,
        model_ref: str = "",
        route_plan: ModelRoutePlan | None = None,
        filters: RouteCandidateFilters | None = None,
    ) -> NodeTurnLease:
        """取得一个节点回合的执行权。

        给了 `route_plan` 时**首次模型选择就发生在这里**：pump 在同一个原子操作里向
        balancer 要候选并拿到该成员/key 的 permit。必须在 request preflight 之后调用，
        这样 context window 与多模态的候选过滤条件已经算好。
        没给 `route_plan` 时保持旧行为：按传入的 `model_ref` 预占 permit。
        """
        normalized_task_id = str(task_id or "").strip()
        normalized_node_id = str(node_id or "").strip()
        normalized_model_ref = str(model_ref or "").strip()
        if not normalized_task_id or not normalized_node_id:
            raise ValueError("task_id and node_id are required")
        if route_plan is None and not normalized_model_ref:
            raise ValueError("model_ref or route_plan is required")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[NodeTurnLease] = loop.create_future()
        with self._lock:
            self._ensure_pump_locked(loop)
            request = _QueuedNodeTurnRequest(
                future=future,
                task_id=normalized_task_id,
                node_id=normalized_node_id,
                model_ref=normalized_model_ref,
                queued_at=_now_iso(),
                queued_mono=time.perf_counter(),
                route_plan=route_plan,
                filters=filters,
            )
            if self._is_task_frozen_locked(normalized_task_id):
                self._frozen_queue_for_task_locked(normalized_task_id).append(request)
            else:
                self._queue.append(request)
        self.poke()
        try:
            return await future
        except Exception:
            with self._lock:
                self._queue = deque(item for item in self._queue if item.future is not future)
                for task_id, queue in list(self._frozen_queues.items()):
                    next_queue = deque(item for item in queue if item.future is not future)
                    if next_queue:
                        self._frozen_queues[task_id] = next_queue
                    else:
                        self._frozen_queues.pop(task_id, None)
            self.poke()
            raise

    def release_turn(self, lease: NodeTurnLease | None) -> None:
        if lease is None:
            return
        with self._lock:
            self._running_leases.pop(int(lease.lease_id or 0), None)
        # 回合结束但准入 permit 没被 chat 消费（取消、preflight 失败、chat 抛错）时在这
        # 里兜底归还，否则那颗 permit 与 reserved 会一直挂在被选中的成员上。
        self.release_route_lease(lease, outcome=LEASE_OUTCOME_CANCELLED)
        self.poke()

    def release_route_lease(self, lease: NodeTurnLease | None, *, outcome: str = LEASE_OUTCOME_SUCCESS, error: str = "") -> None:
        """归还 balancer 侧的 route lease（含底层 permit）。幂等。"""
        if lease is None:
            return
        route_lease = lease.route_lease
        if route_lease is None:
            return
        if self._balancer is not None:
            self._balancer.release(route_lease, outcome=outcome, error=error)
        lease.route_lease = None
        lease.initial_model_permit = None
        logger.info(
            "Model route lease released: group={} selected_model_key={} outcome={} node_id={} error={}",
            route_lease.group_key,
            route_lease.model_key,
            str(outcome or ''),
            lease.node_id,
            str(error or '')[:200],
        )

    def rebind_turn(
        self,
        lease: NodeTurnLease | None,
        *,
        route_index: int | None = None,
        filters: RouteCandidateFilters | None = None,
        excluded_model_keys: frozenset[str] = frozenset(),
        rebind_reason: str = "",
    ) -> ModelRouteLease | None:
        """组内前进：释放旧成员 lease，在**同一个 node-turn lease** 上重绑下一个候选。

        不创建第二个 node-turn lease，否则同一个节点会同时占着两个回合权。
        """
        if lease is None or self._balancer is None:
            return None
        plan = lease.route_plan
        if plan is None:
            return None
        target_index = int(lease.route_index if route_index is None else route_index)
        route = plan.route_at(target_index)
        if route is None or not route.is_load_balance:
            return None
        base_filters = filters or lease.route_filters or RouteCandidateFilters()
        effective_filters = RouteCandidateFilters(
            required_context_window_tokens=base_filters.required_context_window_tokens,
            requires_image_multimodal=base_filters.requires_image_multimodal,
            excluded_model_keys=frozenset(set(base_filters.excluded_model_keys) | set(excluded_model_keys)),
        )
        with self._lock:
            current_model_key = str(getattr(lease.route_lease, "model_key", "") or "") if lease.route_lease is not None else ""
            if lease.route_lease is not None:
                self._balancer.release(lease.route_lease, outcome=LEASE_OUTCOME_BUILD_FAILED)
                lease.route_lease = None
            next_lease, _reason = self._balancer.select(
                node_id=lease.node_id,
                route_index=target_index,
                group_key=str(route.group_key),
                filters=effective_filters,
                task_id=lease.task_id,
                rebind=True,
                rebind_reason=rebind_reason or "fallback_after_failure",
            )
            if next_lease is None:
                lease.initial_model_permit = None
                return None
            lease.route_index = target_index
            lease.group_key = str(next_lease.group_key)
            lease.model_ref = str(next_lease.model_key)
            lease.key_index = int(next_lease.key_index)
            lease.route_lease = next_lease
            lease.initial_model_permit = next_lease.permit
            lease.route_filters = effective_filters
            logger.info(
                "Model node binding rebound: group={} previous_model_key={} selected_model_key={} "
                "rebind_reason={} excluded_model_keys={} node_id={}",
                next_lease.group_key,
                str(current_model_key or ''),
                next_lease.model_key,
                next_lease.sticky_rebind_reason or rebind_reason,
                ",".join(sorted(excluded_model_keys)) or '-',
                lease.node_id,
            )
            return next_lease

    def advance_turn_route(
        self,
        lease: NodeTurnLease | None,
        *,
        route_index: int,
        filters: RouteCandidateFilters | None = None,
    ) -> ModelRouteLease | None:
        """整条 route 前进到下一个 entry（组整体耗尽后进入 direct 或下一组）。"""
        return self.rebind_turn(
            lease,
            route_index=route_index,
            filters=filters,
            excluded_model_keys=frozenset(),
            rebind_reason="group_exhausted",
        )

    def forget_route_binding(self, node_id: str) -> None:
        """节点结束或阶段边界：清掉 balancer 里该节点的粘滞绑定。"""
        if self._balancer is not None:
            self._balancer.forget_node(node_id)

    def record_route_request_start(self, lease: NodeTurnLease | None) -> None:
        """通知 balancer「这一发真的要打到 provider 了」：reserved 转成速率样本。"""
        if lease is None or self._balancer is None:
            return
        self._balancer.record_request_start(lease.route_lease)

    def record_route_outcome(
        self,
        lease: NodeTurnLease | None,
        *,
        status_code: int | None = None,
        error_text: str = "",
    ) -> None:
        if lease is None or self._balancer is None:
            return
        self._balancer.record_outcome(lease.route_lease, status_code=status_code, error_text=error_text)

    def poke(self) -> None:
        wake_event = self._wake_event
        loop = self._loop
        if wake_event is None or loop is None:
            return
        try:
            loop.call_soon_threadsafe(wake_event.set)
        except Exception:
            return

    async def close(self) -> None:
        pump_task = self._pump_task
        self._pump_task = None
        with self._lock:
            self._closed = True
            while self._queue:
                request = self._queue.popleft()
                if not request.future.done():
                    request.future.cancel()
            for task_id in list(self._frozen_queues.keys()):
                queue = self._frozen_queues.pop(task_id, deque())
                while queue:
                    request = queue.popleft()
                    if not request.future.done():
                        request.future.cancel()
        self.poke()
        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)
        self._wake_event = None
        self._loop = None

    def _ensure_pump_locked(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._closed:
            raise RuntimeError("node turn controller is closed")
        self._loop = loop
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        if self._pump_task is not None and not self._pump_task.done():
            return
        self._pump_task = loop.create_task(self._pump(), name="node-turn-controller")

    async def _pump(self) -> None:
        try:
            while True:
                granted = False
                while True:
                    if not bool(self._gate_supplier()):
                        break
                    window = self._grant_window()
                    if not window:
                        if not self._has_pending_requests():
                            return
                        break
                    chosen = None
                    for request in window:
                        lease = self._try_grant(request)
                        if lease is None:
                            continue
                        chosen = request
                        break
                    if chosen is None:
                        break
                    # 只有真被后来者越过的请求才计 skipped；到上界后它成为屏障，
                    # 避免低负载车道的请求把高负载车道的请求无限期推后。
                    for request in window:
                        if request is chosen:
                            break
                        request.skipped += 1
                    granted = True
                wake_event = self._wake_event
                if wake_event is None:
                    return
                if granted:
                    await asyncio.sleep(0)
                    continue
                wake_event.clear()
                try:
                    await asyncio.wait_for(wake_event.wait(), timeout=self._poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._pump_task = None

    def _grant_window(self) -> list[_QueuedNodeTurnRequest]:
        """本轮可以尝试的请求：队头优先，最多看 `MAX_SCAN_REQUESTS` 条。

        到达防饿死上界的请求单独返回，成为硬屏障：它不可用时本轮不再授予后面的人。
        """
        with self._lock:
            self._reconcile_frozen_queues_locked()
            # 已取消的请求就地剔除：留在队列里会让 pump 每轮空转，`_has_pending_requests`
            # 也永远返回 True。
            if any(item.future.cancelled() for item in self._queue):
                self._queue = deque(item for item in self._queue if not item.future.cancelled())
            window: list[_QueuedNodeTurnRequest] = []
            for request in self._queue:
                if request.skipped >= SKIP_AGING_LIMIT:
                    return [request]
                window.append(request)
                if len(window) >= MAX_SCAN_REQUESTS:
                    break
            return window

    def _try_grant(self, request: _QueuedNodeTurnRequest) -> NodeTurnLease | None:
        """为一条请求授予回合权；选择与 permit 在同一把锁内原子完成。"""
        with self._lock:
            plan = request.route_plan
            if plan is None:
                permit = self._model_concurrency_controller.try_acquire_first_available(model_ref=request.model_ref)
                if permit is None:
                    return None
                return self._register_grant_locked(
                    request,
                    model_ref=str(permit.model_ref or request.model_ref),
                    key_index=int(permit.key_index),
                    initial_model_permit=permit,
                )

            for route in plan.routes:
                if route.is_load_balance and self._balancer is not None:
                    route_lease, _reason = self._balancer.select(
                        node_id=request.node_id,
                        route_index=int(route.index),
                        group_key=str(route.group_key),
                        filters=request.filters,
                        task_id=request.task_id,
                    )
                    if route_lease is None:
                        # 这个组给不出候选（busy / 全部冷却 / 无容量），按链向后前进。
                        continue
                    return self._register_grant_locked(
                        request,
                        model_ref=str(route_lease.model_key),
                        key_index=int(route_lease.key_index),
                        initial_model_permit=route_lease.permit,
                        route_plan=plan,
                        route_index=int(route.index),
                        group_key=str(route_lease.group_key),
                        route_lease=route_lease,
                        filters=request.filters,
                    )
                permit = self._model_concurrency_controller.try_acquire_first_available(model_ref=str(route.model_key or ""))
                if permit is None:
                    continue
                return self._register_grant_locked(
                    request,
                    model_ref=str(permit.model_ref or route.model_key),
                    key_index=int(permit.key_index),
                    initial_model_permit=permit,
                    route_plan=plan,
                    route_index=int(route.index),
                )
            return None

    def _register_grant_locked(
        self,
        request: _QueuedNodeTurnRequest,
        *,
        model_ref: str,
        key_index: int,
        initial_model_permit: ModelKeyPermitLease | None,
        route_plan: ModelRoutePlan | None = None,
        route_index: int = 0,
        group_key: str = "",
        route_lease: ModelRouteLease | None = None,
        filters: RouteCandidateFilters | None = None,
    ) -> NodeTurnLease | None:
        # 授予的是**这条**请求，不是队头：有界扫描之后队头可能已经不是它。
        try:
            self._queue.remove(request)
        except ValueError:
            self._discard_grant_locked(route_lease=route_lease, initial_model_permit=initial_model_permit)
            return None
        if request.future.cancelled():
            # 请求已被取消：把刚拿到的 permit / route lease 原样归还，不能留下幽灵占用。
            self._discard_grant_locked(route_lease=route_lease, initial_model_permit=initial_model_permit)
            return None
        self._next_lease_id += 1
        lease = NodeTurnLease(
            lease_id=self._next_lease_id,
            task_id=request.task_id,
            node_id=request.node_id,
            model_ref=str(model_ref or ""),
            key_index=int(key_index),
            acquired_at=_now_iso(),
            initial_model_permit=initial_model_permit,
            queued_at=request.queued_at,
            route_index=int(route_index),
            group_key=str(group_key or ""),
            route_plan=route_plan,
            route_lease=route_lease,
            route_filters=filters,
        )
        self._running_leases[lease.lease_id] = lease
        if route_lease is not None:
            logger.info(
                "Model route selected: group={} selected_model_key={} route_index={} selection_reason={} "
                "rebind_reason={} running_before={} waiting_before={} reserved_before={} rolling_rpm_60s={} "
                "penalty_429={} local_capacity={} load_score={} config_revision={} task_id={} node_id={}",
                route_lease.group_key,
                route_lease.model_key,
                int(route_lease.route_index),
                route_lease.selection_reason,
                route_lease.sticky_rebind_reason,
                int(route_lease.running_before),
                int(route_lease.waiting_before),
                int(route_lease.reserved_before),
                int(route_lease.rolling_rpm),
                round(float(route_lease.penalty_before), 4),
                route_lease.local_capacity,
                float(route_lease.score),
                int(route_lease.config_revision),
                lease.task_id,
                lease.node_id,
            )
        _set_future_result_if_pending(request.future, lease)
        return lease

    def _discard_grant_locked(
        self,
        *,
        route_lease: ModelRouteLease | None,
        initial_model_permit: ModelKeyPermitLease | None,
    ) -> None:
        """归还一次尚未交付的授予。route lease 自带底层 permit，只归还一次。"""
        if route_lease is not None and self._balancer is not None:
            self._balancer.release(route_lease, outcome=LEASE_OUTCOME_CANCELLED)
            return
        if initial_model_permit is not None:
            self._model_concurrency_controller.release(initial_model_permit)

    def _has_pending_requests(self) -> bool:
        with self._lock:
            if self._queue:
                return True
            return any(queue for queue in self._frozen_queues.values())

    def _reconcile_frozen_queues_locked(self) -> None:
        if self._queue:
            next_queue: deque[_QueuedNodeTurnRequest] = deque()
            while self._queue:
                request = self._queue.popleft()
                if request.future.cancelled():
                    continue
                if self._is_task_frozen_locked(request.task_id):
                    self._frozen_queue_for_task_locked(request.task_id).append(request)
                else:
                    next_queue.append(request)
            self._queue = next_queue
        thawed_task_ids = [
            task_id
            for task_id in list(self._frozen_queues.keys())
            if not self._is_task_frozen_locked(task_id)
        ]
        for task_id in thawed_task_ids:
            queue = self._frozen_queues.pop(task_id, deque())
            while queue:
                request = queue.popleft()
                if request.future.cancelled():
                    continue
                self._queue.append(request)

    def _frozen_queue_for_task_locked(self, task_id: str) -> deque[_QueuedNodeTurnRequest]:
        key = str(task_id or "").strip()
        queue = self._frozen_queues.get(key)
        if queue is None:
            queue = deque()
            self._frozen_queues[key] = queue
        return queue

    def _is_task_frozen_locked(self, task_id: str) -> bool:
        try:
            return bool(self._freeze_supplier(str(task_id or "").strip()))
        except Exception:
            return False


def _set_future_result_if_pending(future: asyncio.Future[NodeTurnLease], lease: NodeTurnLease) -> None:
    if not future.done():
        future.set_result(lease)
