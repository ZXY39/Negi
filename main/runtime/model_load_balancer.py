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

from g3ku.providers.fallback import is_retryable_model_error
from main.runtime.model_route import (
    LEASE_OUTCOME_SUCCESS,
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

# 限流判据复用模型链自己的 `429` 关键字表（g3ku/utils/retry_keywords.py），不另起一套文本。
RATE_LIMIT_RETRY_ON = ["429"]

UNRESOLVED_BUCKET_PREFIX = "unresolved:"
# 桶身份要靠配置记录（磁盘 JSON + 解锁后的 overlay）算出来，实测一组 19 条绑定全扫
# 一次 9.9 ms，而 snapshot 每次 worker 状态发布都要扫。换 key 只写 overlay、不 bump
# config_revision，所以除重绑时的整表清空外，再给一条一分钟的重解析上限。
QUOTA_BUCKET_CACHE_TTL_SECONDS = 60.0

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
    """限流判据：状态码优先，取不到时回落到模型链自己的 `429` 关键字表。

    故意不另起一套文本：同一个错误在旧链里判「可重试」，在这里就该判「配额到顶」，两套
    词表会让两边的归因说法漂移。
    """
    if status_code == 429:
        return True
    return is_retryable_model_error(error_text, retry_on=RATE_LIMIT_RETRY_ON)


def _decay(age_seconds: float, half_life_seconds: float) -> float:
    if age_seconds <= 0:
        return 1.0
    return float(0.5 ** (age_seconds / max(0.001, float(half_life_seconds))))


@dataclass(slots=True)
class _BucketState:
    request_starts: deque[float] = field(default_factory=deque)
    penalty_events: deque[float] = field(default_factory=deque)
    throttle_events: deque[tuple[float, str]] = field(default_factory=deque)
    # 换来了回答的完成量（按路由租约成功释放计）。`request_starts` 量的是"发出去多少"，
    # 429 风暴里它被失败重试撑到实际完成量的 3–4 倍（10-12 实盘：rpm 90–138 vs 完成 22–36
    # 次/分），拿它判"抬格换没换来吞吐"等于拿重试当吞吐。
    completions: deque[float] = field(default_factory=deque)


@dataclass(slots=True)
class _MemberState:
    reserved: int = 0
    last_selected_seq: int = 0


@dataclass(slots=True)
class _NodeBinding:
    group_key: str
    model_key: str
    route_index: int
    # 仅作观测：绑定建立时的配置 revision。重绑判定不看它——组成员变化由 configure()
    # 按「绑定成员是否已被移出组」逐节点摘除，能力变化由每次请求的 filters 现场判定。
    config_revision: int


@dataclass(slots=True)
class _MemberMetrics:
    running: int = 0
    waiting: int = 0
    reserved: int = 0
    rolling_rpm: int = 0
    rolling_completions: int = 0
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
        self._quota_bucket_cache: dict[str, tuple[float, list[str]]] = {}

    # ------------------------------------------------------------------ 配置

    def configure(self, *, groups: dict[str, ResolvedLoadBalanceGroup] | None, config_revision: int) -> None:
        """装载组定义，并只摘掉「绑定成员已被移出组」的那些节点绑定。

        这里是重绑唯一精确的车道：加成员不动任何既有绑定（粘滞优先，新成员只会被之后的
        新节点选到），减成员只影响正用它的那些节点，在飞请求不受影响。请求速率与 429 惩罚
        描述的是上游，与本地配置是否改过无关，所以配置刷新本身不清任何观测。
        """
        with self._lock:
            self._groups = dict(groups or {})
            self._config_revision = int(config_revision or 0)
            self._quota_bucket_cache.clear()
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

            candidates, sticky_model, busy_reason, binding_drop_reason = self._eligibility_locked(
                node_id=normalized_node_id,
                group=group,
                filters=effective_filters,
                rebind=rebind,
                rebind_reason=rebind_reason,
            )
            if busy_reason:
                return None, busy_reason

            # 粘滞优先：绑定的成员只要还合格就先用它，只有它拿不出 permit 才换人。
            # 逐回合按 score 重选会把节点在成员之间来回抖，而换 model_key 等于换前缀
            # 缓存命名空间。429 惩罚只进打分（决定一次**新**绑定选谁），不参与已有
            # 绑定的去留：某个成员真被限流时，让位由组内轮换完成——它跑满
            # `maxRetryRounds` 才换下一个成员，与旧模型链同一口径。
            if sticky_model and sticky_model in candidates:
                lease = self._try_bind_locked(
                    node_id=normalized_node_id,
                    task_id=task_id,
                    route_index=route_index,
                    group=group,
                    model_key=sticky_model,
                    sticky=True,
                    rebind_reason="",
                )
                if lease is not None:
                    return lease, ""
                self._bindings.pop(normalized_node_id, None)
                binding_drop_reason = binding_drop_reason or "capacity"
            elif sticky_model:
                binding_drop_reason = binding_drop_reason or self._binding_blocked_reason(sticky_model, effective_filters)
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

    def _eligibility_locked(
        self,
        *,
        node_id: str,
        group: ResolvedLoadBalanceGroup,
        filters: RouteCandidateFilters,
        rebind: bool,
        rebind_reason: str,
    ) -> tuple[list[str], str, str, str]:
        """一个组在给定条件下能给出哪些候选：`(合格候选, 粘滞成员, 拒绝原因, 绑定去留原因)`。

        判据只有这一份，`select()` 与 `peek_selection()` 共用。分叉过一次就要命：压缩按
        一处口径动手、准入按另一处口径拒授，就是 2026-10-12 task:d07d706989b4 的形状。
        调用方必须持 `self._lock`。
        """
        binding = None if rebind else self._bindings.get(node_id)
        binding_drop_reason = str(rebind_reason or "") if rebind else ""
        if binding is not None and binding.group_key != group.group_key:
            binding = None
            binding_drop_reason = "plan_changed"
        elif binding is not None and binding.model_key not in group.candidate_model_keys:
            binding_drop_reason = "plan_changed"
        candidates = [
            member.model_key
            for member in group.members
            if self._candidate_ok(member, filters)
        ]
        busy_reason = "" if candidates else "no_candidate"
        return candidates, (str(binding.model_key) if binding is not None else ""), busy_reason, binding_drop_reason

    def peek_selection(
        self,
        *,
        node_id: str,
        group_key: str,
        filters: RouteCandidateFilters | None = None,
    ) -> tuple[int, str, str]:
        """这一跳**大概率**落到哪个组、那个组里最小的成员窗口是多少。

        与 `select()` 同一份资格判据，但不取 permit、不写绑定、不看容量与限流。压缩发生
        在准入之前，而准入又把压缩后的体量当筛选条件，所以这里只能用"与尺寸无关的那半"
        来预测尺子。预测偏大不会造成误发：真正塞不下的成员不会被授予。
        返回 `(最小合格成员窗口, 预测成员 model_key 或 '', 原因)`；原因非空表示这个组
        给不出候选，调用方按链的下一位继续问。
        """
        effective_filters = filters or RouteCandidateFilters()
        with self._lock:
            group = self._groups.get(str(group_key or "").strip())
            if group is None:
                return 0, "", "unknown_group"
            if not group.enabled:
                return 0, "", "group_disabled"
            candidates, sticky_model, busy_reason, _drop = self._eligibility_locked(
                node_id=str(node_id or "").strip(),
                group=group,
                filters=effective_filters,
                rebind=False,
                rebind_reason="",
            )
            if busy_reason:
                return 0, sticky_model, busy_reason
            windows = [
                int(member.context_window_tokens or 0)
                for member in group.members
                if member.model_key in candidates and int(member.context_window_tokens or 0) > 0
            ]
            return (min(windows) if windows else 0), (sticky_model or str(candidates[0] or "")), ""

    def serveability(self, *, group_key: str, filters: RouteCandidateFilters | None = None) -> tuple[bool, str, int]:
        """这个组在**任何容量下**能不能服务一次请求：只看组是否存在、是否启用、候选过滤是否放行。

        返回 `(能否服务, 原因, 合格候选里最大的窗口)`。给回合准入用来在入队之前挡掉
        "体量比链上任何候选都大"这类请求——它们等不来授予，而一旦入队就会把共享队列
        独占成只剩自己（2026-10-12 task:d07d706989b4：0 次授予、零错误行、110 分钟）。
        与 `select()` 的区别只有一个：这里不碰 permit，也不动粘滞绑定。
        """
        effective_filters = filters or RouteCandidateFilters()
        with self._lock:
            group = self._groups.get(str(group_key or "").strip())
            if group is None:
                return False, "unknown_group", 0
            if not group.enabled:
                return False, "group_disabled", 0
            members = list(group.members or [])
            largest_any = max((int(getattr(member, "context_window_tokens", 0) or 0) for member in members), default=0)
            candidates = [member for member in members if self._candidate_ok(member, effective_filters)]
            if not candidates:
                return False, "no_candidate", largest_any
            largest_ok = max(int(getattr(member, "context_window_tokens", 0) or 0) for member in candidates)
            return True, "", largest_ok

    def _candidate_ok(self, member: RouteMemberView, filters: RouteCandidateFilters) -> bool:
        if not str(member.model_key or "").strip():
            return False
        return bool(filters.allows(member))

    def _binding_blocked_reason(self, model_key: str, filters: RouteCandidateFilters) -> str:
        """解释「为什么粘滞失效」，让日志能回答是哪一类触发。"""
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
        """失败归因：只有上游限流留记忆，记成衰减惩罚并按维度留痕。

        其它失败一类都不记——换节点后同样的请求就可能成功，而"这条配置坏了"本来就该由
        操作者看日志处理。判据复用模型链的 `429` 关键字表，本模块不另起一套文本。
        """
        if lease is None:
            return
        if not is_rate_limited(status_code, error_text):
            return
        now = self._monotonic()
        with self._lock:
            for bucket_key in self._buckets_for(lease.model_key):
                bucket = self._bucket(bucket_key)
                bucket.penalty_events.append(now)
                bucket.throttle_events.append((now, classify_throttle_dimension(error_text)))
                self._trim(bucket.penalty_events, PENALTY_RETENTION_SECONDS)
                self._trim(bucket.throttle_events, PENALTY_RETENTION_SECONDS)

    def release(self, lease: ModelRouteLease, *, outcome: str = LEASE_OUTCOME_SUCCESS, error: str = "") -> None:
        """exactly-once 释放：归还 permit，并把未派发出去的 reserved 归零。"""
        if lease is None or lease.released:
            return
        with self._lock:
            lease.released = True
            if not lease.request_started:
                state = self._state(lease.model_key)
                state.reserved = max(0, int(state.reserved) - 1)
            permit = lease.permit
            lease.permit = None
            if permit is not None and self._permit_source is not None:
                self._permit_source.release(permit)
            # 完成量只在真的发过请求且这一发换来了回答时计一次；没发出去的 lease
            # （组内提前耗尽）与失败释放都不算，否则这条轴又会把重试计成产出。
            if str(outcome or '') == LEASE_OUTCOME_SUCCESS and lease.request_started:
                now = self._monotonic()
                for bucket_key in self._buckets_for(lease.model_key):
                    bucket = self._bucket(bucket_key)
                    bucket.completions.append(now)
                    self._trim(bucket.completions, RPM_WINDOW_SECONDS)

    def forget_node(self, node_id: str) -> None:
        """节点终态：解除粘滞，下次准入重新按负载选择。"""
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

    def rate_pressure(self) -> dict[str, Any]:
        """给回合闸用的聚合上游读数：最大衰减 429 惩罚、最大滚动 RPM、滚动 RPM 合计，
        以及"换来了回答"的完成量合计。

        监控每秒调一次，所以这里只取聚合数，不重建 `snapshot()` 那份给人看的完整载荷。
        惩罚本身已在 60 秒半衰（对齐上游的分钟窗口），不需要这里再算窗口。
        回合闸的"抬一格换没换来吞吐"判据用 `rolling_completions_60s_sum`，**不要**退回
        `rolling_rpm_60s_sum`：后者是"实际发起了多少发"，429 风暴里它把失败重试也计进去，
        实测能撑到完成量的 3–4 倍（10-12）。同一 key 可能挂在多个组里，按 model_key
        去重后再相加，否则合计会翻倍。
        """
        penalty_max = 0.0
        rpm_max = 0.0
        rpm_sum = 0
        completions_sum = 0
        with self._lock:
            seen: set[str] = set()
            for group in self._groups.values():
                for member in group.members:
                    model_key = str(member.model_key or '').strip()
                    if not model_key or model_key in seen:
                        continue
                    seen.add(model_key)
                    metrics = self._metrics(model_key)
                    penalty_max = max(penalty_max, float(metrics.penalty or 0.0))
                    rpm_max = max(rpm_max, float(metrics.rolling_rpm or 0.0))
                    rpm_sum += int(metrics.rolling_rpm or 0)
                    completions_sum += int(metrics.rolling_completions or 0)
        return {
            'penalty_429_max': penalty_max,
            'rolling_rpm_60s_max': rpm_max,
            'rolling_rpm_60s_sum': rpm_sum,
            'rolling_completions_60s_sum': completions_sum,
        }

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
        completions = 0
        penalty = 0.0
        for bucket_key in self._buckets_for(model_key):
            bucket = self._buckets.get(bucket_key)
            if bucket is None:
                continue
            rolling += sum(1 for started in bucket.request_starts if now - float(started) <= RPM_WINDOW_SECONDS)
            completions += sum(1 for done in bucket.completions if now - float(done) <= RPM_WINDOW_SECONDS)
            penalty += sum(_decay(now - float(at), PENALTY_HALF_LIFE_SECONDS) for at in bucket.penalty_events)

        metrics = _MemberMetrics(
            rolling_rpm=rolling,
            rolling_completions=completions,
            penalty=float(penalty),
            reserved=int(self._state(model_key).reserved),
        )
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
        key = str(model_key or "").strip()
        cached = self._quota_bucket_cache.get(key)
        if cached is not None and cached[0] > self._monotonic():
            return list(cached[1])
        if self._resolve_quota_buckets is None:
            return [f"{UNRESOLVED_BUCKET_PREFIX}{key}"]
        try:
            raw = list(self._resolve_quota_buckets(key) or [])
        except Exception:
            return [f"{UNRESOLVED_BUCKET_PREFIX}{key}"]
        buckets = [str(item or "").strip() for item in raw if str(item or "").strip()]
        if not buckets:
            # 解析不到密钥材料时（未解锁、或不在 worker 进程里）每个成员各自成桶。空值互并
            # 会把整组塌成一个「空 key」桶，产出假阳性的重复配额结论。
            return [f"{UNRESOLVED_BUCKET_PREFIX}{key}"]
        # 未解析的结果不进缓存：解锁之后的第一次解析必须真去读，否则进程会带着
        # 「身份未知」的桶一直到 TTL 到期。
        if buckets and not str(buckets[0]).startswith(UNRESOLVED_BUCKET_PREFIX):
            self._quota_bucket_cache[key] = (
                self._monotonic() + QUOTA_BUCKET_CACHE_TTL_SECONDS,
                list(buckets),
            )
        return buckets

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
