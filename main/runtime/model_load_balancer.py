"""全局模型负载均衡器：组内平级选择、配额桶观测与节点粘滞绑定。

职责边界：本模块只回答「这个节点下一次该绑哪个成员」，并持有选择所需的运行态。请求
生命周期与 retry contract 留在 `chat_backend.py`，选择发生的时机由
`node_turn_controller.py` 决定。

三条不能省的约束：

1. **选择与 permit 原子**。分两步（先读负载、再单独 acquire）会让并发节点同时读到同一
   个最低值，然后全部打到同一成员，所以整个选择-预占-取permit 在本模块的一把锁内完成。
2. **负载口径不能只有本地在飞**。现网所有绑定的 `singleApiKeyMaxConcurrency` 都没配
   （`model_key_concurrency._slot_has_capacity_locked` 因此恒判可用），本地不会排队；
   真正的瓶颈是上游按分钟计的 RPM，所以 60 秒滚动请求数与衰减的 429 惩罚必须参与打分。
3. **配额按桶而不是按 binding**。多条绑定可以共用同一 endpoint + 同一把 key，把它们当
   成独立容量会虚增组容量。密钥明文只在进程内哈希，任何输出只给桶序号。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from main.runtime.model_route import (
    LEASE_OUTCOME_SUCCESS,
    LEASE_OUTCOME_UNAVAILABLE,
    ModelRouteLease,
    ResolvedLoadBalanceGroup,
    RouteCandidateFilters,
    RouteMemberView,
)

# 打分权重。三项都是归一化后的比值，量纲一致，压测时统一调这里。
W_LOCAL_INFLIGHT = 1.0
W_ROLLING_RPM = 0.6
W_429_PENALTY = 2.0

RPM_WINDOW_SECONDS = 60.0
# 惩罚半衰期对齐上游的分钟窗口：超过一分钟的 429 不该继续影响下一次选择，否则一次抖动
# 会把成员长期钉死。
PENALTY_HALF_LIFE_SECONDS = 60.0
PENALTY_RETENTION_SECONDS = PENALTY_HALF_LIFE_SECONDS * 6

COOLDOWN_SECONDS_DEFAULT = 30.0
COOLDOWN_SECONDS_MAX = 300.0
# 粘滞优先，但上游连续打回 429 到一定程度就必须换人：这个阈值是「衰减后的 429 计数」，
# 1.0 约等于最近一分钟内吃过一次满权重惩罚且尚未衰减。
PENALTY_REBIND_THRESHOLD = 1.0

UNRESOLVED_BUCKET_PREFIX = "unresolved:"

THROTTLE_DIMENSION_RPM = "rpm"
THROTTLE_DIMENSION_TPM = "tpm"
THROTTLE_DIMENSION_RPS = "rps"
THROTTLE_DIMENSION_TOKEN = "token"
THROTTLE_DIMENSION_UNKNOWN = "unknown"


class PermitSource(Protocol):
    """`ModelKeyConcurrencyController` 在本模块用到的那几个方法上的投影。"""

    def acquire_least_loaded(self, *, model_ref: str) -> Any: ...

    def release(self, permit: Any) -> None: ...

    def model_state(self, model_ref: str) -> dict[str, Any]: ...

    def effective_capacity(self, model_ref: str) -> int | None: ...


QuotaBucketResolver = Callable[[str], list[str]]


def classify_throttle_dimension(error_text: str) -> str:
    """尽力把 429 归到限流维度，归不出来记 unknown。

    只用于观测与加权，不参与「能不能 fallback」的判定——网关会把 429 标成
    `invalid_request_error`，文本不是可靠的分类依据（见 providers/fallback.py 里
    `is_request_shape_error` 的同款注释）。
    """
    lowered = str(error_text or "").lower()
    if "rpm" in lowered:
        return THROTTLE_DIMENSION_RPM
    if "tpm" in lowered:
        return THROTTLE_DIMENSION_TPM
    if "rps" in lowered:
        return THROTTLE_DIMENSION_RPS
    if "token" in lowered:
        return THROTTLE_DIMENSION_TOKEN
    return THROTTLE_DIMENSION_UNKNOWN


def is_rate_limited(status_code: int | None, error_text: str = "") -> bool:
    if status_code == 429:
        return True
    lowered = str(error_text or "").lower()
    return "error code: 429" in lowered or "ratelimiterror" in lowered or "rate limit" in lowered


def _looks_unavailable(error_text: str) -> bool:
    """配置/认证/无可用 key 这类「重试同一成员没意义」的错误，进入短期冷却。"""
    lowered = str(error_text or "").lower()
    markers = (
        "all configured api keys are disabled",
        "error code: 401",
        "error code: 403",
        "invalid api key",
        "api_key is required",
        "unknown model key",
    )
    return any(marker in lowered for marker in markers)


def _decay(age_seconds: float, half_life_seconds: float) -> float:
    if age_seconds <= 0:
        return 1.0
    return float(0.5 ** (age_seconds / max(0.001, float(half_life_seconds))))


@dataclass(slots=True)
class _BucketState:
    request_starts: deque[float] = field(default_factory=deque)
    penalty_events: deque[float] = field(default_factory=deque)
    throttle_events: deque[tuple[float, str]] = field(default_factory=deque)


@dataclass(slots=True)
class _MemberState:
    reserved: int = 0
    last_selected_seq: int = 0
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    consecutive_unavailable: int = 0


@dataclass(slots=True)
class _NodeBinding:
    group_key: str
    model_key: str
    route_index: int
    config_revision: int


@dataclass(slots=True)
class _MemberMetrics:
    running: int = 0
    waiting: int = 0
    reserved: int = 0
    rolling_rpm: int = 0
    penalty: float = 0.0
    local_capacity: int | None = None
    key_count: int = 1
    score: float = 0.0

    def as_parts(self) -> dict[str, float]:
        return {
            "local": self.running + self.waiting + self.reserved,
            "rolling_rpm": float(self.rolling_rpm),
            "penalty_429": float(self.penalty),
        }


class ModelLoadBalancer:
    def __init__(
        self,
        *,
        permit_source: PermitSource | None = None,
        resolve_quota_buckets: QuotaBucketResolver | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._permit_source = permit_source
        self._resolve_quota_buckets = resolve_quota_buckets if callable(resolve_quota_buckets) else None
        self._monotonic = monotonic
        self._groups: dict[str, ResolvedLoadBalanceGroup] = {}
        self._config_revision = 0
        self._selection_seq = 0
        self._lease_seq = 0
        self._buckets: dict[str, _BucketState] = {}
        self._members: dict[str, _MemberState] = {}
        self._bindings: dict[str, _NodeBinding] = {}

    # ------------------------------------------------------------------ 配置

    def configure(self, *, groups: dict[str, ResolvedLoadBalanceGroup] | None, config_revision: int) -> None:
        """装载组定义。revision 前进时清掉 cooldown，但保留 RPM 与惩罚观测。

        cooldown 表达的是「这条车道在当前状态下不可用」，配置被修好后不该继续被旧结论
        挡住；而最近的请求速率与 429 记录描述的是上游，与本地配置是否改过无关。
        """
        with self._lock:
            revision_changed = int(config_revision or 0) != int(self._config_revision or 0)
            self._groups = dict(groups or {})
            self._config_revision = int(config_revision or 0)
            live_members = {
                member.model_key
                for group in self._groups.values()
                for member in group.members
                if str(member.model_key or "").strip()
            }
            for model_key in list(self._members.keys()):
                if model_key not in live_members:
                    self._members.pop(model_key, None)
            for node_id, binding in list(self._bindings.items()):
                group = self._groups.get(binding.group_key)
                if group is None or binding.model_key not in group.candidate_model_keys:
                    # 成员被移出组：解绑，让该节点下次准入重选。在飞请求不受影响。
                    self._bindings.pop(node_id, None)
            if revision_changed:
                for state in self._members.values():
                    state.cooldown_until = 0.0
                    state.cooldown_reason = ""
                    state.consecutive_unavailable = 0

    @property
    def config_revision(self) -> int:
        return int(self._config_revision or 0)

    def group(self, group_key: str) -> ResolvedLoadBalanceGroup | None:
        return self._groups.get(str(group_key or "").strip())

    # ------------------------------------------------------------------ 选择

    def select(
        self,
        *,
        node_id: str,
        route_index: int,
        group_key: str,
        filters: RouteCandidateFilters | None = None,
        task_id: str = "",
        rebind: bool = False,
        rebind_reason: str = "",
    ) -> tuple[ModelRouteLease | None, str]:
        """给节点绑一个成员并原子取得 permit。

        返回 `(lease, busy_reason)`。lease 为 None 时 reason 说明这个组为什么给不出
        候选：`unknown_group` / `group_disabled` / `no_candidate` / `no_capacity`。
        调用方据此向后一个 route entry 前进，不在组里死等。
        """
        normalized_node_id = str(node_id or "").strip()
        if not normalized_node_id:
            raise ValueError("node_id is required for load-balance selection")
        effective_filters = filters or RouteCandidateFilters()
        with self._lock:
            group = self._groups.get(str(group_key or "").strip())
            if group is None:
                return None, "unknown_group"
            if not group.enabled:
                return None, "group_disabled"

            binding = None if rebind else self._bindings.get(normalized_node_id)
            binding_drop_reason = str(rebind_reason or "") if rebind else ""
            if binding is not None and binding.group_key != group.group_key:
                binding = None
                binding_drop_reason = "plan_changed"
            elif binding is not None and binding.config_revision != self._config_revision:
                binding_drop_reason = "plan_changed"
            elif binding is not None and binding.model_key not in group.candidate_model_keys:
                binding_drop_reason = "plan_changed"

            candidates = [
                member.model_key
                for member in group.members
                if self._candidate_ok(member, effective_filters)
            ]
            if not candidates:
                return None, "no_candidate"

            if binding is not None and binding.model_key in candidates:
                metrics = self._metrics(binding.model_key)
                if metrics.penalty >= PENALTY_REBIND_THRESHOLD:
                    binding_drop_reason = "penalty_threshold"
                    self._bindings.pop(normalized_node_id, None)
                    binding = None

            # 粘滞优先：绑定的成员只要还合格就先用它，只有它拿不出 permit 才换人。
            # 逐回合按 score 重选会把节点在成员之间来回抖，而换 model_key 等于换前缀
            # 缓存命名空间。
            if binding is not None and binding.model_key in candidates:
                lease = self._try_bind_locked(
                    node_id=normalized_node_id,
                    task_id=task_id,
                    route_index=route_index,
                    group=group,
                    model_key=binding.model_key,
                    sticky=True,
                    rebind_reason="",
                )
                if lease is not None:
                    return lease, ""
                self._bindings.pop(normalized_node_id, None)
                binding_drop_reason = binding_drop_reason or "capacity"
            elif binding is not None:
                binding_drop_reason = binding_drop_reason or self._binding_blocked_reason(binding.model_key, effective_filters)
                self._bindings.pop(normalized_node_id, None)

            ordered = sorted(candidates, key=lambda model_key: self._order_key(model_key))
            for model_key in ordered:
                lease = self._try_bind_locked(
                    node_id=normalized_node_id,
                    task_id=task_id,
                    route_index=route_index,
                    group=group,
                    model_key=model_key,
                    sticky=False,
                    rebind_reason=binding_drop_reason,
                )
                if lease is not None:
                    return lease, ""
            return None, "no_capacity"

    def _candidate_ok(self, member: RouteMemberView, filters: RouteCandidateFilters) -> bool:
        if not str(member.model_key or "").strip():
            return False
        if not filters.allows(member):
            return False
        return not self._in_cooldown(member.model_key)

    def _binding_blocked_reason(self, model_key: str, filters: RouteCandidateFilters) -> str:
        """解释「为什么粘滞失效」，让日志能回答是哪一类触发。"""
        state = self._state(model_key)
        if state.cooldown_until > self._monotonic():
            return "cooldown"
        member = None
        for group in self._groups.values():
            for candidate in group.members:
                if candidate.model_key == model_key:
                    member = candidate
                    break
            if member is not None:
                break
        if member is None:
            return "plan_changed"
        if not member.enabled or model_key in filters.excluded_model_keys:
            return "plan_changed"
        if filters.required_context_window_tokens and member.context_window_tokens < filters.required_context_window_tokens:
            return "filter_changed"
        if filters.requires_image_multimodal and not member.image_multimodal_enabled:
            return "filter_changed"
        return "plan_changed"

    def _order_key(self, model_key: str) -> tuple[float, int, str]:
        """score 升序 → 最久未被选中 → 稳定 key，保证可测且不固定首项。"""
        metrics = self._metrics(model_key)
        return (metrics.score, int(self._state(model_key).last_selected_seq), str(model_key))

    # ------------------------------------------------------------------ 观测与释放

    def record_request_start(self, lease: ModelRouteLease) -> None:
        """真实 provider 请求启动：reserved 交给底层 running，并给配额桶记速率样本。"""
        if lease is None or lease.request_started:
            return
        with self._lock:
            lease.request_started = True
            state = self._state(lease.model_key)
            state.reserved = max(0, int(state.reserved) - 1)
            now = self._monotonic()
            for bucket_key in self._buckets_for(lease.model_key):
                bucket = self._bucket(bucket_key)
                bucket.request_starts.append(now)
                self._trim(bucket.request_starts, RPM_WINDOW_SECONDS)

    def record_outcome(self, lease: ModelRouteLease, *, status_code: int | None = None, error_text: str = "") -> None:
        """失败归因：429 记衰减惩罚并按维度留痕；不可用类错误叠加短期冷却。"""
        if lease is None:
            return
        now = self._monotonic()
        rate_limited = is_rate_limited(status_code, error_text)
        with self._lock:
            buckets = self._buckets_for(lease.model_key)
            if rate_limited:
                for bucket_key in buckets:
                    bucket = self._bucket(bucket_key)
                    bucket.penalty_events.append(now)
                    bucket.throttle_events.append((now, classify_throttle_dimension(error_text)))
                    self._trim(bucket.penalty_events, PENALTY_RETENTION_SECONDS)
                    self._trim(bucket.throttle_events, PENALTY_RETENTION_SECONDS)
                return

            text = str(error_text or "").strip()
            if not text:
                return
            state = self._state(lease.model_key)
            if _looks_unavailable(text):
                state.consecutive_unavailable += 1
                seconds = min(COOLDOWN_SECONDS_MAX, COOLDOWN_SECONDS_DEFAULT * max(1, state.consecutive_unavailable))
                state.cooldown_until = now + seconds
                state.cooldown_reason = f"unavailable:{text[:120]}"
            else:
                # provider 侧的可重试失败不叠加 cooldown：冷却专留给重试没意义的状态。
                state.consecutive_unavailable = 0

    def release(self, lease: ModelRouteLease, *, outcome: str = LEASE_OUTCOME_SUCCESS, error: str = "") -> None:
        """exactly-once 释放：归还 permit，并把未派发出去的 reserved 归零。"""
        if lease is None or lease.released:
            return
        with self._lock:
            lease.released = True
            if not lease.request_started:
                state = self._state(lease.model_key)
                state.reserved = max(0, int(state.reserved) - 1)
            if outcome == LEASE_OUTCOME_UNAVAILABLE and error:
                self.record_outcome(lease, status_code=None, error_text=error)
            permit = lease.permit
            lease.permit = None
            if permit is not None and self._permit_source is not None:
                self._permit_source.release(permit)

    def forget_node(self, node_id: str) -> None:
        """节点结束或阶段边界：解除粘滞，下次准入重新按负载选择。"""
        normalized = str(node_id or "").strip()
        if not normalized:
            return
        with self._lock:
            self._bindings.pop(normalized, None)

    def bound_model_for_node(self, node_id: str) -> str:
        with self._lock:
            binding = self._bindings.get(str(node_id or "").strip())
            return str(binding.model_key) if binding is not None else ""

    # ------------------------------------------------------------------ 诊断

    def snapshot(self, *, group_key: str | None = None, filters: RouteCandidateFilters | None = None) -> dict[str, Any]:
        effective_filters = filters or RouteCandidateFilters()
        with self._lock:
            groups_payload: dict[str, Any] = {}
            for key, group in self._groups.items():
                if group_key is not None and str(key) != str(group_key):
                    continue
                bucket_indices = self._bucket_index_map([member.model_key for member in group.members])
                members_payload: list[dict[str, Any]] = []
                for member in group.members:
                    metrics = self._metrics(member.model_key)
                    state = self._state(member.model_key)
                    members_payload.append(
                        {
                            "model_key": member.model_key,
                            "enabled": bool(member.enabled),
                            "selectable": self._candidate_ok(member, effective_filters),
                            "running": metrics.running,
                            "waiting": metrics.waiting,
                            "reserved": metrics.reserved,
                            "rolling_rpm_60s": metrics.rolling_rpm,
                            "penalty_429": round(metrics.penalty, 6),
                            "local_capacity": metrics.local_capacity,
                            "score": round(metrics.score, 6),
                            "context_window_tokens": int(member.context_window_tokens or 0),
                            "image_multimodal_enabled": bool(member.image_multimodal_enabled),
                            "cooldown_until_monotonic": round(float(state.cooldown_until), 3),
                            "cooldown_reason": state.cooldown_reason,
                            "consecutive_unavailable": int(state.consecutive_unavailable),
                            "last_selected_at": int(state.last_selected_seq),
                            "quota_bucket_index": bucket_indices.get(member.model_key, -1),
                        }
                    )
                resolved_indices = [idx for idx in bucket_indices.values() if idx >= 0]
                unresolved_count = sum(1 for idx in bucket_indices.values() if idx < 0)
                groups_payload[str(key)] = {
                    "enabled": bool(group.enabled),
                    "max_retry_rounds": int(group.max_retry_rounds),
                    "members": members_payload,
                    "quota_bucket_count": len(set(resolved_indices)),
                    # 只报数量，不报指纹：日志与管理面都不允许出现 endpoint/key 线索。
                    # 「共享」= 落在同一个已解析桶里的多余成员数。
                    "shared_bucket_member_count": len(resolved_indices) - len(set(resolved_indices)),
                    "unresolved_bucket_count": unresolved_count,
                }
            return {
                "config_revision": int(self._config_revision),
                "groups": groups_payload,
                "node_bindings": [
                    {
                        "node_id": node_id,
                        "group_key": binding.group_key,
                        "model_key": binding.model_key,
                        "route_index": int(binding.route_index),
                        "config_revision": int(binding.config_revision),
                    }
                    for node_id, binding in self._bindings.items()
                ],
            }

    # ------------------------------------------------------------------ 内部

    def _try_bind_locked(
        self,
        *,
        node_id: str,
        task_id: str,
        route_index: int,
        group: ResolvedLoadBalanceGroup,
        model_key: str,
        sticky: bool,
        rebind_reason: str,
    ) -> ModelRouteLease | None:
        # 先取决策时刻的观测快照，再抢 permit：`running_before` 要描述「选中它时它有多忙」，
        # 而不是把这次自己的占用也算进去。整个顺序仍在一把锁内，选择与预占之间没有窗口。
        metrics = self._metrics(model_key)
        permit = None
        if self._permit_source is not None:
            permit = self._permit_source.acquire_least_loaded(model_ref=model_key)
            if permit is None:
                return None

        state = self._state(model_key)
        self._lease_seq += 1
        self._selection_seq += 1
        state.reserved = int(state.reserved) + 1
        state.last_selected_seq = self._selection_seq

        lease = ModelRouteLease(
            lease_id=self._lease_seq,
            node_id=node_id,
            task_id=str(task_id or ""),
            route_index=int(route_index),
            group_key=str(group.group_key),
            model_key=str(model_key),
            key_index=int(getattr(permit, "key_index", 0) or 0),
            quota_bucket_key=self._primary_bucket(model_key),
            selection_reason="sticky_reuse" if sticky else "least_load",
            config_revision=int(self._config_revision),
            permit=permit,
            score=round(metrics.score, 6),
            score_parts=metrics.as_parts(),
            running_before=metrics.running,
            waiting_before=metrics.waiting,
            reserved_before=metrics.reserved,
            rolling_rpm=metrics.rolling_rpm,
            penalty_before=round(metrics.penalty, 6),
            local_capacity=metrics.local_capacity,
            max_retry_rounds=int(group.max_retry_rounds or 1),
            sticky_rebind_reason="" if sticky else str(rebind_reason or ""),
        )
        self._bindings[node_id] = _NodeBinding(
            group_key=str(group.group_key),
            model_key=str(model_key),
            route_index=int(route_index),
            config_revision=int(self._config_revision),
        )
        return lease

    def _metrics(self, model_key: str) -> _MemberMetrics:
        now = self._monotonic()
        rolling = 0
        penalty = 0.0
        for bucket_key in self._buckets_for(model_key):
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                continue
            rolling += sum(1 for started in bucket.request_starts if now - float(started) <= RPM_WINDOW_SECONDS)
            penalty += sum(_decay(now - float(at), PENALTY_HALF_LIFE_SECONDS) for at in bucket.penalty_events)

        metrics = _MemberMetrics(rolling_rpm=rolling, penalty=float(penalty), reserved=int(self._state(model_key).reserved))
        if self._permit_source is not None:
            state = dict(self._permit_source.model_state(model_key) or {})
            metrics.running = sum(int(value or 0) for value in dict(state.get("running") or {}).values())
            metrics.waiting = sum(int(value or 0) for value in dict(state.get("waiting") or {}).values())
            metrics.key_count = max(1, int(state.get("key_count") or 1))
            metrics.local_capacity = self._permit_source.effective_capacity(model_key)

        inflight = metrics.running + metrics.waiting + metrics.reserved
        capacity = metrics.local_capacity
        # 本地没有显式上限时容量记 None：分母退回 key 数，而不是假装「无上限 = 容量 1」。
        denominator = float(capacity) if capacity else float(max(1, metrics.key_count))
        local_part = float(inflight) / max(1.0, denominator)
        rpm_part = float(rolling) / float(max(1, metrics.key_count))
        metrics.score = W_LOCAL_INFLIGHT * local_part + W_ROLLING_RPM * rpm_part + W_429_PENALTY * penalty
        return metrics

    def _state(self, model_key: str) -> _MemberState:
        return self._members.setdefault(str(model_key or "").strip(), _MemberState())

    def _in_cooldown(self, model_key: str) -> bool:
        state = self._state(model_key)
        if state.cooldown_until <= 0:
            return False
        if self._monotonic() < float(state.cooldown_until):
            return True
        state.cooldown_until = 0.0
        state.cooldown_reason = ""
        state.consecutive_unavailable = 0
        return False

    def _bucket(self, bucket_key: str) -> _BucketState:
        bucket = self._buckets.get(bucket_key)
        if bucket is None:
            bucket = _BucketState()
            self._buckets[bucket_key] = bucket
        return bucket

    def _trim(self, items: deque[Any], retention_seconds: float) -> None:
        """按保留窗口修剪队列。`throttle_events` 存的是 (时刻, 维度) 元组。"""
        cutoff = self._monotonic() - retention_seconds
        while items:
            head = items[0]
            started_at = float(head[0]) if isinstance(head, tuple) else float(head)
            if started_at >= cutoff:
                break
            items.popleft()

    def _buckets_for(self, model_key: str) -> list[str]:
        if self._resolve_quota_buckets is None:
            return [f"{UNRESOLVED_BUCKET_PREFIX}{model_key}"]
        try:
            raw = list(self._resolve_quota_buckets(str(model_key or "").strip()) or [])
        except Exception:
            return [f"{UNRESOLVED_BUCKET_PREFIX}{model_key}"]
        buckets = [str(item or "").strip() for item in raw if str(item or "").strip()]
        if buckets:
            return buckets
        # 解析不到密钥材料时（未解锁、或不在 worker 进程里）每个成员各自成桶。空值互并
        # 会把整组塌成一个「空 key」桶，产出假阳性的重复配额结论。
        return [f"{UNRESOLVED_BUCKET_PREFIX}{model_key}"]

    def _primary_bucket(self, model_key: str) -> str:
        buckets = self._buckets_for(model_key)
        return str(buckets[0]) if buckets else f"{UNRESOLVED_BUCKET_PREFIX}{model_key}"

    def _bucket_index_map(self, model_keys: list[str]) -> dict[str, int]:
        """给每个成员一个稳定的桶序号；负数表示该成员的配额身份不可解析。"""
        indices: dict[str, int] = {}
        seen: dict[str, int] = {}
        for model_key in model_keys:
            key = str(model_key or "").strip()
            if not key:
                continue
            buckets = self._buckets_for(key)
            if buckets and str(buckets[0]).startswith(UNRESOLVED_BUCKET_PREFIX):
                indices[key] = -1
                continue
            shared = min(buckets)
            if shared not in seen:
                seen[shared] = len(seen)
            indices[key] = seen[shared]
        return indices


def quota_bucket_key(*, endpoint: str, api_key: str, quota_pool_key: str = "") -> str:
    """由 endpoint + api key 生成仅存在于内存的桶标识（不可逆摘要）。

    `quota_pool_key` 是 operator 显式声明的共享账户，优先于自动指纹；解析不到 endpoint
    或 key 时返回空串，调用方必须按「身份未知」处理而不是互并。
    """
    pool = str(quota_pool_key or "").strip()
    if pool:
        return f"pool:{pool}"
    endpoint_text = str(endpoint or "").strip()
    key_text = str(api_key or "").strip()
    if not endpoint_text or not key_text:
        return ""
    digest = hashlib.sha256(f"{endpoint_text}\n{key_text}".encode("utf-8")).hexdigest()
    return f"key:{digest[:16]}"
