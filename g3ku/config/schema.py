"""Configuration schema using Pydantic."""

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

from g3ku.utils.api_keys import (
    SingleAPIKeyMaxConcurrency,
    has_api_keys,
    normalize_single_api_key_max_concurrency,
    resolve_api_key_concurrency_layout,
)
from g3ku.utils.retry_keywords import DEFAULT_RETRY_ON_KEYWORDS, split_retry_keywords

ROLE_SCOPE_ALIASES = {
    "ceo": "ceo",
    "execution": "execution",
    "inspection": "inspection",
    "memory": "memory",
    "checker": "inspection",
}

REQUIRED_MODEL_ROLES = ("ceo", "execution", "inspection")
DEFAULT_ROLE_MAX_ITERATIONS = {
    "ceo": None,
    "execution": None,
    "inspection": None,
    "memory": None,
}
DEFAULT_ROLE_MAX_CONCURRENCY = {
    "ceo": None,
    "execution": None,
    "inspection": None,
    "memory": 1,
}
DEFAULT_NODE_DISPATCH_CONCURRENCY = {
    "execution": 8,
    "inspection": 4,
}

# load-balance group 的组内预算边界（份文档 4.4/15.2）：语义是「每个成员允许的完整
# key pass 数」，默认 1。上限刻意很小——成员 catalog 的 retry_count 可达 9999999，
# 若被组继承，组内平级 fallback 永远不会发生。超上限是配置错误而不是需要夹断的输入：
# 夹断会把写错的意图静默改成另一套行为。
GROUP_MAX_RETRY_ROUNDS_LIMIT = 3
GROUP_DEFAULT_MAX_RETRY_ROUNDS = 1
MODEL_ROUTE_ENTRY_TYPES = ("model", "load_balance")
# 第一阶段只允许这两条车道使用负载均衡组。
LOAD_BALANCE_ROUTE_SCOPES = ("execution", "inspection")

DEFAULT_MAX_OUTPUT_TOKENS = 65536
VALID_REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
DEFAULT_REASONING_EFFORT = "medium"


def normalize_reasoning_effort(value: Any) -> str:
    """Normalize a reasoning_effort value to one of the six managed levels.

    ``none`` means deep thinking is explicitly disabled. Missing/empty values
    fall back to the default level so existing configs keep working.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return DEFAULT_REASONING_EFFORT
    return raw


def normalize_role_scope(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    normalized = ROLE_SCOPE_ALIASES.get(raw)
    if normalized is None:
        raise ValueError(f"Invalid scope: {value}")
    return normalized


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class AgentMiddlewareConfig(Base):
    """Config entry for runtime middleware hooks."""

    enabled: bool = False
    name: str = ""  # e.g. "prepend_system_message", "tool_result_suffix"
    class_path: str = ""  # optional: "package.module:ClassName"
    options: dict[str, Any] = Field(default_factory=dict)


class AgentDefaults(Base):
    """Default agent configuration."""

    workspace: str = "."
    model: str = ""
    provider: str = "auto"  # Deprecated; provider selection is derived from managed models.
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    temperature: float = 0.1
    max_tool_iterations: int = 40
    memory_window: int = 100
    reasoning_effort: str = DEFAULT_REASONING_EFFORT  # none / low / medium / high / xhigh / max; none disables deep thinking
    middlewares: list[AgentMiddlewareConfig] = Field(default_factory=list)

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_reasoning_effort(cls, value: Any) -> str:
        return normalize_reasoning_effort(value)

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_reasoning_effort(cls, value: Any) -> str:
        normalized = str(normalize_reasoning_effort(value)).strip().lower()
        if normalized not in VALID_REASONING_EFFORTS:
            raise ValueError(
                f"agents.defaults.reasoning_effort must be one of {', '.join(VALID_REASONING_EFFORTS)}"
            )
        return normalized


class RoleIterationConfig(Base):
    """Per-role loop limits for CEO, execution, inspection, and memory runtimes."""

    ceo: int | None = Field(default=DEFAULT_ROLE_MAX_ITERATIONS["ceo"], ge=0)
    execution: int | None = Field(default=DEFAULT_ROLE_MAX_ITERATIONS["execution"], ge=0)
    inspection: int | None = Field(default=DEFAULT_ROLE_MAX_ITERATIONS["inspection"], ge=0)
    memory: int | None = Field(default=DEFAULT_ROLE_MAX_ITERATIONS["memory"], ge=0)

    @field_validator("ceo", "execution", "inspection", "memory", mode="before")
    @classmethod
    def _normalize_iterations(cls, value: Any, info: ValidationInfo) -> int | None:
        if value is None:
            return DEFAULT_ROLE_MAX_ITERATIONS[info.field_name]
        if isinstance(value, str) and not value.strip():
            return DEFAULT_ROLE_MAX_ITERATIONS[info.field_name]
        return int(value)


class RoleConcurrencyConfig(Base):
    """Per-role parallel work caps for CEO, execution, inspection, and memory runtimes."""

    ceo: int | None = Field(default=DEFAULT_ROLE_MAX_CONCURRENCY["ceo"], ge=0)
    execution: int | None = Field(default=DEFAULT_ROLE_MAX_CONCURRENCY["execution"], ge=0)
    inspection: int | None = Field(default=DEFAULT_ROLE_MAX_CONCURRENCY["inspection"], ge=0)
    memory: int = Field(default=DEFAULT_ROLE_MAX_CONCURRENCY["memory"], ge=1, le=1)

    @field_validator("ceo", "execution", "inspection", mode="before")
    @classmethod
    def _normalize_concurrency(cls, value: Any, info: ValidationInfo) -> int | None:
        if value is None:
            return DEFAULT_ROLE_MAX_CONCURRENCY[info.field_name]
        if isinstance(value, str) and not value.strip():
            return DEFAULT_ROLE_MAX_CONCURRENCY[info.field_name]
        return int(value)

    @field_validator("memory", mode="before")
    @classmethod
    def _normalize_memory_concurrency(cls, value: Any) -> int:
        if value is None:
            return 1
        if isinstance(value, str) and not value.strip():
            return 1
        normalized = int(value)
        if normalized != 1:
            raise ValueError("agents.roleConcurrency.memory is fixed at 1")
        return 1


class ModelFallbackTarget(Base):
    model_key: str
    retry_on: list[str] = Field(default_factory=lambda: list(DEFAULT_RETRY_ON_KEYWORDS))
    retry_count: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _migrate_alias_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "model_key" not in payload and "provider_model" in payload:
            payload["model_key"] = payload.pop("provider_model")
        return payload

    @field_validator("model_key")
    @classmethod
    def _validate_model_key(cls, value: str) -> str:
        model_key = str(value or "").strip()
        if not model_key:
            raise ValueError("model_key is required")
        return model_key

    @field_validator("retry_on", mode="before")
    @classmethod
    def _normalize_retry_on(cls, value: Any) -> list[str]:
        # 区分"未设置"(None → 用默认关键字) 与"显式置空"([] / "" → 尊重为空，关闭关键字
        # 重试)。字段 default_factory 已保证省略时用默认；本 validator 只在显式提供时运行，
        # 故 None 视作未设置回退默认，其余按实际值（含空）规范化。
        if value is None:
            return list(DEFAULT_RETRY_ON_KEYWORDS)
        return split_retry_keywords(value)

    @field_validator("retry_count", mode="before")
    @classmethod
    def _normalize_retry_count(cls, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str) and not value.strip():
            return 0
        return int(value)

    @property
    def provider_model(self) -> str:
        return self.model_key


class ManagedModelConfig(Base):
    """Managed model profile with credentials and runtime defaults."""

    key: str
    llm_config_id: str | None = None
    provider_model: str = ""
    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None
    enabled: bool = True
    max_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str = DEFAULT_REASONING_EFFORT  # none / low / medium / high / xhigh / max; none disables deep thinking
    retry_on: list[str] = Field(default_factory=lambda: list(DEFAULT_RETRY_ON_KEYWORDS))
    retry_count: int = Field(default=0, ge=0)
    single_api_key_max_concurrency: SingleAPIKeyMaxConcurrency = None
    description: str = ""
    name: str = ""
    context_window_tokens: int | None = None
    # 单次 provider 请求（attempt）的超时秒数；None/空白 → 运行时默认
    # （DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS，600s）。同时约束外层 attempt
    # 看门狗与流式首块/块间空闲超时。
    request_timeout_seconds: float | None = None
    image_multimodal_enabled: bool = False
    # 显式声明「这条 binding 与哪些 binding 共享上游配额账户」。只由 operator 填写：
    # 运行时不按 provider 名称或 endpoint 猜测共享，猜错会把两个独立配额当一个用。
    quota_pool_key: str | None = None

    @field_validator("quota_pool_key", mode="before")
    @classmethod
    def _normalize_quota_pool_key(cls, value: Any) -> str | None:
        pool_key = str(value or "").strip()
        return pool_key or None

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        key = str(value or "").strip()
        if not key:
            raise ValueError("models.catalog[].key is required")
        return key

    @field_validator("provider_model")
    @classmethod
    def _validate_provider_model(cls, value: str) -> str:
        provider_model = str(value or "").strip()
        if provider_model:
            Config.parse_provider_model(provider_model)
        return provider_model

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("api_base")
    @classmethod
    def _normalize_api_base(cls, value: str | None) -> str | None:
        api_base = str(value or "").strip()
        return api_base or None

    @field_validator("retry_on", mode="before")
    @classmethod
    def _normalize_retry_on(cls, value: Any) -> list[str]:
        # 区分"未设置"(None → 用默认关键字) 与"显式置空"([] / "" → 尊重为空，关闭关键字
        # 重试)。字段 default_factory 已保证省略时用默认；本 validator 只在显式提供时运行，
        # 故 None 视作未设置回退默认，其余按实际值（含空）规范化。
        if value is None:
            return list(DEFAULT_RETRY_ON_KEYWORDS)
        return split_retry_keywords(value)

    @field_validator("retry_count", mode="before")
    @classmethod
    def _normalize_retry_count(cls, value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, str) and not value.strip():
            return 0
        return int(value)

    @field_validator("context_window_tokens", mode="before")
    @classmethod
    def _normalize_context_window_tokens(cls, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        resolved = int(value)
        if resolved <= 25_000:
            raise ValueError("models.catalog[].context_window_tokens must be > 25000")
        return resolved

    @field_validator("request_timeout_seconds", mode="before")
    @classmethod
    def _normalize_request_timeout_seconds(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        resolved = float(value)
        if resolved <= 0:
            raise ValueError("models.catalog[].request_timeout_seconds must be > 0")
        return resolved

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _normalize_catalog_reasoning_effort(cls, value: Any) -> str:
        return normalize_reasoning_effort(value)

    @field_validator("reasoning_effort")
    @classmethod
    def _validate_catalog_reasoning_effort(cls, value: Any) -> str:
        normalized = str(normalize_reasoning_effort(value)).strip().lower()
        if normalized not in VALID_REASONING_EFFORTS:
            raise ValueError(
                f"models.catalog[].reasoning_effort must be one of {', '.join(VALID_REASONING_EFFORTS)}"
            )
        return normalized

    @field_validator("single_api_key_max_concurrency", mode="before")
    @classmethod
    def _normalize_single_api_key_max_concurrency(cls, value: Any) -> SingleAPIKeyMaxConcurrency:
        return normalize_single_api_key_max_concurrency(value)

    @model_validator(mode="after")
    def _validate_binding_or_inline_credentials(self) -> "ManagedModelConfig":
        llm_config_id = str(self.llm_config_id or "").strip()
        provider_model = str(self.provider_model or "").strip()
        api_key = str(self.api_key or "").strip()
        if llm_config_id:
            self.llm_config_id = llm_config_id
            return self
        if not provider_model:
            raise ValueError("models.catalog[].provider_model or llm_config_id is required")
        if not has_api_keys(api_key):
            raise ValueError("models.catalog[].api_key is required before migration")
        resolve_api_key_concurrency_layout(
            api_key,
            self.single_api_key_max_concurrency,
            include_empty_slot=False,
            reject_all_zero=True,
        )
        return self


class ModelRouteEntry(Base):
    """模型链上的一个跳：要么直接指向一个 model key，要么指向一个负载均衡组。

    链上顺序 = fallback 优先级；组内成员顺序不参与选择。业务代码不得再判断
    「这一项是字符串还是对象」——加载时一律规范化成该类型，见
    ``RoleModelRoutingConfig``。
    """

    type: Literal["model", "load_balance"] = "model"
    model_key: str | None = None
    group_key: str | None = None

    @field_validator("type", mode="before")
    @classmethod
    def _normalize_type(cls, value: Any) -> str:
        normalized = str(value or "").strip().lower().replace("-", "_")
        if not normalized:
            return "model"
        if normalized not in MODEL_ROUTE_ENTRY_TYPES:
            raise ValueError(
                f"models.roles route entry type must be one of {', '.join(MODEL_ROUTE_ENTRY_TYPES)}: {value}"
            )
        return normalized

    @field_validator("model_key", "group_key", mode="before")
    @classmethod
    def _normalize_keys(cls, value: Any) -> str | None:
        key = str(value or "").strip()
        return key or None

    @model_validator(mode="after")
    def _validate_entry_shape(self) -> "ModelRouteEntry":
        if self.type == "model":
            if not self.model_key:
                raise ValueError("models.roles route entry with type=model requires modelKey")
            if self.group_key:
                raise ValueError("models.roles route entry with type=model must not set groupKey")
            return self
        if self.group_key and self.model_key:
            raise ValueError("models.roles route entry with type=load_balance must not set modelKey")
        # 缺 groupKey 的空 load_balance 条目不能在此抛错：旧配置里可能带一个只写了
        # type 的占位条目，加载时必须继续可读；由保存路径和管理面校验拒绝。
        return self


class ModelLoadBalanceGroup(Base):
    """链内平级候选组：成员之间不排序，运行时按综合负载选一个绑定到节点。"""

    enabled: bool = True
    max_retry_rounds: int = GROUP_DEFAULT_MAX_RETRY_ROUNDS
    model_keys: list[str] = Field(default_factory=list)

    @field_validator("max_retry_rounds", mode="before")
    @classmethod
    def _normalize_max_retry_rounds(cls, value: Any) -> int:
        # 区分「未配置」（走默认）与「写了非法值」（报错），与 retry_on 的处理口径一致。
        if value is None:
            return GROUP_DEFAULT_MAX_RETRY_ROUNDS
        if isinstance(value, str) and not value.strip():
            return GROUP_DEFAULT_MAX_RETRY_ROUNDS
        try:
            rounds = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("models.loadBalanceGroups.*.maxRetryRounds must be an integer") from exc
        if rounds < 1 or rounds > GROUP_MAX_RETRY_ROUNDS_LIMIT:
            raise ValueError(
                "models.loadBalanceGroups.*.maxRetryRounds must be between 1 and "
                f"{GROUP_MAX_RETRY_ROUNDS_LIMIT}; member catalog retryCount is not inherited by group routes"
            )
        return rounds

    @field_validator("model_keys", mode="before")
    @classmethod
    def _normalize_model_keys(cls, value: Any) -> list[str]:
        items = value if isinstance(value, list) else []
        clean: list[str] = []
        seen: set[str] = set()
        for item in items:
            key = str(item or "").strip()
            if not key:
                continue
            # 组内重复成员必须报错而不是静默去重：静默去重会改变均衡权重并隐藏配置错误
            # （组永远是显式结构，没有 legacy 兼容负担）。
            if key in seen:
                raise ValueError(f"models.loadBalanceGroups member appears twice: {key}")
            seen.add(key)
            clean.append(key)
        return clean


class RoleModelRoutingConfig(Base):
    """Ordered route entries for each runtime scope.

    输入可以是旧的字符串数组（``[model_a, model_b]``）或显式 route 对象，两者在加载时
    统一规范化为 ``ModelRouteEntry``；序列化回配置文件时，全是 direct model 的链会保持
    旧的字符串数组形状，避免出现无意义的 config diff（见 loader 的 roles payload）。
    """

    ceo: list[ModelRouteEntry] = Field(default_factory=list)
    execution: list[ModelRouteEntry] = Field(default_factory=list)
    inspection: list[ModelRouteEntry] = Field(default_factory=list)
    memory: list[ModelRouteEntry] = Field(default_factory=list)

    @field_validator("ceo", "execution", "inspection", "memory", mode="before")
    @classmethod
    def _normalize_chain(cls, value: Any) -> list[ModelRouteEntry]:
        items = value if isinstance(value, list) else []
        entries: list[ModelRouteEntry] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            entry = coerce_route_entry(item)
            identity = (entry.type, entry.group_key or entry.model_key or "")
            # 与旧行为一致：加载时静默丢弃空项与重复项，保证存量配置和旧客户端可读。
            # 保存路径按「显式 route_entries 重复即 400」单独严格校验（model_manager）。
            if not identity[1] or identity in seen:
                continue
            seen.add(identity)
            entries.append(entry)
        return entries


class ModelsConfig(Base):
    """Managed model catalog, role routing and load-balance groups."""

    catalog: list[ManagedModelConfig] = Field(default_factory=list)
    roles: RoleModelRoutingConfig = Field(default_factory=RoleModelRoutingConfig)
    load_balance_groups: dict[str, ModelLoadBalanceGroup] = Field(default_factory=dict)

    @field_validator("load_balance_groups", mode="before")
    @classmethod
    def _normalize_groups(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        normalized: dict[str, Any] = {}
        for raw_key, raw_group in value.items():
            group_key = str(raw_key or "").strip()
            if not group_key:
                continue
            payload = raw_group if isinstance(raw_group, dict) else {}
            normalized[group_key] = payload
        return normalized


def coerce_route_entry(item: Any) -> ModelRouteEntry:
    """把字符串或 dict 规范化为 ModelRouteEntry。"""
    if isinstance(item, ModelRouteEntry):
        return item
    if isinstance(item, str):
        return ModelRouteEntry(type="model", model_key=item.strip() or None)
    if isinstance(item, dict):
        payload = dict(item)
        if "type" not in payload and ("modelKey" in payload or "model_key" in payload):
            payload["type"] = "model"
        return ModelRouteEntry.model_validate(payload)
    return ModelRouteEntry(type="model", model_key=str(item or "").strip() or None)


class MultiAgentConfig(Base):
    """Dynamic subagent orchestration configuration."""

    enabled: bool = True
    orchestrator_model_key: str | None = None
    session_store_path: str = ".g3ku/dynamic-subagents.sqlite3"
    background_store_path: str = ".g3ku/background-tasks.sqlite3"
    destroy_sync_sessions: bool = True
    freeze_ttl_seconds: int = 86400
    background_ttl_seconds: int = 604800
    max_parallel_background_tasks: int = 8
    max_parallel_subagents_per_turn: int = 6
    sync_subagent_timeout_seconds: int = 180
    max_browser_steps_per_subagent: int = 10
    browser_no_progress_threshold: int = 3
    repeated_action_window: int = 3
    repeated_action_threshold: int = 3
    blackboard_dir: str = ".g3ku/blackboard"
    interrupt_mode: str = "ticket"

    @field_validator("orchestrator_model_key")
    @classmethod
    def _normalize_orchestrator_model_key(cls, value: str | None) -> str | None:
        model_key = str(value or "").strip()
        return model_key or None

    @property
    def orchestrator_model(self) -> str | None:
        return self.orchestrator_model_key


class NodeParallelismConfig(Base):
    """Per-node parallel tool and child pipeline execution controls."""

    enabled: bool = True
    max_parallel_tool_calls_per_node: int | None = None
    max_parallel_child_pipelines_per_node: int | None = None
    adaptive_total_tool_budget_enabled: bool = True
    adaptive_total_tool_budget_normal_limit: int = 6
    adaptive_total_tool_budget_throttled_limit: int = 2
    adaptive_total_tool_budget_critical_limit: int = 1
    adaptive_total_tool_budget_step_up: int = 1
    adaptive_total_tool_budget_sample_seconds: float = 1.0
    adaptive_total_tool_budget_recover_window_seconds: float = 1.0
    adaptive_total_tool_budget_warn_consecutive_samples: int = 3
    adaptive_total_tool_budget_safe_consecutive_samples: int = 3
    adaptive_event_loop_warn_ms: float = 250.0
    adaptive_event_loop_safe_ms: float = 100.0
    adaptive_event_loop_critical_ms: float = 1500.0
    adaptive_writer_queue_warn: int = 50
    adaptive_writer_queue_safe: int = 10
    adaptive_writer_queue_critical: int = 100
    adaptive_pressure_snapshot_stale_after_seconds: float = 3.0
    adaptive_machine_cpu_warn_percent: float = 85.0
    adaptive_machine_cpu_safe_percent: float = 55.0
    adaptive_machine_cpu_critical_percent: float = 95.0
    adaptive_machine_memory_warn_percent: float = 88.0
    adaptive_machine_memory_safe_percent: float = 95.0
    adaptive_machine_memory_critical_percent: float = 94.0
    adaptive_machine_disk_busy_warn_percent: float = 70.0
    adaptive_machine_disk_busy_safe_percent: float = 35.0
    adaptive_machine_disk_busy_critical_percent: float = 90.0
    adaptive_sqlite_write_wait_warn_ms: float = 200.0
    adaptive_sqlite_write_wait_safe_ms: float = 50.0
    adaptive_sqlite_write_wait_critical_ms: float = 250.0
    adaptive_sqlite_query_warn_ms: float = 150.0
    adaptive_sqlite_query_safe_ms: float = 30.0
    adaptive_sqlite_query_critical_ms: float = 250.0
    adaptive_process_cpu_warn_ratio: float = 0.85
    adaptive_process_cpu_safe_ratio: float = 0.50
    adaptive_pressure_max_dwell_seconds: float = 60.0
    adaptive_pressure_max_tool_wait_ms: float = 30000.0
    adaptive_pressure_local_recovery_enabled: bool = True
    adaptive_pressure_gate_stale_after_seconds: float = 10.0
    adaptive_pressure_gate_close_on_machine_critical: bool = False

    @field_validator(
        "max_parallel_tool_calls_per_node",
        "max_parallel_child_pipelines_per_node",
    )
    @classmethod
    def _clamp_parallel_limit(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return max(0, int(value))

    @field_validator(
        "adaptive_total_tool_budget_normal_limit",
        "adaptive_total_tool_budget_throttled_limit",
        "adaptive_total_tool_budget_critical_limit",
        "adaptive_total_tool_budget_step_up",
        "adaptive_total_tool_budget_warn_consecutive_samples",
        "adaptive_total_tool_budget_safe_consecutive_samples",
        "adaptive_writer_queue_warn",
        "adaptive_writer_queue_safe",
        "adaptive_writer_queue_critical",
    )
    @classmethod
    def _clamp_positive_int(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator(
        "adaptive_total_tool_budget_sample_seconds",
        "adaptive_total_tool_budget_recover_window_seconds",
        "adaptive_pressure_snapshot_stale_after_seconds",
        "adaptive_event_loop_warn_ms",
        "adaptive_event_loop_safe_ms",
        "adaptive_event_loop_critical_ms",
        "adaptive_machine_cpu_warn_percent",
        "adaptive_machine_cpu_safe_percent",
        "adaptive_machine_cpu_critical_percent",
        "adaptive_machine_memory_warn_percent",
        "adaptive_machine_memory_safe_percent",
        "adaptive_machine_memory_critical_percent",
        "adaptive_machine_disk_busy_warn_percent",
        "adaptive_machine_disk_busy_safe_percent",
        "adaptive_machine_disk_busy_critical_percent",
        "adaptive_sqlite_write_wait_warn_ms",
        "adaptive_sqlite_write_wait_safe_ms",
        "adaptive_sqlite_write_wait_critical_ms",
        "adaptive_sqlite_query_warn_ms",
        "adaptive_sqlite_query_safe_ms",
        "adaptive_sqlite_query_critical_ms",
        "adaptive_process_cpu_warn_ratio",
        "adaptive_process_cpu_safe_ratio",
        "adaptive_pressure_max_dwell_seconds",
        "adaptive_pressure_max_tool_wait_ms",
        "adaptive_pressure_gate_stale_after_seconds",
    )
    @classmethod
    def _clamp_positive_float(cls, value: float) -> float:
        return max(0.0, float(value))


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    role_iterations: RoleIterationConfig = Field(default_factory=RoleIterationConfig)
    role_concurrency: RoleConcurrencyConfig = Field(default_factory=RoleConcurrencyConfig)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)
    node_parallelism: NodeParallelismConfig = Field(default_factory=NodeParallelismConfig)
    # 统一工具调用最大运行时长默认值（秒）：所有工具的全局保底上限，
    # 模型可在单次调用显式传更大的 timeout_seconds 参数覆盖；无上限约束。
    tool_default_timeout_seconds: float = 600.0

    @field_validator("tool_default_timeout_seconds")
    @classmethod
    def _clamp_tool_default_timeout(cls, value: float) -> float:
        # 下限与全局 MIN_TOOL_TIMEOUT_SECONDS 对齐，支持亚秒默认（0.05s）。
        return max(0.05, float(value or 600.0))


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers.

    Only the two supported OpenAI protocol entries:
    - openai:    Chat Completions protocol (/v1/chat/completions)
    - responses: Responses protocol (/v1/responses)
    """

    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    responses: ProviderConfig = Field(default_factory=ProviderConfig)


class WebConfig(Base):
    """Web server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790


class MemoryIsolationConfig(Base):
    """Namespace isolation controls."""

    mode: Literal["session", "channel", "global"] = "global"
    namespace_template: list[str] = Field(default_factory=lambda: ["memory", "global"])


class MemoryGuardConfig(Base):
    """Write-guard configuration for long-term memory."""

    mode: Literal["tiered", "auto", "manual"] = "tiered"
    auto_fact_confidence: float = 0.8


class MemoryFeaturesConfig(Base):
    """Feature switches for memory architecture v2."""

    unified_context: bool = True
    layered_loading: bool = True
    query_planner: bool = True
    commit_pipeline: bool = True
    observability: bool = True


class MemoryCommitConfig(Base):
    """Session commit trigger controls."""

    turn_trigger: int = 20
    idle_minutes_trigger: int = 360


class MemoryCostConfig(Base):
    """Cost governance controls."""

    max_increase_pct: int = 15


class MemoryDocumentConfig(Base):
    """Markdown memory document limits and paths."""

    summary_max_chars: int = Field(default=300, ge=1)
    document_max_chars: int = Field(default=20000, ge=1)
    compress_trigger_chars: int = Field(default=16000, ge=1)
    compress_target_chars: int = Field(default=13000, ge=1)
    memory_file: str = "memory/MEMORY.md"
    notes_dir: str = "memory/notes"


class MemoryQueueConfig(Base):
    """Queued memory worker controls."""

    queue_file: str = "memory/queue.jsonl"
    ops_file: str = "memory/ops.jsonl"
    failed_file: str = "memory/failed.jsonl"
    batch_max_chars: int = Field(default=50000, ge=1)
    max_wait_seconds: int = Field(default=3, ge=0)
    review_interval_turns: int = Field(default=5, ge=1)
    # 失败停车区的 provider/瞬时错误条目在"队列有新批次成功应用"时自动重新入队尾；
    # 没有成功信号则一直停车等待，不产生任何自动重试成本。
    auto_requeue_on_success: bool = True


class MemoryAgentConfig(Base):
    """Dedicated memory agent execution controls."""

    model_key: str = ""
    repair_attempt_limit: int = Field(default=2, ge=0)


class MemoryAssemblyConfig(Base):
    """Frontdoor dynamic tool and skill selection controls."""

    skill_inventory_top_k: int = 16
    extension_tool_top_k: int = 16
    node_tool_top_k: int = 16
    frontdoor_compaction_max_context_tokens: int = Field(default=200000, ge=1)
    frontdoor_compaction_trigger_ratio: float = Field(default=0.10, ge=0.0, le=1.0)
    frontdoor_interrupt_approval_enabled: bool = False
    frontdoor_interrupt_tool_names: list[str] = Field(
        default_factory=lambda: ["create_async_task"]
    )
    core_tools: list[str] = Field(
        default_factory=lambda: [
            'content',
            'create_async_task',
            'task_summary',
            'task_list',
            'task_progress',
            'memory_write',
            'memory_delete',
            'memory_note',
            'task_runtime',
            'skill_access',
        ]
    )


class MemoryToolsConfig(Base):
    """Queued Markdown memory runtime configuration."""

    enabled: bool = True
    arch_version: Literal["v1", "v2"] = "v2"
    features: MemoryFeaturesConfig = Field(default_factory=MemoryFeaturesConfig)
    isolation: MemoryIsolationConfig = Field(default_factory=MemoryIsolationConfig)
    guard: MemoryGuardConfig = Field(default_factory=MemoryGuardConfig)
    commit: MemoryCommitConfig = Field(default_factory=MemoryCommitConfig)
    cost: MemoryCostConfig = Field(default_factory=MemoryCostConfig)
    assembly: MemoryAssemblyConfig = Field(default_factory=MemoryAssemblyConfig)
    retention_days: int | None = None
    document: MemoryDocumentConfig = Field(default_factory=MemoryDocumentConfig)
    queue: MemoryQueueConfig = Field(default_factory=MemoryQueueConfig)
    agent: MemoryAgentConfig = Field(default_factory=MemoryAgentConfig)



class ResourceReloadConfig(Base):
    enabled: bool = True
    poll_interval_ms: int = 1000
    debounce_ms: int = 400
    lazy_reload_on_access: bool = True
    keep_last_good_version: bool = True


class ResourceLocksConfig(Base):
    lock_dir: str = ".g3ku/resource-locks"
    logical_delete_guard: bool = True
    windows_fs_lock: bool = True


class ResourceRuntimeConfig(Base):
    enabled: bool = True
    skills_dir: str = "skills"
    tools_dir: str = "tools"
    manifest_name: str = "resource.yaml"
    reload: ResourceReloadConfig = Field(default_factory=ResourceReloadConfig)
    locks: ResourceLocksConfig = Field(default_factory=ResourceLocksConfig)
    state_path: str = ".g3ku/resources.state.json"



class MainRuntimeConfig(Base):
    enabled: bool = True
    store_path: str = '.g3ku/main-runtime/runtime.sqlite3'
    files_base_dir: str = '.g3ku/main-runtime/tasks'
    artifact_dir: str = '.g3ku/main-runtime/artifacts'
    governance_store_path: str = '.g3ku/main-runtime/governance.sqlite3'
    default_max_depth: int = 1
    hard_max_depth: int = 4
    event_history: "MainRuntimeEventHistoryConfig" = Field(default_factory=lambda: MainRuntimeEventHistoryConfig())
    disk_guard: "MainRuntimeDiskGuardConfig" = Field(default_factory=lambda: MainRuntimeDiskGuardConfig())
    node_dispatch_concurrency: "NodeDispatchConcurrencyConfig" = Field(default_factory=lambda: NodeDispatchConcurrencyConfig())
    duplicate_precheck: "MainRuntimeDuplicatePrecheckConfig" = Field(default_factory=lambda: MainRuntimeDuplicatePrecheckConfig())
    # 负载均衡总闸。关掉后含组的链按配置顺序摊平成 direct 候选，准入与 chat 一起回到
    # 改造前的有序链语义——这是回滚路径，不是给正常运维准备的开关。
    model_route_load_balance_enabled: bool = True


class MainRuntimeDiskGuardConfig(Base):
    """磁盘治理（P0 止血包）：ENOSPC 写保护、artifact gzip、终态清理。

    - `write_guard_enabled=False` 回滚到事故前行为（写异常原样上抛、不做预检）。
    - `artifact_gzip_threshold_bytes<=0` 关闭 artifact 压缩（纯明文落盘）。
    - `terminal_cleanup_enabled=False` 完整停用终态中间产物清理。
    - `terminal_temp_dir_cleanup_enabled=False`（默认）终态不自动硬删
      `temp/tasks/<id>` 任务临时目录；置 True 恢复终态即删的历史行为。
    紧急线 = max(emergency_min_bytes, 盘总量 * emergency_min_ratio)。
    """

    write_guard_enabled: bool = True
    emergency_min_bytes: int = 300 * 1024 * 1024
    emergency_min_ratio: float = 0.01
    usage_ttl_seconds: float = 5.0
    artifact_gzip_threshold_bytes: int = 1024 * 1024
    terminal_cleanup_enabled: bool = True
    terminal_temp_dir_cleanup_enabled: bool = False
    # P1：紧急态行为（自动暂停 + 防抖）。
    auto_pause_enabled: bool = True
    emergency_streak_samples: int = 3
    emergency_recovery_samples: int = 5
    alert_on_disk_emergency: bool = True
    # P3：终态任务大行裁剪。默认 0=停用——任务数据只随用户/模型工具手动
    # 删除而清除；配置 >0 恢复按天裁剪终态任务的五张大行表。
    detail_retention_days: int = 0

    @field_validator("detail_retention_days", mode="before")
    @classmethod
    def _normalize_detail_retention_days(cls, value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    @field_validator("emergency_streak_samples", "emergency_recovery_samples", mode="before")
    @classmethod
    def _normalize_streak_samples(cls, value: Any) -> int:
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 3

    @field_validator("emergency_min_bytes", mode="before")
    @classmethod
    def _normalize_emergency_min_bytes(cls, value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 300 * 1024 * 1024

    @field_validator("emergency_min_ratio", mode="before")
    @classmethod
    def _normalize_emergency_min_ratio(cls, value: Any) -> float:
        try:
            return min(max(0.0, float(value)), 0.5)
        except (TypeError, ValueError):
            return 0.01

    @field_validator("artifact_gzip_threshold_bytes", mode="before")
    @classmethod
    def _normalize_artifact_gzip_threshold_bytes(cls, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1024 * 1024


class MainRuntimeEventHistoryConfig(Base):
    enabled: bool = True
    dir: str = ''
    live_patch_persist_window_ms: int = 1000
    archive_encoding: str = 'gzip'

    @field_validator("live_patch_persist_window_ms", mode="before")
    @classmethod
    def _normalize_live_patch_persist_window_ms(cls, value: Any) -> int:
        if value is None:
            return 1000
        if isinstance(value, str) and not value.strip():
            return 1000
        return max(0, int(value))

    @field_validator("archive_encoding", mode="before")
    @classmethod
    def _normalize_archive_encoding(cls, value: Any) -> str:
        normalized = str(value or "gzip").strip().lower() or "gzip"
        if normalized not in {"gzip", "plain"}:
            return "gzip"
        return normalized


class MainRuntimeDuplicatePrecheckConfig(Base):
    """Duplicate-detection gate for create_async_task.

    `llm_review_enabled` toggles the semantic review pass; the deterministic
    rule layer (normalized target text + keyword fingerprint) always runs.
    """

    llm_review_enabled: bool = True


class NodeDispatchConcurrencyConfig(Base):
    execution: int = Field(default=DEFAULT_NODE_DISPATCH_CONCURRENCY["execution"], ge=1)
    inspection: int = Field(default=DEFAULT_NODE_DISPATCH_CONCURRENCY["inspection"], ge=1)

    @field_validator("execution", "inspection", mode="before")
    @classmethod
    def _normalize_dispatch_concurrency(cls, value: Any, info: ValidationInfo) -> int:
        if value is None:
            return DEFAULT_NODE_DISPATCH_CONCURRENCY[info.field_name]
        if isinstance(value, str) and not value.strip():
            return DEFAULT_NODE_DISPATCH_CONCURRENCY[info.field_name]
        return int(value)


class ExternalApiTokenConfig(Base):
    """One external bridge credential entry. The mapping key is the bridge id."""

    token: str = ""
    label: str = ""
    enabled: bool = True


def _normalize_external_token_id(value: Any) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")
    return normalized or "bridge"


class ExternalApiConfig(Base):
    """External Agent API (headless channel-bridge surface, `/api/v1`).

    Disabled by default: operators opt in and provision one token per bridge
    application. Token secrets live in the bootstrap secret overlay
    (extracted on save, stripped from the on-disk payload, re-applied on
    unlock).
    """

    enabled: bool = False
    event_buffer_size: int = 512
    tokens: dict[str, ExternalApiTokenConfig] = Field(default_factory=dict)

    @field_validator("tokens", mode="before")
    @classmethod
    def _normalize_token_ids(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, Any] = {}
        for raw_key, entry in value.items():
            normalized[_normalize_external_token_id(raw_key)] = entry
        return normalized


class QqBotAccountConfig(Base):
    """One official QQ bot application. The mapping key is the AppID."""

    app_secret: str = ""
    sandbox: bool = False
    enabled: bool = True
    label: str = ""


def _normalize_qq_app_id(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip()).strip("-")


class QqBotConfig(Base):
    """First-party official QQ bot (tencent-connect/botpy) bridge.

    Opt-in: operators paste one or more QQ open-platform AppID + AppSecret and
    flip ``enabled``. One AppID is one account: ``accounts`` is keyed by AppID
    (same shape as ``externalApi.tokens``), each getting its own bridge id
    (``qq-official-<appId>``), its own auto-provisioned token and its own
    in-process botpy connection. ``app_secret`` lives in the bootstrap secret
    overlay (extracted on save, stripped from disk, re-applied on unlock); the
    adapter talks to the local ``/api/v1`` like any external bridge.
    """

    enabled: bool = False
    accounts: dict[str, QqBotAccountConfig] = Field(default_factory=dict)

    @field_validator("accounts", mode="before")
    @classmethod
    def _normalize_account_ids(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, Any] = {}
        for raw_key, entry in value.items():
            normalized[_normalize_qq_app_id(raw_key)] = entry
        return normalized


class CronConfig(Base):
    """Cron scheduler dispatch robustness tunables.

    ``dispatch_timeout_seconds`` is the delivery-level watchdog budget: a job
    dispatch that has not returned within it gets its live await chain dumped
    to the log, is cancelled, and the run is finalized as ``timeout`` so one
    wedged session turn can never pin the scheduler. The default is
    deliberately larger than the longest reasonable agent turn (provider
    attempt timeout 10 min + retry rounds); ``<= 0`` disables the watchdog
    (unbounded await, legacy behavior).
    ``dispatch_cancel_grace_seconds`` bounds how long the watchdog waits for
    the cancelled dispatch to unwind before abandoning it detached (the job
    stays in-flight until the task really ends).
    """

    dispatch_timeout_seconds: float = 1800.0
    dispatch_cancel_grace_seconds: float = 10.0


STT_MODEL_NAMES = ("tiny", "base", "small")


class SttConfig(Base):
    """Local speech-to-text, driven by the official ``whisper.cpp`` CLI binary.

    On by default: a fresh install is *allowed* to transcribe, and turning this
    off is the operator's way of saying "never touch my microphone lane". What
    stays explicit is the ~157 MB of artifacts (15 MB binary + 142 MB ``base``
    model) -- nothing here downloads or runs inference because a default said
    so: the engine only reports ``ready=False`` until ``g3ku stt prepare`` (or
    the composer's first-click download) has placed them.

    Every default below is measured on this product's own floor (2 vCPU / 4
    logical CPUs, 7.7 GB RAM, no GPU) against a 22.9s Mandarin clip, not copied
    from upstream docs:

    * ``base``: 6.9s wall, 318 MB peak RSS. ``tiny``: 3.8s, ~250 MB, more
      errors. ``small``: 20.9-32.9s (slower than realtime), 854 MB.
    * ``threads`` 4 instead of 2 is 26% faster but saturates the box and
      competes with a live agent turn.
    * ``language`` ``zh`` is 33% faster than ``auto`` (6.9 s vs 9.2 s on the
      same clip) but answered 3 s of silence with two fluent sentences, while
      ``auto`` returned empty. Correctness wins the default; ``min_rms_dbfs``
      reclaims most of the speed for the empty-recording case.
    * ``-bo 1`` is deliberately not offered: on 3s of silence the default
      best-of rejected the output (empty), best-of 1 emitted two fluent
      sentences.
    * ``simplify_chinese``: whisper's ``base``/``tiny`` wrote every Chinese
      clip in traditional characters on this box; ``small`` wrote simplified.
      Conversion costs 48 microseconds, so it is a post-pass, not a model
      upgrade.

    The binary is pinned by release tag *and* digest on purpose: upstream
    publishes Windows builds under build tags (``b5130`` shipped the same day
    as v1.9.4), and this machine reaches GitHub assets at ~20 KB/s, so a
    silent "latest" lookup would be neither auditable nor fast.
    """

    enabled: bool = True
    model: str = "base"
    language: str = "auto"
    threads: int = 2
    max_audio_seconds: int = 60
    min_rms_dbfs: float = -70.0
    timeout_seconds: int = 45
    simplify_chinese: bool = True
    model_dir: str = ".g3ku/stt"
    binary_path: str = ""
    binary_release_tag: str = "b5130"
    binary_sha256: str = "f9ec6c52a2e949b62ab51fa21d0d497958f9e41c3010c157c4e42932d5316f3c"
    binary_download_base_url: str = "https://github.com/ggml-org/whisper.cpp/releases/download"
    model_download_base_url: str = "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main"

    @field_validator("model", mode="before")
    @classmethod
    def _normalize_model(cls, value: Any) -> str:
        name = str(value or "").strip().lower()
        return name if name in STT_MODEL_NAMES else "base"

    @field_validator("binary_download_base_url", "model_download_base_url", mode="after")
    @classmethod
    def _reject_insecure_download_base(cls, value: str) -> str:
        if not str(value or "").strip().lower().startswith("https://"):
            raise ValueError("stt download URLs must be https://")
        return str(value).strip().rstrip("/")


class UpdateCheckConfig(Base):
    """Release-tag polling cadence for an installed device.

    Only the web process reads this, and only after the project is unlocked:
    the check rides the outbox reconcile loop, which never starts while the
    bus is absent. A check reads one remote tag list and never reports the
    local version, so an offline or non-GitHub remote simply leaves the ledger
    stale — which the UI renders as "unknown", never as "up to date".
    """

    enabled: bool = True
    interval_hours: float = Field(default=5.0, ge=0.1, le=24.0)


class Config(BaseSettings):
    """Root configuration for g3ku."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    tool_secrets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resources: ResourceRuntimeConfig = Field(default_factory=ResourceRuntimeConfig)
    main_runtime: MainRuntimeConfig = Field(default_factory=MainRuntimeConfig)
    external_api: ExternalApiConfig = Field(default_factory=ExternalApiConfig)
    qq_bot: QqBotConfig = Field(default_factory=QqBotConfig)
    cron: CronConfig = Field(default_factory=CronConfig)
    stt: SttConfig = Field(default_factory=SttConfig)
    update_check: UpdateCheckConfig = Field(default_factory=UpdateCheckConfig)

    @model_validator(mode="after")
    def _validate_model_runtime_contract(self) -> "Config":
        catalog = list(self.models.catalog or [])
        catalog_by_key: dict[str, ManagedModelConfig] = {}
        for item in catalog:
            key = str(item.key or "").strip()
            existing = catalog_by_key.get(key)
            if existing is not None:
                raise ValueError(f"Duplicate model key in models.catalog: {key}")
            catalog_by_key[key] = item

        groups = dict(self.models.load_balance_groups or {})
        for group_key, group in groups.items():
            # group key 与 model key 共用一个命名空间会让人读不出「这个引用指向谁」，
            # 而两条解析车道完全不同（组要先展开成员）。
            if group_key in catalog_by_key:
                raise ValueError(
                    f"models.loadBalanceGroups key collides with a model key: {group_key}"
                )
            if not group.model_keys:
                raise ValueError(f"models.loadBalanceGroups.{group_key} must configure modelKeys")
            for member in group.model_keys:
                item = catalog_by_key.get(str(member or "").strip())
                if item is None:
                    raise ValueError(
                        f"models.loadBalanceGroups.{group_key} references unknown model key: {member}"
                    )
                if not item.enabled:
                    raise ValueError(
                        f"models.loadBalanceGroups.{group_key} references disabled model key: {member}"
                    )

        # 组引用检查覆盖全部四个 scope：`memory` 不在 REQUIRED_MODEL_ROLES 里，但第一
        # 阶段同样不允许它用组（记忆车道有固定单并发与 chat capability 契约）。
        for scope in ("ceo", "execution", "inspection", "memory"):
            for entry in self.get_role_model_routes(scope):
                if entry.type != "load_balance":
                    continue
                if scope not in LOAD_BALANCE_ROUTE_SCOPES:
                    raise ValueError(
                        f"负载均衡组当前仅支持 execution/inspection，models.roles.{scope} "
                        f"不能引用 groupKey: {entry.group_key}"
                    )
                group_key = str(entry.group_key or "").strip()
                if not group_key:
                    raise ValueError(f"models.roles.{scope} has a load_balance entry without groupKey")
                if group_key not in groups:
                    raise ValueError(f"models.roles.{scope} references unknown group key: {group_key}")

        for scope in REQUIRED_MODEL_ROLES:
            for entry in self.get_role_model_routes(scope):
                if entry.type == "load_balance":
                    continue
                model_key = str(entry.model_key or "").strip()
                item = catalog_by_key.get(model_key)
                if item is None:
                    raise ValueError(f"models.roles.{scope} references unknown model key: {model_key}")
                if not item.enabled:
                    raise ValueError(f"models.roles.{scope} references disabled model key: {model_key}")

        return self

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    @staticmethod
    def parse_provider_model(value: str) -> tuple[str, str]:
        """Parse strict provider:model syntax and return (provider_id, model_id)."""
        from g3ku.providers.registry import PROVIDERS, find_by_name

        raw = (value or "").strip()
        if not raw:
            raise ValueError(
                "Invalid provider_model.\n"
                "Original field value: ''\n"
                "New required format: provider:model\n"
                "Example fix: provider_model = 'openai:gpt-4.1'"
            )
        if ":" not in raw:
            hint = "openai:gpt-4.1" if "/" in raw else "anthropic:claude-sonnet-4-5"
            raise ValueError(
                "Invalid provider_model syntax.\n"
                f"Original field value: {raw!r}\n"
                "New required format: provider:model (colon separator).\n"
                f"Example fix: provider_model = '{hint}'"
            )

        provider_part, model_part = raw.split(":", 1)
        provider_id = provider_part.strip().lower().replace("-", "_")
        model_id = model_part.strip()
        if not provider_id or not model_id:
            raise ValueError(
                "Invalid provider_model.\n"
                f"Original field value: {raw!r}\n"
                "New required format: provider:model (both provider and model must be non-empty).\n"
                "Example fix: provider_model = 'openrouter:anthropic/claude-sonnet-4-5'"
            )

        if find_by_name(provider_id) is None:
            supported = ", ".join(spec.name for spec in PROVIDERS)
            raise ValueError(
                "Unknown provider in provider_model.\n"
                f"Original provider: {provider_part!r}\n"
                f"New supported providers: {supported}\n"
                "Example fix: provider_model = 'openai:gpt-4.1'"
            )

        return provider_id, model_id

    def get_model_target(self, model_key: str | None = None) -> tuple[str, str]:
        """Get parsed (provider_id, model_id) for a managed model key."""
        managed = self.get_managed_model(model_key)
        if managed is None:
            raise ValueError(f"Unknown model key: {model_key}")
        if str(managed.llm_config_id or "").strip():
            from g3ku.llm_config.facade import LLMConfigFacade

            binding = LLMConfigFacade(self.workspace_path).get_binding(self, managed.key)
            provider_model = str(binding.get("provider_model") or "").strip()
            if provider_model:
                return self.parse_provider_model(provider_model)
        return self.parse_provider_model(str(managed.provider_model or "").strip())

    def get_role_model_routes(self, role: str) -> list[ModelRouteEntry]:
        """该角色的有序 route entry 列表（fallback 顺序）。

        返回值是副本：调用方改动不应影响活配置对象，热刷新靠整份 config 替换。
        """
        normalized = normalize_role_scope(role)
        return [entry.model_copy(deep=True) for entry in getattr(self.models.roles, normalized)]

    def get_load_balance_group(self, group_key: str | None) -> ModelLoadBalanceGroup | None:
        key = str(group_key or "").strip()
        if not key:
            return None
        group = dict(self.models.load_balance_groups or {}).get(key)
        return group.model_copy(deep=True) if group is not None else None

    def get_role_model_keys(self, role: str) -> list[str]:
        """展开后的候选 model key 列表（去重，保持 route 顺序与组内声明顺序）。

        这是**候选视图**，不再代表 fallback 顺序：组内的顺序只用于展示与稳定序列化。
        需要按序 fallback 的代码必须改用 ``get_role_model_routes``；仍读第一个元素的
        代码只允许出现在 legacy 扁平链与纯展示路径上（见 FIX_PLAN §13 Q6）。
        被禁用的组（``enabled=false``）整段跳过，等于该 route entry 不生效。
        """
        normalized = normalize_role_scope(role)
        keys: list[str] = []
        seen: set[str] = set()
        for entry in getattr(self.models.roles, normalized):
            for candidate in self._route_entry_model_keys(entry):
                if candidate in seen:
                    continue
                seen.add(candidate)
                keys.append(candidate)
        return keys

    def _route_entry_model_keys(self, entry: ModelRouteEntry) -> list[str]:
        if entry.type == "load_balance":
            group = self.get_load_balance_group(entry.group_key)
            if group is None or not group.enabled:
                return []
            return [str(key or "").strip() for key in group.model_keys if str(key or "").strip()]
        model_key = str(entry.model_key or "").strip()
        return [model_key] if model_key else []

    def set_role_model_routes(self, role: str, entries: list[Any]) -> None:
        """写入某个 scope 的 route 列表（字符串/dict/ModelRouteEntry 混合都吃）。

        所有写路径都必须经过这里：字段类型是 `ModelRouteEntry`，直接塞裸字符串不会
        被 pydantic 拦下（未开 validate_assignment），但会让重命名/删除这类按 key 比较
        的写路径静默失配，并在序列化时产出 unexpected-value 警告。
        """
        normalized = normalize_role_scope(role)
        resolved: list[ModelRouteEntry] = []
        seen: set[tuple[str, str]] = set()
        for item in list(entries or []):
            entry = coerce_route_entry(item)
            identity = (entry.type, entry.group_key or entry.model_key or "")
            if not identity[1] or identity in seen:
                continue
            seen.add(identity)
            resolved.append(entry)
        setattr(self.models.roles, normalized, resolved)

    def set_role_model_keys(self, role: str, model_keys: list[str]) -> None:
        """按旧的扁平字符串数组写入一条链（全是 direct model，不掺组）。"""
        keys = [str(key or "").strip() for key in list(model_keys or []) if str(key or "").strip()]
        self.set_role_model_routes(role, keys)

    def iter_role_scopes(self) -> tuple[str, ...]:
        return ("ceo", "execution", "inspection", "memory")

    def rename_model_key_in_routing(self, old_key: str, new_key: str) -> None:
        """把一个绑定 key 的全部路由引用改名：链上的 direct entry + 组内成员。

        组内改名撞上已有成员时直接报错——静默去重会悄悄改掉这个组的负载权重。
        """
        old = str(old_key or "").strip()
        new = str(new_key or "").strip()
        if not old or not new or old == new:
            return
        for scope in self.iter_role_scopes():
            entries = self.get_role_model_routes(scope)
            touched = False
            for entry in entries:
                if entry.type == "model" and entry.model_key == old:
                    entry.model_key = new
                    touched = True
            if touched:
                self.set_role_model_routes(scope, entries)

        groups = dict(self.models.load_balance_groups or {})
        for group_key, group in groups.items():
            members = [str(key or "").strip() for key in list(group.model_keys or [])]
            if old not in members:
                continue
            renamed: list[str] = []
            for member in members:
                renamed.append(new if member == old else member)
            if len(set(renamed)) != len(renamed):
                raise ValueError(
                    f"models.loadBalanceGroups.{group_key} 已经有成员 {new}，无法把 {old} 改名过去"
                )
            group.model_keys = renamed
        self.models.load_balance_groups = groups

    def collect_model_key_routing_refs(self, key: str) -> dict[str, Any]:
        """列出某个 model key 被谁引用，用于删除/禁用前的可读错误。"""
        target = str(key or "").strip()
        refs: dict[str, Any] = {"roles": [], "groups": [], "group_backed_roles": []}
        if not target:
            return refs
        for scope in self.iter_role_scopes():
            for entry in self.get_role_model_routes(scope):
                if entry.type == "model" and entry.model_key == target:
                    refs["roles"].append(scope)
                elif entry.type == "load_balance":
                    group = self.get_load_balance_group(entry.group_key)
                    if group is not None and target in [str(m or "").strip() for m in group.model_keys]:
                        refs["group_backed_roles"].append(scope)
        for group_key, group in dict(self.models.load_balance_groups or {}).items():
            if target in [str(m or "").strip() for m in list(group.model_keys or [])]:
                refs["groups"].append(str(group_key))
        return refs

    def remove_model_key_from_routing(self, key: str) -> None:
        """从链上与组里摘掉一个 model key。

        链上 direct entry 直接删（与旧的静默移除行为一致）。组里删成员后若组空了，
        **报错而不是顺手删组**：那会让引用它的整条车道静默失去负载均衡。
        """
        target = str(key or "").strip()
        if not target:
            return
        groups = dict(self.models.load_balance_groups or {})
        emptied = [
            str(group_key)
            for group_key, group in groups.items()
            if [str(m or "").strip() for m in list(group.model_keys or [])] == [target]
        ]
        if emptied:
            refs = self.collect_model_key_routing_refs(target)
            used_by = ", ".join(
                [f"models.loadBalanceGroups.{item}" for item in emptied]
                + [f"models.roles.{item}" for item in sorted(set(refs["group_backed_roles"]))]
            )
            raise ValueError(
                f"模型 {target} 是负载均衡组的最后一个成员，不能直接删除：{used_by}。"
                "先给这些组加入其他成员，或改用整链编辑。"
            )
        for group_key, group in groups.items():
            members = [str(m or "").strip() for m in list(group.model_keys or [])]
            if target in members:
                group.model_keys = [m for m in members if m != target]
        self.models.load_balance_groups = groups

        for scope in self.iter_role_scopes():
            entries = self.get_role_model_routes(scope)
            kept = [entry for entry in entries if not (entry.type == "model" and entry.model_key == target)]
            if len(kept) != len(entries):
                self.set_role_model_routes(scope, kept)

    def get_role_max_iterations(self, role: str) -> int | None:
        normalized = normalize_role_scope(role)
        value = getattr(self.agents.role_iterations, normalized, DEFAULT_ROLE_MAX_ITERATIONS[normalized])
        if value is None:
            return None
        return max(0, int(value))

    def get_role_max_concurrency(self, role: str) -> int | None:
        normalized = normalize_role_scope(role)
        value = getattr(self.agents.role_concurrency, normalized, DEFAULT_ROLE_MAX_CONCURRENCY[normalized])
        if value is None:
            return None
        return max(0, int(value))

    def get_node_dispatch_concurrency(self, role: str) -> int:
        normalized = normalize_role_scope(role)
        if normalized not in DEFAULT_NODE_DISPATCH_CONCURRENCY:
            raise ValueError(f"Invalid node dispatch scope: {role}")
        value = getattr(
            self.main_runtime.node_dispatch_concurrency,
            normalized,
            DEFAULT_NODE_DISPATCH_CONCURRENCY[normalized],
        )
        return max(1, int(value or DEFAULT_NODE_DISPATCH_CONCURRENCY[normalized]))

    def resolve_role_model_key(self, role: str) -> str:
        refs = self.get_role_model_keys(role)
        if refs:
            return str(refs[0]).strip()
        raise ValueError(f"No model configured for role '{role}'.")

    def get_role_model_target(self, role: str) -> tuple[str, str]:
        return self.get_model_target(self.resolve_role_model_key(role))

    def resolve_scope_model_reference(self, scope: str) -> str:
        """Resolve the primary model reference configured for a runtime scope."""
        return self.resolve_role_model_key(scope)

    def get_scope_model_target(self, scope: str) -> tuple[str, str]:
        """Get parsed (provider_id, model_id) for a runtime scope."""
        return self.get_role_model_target(scope)

    def get_managed_model(self, ref: str | None = None) -> ManagedModelConfig | None:
        key = str(ref or "").strip()
        if not key:
            return None
        for item in self.models.catalog:
            if str(item.key or "").strip() == key:
                return item
        return None

    def resolve_provider_model_reference(self, ref: str | None = None) -> str:
        raw = str(ref or "").strip()
        managed = self.get_managed_model(raw)
        if managed is None:
            raise ValueError(f"Unknown model key: {ref}")
        if str(managed.llm_config_id or "").strip():
            from g3ku.llm_config.facade import LLMConfigFacade

            binding = LLMConfigFacade(self.workspace_path).get_binding(self, managed.key)
            return str(binding.get("provider_model") or "").strip()
        return str(managed.provider_model or "").strip()

    def get_provider(self, model_key: str | None = None) -> ProviderConfig | None:
        """Get provider config selected by managed model key."""
        managed = self.get_managed_model(model_key)
        if managed is None:
            raise ValueError(f"Unknown model key: {model_key}")
        if str(managed.llm_config_id or "").strip():
            from g3ku.llm_config.facade import LLMConfigFacade

            binding = LLMConfigFacade(self.workspace_path).get_binding(self, managed.key)
            return ProviderConfig(
                api_key=str(binding.get("api_key") or ""),
                api_base=binding.get("api_base"),
                extra_headers=binding.get("extra_headers"),
            )
        return ProviderConfig(
            api_key=str(managed.api_key or ""),
            api_base=managed.api_base,
            extra_headers=managed.extra_headers,
        )

    def get_provider_name(self, model_key: str | None = None) -> str | None:
        """Get provider name from managed model key."""
        provider_id, _ = self.get_model_target(model_key)
        return provider_id

    def get_api_key(self, model_key: str | None = None) -> str | None:
        """Get API key for the provider selected by managed model key."""
        p = self.get_provider(model_key)
        return p.api_key if p else None

    def get_api_base(self, model_key: str | None = None) -> str | None:
        """Get API base URL for a managed model key."""
        managed = self.get_managed_model(model_key)
        if managed is None:
            raise ValueError(f"Unknown model key: {model_key}")
        if str(managed.llm_config_id or "").strip():
            from g3ku.llm_config.facade import LLMConfigFacade

            binding = LLMConfigFacade(self.workspace_path).get_binding(self, managed.key)
            api_base = str(binding.get("api_base") or "").strip()
            return api_base or None
        if managed is not None and managed.api_base:
            return managed.api_base
        provider_id, _ = self.get_model_target(model_key)
        p = getattr(self.providers, provider_id, None)
        if p and p.api_base:
            return p.api_base
        return None

    def get_model_runtime_profile(self, model_key: str | None = None) -> ManagedModelConfig | None:
        return self.get_managed_model(model_key)

    def get_scope_model_refs(self, scope: str) -> list[str]:
        return self.get_role_model_keys(scope)

    def get_scope_model_chain(self, scope: str) -> list[ModelFallbackTarget]:
        chain: list[ModelFallbackTarget] = []
        for ref in self.get_scope_model_refs(scope):
            key = str(ref or "").strip()
            if not key:
                continue
            managed = self.get_managed_model(key)
            if managed is not None:
                if not managed.enabled:
                    continue
                chain.append(
                    ModelFallbackTarget(
                        model_key=key,
                        retry_on=list(managed.retry_on or []),
                        retry_count=int(getattr(managed, "retry_count", 0) or 0),
                    )
                )
                continue
            chain.append(ModelFallbackTarget(model_key=key))
        return chain

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, env_prefix="G3KU_", env_nested_delimiter="__")












