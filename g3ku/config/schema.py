"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings

ROLE_SCOPE_ALIASES = {
    "agent": "agent",
    "main": "agent",
    "default": "agent",
    "ceo": "ceo",
    "org_graph.ceo": "ceo",
    "org_graph_ceo": "ceo",
    "execution": "execution",
    "org_graph.execution": "execution",
    "org_graph_execution": "execution",
    "inspection": "inspection",
    "checker": "inspection",
    "org_graph.inspection": "inspection",
    "org_graph_inspection": "inspection",
}

REQUIRED_MODEL_ROLES = ("agent", "ceo", "execution", "inspection")


def normalize_role_scope(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    normalized = ROLE_SCOPE_ALIASES.get(raw)
    if normalized is None:
        raise ValueError(f"Invalid scope: {value}")
    return normalized


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class WhatsAppConfig(Base):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (optional, recommended)
    allow_from: list[str] = Field(default_factory=list)  # Allowed phone numbers


class TelegramConfig(Base):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    reply_to_message: bool = False  # If true, bot replies quote the original message


class FeishuConfig(Base):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(default_factory=list)  # Allowed user open_ids
    react_emoji: str = "THUMBSUP"  # Emoji type for message reactions (e.g. THUMBSUP, OK, DONE, SMILE)


class DingTalkConfig(Base):
    """DingTalk channel configuration using Stream mode."""

    enabled: bool = False
    client_id: str = ""  # AppKey
    client_secret: str = ""  # AppSecret
    allow_from: list[str] = Field(default_factory=list)  # Allowed staff_ids


class DiscordConfig(Base):
    """Discord channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT


class MatrixConfig(Base):
    """Matrix (Element) channel configuration."""

    enabled: bool = False
    homeserver: str = "https://matrix.org"
    access_token: str = ""
    user_id: str = ""  # @bot:matrix.org
    device_id: str = ""
    e2ee_enabled: bool = True # Enable Matrix E2EE support (encryption + encrypted room handling).
    sync_stop_grace_seconds: int = 2 # Max seconds to wait for sync_forever to stop gracefully before cancellation fallback.
    max_media_bytes: int = 20 * 1024 * 1024 # Max attachment size accepted for Matrix media handling (inbound + outbound).
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["open", "mention", "allowlist"] = "open"
    group_allow_from: list[str] = Field(default_factory=list)
    allow_room_mentions: bool = False


class EmailConfig(Base):
    """Email channel configuration (IMAP inbound + SMTP outbound)."""

    enabled: bool = False
    consent_granted: bool = False  # Explicit owner permission to access mailbox data

    # IMAP (receive)
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True

    # SMTP (send)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""

    # Behavior
    auto_reply_enabled: bool = True  # If false, inbound email is read but no automatic reply is sent
    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "
    allow_from: list[str] = Field(default_factory=list)  # Allowed sender email addresses


class MochatMentionConfig(Base):
    """Mochat mention behavior configuration."""

    require_in_groups: bool = False


class MochatGroupRule(Base):
    """Mochat per-group mention requirement."""

    require_mention: bool = False


class MochatConfig(Base):
    """Mochat channel configuration."""

    enabled: bool = False
    base_url: str = "https://mochat.io"
    socket_url: str = ""
    socket_path: str = "/socket.io"
    socket_disable_msgpack: bool = False
    socket_reconnect_delay_ms: int = 1000
    socket_max_reconnect_delay_ms: int = 10000
    socket_connect_timeout_ms: int = 10000
    refresh_interval_ms: int = 30000
    watch_timeout_ms: int = 25000
    watch_limit: int = 100
    retry_delay_ms: int = 500
    max_retry_attempts: int = 0  # 0 means unlimited retries
    claw_token: str = ""
    agent_user_id: str = ""
    sessions: list[str] = Field(default_factory=list)
    panels: list[str] = Field(default_factory=list)
    allow_from: list[str] = Field(default_factory=list)
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)
    reply_delay_mode: str = "non-mention"  # off | non-mention
    reply_delay_ms: int = 120000


class SlackDMConfig(Base):
    """Slack DM policy configuration."""

    enabled: bool = True
    policy: str = "open"  # "open" or "allowlist"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs


class SlackConfig(Base):
    """Slack channel configuration."""

    enabled: bool = False
    mode: str = "socket"  # "socket" supported
    webhook_path: str = "/slack/events"
    bot_token: str = ""  # xoxb-...
    app_token: str = ""  # xapp-...
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs (sender-level)
    group_policy: str = "mention"  # "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # Allowed channel IDs if allowlist
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class QQConfig(Base):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = ""  # Bot ID (AppID) from q.qq.com
    secret: str = ""  # Bot secret (AppSecret) from q.qq.com
    allow_from: list[str] = Field(default_factory=list)  # Allowed user openids (empty = public access)

class ChannelsConfig(Base):
    """Configuration for chat channels."""

    send_progress: bool = True    # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file(...))
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    mochat: MochatConfig = Field(default_factory=MochatConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)




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
    provider_model: str
    api_key: str
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
        if not provider_model:
            raise ValueError("models.catalog[].provider_model is required")
        Config.parse_provider_model(provider_model)
        return provider_model

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: str) -> str:
        api_key = str(value or "").strip()
        if not api_key:
            raise ValueError("models.catalog[].api_key is required")
        return api_key

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


class RoleModelRoutingConfig(Base):
    """Ordered model references for each runtime scope."""

    agent: list[str] = Field(default_factory=list)
    ceo: list[str] = Field(default_factory=list)
    execution: list[str] = Field(default_factory=list)
    inspection: list[str] = Field(default_factory=list)

    @field_validator("agent", "ceo", "execution", "inspection", mode="before")
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


class WebSearchConfig(Base):
    """Web search tool configuration."""

    api_key: str = ""  # Brave Search API key
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    timeout: int = 60
    path_append: str = ""


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
    rerank_provider_model: str = ""


class MemoryEmbeddingConfig(Base):
    """Embedding source configuration."""

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
    bootstrap_mode: Literal["new_only", "full", "none"] = "new_only"
    retention_days: int | None = None



class PictureWashingToolConfig(Base):
    """Picture washing tool configuration."""

    base_url: str = ""
    authorization: str = ""
    style: str = "写实"
    model: str = "Seedream 4.5"
    stream: bool = False
    timeout_s: int = 120
    auto_probe_authorization: bool = True
    authorization_probe_url: str = ""
    authorization_probe_timeout_s: int = 45
    authorization_cookie_names: list[str] = Field(default_factory=lambda: ["sessionid", "session_id"])


class AgentBrowserToolConfig(Base):
    """External agent-browser CLI tool configuration."""

    enabled: bool = True
    command: str = "agent-browser"
    npm_command: str = "npm"
    node_command: str = "node"
    required_min_version: str = "0.16.3"
    install_spec: str = "agent-browser@latest"
    auto_install: bool = True
    auto_upgrade_if_below_min_version: bool = True
    auto_install_browser: bool = True
    browser_install_args: list[str] = Field(default_factory=lambda: ["install"])
    default_headless: bool = False
    command_timeout_s: int = 120
    install_timeout_s: int = 900
    session_env_key: str = "AGENT_BROWSER_SESSION"
    max_stdout_chars: int = 120000
    max_stderr_chars: int = 120000
    extra_env: dict[str, str] = Field(default_factory=dict)
    allow_file_access: bool = False
    default_color_scheme: Literal["light", "dark", "no-preference"] | None = None
    default_download_path: str = ""


class FileVaultConfig(Base):
    """Uploaded file vault configuration."""

    enabled: bool = True
    root_dir: str = "memory/uploads"
    index_db_path: str = "memory/file_vault.db"
    max_storage_bytes: int = 4 * 1024 * 1024 * 1024
    threshold_pct: int = 70
    cleanup_target_pct: int = 55
    recent_protect_hours: int = 24


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    url: str = ""  # HTTP: streamable HTTP endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP: Custom HTTP Headers
    tool_timeout: int = 30  # Seconds before a tool call is cancelled


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

class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    memory: MemoryToolsConfig = Field(default_factory=MemoryToolsConfig)
    file_vault: FileVaultConfig = Field(default_factory=FileVaultConfig)
    picture_washing: PictureWashingToolConfig = Field(default_factory=PictureWashingToolConfig)
    agent_browser: AgentBrowserToolConfig = Field(default_factory=AgentBrowserToolConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)




class OrgGraphGovernanceConfig(Base):
    """Governance configuration for org-graph resource authorization."""

    enabled: bool = True
    governance_store_path: str = ".g3ku/org-graph/governance.sqlite3"


class OrgGraphConfig(Base):
    """Recursive org-graph orchestration configuration."""

    enabled: bool = True
    ceo_model: str | None = None
    execution_model: str | None = None
    inspection_model: str | None = None
    project_store_path: str = ".g3ku/org-graph/projects.sqlite3"
    checkpoint_store_path: str = ".g3ku/org-graph/checkpoints.sqlite3"
    task_monitor_store_path: str = ".g3ku/org-graph/task-monitor.sqlite3"
    artifact_dir: str = ".g3ku/org-graph/artifacts"
    default_max_depth: int = 1
    hard_max_depth: int = 4
    max_parallel_units_total: int = -1
    max_active_projects_per_session: int = 32
    project_notice_retention: int = 200
    event_replay_limit: int = 1000
    governance: OrgGraphGovernanceConfig = Field(default_factory=OrgGraphGovernanceConfig)

class Config(BaseSettings):
    """Root configuration for g3ku."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    resources: ResourceRuntimeConfig = Field(default_factory=ResourceRuntimeConfig)
    org_graph: OrgGraphConfig = Field(default_factory=OrgGraphConfig)

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

        orchestrator_model_key = str(self.agents.multi_agent.orchestrator_model_key or "").strip()
        if orchestrator_model_key:
            item = catalog_by_key.get(orchestrator_model_key)
            if item is None:
                raise ValueError(f"agents.multi_agent.orchestrator_model_key references unknown model key: {orchestrator_model_key}")
            if not item.enabled:
                raise ValueError(f"agents.multi_agent.orchestrator_model_key references disabled model key: {orchestrator_model_key}")

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
        return str(managed.provider_model or "").strip()

    def get_provider(self, model_key: str | None = None) -> ProviderConfig | None:
        """Get provider config selected by managed model key."""
        managed = self.get_managed_model(model_key)
        if managed is None:
            raise ValueError(f"Unknown model key: {model_key}")
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

    model_config = ConfigDict(env_prefix="G3KU_", env_nested_delimiter="__")












