from __future__ import annotations

from pathlib import Path

from loguru import logger

from g3ku.agent.file_vault import FileVault
from g3ku.agent.session_commit import SessionCommitService
from g3ku.agent.tools.agent_browser import AgentBrowserTool
from g3ku.agent.tools.cron import CronTool
from g3ku.agent.tools.file_vault import (
    FileVaultCleanupTool,
    FileVaultLookupTool,
    FileVaultReadTool,
    FileVaultSetPolicyTool,
    FileVaultStatsTool,
)
from g3ku.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from g3ku.agent.tools.memory_search import MemorySearchTool
from g3ku.agent.tools.message import MessageTool
from g3ku.agent.tools.picture_washing import PictureWashingTool
from g3ku.agent.tools.shell import ExecTool
from g3ku.agent.tools.web import WebFetchTool, WebSearchTool
from g3ku.capabilities.index_registry import CapabilityIndexRegistry
from g3ku.capabilities.installer import CapabilityInstaller
from g3ku.capabilities.loader import CapabilityLoader
from g3ku.capabilities.registry import CapabilityRegistry
from g3ku.capabilities.source_registry import CapabilitySourcePolicy, CapabilitySourceRegistry
from g3ku.capabilities.validator import CapabilityValidator
from g3ku.runtime.multi_agent.dynamic import (
    BackgroundPool,
    BackgroundTaskStore,
    CategoryResolver,
    DynamicPromptBuilder,
    DynamicSubagentController,
    DynamicSubagentSessionStore,
    ModelChainExecutor,
    OrchestratorRunner,
)
from g3ku.runtime.multi_agent.blackboard import BlackboardStore
from g3ku.utils.helpers import ensure_dir, resolve_path_in_workspace


class RuntimeBootstrapBridge:
    """Bootstrap and runtime initialization bridge for AgentLoop."""

    def __init__(self, loop):
        self._loop = loop

    def log_memory_self_check(self) -> None:
        cfg = self._loop.memory_config
        if cfg is None or not getattr(cfg, "enabled", False):
            logger.warning("Memory self-check: tools.memory is disabled.")
            return
        mode = str(getattr(cfg, "mode", "legacy") or "legacy").lower()
        if mode != "rag":
            logger.warning(
                "Memory self-check alert: tools.memory.mode='{}' (expected 'rag').",
                mode,
            )
        else:
            logger.info("Memory self-check: tools.memory.mode='rag'.")

    def init_capability_runtime(self) -> None:
        cfg = self._loop.capability_config
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return
        try:
            state_path = resolve_path_in_workspace(getattr(cfg, "state_path", ".g3ku/capabilities.lock.json"), self._loop.workspace)
            workspace_dir = resolve_path_in_workspace(getattr(cfg, "workspace_dir", "capabilities"), self._loop.workspace)
            self._loop.capability_registry = CapabilityRegistry(
                self._loop.workspace,
                workspace_dir=workspace_dir,
                state_path=state_path,
                admin_enabled=bool(getattr(cfg, "admin_enabled", False)),
            )
            self._loop.capability_source_registry = CapabilitySourceRegistry(
                CapabilitySourcePolicy(
                    allow_local=bool(getattr(cfg, "allow_local", True)),
                    allow_git=bool(getattr(cfg, "allow_git", True)),
                    allowed_git_hosts=[str(item) for item in (getattr(cfg, "allowed_git_hosts", []) or [])],
                )
            )
            index_paths = []
            for raw_path in (getattr(cfg, "index_paths", []) or []):
                candidate = Path(raw_path).expanduser()
                index_paths.append(candidate if candidate.is_absolute() else resolve_path_in_workspace(candidate, self._loop.workspace))
            self._loop.capability_index_registry = CapabilityIndexRegistry(self._loop.workspace, index_paths=index_paths)
            self._loop.capability_loader = CapabilityLoader(self._loop.capability_registry)
            self._loop.capability_installer = CapabilityInstaller(
                self._loop.capability_registry,
                source_registry=self._loop.capability_source_registry,
                index_registry=self._loop.capability_index_registry,
            )
            self._loop.capability_validator = CapabilityValidator(self._loop.capability_registry)
            logger.info(
                "Capability registry enabled (capabilities={}, tools={}, skills={}, agents={})",
                len(self._loop.capability_registry.list_capabilities()),
                len(self._loop.capability_registry.list_tools()),
                len(self._loop.capability_registry.list_skills()),
                len(self._loop.capability_registry.list_agents()),
            )
        except Exception as exc:
            self._loop.capability_registry = None
            self._loop.capability_loader = None
            self._loop.capability_installer = None
            self._loop.capability_validator = None
            logger.warning("Capability runtime init failed, falling back to legacy registration: {}", exc)

    def init_multi_agent_runtime(self) -> None:
        cfg = getattr(self._loop, "multi_agent_config", None)
        self._loop.multi_agent_role_registry = None
        self._loop.multi_agent_runner = None
        self._loop.blackboard_store = None
        self._loop.dynamic_subagent_session_store = None
        self._loop.background_task_store = None
        self._loop.dynamic_subagent_controller = None
        self._loop.background_pool = None
        if cfg is None:
            return
        try:
            blackboard_root = resolve_path_in_workspace(getattr(cfg, "blackboard_dir", ".g3ku/blackboard"), self._loop.workspace)
            ensure_dir(blackboard_root)
            self._loop.blackboard_store = BlackboardStore(blackboard_root)
            session_store_path = resolve_path_in_workspace(getattr(cfg, "session_store_path", ".g3ku/dynamic-subagents.sqlite3"), self._loop.workspace)
            background_store_path = resolve_path_in_workspace(getattr(cfg, "background_store_path", ".g3ku/background-tasks.sqlite3"), self._loop.workspace)
            self._loop.dynamic_subagent_session_store = DynamicSubagentSessionStore(session_store_path)
            self._loop.background_task_store = BackgroundTaskStore(background_store_path)
            category_resolver = CategoryResolver(loop=self._loop, config=cfg)
            prompt_builder = DynamicPromptBuilder(loop=self._loop)
            model_chain_executor = ModelChainExecutor(loop=self._loop)
            controller = DynamicSubagentController(
                loop=self._loop,
                session_store=self._loop.dynamic_subagent_session_store,
                category_resolver=category_resolver,
                prompt_builder=prompt_builder,
                model_chain_executor=model_chain_executor,
                freeze_ttl_seconds=int(getattr(cfg, "freeze_ttl_seconds", 86400) or 86400),
                repeated_action_window=int(getattr(cfg, "repeated_action_window", 3) or 3),
                repeated_action_threshold=int(getattr(cfg, "repeated_action_threshold", 3) or 3),
            )
            self._loop.dynamic_subagent_controller = controller
            self._loop.background_pool = BackgroundPool(
                controller=controller,
                store=self._loop.background_task_store,
                max_parallel_tasks=int(getattr(cfg, "max_parallel_background_tasks", 8) or 8),
            )
            controller.set_background_pool(self._loop.background_pool)
            self._loop.multi_agent_runner = OrchestratorRunner(
                loop=self._loop,
                controller=controller,
                config=cfg,
            )
            logger.info(
                "Dynamic multi-agent runtime initialized (session_store={}, background_store={}, blackboard={})",
                session_store_path,
                background_store_path,
                blackboard_root,
            )
        except Exception as exc:
            self._loop.multi_agent_runner = None
            self._loop.blackboard_store = None
            self._loop.dynamic_subagent_session_store = None
            self._loop.background_task_store = None
            self._loop.dynamic_subagent_controller = None
            self._loop.background_pool = None
            logger.warning("Dynamic multi-agent runtime init failed: {}", exc)

    def register_default_tools(self) -> None:
        if self._loop.capability_loader is not None:
            try:
                loaded = self._loop.capability_loader.build_tools(loop=self._loop)
                loaded = [tool for tool in loaded if tool.name not in {"spawn", "enter_deep_mode", "deep_mode_status", "deep_mode_pause", "deep_mode_resume", "deep_mode_cancel"}]
                for tool in loaded:
                    self._loop.tools.register(tool)
                logger.info("Registered {} tools via capability registry", len(loaded))
                return
            except Exception as exc:
                logger.warning("Capability-based tool registration failed, using legacy wiring: {}", exc)
        self.register_legacy_tools()

    def register_legacy_tools(self) -> None:
        allowed_dir = self._loop.workspace if self._loop.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self._loop.tools.register(cls(workspace=self._loop.workspace, allowed_dir=allowed_dir))
        self._loop.tools.register(
            ExecTool(
                working_dir=str(self._loop.workspace),
                timeout=self._loop.exec_config.timeout,
                restrict_to_workspace=self._loop.restrict_to_workspace,
                path_append=self._loop.exec_config.path_append,
            )
        )
        self._loop.tools.register(WebSearchTool(api_key=self._loop.brave_api_key, proxy=self._loop.web_proxy))
        self._loop.tools.register(WebFetchTool(proxy=self._loop.web_proxy))
        self._loop.tools.register(
            PictureWashingTool(
                defaults=self._loop.picture_washing_config,
                agent_browser_defaults=self._loop.agent_browser_config,
            )
        )
        try:
            self._loop.tools.register(AgentBrowserTool(defaults=self._loop.agent_browser_config))
        except Exception as exc:
            logger.debug("AgentBrowserTool unavailable, skipping registration: {}", exc)
        self._loop.tools.register(MessageTool(send_callback=self._loop.bus.publish_outbound))
        if self._loop.cron_service:
            self._loop.tools.register(CronTool(self._loop.cron_service))

        if self._loop.memory_manager and self._loop._store_enabled:
            try:
                self._loop.tools.register(
                    MemorySearchTool(
                        manager=self._loop.memory_manager,
                        default_limit=self._loop.memory_config.retrieval.context_top_k if self._loop.memory_config else 8,
                    )
                )
            except Exception as exc:
                logger.warning("memory_search tool disabled: {}", exc)

        if self._loop.file_vault is not None:
            try:
                self._loop.tools.register(FileVaultLookupTool(vault=self._loop.file_vault))
                self._loop.tools.register(FileVaultReadTool(vault=self._loop.file_vault))
                self._loop.tools.register(FileVaultStatsTool(vault=self._loop.file_vault))
                self._loop.tools.register(FileVaultSetPolicyTool(vault=self._loop.file_vault))
                self._loop.tools.register(FileVaultCleanupTool(vault=self._loop.file_vault))
            except Exception as exc:
                logger.warning("file vault tools disabled: {}", exc)


    def init_file_vault(self) -> None:
        cfg = self._loop.file_vault_config
        if cfg is None or not getattr(cfg, "enabled", False):
            return
        try:
            self._loop.file_vault = FileVault(
                workspace=self._loop.workspace,
                enabled=bool(cfg.enabled),
                root_dir=cfg.root_dir,
                index_db_path=cfg.index_db_path,
                max_storage_bytes=cfg.max_storage_bytes,
                threshold_pct=cfg.threshold_pct,
                cleanup_target_pct=cfg.cleanup_target_pct,
                recent_protect_hours=cfg.recent_protect_hours,
            )
            logger.info("File vault enabled at {}", self._loop.file_vault.root_dir)
        except Exception as exc:
            self._loop.file_vault = None
            logger.warning("File vault init failed: {}", exc)

    def init_memory_runtime(self) -> None:
        cfg = self._loop.memory_config
        if cfg is None or not getattr(cfg, "enabled", False):
            return

        if self._loop._use_rag_memory():
            try:
                manager_cls = getattr(self._loop, "_memory_manager_cls", None)
                if manager_cls is None:
                    raise RuntimeError("memory manager class is not configured")
                self._loop.memory_manager = manager_cls(self._loop.workspace, cfg)
                self._loop._store = self._loop.memory_manager.store
                self._loop._store_enabled = True
                logger.info("RAG memory store enabled ({})", type(self._loop._store).__name__)
            except Exception as exc:
                logger.warning("RAG memory store init failed, fallback to legacy memory: {}", exc)
                self._loop.memory_manager = None
                self._loop._store = None
                self._loop._store_enabled = False

        try:
            cp_cfg = cfg.checkpointer
            backend = str(cp_cfg.backend or "sqlite").lower()
            self._loop._checkpointer_backend = backend
            if backend == "memory":
                from langgraph.checkpoint.memory import InMemorySaver

                self._loop._checkpointer = InMemorySaver()
                self._loop._checkpointer_enabled = True
                self._loop._checkpointer_path = None
            elif backend == "sqlite":
                try:
                    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                    cp_path = resolve_path_in_workspace(cp_cfg.path, self._loop.workspace)
                    ensure_dir(cp_path.parent)
                    # Async runtime must use AsyncSqliteSaver; defer actual connection until an event loop is active.
                    self._loop._checkpointer_path = cp_path
                    self._loop._checkpointer_cm = None
                    self._loop._checkpointer = None
                    self._loop._checkpointer_enabled = True
                    _ = AsyncSqliteSaver
                except Exception:
                    logger.warning(
                        "SQLite async checkpointer unavailable; fallback to session-file history "
                        "(install langgraph-checkpoint-sqlite and aiosqlite to enable persistent thread checkpoints)"
                    )
                    self._loop._checkpointer = None
                    self._loop._checkpointer_cm = None
                    self._loop._checkpointer_path = None
                    self._loop._checkpointer_backend = "disabled"
                    self._loop._checkpointer_enabled = False
        except Exception as exc:
            logger.warning("Checkpointer init failed, disable short-term persistence: {}", exc)
            self._loop._checkpointer = None
            self._loop._checkpointer_cm = None
            self._loop._checkpointer_path = None
            self._loop._checkpointer_backend = "disabled"
            self._loop._checkpointer_enabled = False

    def init_commit_service(self) -> None:
        cfg = self._loop.memory_config
        if cfg is None or self._loop.memory_manager is None:
            return
        features = getattr(cfg, "features", None)
        commit_enabled = True if features is None else bool(getattr(features, "commit_pipeline", False))
        if not commit_enabled:
            return
        commit_cfg = getattr(cfg, "commit", None)
        turn_trigger = int(getattr(commit_cfg, "turn_trigger", 20) if commit_cfg else 20)
        idle_trigger = int(getattr(commit_cfg, "idle_minutes_trigger", 360) if commit_cfg else 360)
        self._loop.commit_service = SessionCommitService(
            memory_manager=self._loop.memory_manager,
            turn_trigger=turn_trigger,
            idle_minutes_trigger=idle_trigger,
        )

