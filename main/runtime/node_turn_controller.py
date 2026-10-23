from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from loguru import logger

from main.runtime.model_key_concurrency import ModelKeyConcurrencyController, ModelKeyPermitLease
from main.runtime.model_load_balancer import (
    ModelLoadBalancer,
    classify_throttle_dimension,
    is_rate_limited,
)
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
# 队头独占的兜底时限：一条请求独占授予窗口（`skipped` 已到上界）超过这么久仍未被授予，
# 就说明它等不来答案。静态不可能的当场结清（否则这条独占会把所有任务的回合队列钉死，
# 2026-10-12 task:d07d706989b4：11:55:29 之后 0 次授予、零错误行、110 分钟）；
# 纯容量类的只出读数，不打断正常排队。
HEAD_PIN_LIMIT_SECONDS = 600.0
# 静态不可能：候选过滤/组本身决定了无论多少容量都不会被授予，入队前就挡掉。
UNSERVABLE_REASONS = frozenset({"no_candidate", "unknown_group", "group_disabled", "empty_plan", "empty_model_ref"})


class NodeTurnUnservableError(RuntimeError):
    """这次回合的请求条件比链上任何候选都宽（或链本身不存在），排队等不来授予。

    携带判定时的观测值，让调用方能落一条带名字的暂停而不是"节点永远 in_progress"。
    """

    def __init__(
        self,
        *,
        task_id: str,
        node_id: str,
        reason: str,
        required_window_tokens: int = 0,
        largest_candidate_window_tokens: int = 0,
        detail: str = "",
    ) -> None:
        self.task_id = str(task_id or "")
        self.node_id = str(node_id or "")
        self.reason = str(reason or "")
        self.required_window_tokens = int(required_window_tokens or 0)
        self.largest_candidate_window_tokens = int(largest_candidate_window_tokens or 0)
        self.detail = str(detail or "")
        super().__init__(
            f"node turn unservable: task={self.task_id} node={self.node_id} reason={self.reason} "
            f"required_window={self.required_window_tokens} largest_candidate_window="
            f"{self.largest_candidate_window_tokens} {self.detail}".strip()
        )


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
    # ---- 逐跳路由归属观测（只读台账用，不参与任何判据）----
    # 准入落点与 `route_index`/`group_key` 的区别：后两者会被组内换人和升档改写，
    # 而"这一跳本来被授到哪一档"只在第一次授予时记一次，之后不再动。
    admission_route_index: int = -1
    admission_group_key: str = ""
    # 真正把请求发出去时绑的档（换过人就算成后来那个）。
    served_route_index: int = -1
    served_group_key: str = ""
    # 这一跳内 route_index 前进过几次、最后一次为什么。词表沿用现有 release/rebind
    # 的字面值（group_exhausted / route_entry），不发明新词。
    escalation_count: int = 0
    escalation_reason: str = ""
    # 这一跳吃过的限流维度集合（rps/tpm/rpm/tpm_rpm/token/unknown），按首次出现顺序。
    throttle_dimensions: list[str] = field(default_factory=list)

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
    # 最近一次"为什么没给它授予"的原因，由 `_try_grant` 记下；队头独占的兜底判据读它。
    busy_reason: str = ""
    # 它第一次把授予窗口独占成单条的时刻（`skipped` 到达上界），独占时限判据读这个而不是入队时刻。
    pinned_mono: float = 0.0


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
            head = self._pinned_head()
            head_pinned_seconds = 0.0
            if head is not None and head.pinned_mono:
                head_pinned_seconds = max(0.0, time.perf_counter() - float(head.pinned_mono))
            return {
                "node_queue_running_count": int(len(self._running_leases)),
                "node_queue_waiting_count": int(len(self._queue) + sum(len(queue) for queue in self._frozen_queues.values())),
                "node_queue_frozen_count": int(sum(len(queue) for queue in self._frozen_queues.values())),
                "node_queue_oldest_wait_ms": round(oldest_wait_ms, 3),
                # 队头读数：跑动中却一次授予都不发时，这三个数就是"谁把队列钉住、为什么"。
                "node_queue_head_pinned_seconds": round(head_pinned_seconds, 3),
                "node_queue_head_busy_reason": str(getattr(head, "busy_reason", "") or ""),
                "node_queue_head_node_id": str(getattr(head, "node_id", "") or ""),
                "node_queue_head_required_window": int(
                    getattr(getattr(head, "filters", None), "required_context_window_tokens", 0) or 0
                ),
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
        # 静态不可能（体量比链上任何候选都大、组不存在或被停）一律不入队：这类请求一旦
        # 入队，`skipped` 爬到上界后会把授予窗口独占成只剩它自己，所有任务的回合一起停。
        unservable = self._unservable_error(
            task_id=normalized_task_id,
            node_id=normalized_node_id,
            model_ref=normalized_model_ref,
            route_plan=route_plan,
            filters=filters,
        )
        if unservable is not None:
            raise unservable
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
        except BaseException:
            # `CancelledError` 不是 `Exception`：按 Exception 接就漏掉取消路径，队列里会留下
            # 永远无人应答的项（与 0a22fb65 在回合闸那侧修掉的是同一个形状）。
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

    def release_turn(self, lease: NodeTurnLease | None, *, outcome: str = LEASE_OUTCOME_CANCELLED) -> None:
        """收尾一次回合：归还准入槽，并把没被 chat 消费掉的 permit 兜底还回去。

        `outcome` 由调用方按这次授予的真实终态给：默认 `cancelled` 表示授予始终没换成一次
        成功的模型调用（取消、preflight 失败、chat 抛错），否则那颗 permit 与 reserved 会
        一直挂在被选中的成员上。
        """
        if lease is None:
            return
        with self._lock:
            self._running_leases.pop(int(lease.lease_id or 0), None)
        self.release_route_lease(lease, outcome=outcome)
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
            "Model route lease released: group={} selected_model_key={} route_index={} outcome={} "
            "escalations={} node_id={} error={}",
            route_lease.group_key,
            route_lease.model_key,
            int(route_lease.route_index),
            str(outcome or ''),
            int(lease.escalation_count),
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
        previous_index = int(lease.route_index)
        target_index = int(lease.route_index if route_index is None else route_index)
        route = plan.route_at(target_index)
        if route is None or not route.is_load_balance:
            return None
        base_filters = filters or lease.route_filters or RouteCandidateFilters()
        effective_filters = RouteCandidateFilters(
            required_context_window_tokens=base_filters.required_context_window_tokens,
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
            if int(target_index) > previous_index:
                # 链上前进一格 = 跨档（组内换人不计，那一格号不变）。这是台账里
                # `route_escalation_count` 的唯一增点，词表沿用调用方传进来的 reason。
                lease.escalation_count += 1
                lease.escalation_reason = str(rebind_reason or "route_entry")
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
        """节点结束：清掉 balancer 里该节点的粘滞绑定（绑定跨回合保留）。"""
        if self._balancer is not None:
            self._balancer.forget_node(node_id)

    def record_route_request_start(self, lease: NodeTurnLease | None) -> None:
        """通知 balancer「这一发真的要打到 provider 了」：reserved 转成速率样本。"""
        if lease is None or self._balancer is None:
            return
        route_lease = lease.route_lease
        if route_lease is not None:
            # 服务档：只有真的发出去的那一发才算。组内换人/升档之后再发，读到的就是后来
            # 那个档；从没发出去过的跳保持哨兵。
            lease.served_route_index = int(route_lease.route_index)
            lease.served_group_key = str(route_lease.group_key or "")
        self._balancer.record_request_start(route_lease)

    def record_route_outcome(
        self,
        lease: NodeTurnLease | None,
        *,
        status_code: int | None = None,
        error_text: str = "",
    ) -> None:
        if lease is None or self._balancer is None:
            return
        route_lease = lease.route_lease
        if route_lease is not None and is_rate_limited(status_code, error_text):
            # 限流维度：balancer 内部早就分类过（它只把它存进 throttle_events 然后裁掉，
            # 全仓没有读者）。这里按同一份判据把它留到**这一跳**的观察面上——后续"该等多久"
            # 的改动唯一的读数来源就是它。按首次出现顺序去重。
            dimension = classify_throttle_dimension(error_text)
            if dimension not in lease.throttle_dimensions:
                lease.throttle_dimensions.append(dimension)
        self._balancer.record_outcome(route_lease, status_code=status_code, error_text=error_text)

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
                    if len(window) == 1 and self._settle_pinned_request(window[0]):
                        # 队头被结清，授予窗口立刻重新计算（不等下一拍，也不睡）。
                        granted = True
                        continue
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
                    if not request.pinned_mono:
                        request.pinned_mono = time.perf_counter()
                    return [request]
                window.append(request)
                if len(window) >= MAX_SCAN_REQUESTS:
                    break
            return window

    def _unservable_error(
        self,
        *,
        task_id: str,
        node_id: str,
        model_ref: str,
        route_plan: ModelRoutePlan | None,
        filters: RouteCandidateFilters | None,
    ) -> NodeTurnUnservableError | None:
        """判这条请求是否"无论多少容量都不会被授予"。判不出来就返回 None，保持原排队语义。

        只认静态条件（组存在且启用、候选过滤放得下这个体量），不看 permit/在飞/限流——
        那些是"暂时没位置"，该排队。误挡一次是把一个本可前进的节点打成暂停，代价比
        让一条永不满足的请求钉死整机小得多，所以判据要求"整条链都不可服务"才动手。
        """
        required_window = int(getattr(filters, "required_context_window_tokens", 0) or 0) if filters is not None else 0
        if route_plan is None:
            if str(model_ref or "").strip():
                return None
            return self._build_unservable_error(
                task_id=task_id,
                node_id=node_id,
                reason="empty_model_ref",
                required_window=required_window,
                largest_candidate_window=0,
            )
        routes = list(getattr(route_plan, "routes", None) or [])
        if not routes:
            return self._build_unservable_error(
                task_id=task_id,
                node_id=node_id,
                reason="empty_plan",
                required_window=required_window,
                largest_candidate_window=0,
            )
        serveability = getattr(self._balancer, "serveability", None)
        last_reason = ""
        largest = 0
        for route in routes:
            if not bool(getattr(route, "is_load_balance", False)):
                if str(getattr(route, "model_key", "") or "").strip():
                    return None
                last_reason = "empty_model_ref"
                continue
            if not callable(serveability):
                return None
            ok, reason, candidate_largest = serveability(
                group_key=str(getattr(route, "group_key", "") or ""),
                filters=filters,
            )
            largest = max(largest, int(candidate_largest or 0))
            if ok:
                return None
            last_reason = str(reason or "") or last_reason
        if last_reason not in UNSERVABLE_REASONS:
            return None
        return self._build_unservable_error(
            task_id=task_id,
            node_id=node_id,
            reason=last_reason,
            required_window=required_window,
            largest_candidate_window=largest,
        )

    @staticmethod
    def _build_unservable_error(
        *,
        task_id: str,
        node_id: str,
        reason: str,
        required_window: int,
        largest_candidate_window: int,
        detail: str = "",
    ) -> NodeTurnUnservableError:
        error = NodeTurnUnservableError(
            task_id=task_id,
            node_id=node_id,
            reason=reason,
            required_window_tokens=required_window,
            largest_candidate_window_tokens=largest_candidate_window,
            detail=detail,
        )
        logger.warning(
            "Node turn request rejected as unservable: task_id={} node_id={} reason={} "
            "required_window={} largest_candidate_window={} {}",
            task_id,
            node_id,
            reason,
            int(required_window or 0),
            int(largest_candidate_window or 0),
            str(detail or ""),
        )
        return error

    def peek_hop_windows(self, *, node_id: str, route_plan: ModelRoutePlan | None) -> dict[str, Any] | None:
        """给压缩尺子指一个来源：按授予那侧同一条"从链首往后"的走法，预测这一跳落到哪个组。

        只用与尺寸无关的那半资格（组存在且启用、成员还在计划里、粘滞成员仍合格）：请求
        体量本来就是要由这把尺子决定的，拿体量当预测条件就是循环。预测偏大也不会误发——
        授予那侧仍按 `required_context_window_tokens` 筛成员，塞不下的成员拿不到 lease。
        判不出来（没有 balancer、这一跳落在 direct 链位、整条链都没人接单）返回 None，
        调用方回落到链级窗口：宁可退回旧口径，也不要在预测失败时把尺子打成 0。
        """
        if not str(node_id or "").strip() or route_plan is None:
            return None
        peek = getattr(self._balancer, "peek_selection", None)
        if not callable(peek):
            return None
        for route in list(getattr(route_plan, "routes", None) or []):
            if not bool(getattr(route, "is_load_balance", False)):
                return None
            group_key = str(getattr(route, "group_key", "") or "").strip()
            if not group_key:
                continue
            min_window, model_key, reason = peek(
                node_id=str(node_id or "").strip(),
                group_key=group_key,
                filters=RouteCandidateFilters(),
            )
            if reason:
                continue
            if int(min_window or 0) <= 0:
                continue
            return {
                "group_key": group_key,
                "model_key": str(model_key or ""),
                "min_window": int(min_window or 0),
            }
        return None

    def _drop_request_locked(self, request: _QueuedNodeTurnRequest) -> None:
        self._queue = deque(item for item in self._queue if item is not request)
        for frozen_task_id, frozen_queue in list(self._frozen_queues.items()):
            kept = deque(item for item in frozen_queue if item is not request)
            if kept:
                self._frozen_queues[frozen_task_id] = kept
            else:
                self._frozen_queues.pop(frozen_task_id, None)

    def _settle_pinned_request(self, request: _QueuedNodeTurnRequest) -> bool:
        """队头独占超过时限：静态不可能的当场结清，把整条队列让出来；容量类的不动。"""
        reason = str(request.busy_reason or "")
        if reason not in UNSERVABLE_REASONS:
            return False
        if not request.pinned_mono:
            return False
        pinned_seconds = max(0.0, time.perf_counter() - float(request.pinned_mono))
        if pinned_seconds < HEAD_PIN_LIMIT_SECONDS:
            return False
        error = self._build_unservable_error(
            task_id=request.task_id,
            node_id=request.node_id,
            reason=reason,
            required_window=int(getattr(request.filters, "required_context_window_tokens", 0) or 0),
            largest_candidate_window=0,
            detail=f"pinned_head_seconds={round(pinned_seconds)}",
        )
        with self._lock:
            self._drop_request_locked(request)
        if not request.future.done():
            request.future.set_exception(error)
        self.poke()
        return True

    def _pinned_head(self) -> _QueuedNodeTurnRequest | None:
        with self._lock:
            for request in self._queue:
                if int(request.skipped or 0) >= SKIP_AGING_LIMIT:
                    return request
            return self._queue[0] if self._queue else None

    def _try_grant(self, request: _QueuedNodeTurnRequest) -> NodeTurnLease | None:
        """为一条请求授予回合权；选择与 permit 在同一把锁内原子完成。"""
        with self._lock:
            plan = request.route_plan
            if plan is None:
                permit = self._model_concurrency_controller.try_acquire_first_available(model_ref=request.model_ref)
                if permit is None:
                    request.busy_reason = "no_capacity"
                    return None
                request.busy_reason = ""
                return self._register_grant_locked(
                    request,
                    model_ref=str(permit.model_ref or request.model_ref),
                    key_index=int(permit.key_index),
                    initial_model_permit=permit,
                )

            last_reason = ""
            for route in plan.routes:
                if route.is_load_balance and self._balancer is not None:
                    route_lease, busy_reason = self._balancer.select(
                        node_id=request.node_id,
                        route_index=int(route.index),
                        group_key=str(route.group_key),
                        filters=request.filters,
                        task_id=request.task_id,
                    )
                    if route_lease is None:
                        # 这个组给不出候选（全部不合格 / 无容量），按链向后前进。
                        # 原因要留下：队头独占的兜底判据靠它分清"等得到"与"等不到"。
                        last_reason = str(busy_reason or "") or last_reason
                        continue
                    request.busy_reason = ""
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
                    last_reason = last_reason or "no_capacity"
                    continue
                request.busy_reason = ""
                return self._register_grant_locked(
                    request,
                    model_ref=str(permit.model_ref or route.model_key),
                    key_index=int(permit.key_index),
                    initial_model_permit=permit,
                    route_plan=plan,
                    route_index=int(route.index),
                )
            request.busy_reason = last_reason or "no_capacity"
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
            # 准入落点只在授予这一刻记一次：`route_index`/`group_key` 之后会被组内换人和
            # 升档改写，而"这一跳本来被授到哪一档"必须保持原值。无租约（direct 模型、
            # 前门/辅助车道）保持哨兵，不写成第 0 档。
            admission_route_index=int(route_lease.route_index) if route_lease is not None else -1,
            admission_group_key=str(route_lease.group_key) if route_lease is not None else "",
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
