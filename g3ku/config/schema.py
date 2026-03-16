"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

ROLE_SCOPE_ALIASES = {
    "ceo": "ceo",
    "execution": "execution",
    "inspection": "inspection",
    "checker": "inspection",
}

REQUIRED_MODEL_ROLES = ("ceo", "execution", "inspection")


def normalize_role_scope(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    normalized = ROLE_SCOPE_ALIASES.get(raw)
    if normalized is None:
        raise ValueError(f"Invalid scope: {value}")
    return normalized


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChinaCompatConfig(Base):
    """Compat config for extracted china channels; allow upstream fields to pass through."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    enabled: bool = False
    name: str | None = None
    default_account: str | None = None
    accounts: dict[str, dict[str, Any]] = Field(default_factory=dict)


class QQBotCompatConfig(ChinaCompatConfig):
    app_id: str | int | None = None
    client_secret: str | None = None
    token: str | None = None
    webhook_path: str | None = None
    mode: str | None = None


class WecomCompatConfig(ChinaCompatConfig):
    bot_id: str | None = None
    secret: str | None = None
    token: str | None = None
    encoding_aes_key: str | None = None
    receive_id: str | None = None
    webhook_path: str | None = None
    mode: str | None = None


class WecomAppCompatConfig(ChinaCompatConfig):
    corp_id: str | None = None
    corp_secret: str | None = None
    agent_id: int | None = None
    token: str | None = None
    encoding_aes_key: str | None = None
    webhook_path: str | None = None
    mode: str | None = None


class FeishuChinaCompatConfig(ChinaCompatConfig):
    app_id: str | None = None
    app_secret: str | None = None
    token: str | None = None
    webhook_path: str | None = None
    mode: str | None = None

class ChinaBridgeChannelsConfig(Base):
    """China channel configs hosted by the internal Node communication subsystem."""

    qqbot: QQBotCompatConfig = Field(default_factory=QQBotCompatConfig)
    dingtalk: ChinaCompatConfig = Field(default_factory=ChinaCompatConfig)
    wecom: WecomCompatConfig = Field(default_factory=WecomCompatConfig)
    wecom_app: WecomAppCompatConfig = Field(default_factory=WecomAppCompatConfig)
    feishu_china: FeishuChinaCompatConfig = Field(default_factory=FeishuChinaCompatConfig)




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
    runtime: Literal["langgraph"] = "langgraph"  # Single runtime mode (classic removed)
    max_tokens: int = 8192
    temperature: float = 0.1
    max_tool_iterations: int = 40
    memory_window: int = 100
    reasoning_effort: str | None = None  # low / medium / high ? enables LLM thinking mode
    middlewares: list[AgentMiddlewareConfig] = Field(default_factory=list)

    @field_validator("runtime", mode="before")
    @classmethod
    def _validate_runtime(cls, value: Any) -> str:
        runtime = str(value or "langgraph").strip().lower()
        if runtime == "classic":
            raise ValueError(
                "Unsupported config after 1.0 migration.\n"
                "Original field: agents.defaults.runtime = 'classic'\n"
                "New behavior: g3ku only supports LangGraph runtime.\n"
                "Example fix:\n"
                "  {\n"
                "    \"agents\": {\n"
                "      \"defaults\": {\n"
                "        \"model\": \"openai:gpt-4.1\"\n"
                "      }\n"
                "    }\n"
                "  }"
            )
        if runtime != "langgraph":
            raise ValueError(
                "Invalid agents.defaults.runtime.\n"
                f"Original field value: {value!r}\n"
                "New supported value: 'langgraph' (single runtime mode).\n"
                "Example fix: set agents.defaults.runtime to 'langgraph' or remove the field."
            )
        return "langgraph"


class ModelFallbackTarget(Base):
    model_key: str
    retry_on: list[str] = Field(default_factory=lambda: ["network", "429", "5xx"])

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_payload(cls, value: Any) -> Any:
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
        items = value if isinstance(value, list) else ["network", "429", "5xx"]
        clean: list[str] = []
        seen: set[str] = set()
        for item in items:
            token = str(item or "").strip().lower()
            if not token or token in seen:
                continue
            seen.add(token)
            clean.append(token)
        return clean or ["network", "429", "5xx"]

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
    max_tokens: int = 4096
    temperature: float = 0.1
    reasoning_effort: str | None = None
    retry_on: list[str] = Field(default_factory=lambda: ["network", "429", "5xx"])
    description: str = ""

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
        items = value if isinstance(value, list) else ["network", "429", "5xx"]
        clean: list[str] = []
        seen: set[str] = set()
        for item in items:
            token = str(item or "").strip().lower()
            if not token or token in seen:
                continue
            seen.add(token)
            clean.append(token)
        return clean or ["network", "429", "5xx"]

    @model_validator(mode="after")
    def _validate_binding_or_legacy_payload(self) -> "ManagedModelConfig":
        llm_config_id = str(self.llm_config_id or "").strip()
        provider_model = str(self.provider_model or "").strip()
        api_key = str(self.api_key or "").strip()
        if llm_config_id:
            self.llm_config_id = llm_config_id
            return self
        if not provider_model:
            raise ValueError("models.catalog[].provider_model or llm_config_id is required")
        if not api_key:
            raise ValueError("models.catalog[].api_key is required before migration")
        return self


class RoleModelRoutingConfig(Base):
    """Ordered model references for each runtime scope."""

    ceo: list[str] = Field(default_factory=list)
    execution: list[str] = Field(default_factory=list)
    inspection: list[str] = Field(default_factory=list)

    @field_validator("ceo", "execution", "inspection", mode="before")
    @classmethod
    def _normalize_chain(cls, value: Any) -> list[str]:
        items = value if isinstance(value, list) else []
        clean: list[str] = []
        seen: set[str] = set()
        for item in items:
            key = str(item or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            clean.append(key)
        return clean


class ModelsConfig(Base):
    """Managed model catalog and role routing."""

    catalog: list[ManagedModelConfig] = Field(default_factory=list)
    roles: RoleModelRoutingConfig = Field(default_factory=RoleModelRoutingConfig)


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


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)
    multi_agent: MultiAgentConfig = Field(default_factory=MultiAgentConfig)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # DashScope API gateway
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow API gateway
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine API gateway
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig)  # Github Copilot (OAuth)
    responses: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Responses (/v1/responses)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)


class MemoryCheckpointerConfig(Base):
    """LangGraph checkpointer configuration."""

    backend: Literal["sqlite", "memory"] = "sqlite"
    path: str = "memory/checkpoints.sqlite3"


class MemoryStoreBackendConfig(Base):
    """Hybrid memory store backend configuration."""

    backend: Literal["hybrid"] = "hybrid"
    qdrant_path: str = "memory/qdrant"
    qdrant_collection: str = "g3ku_memory_v1"
    sqlite_path: str = "memory/memory.db"


class MemoryRetrievalConfig(Base):
    """RAG retrieval controls."""

    dense_top_k: int = 24
    sparse_top_k: int = 24
    fused_top_k: int = 12
    context_top_k: int = 8
    sentence_window: int = 3
    max_context_tokens: int = 1200
    default_load_level: Literal["l0", "l1", "l2"] = "l1"
    rerank_model_key: str | None = None
    rerank_provider_model: str = ""


class MemoryEmbeddingConfig(Base):
    """Embedding source configuration."""

    model_key: str | None = None
    provider_model: str = "openai:text-embedding-3-large"
    batch_size: int = 32


class MemoryIsolationConfig(Base):
    """Namespace isolation controls."""

    mode: Literal["session", "channel", "global"] = "session"
    namespace_template: list[str] = Field(default_factory=lambda: ["memory", "{channel}", "{chat_id}"])


class MemoryGuardConfig(Base):
    """Write-guard configuration for long-term memory."""

    mode: Literal["tiered", "auto", "manual"] = "tiered"
    auto_fact_confidence: float = 0.8


class MemoryCompatConfig(Base):
    """Compatibility toggles for legacy files."""

    dual_write_legacy_files: bool = True


class MemoryFeaturesConfig(Base):
    """Feature switches for memory architecture v2."""

    unified_context: bool = True
    layered_loading: bool = True
    query_planner: bool = True
    commit_pipeline: bool = True
    split_store: bool = True
    observability: bool = True


class MemoryCommitConfig(Base):
    """Session commit trigger controls."""

    turn_trigger: int = 20
    idle_minutes_trigger: int = 360


class MemoryCostConfig(Base):
    """Cost governance controls."""

    max_increase_pct: int = 15


class MemoryAssemblyConfig(Base):
    """Prompt/context assembly controls for frontdoor orchestration."""

    recent_messages_limit: int = 24
    archive_summary_top_k: int = 2
    archive_summary_max_tokens: int = 320
    skill_inventory_top_k: int = 8
    skill_inventory_max_tokens: int = 480
    extension_tool_top_k: int = 6
    core_tools: list[str] = Field(
        default_factory=lambda: [
            'create_async_task',
            'task_summary',
            'task_list',
            'task_progress',
            'memory_search',
            'message',
        ]
    )


class MemoryCatalogSummaryConfig(Base):
    """Catalog summary model selection for skill/tool layered abstracts."""

    model_key: str | None = None


class MemoryToolsConfig(Base):
    """RAG memory runtime configuration."""

    enabled: bool = True
    mode: Literal["legacy", "rag", "dual"] = "dual"
    backend: Literal["rag"] = "rag"
    arch_version: Literal["v1", "v2"] = "v2"
    features: MemoryFeaturesConfig = Field(default_factory=MemoryFeaturesConfig)
    checkpointer: MemoryCheckpointerConfig = Field(default_factory=MemoryCheckpointerConfig)
    store: MemoryStoreBackendConfig = Field(default_factory=MemoryStoreBackendConfig)
    retrieval: MemoryRetrievalConfig = Field(default_factory=MemoryRetrievalConfig)
    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)
    isolation: MemoryIsolationConfig = Field(default_factory=MemoryIsolationConfig)
    guard: MemoryGuardConfig = Field(default_factory=MemoryGuardConfig)
    compat: MemoryCompatConfig = Field(default_factory=MemoryCompatConfig)
    commit: MemoryCommitConfig = Field(default_factory=MemoryCommitConfig)
    cost: MemoryCostConfig = Field(default_factory=MemoryCostConfig)
    assembly: MemoryAssemblyConfig = Field(default_factory=MemoryAssemblyConfig)
    catalog_summary: MemoryCatalogSummaryConfig = Field(default_factory=MemoryCatalogSummaryConfig)
    bootstrap_mode: Literal["new_only", "full", "none"] = "new_only"
    retention_days: int | None = None



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


class ChinaBridgeConfig(Base):
    enabled: bool = False
    bind_host: str = "0.0.0.0"
    public_port: int = 18889
    control_host: str = "127.0.0.1"
    control_port: int = 18989
    control_token: str = ""
    auto_start: bool = True
    node_bin: str = "node"
    npm_client: str = "pnpm"
    state_dir: str = ".g3ku/china-bridge"
    log_level: str = "info"
    send_progress: bool = True
    send_tool_hints: bool = False
    channels: ChinaBridgeChannelsConfig = Field(default_factory=ChinaBridgeChannelsConfig)

class Config(BaseSettings):
    """Root configuration for g3ku."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tool_secrets: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resources: ResourceRuntimeConfig = Field(default_factory=ResourceRuntimeConfig)
    main_runtime: MainRuntimeConfig = Field(default_factory=MainRuntimeConfig)
    china_bridge: ChinaBridgeConfig = Field(default_factory=ChinaBridgeConfig)

    @model_validator(mode="after")
    def _validate_model_runtime_contract(self) -> "Config":
        catalog = list(self.models.catalog or [])
        if not catalog:
            raise ValueError("models.catalog must be non-empty")

        catalog_by_key: dict[str, ManagedModelConfig] = {}
        for item in catalog:
            key = str(item.key or "").strip()
            existing = catalog_by_key.get(key)
            if existing is not None:
                raise ValueError(f"Duplicate model key in models.catalog: {key}")
            catalog_by_key[key] = item

        for scope in REQUIRED_MODEL_ROLES:
            chain = getattr(self.models.roles, scope)
            if not chain:
                raise ValueError(f"models.roles.{scope} must be non-empty")
            for model_key in chain:
                item = catalog_by_key.get(str(model_key or "").strip())
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

    def get_role_model_keys(self, role: str) -> list[str]:
        normalized = normalize_role_scope(role)
        return list(getattr(self.models.roles, normalized))

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
        """Get API base URL for a managed model key, with gateway defaults."""
        from g3ku.providers.registry import find_by_name

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
        spec = find_by_name(provider_id)
        if spec and spec.is_gateway and spec.default_api_base:
            return spec.default_api_base
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
                chain.append(ModelFallbackTarget(model_key=key, retry_on=list(managed.retry_on or [])))
                continue
            chain.append(ModelFallbackTarget(model_key=key))
        return chain

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, env_prefix="G3KU_", env_nested_delimiter="__")












