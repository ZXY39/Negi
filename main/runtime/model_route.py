"""运行时模型路由类型：route plan、组解析、候选过滤与 lease。

这里只放轻量数据结构和解析，不含选择算法（在 `model_load_balancer.py`），也不含请求
生命周期（在 `chat_backend.py`）。配置对象永远以「规范化后的 entry」出现，业务代码不
再判断某一项是字符串还是对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

MODEL_ROUTE_KIND_MODEL = "model"
MODEL_ROUTE_KIND_LOAD_BALANCE = "load_balance"

# lease 的释放结局，用于惩罚统计与日志归因。
LEASE_OUTCOME_SUCCESS = "success"
LEASE_OUTCOME_RETRYABLE_FAILURE = "retryable_failure"
LEASE_OUTCOME_SHAPE_ERROR = "shape_error"
LEASE_OUTCOME_UNAVAILABLE = "unavailable"
LEASE_OUTCOME_CANCELLED = "cancelled"
LEASE_OUTCOME_BUILD_FAILED = "build_failed"
LEASE_OUTCOME_GROUP_EXHAUSTED = "group_exhausted"


@dataclass(slots=True)
class RouteCandidateFilters:
    """一次发送对候选的硬性要求。

    由 preflight 侧算好后传进选择器：模型选择发生在准入层，而这些条件在准入之前就已
    经确定，所以「先过滤再选」不需要重新估算请求体。
    """

    required_context_window_tokens: int = 0
    requires_image_multimodal: bool = False
    # 本次请求已经试过的成员；组内不得重复选择。
    excluded_model_keys: frozenset[str] = frozenset()

    def allows(self, member: "RouteMemberView") -> bool:
        if not member.enabled:
            return False
        if member.model_key in self.excluded_model_keys:
            return False
        if self.required_context_window_tokens and member.context_window_tokens < self.required_context_window_tokens:
            return False
        if self.requires_image_multimodal and not member.image_multimodal_enabled:
            return False
        return True


@dataclass(slots=True)
class RouteMemberView:
    """组成员在运行时的可读视图（配置侧解析结果，不含密钥明文）。"""

    model_key: str
    enabled: bool = True
    context_window_tokens: int = 0
    image_multimodal_enabled: bool = False
    quota_pool_key: str = ""


@dataclass(slots=True)
class ResolvedLoadBalanceGroup:
    group_key: str
    enabled: bool
    max_retry_rounds: int
    members: list[RouteMemberView] = field(default_factory=list)

    @property
    def candidate_model_keys(self) -> list[str]:
        return [member.model_key for member in self.members]


@dataclass(slots=True)
class ResolvedModelRoute:
    """链上一个已解析的跳。

    `candidates` 是展开后的候选视图，只用于诊断与旧客户端兼容展示；选择一律走 `group`
    或 `model_key`，不能拿 `candidates[0]` 当首选模型。
    """

    index: int
    kind: str
    model_key: str = ""
    group_key: str = ""
    group: ResolvedLoadBalanceGroup | None = None
    candidates: tuple[str, ...] = ()

    @property
    def is_load_balance(self) -> bool:
        return self.kind == MODEL_ROUTE_KIND_LOAD_BALANCE


@dataclass(slots=True)
class ModelRoutePlan:
    routes: list[ResolvedModelRoute] = field(default_factory=list)
    config_revision: int = 0

    @property
    def candidate_model_keys(self) -> list[str]:
        seen: set[str] = set()
        keys: list[str] = []
        for route in self.routes:
            for key in route.candidates:
                if key and key not in seen:
                    seen.add(key)
                    keys.append(key)
        return keys

    @property
    def load_balance_group_keys(self) -> list[str]:
        seen: set[str] = set()
        keys: list[str] = []
        for route in self.routes:
            if route.is_load_balance and route.group_key and route.group_key not in seen:
                seen.add(route.group_key)
                keys.append(route.group_key)
        return keys

    def route_at(self, index: int) -> ResolvedModelRoute | None:
        if 0 <= index < len(self.routes):
            return self.routes[index]
        return None

    def signature(self) -> str:
        """route 的稳定签名，用于 prompt cache key 与诊断，而不是「第一个候选」。"""
        parts: list[str] = []
        for route in self.routes:
            if route.is_load_balance:
                members = ",".join(route.candidates)
                parts.append(f"lb:{route.group_key}[{members}]")
            else:
                parts.append(f"model:{route.model_key}")
        return "|".join(parts)


@dataclass(slots=True)
class ModelRouteLease:
    """一次 provider attempt 的模型绑定凭据。

    `permit` 是底层 model/key 并发许可；持有者必须在 attempt 结束时 exactly-once
    `release`，包括 provider 构建失败与取消路径。
    """

    lease_id: int
    node_id: str
    task_id: str
    route_index: int
    group_key: str
    model_key: str
    key_index: int
    quota_bucket_key: str
    selection_reason: str
    config_revision: int
    permit: Any = None
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)
    running_before: int = 0
    waiting_before: int = 0
    reserved_before: int = 0
    rolling_rpm: int = 0
    penalty_before: float = 0.0
    local_capacity: int | None = None
    released: bool = False
    request_started: bool = False
    sticky_rebind_reason: str = ""
    max_retry_rounds: int = 1
    passes_used: int = 0

    @property
    def held_permit(self) -> Any:
        return self.permit


RoutePlanSupplier = Callable[[], ModelRoutePlan]


def build_model_route_plan(config: Any, scope: str, *, revision: int = 0) -> ModelRoutePlan:
    """把配置里的 route entry 解析成运行时 route plan。

    成员的能力视图（context window、多模态）在这里一次性解析，调用方不再各自去读
    「链上第一个模型」。禁用成员保留在候选里但会被 direct 链过滤，与既有
    `get_scope_model_chain` 的过滤口径一致。

    `mainRuntime.modelRouteLoadBalanceEnabled=false` 是回滚闸门：组被摊平成「按配置顺序
    的 direct 候选」，准入与 chat 一起回到改造前的有序链语义。
    """
    load_balance_enabled = bool(
        getattr(getattr(config, "main_runtime", None), "model_route_load_balance_enabled", True)
    )
    routes: list[ResolvedModelRoute] = []
    for entry in list(config.get_role_model_routes(scope) or []):
        entry_type = str(getattr(entry, "type", MODEL_ROUTE_KIND_MODEL) or MODEL_ROUTE_KIND_MODEL)
        if entry_type == MODEL_ROUTE_KIND_LOAD_BALANCE:
            group_key = str(getattr(entry, "group_key", "") or "").strip()
            group_config = config.get_load_balance_group(group_key)
            if group_config is None:
                continue
            members: list[RouteMemberView] = []
            for member_key in list(group_config.model_keys or []):
                key = str(member_key or "").strip()
                if not key:
                    continue
                members.append(_member_view_for(config, key))
            enabled = bool(getattr(group_config, "enabled", True))
            if not load_balance_enabled:
                for member in members:
                    if not member.enabled:
                        continue
                    routes.append(
                        ResolvedModelRoute(
                            index=len(routes),
                            kind=MODEL_ROUTE_KIND_MODEL,
                            model_key=member.model_key,
                            candidates=(member.model_key,),
                        )
                    )
                continue
            routes.append(
                ResolvedModelRoute(
                    index=len(routes),
                    kind=MODEL_ROUTE_KIND_LOAD_BALANCE,
                    group_key=group_key,
                    group=ResolvedLoadBalanceGroup(
                        group_key=group_key,
                        enabled=enabled,
                        max_retry_rounds=int(getattr(group_config, "max_retry_rounds", 1) or 1),
                        members=members if enabled else [],
                    ),
                    candidates=tuple(member.model_key for member in members) if enabled else (),
                )
            )
            continue
        model_key = str(getattr(entry, "model_key", "") or "").strip()
        if not model_key:
            continue
        managed = config.get_managed_model(model_key)
        if managed is not None and not bool(getattr(managed, "enabled", True)):
            continue
        routes.append(
            ResolvedModelRoute(
                index=len(routes),
                kind=MODEL_ROUTE_KIND_MODEL,
                model_key=model_key,
                candidates=(model_key,),
            )
        )
    return ModelRoutePlan(routes=routes, config_revision=int(revision or 0))


def _member_view_for(config: Any, model_key: str) -> RouteMemberView:
    managed = config.get_managed_model(model_key)
    if managed is None:
        return RouteMemberView(model_key=model_key, enabled=False)
    return RouteMemberView(
        model_key=model_key,
        enabled=bool(getattr(managed, "enabled", True)),
        context_window_tokens=int(getattr(managed, "context_window_tokens", 0) or 0),
        image_multimodal_enabled=bool(getattr(managed, "image_multimodal_enabled", False)),
    )
