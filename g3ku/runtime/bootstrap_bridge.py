from __future__ import annotations

from pathlib import Path

from loguru import logger

from g3ku.agent.file_vault import FileVault
from g3ku.agent.session_commit import SessionCommitService
from g3ku.resources import get_shared_resource_manager
from g3ku.runtime.frontdoor import CeoFrontDoorRunner
from g3ku.org_graph.config import resolve_org_graph_config
from g3ku.org_graph.service.project_service import ProjectService
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

    def init_resource_runtime(self) -> None:
        cfg = getattr(self._loop, "resource_config", None)
        if cfg is None or not bool(getattr(cfg, "enabled", True)):
            return
        try:
            self.init_org_graph_runtime()
            manager = get_shared_resource_manager(
                self._loop.workspace,
                app_config=getattr(self._loop, "app_config", None),
                service_getter=self._resource_services,
                on_change=self._on_resource_snapshot,
            )
            self._loop.resource_manager = manager
            manager.start()
            self._on_resource_snapshot(manager.reload_now(trigger="bootstrap"))
            service = getattr(self._loop, "org_graph_service", None)
            if service is not None and hasattr(service, "bind_resource_manager"):
                service.bind_resource_manager(manager)
                try:
                    service.resource_registry.refresh()
                except Exception as refresh_exc:
                    logger.debug("org-graph resource refresh after bind skipped: {}", refresh_exc)
            logger.info(
                "Resource runtime initialized (skills_dir={}, tools_dir={})",
                cfg.skills_dir,
                cfg.tools_dir,
            )
        except Exception as exc:
            self._loop.resource_manager = None
            logger.warning("Resource runtime init failed: {}", exc)

    def init_multi_agent_runtime(self) -> None:
        cfg = getattr(self._loop, "multi_agent_config", None)
        self._loop.multi_agent_role_registry = None
        self._loop.multi_agent_runner = None
        self._loop.blackboard_store = None
        self._loop.dynamic_subagent_session_store = None
        self._loop.background_task_store = None
        self._loop.dynamic_subagent_controller = None
        self._loop.background_pool = None
        if cfg is not None:
            default_cfg = type(cfg)()
            if cfg.model_dump(mode="python") != default_cfg.model_dump(mode="python"):
                logger.warning(
                    "agents.multiAgent settings are deprecated and ignored; top-level split mode is disabled. Use orggraph_create_project for complex work."
                )
        self._loop.multi_agent_runner = CeoFrontDoorRunner(loop=self._loop)

    def init_org_graph_runtime(self) -> None:
        current = getattr(self._loop, 'org_graph_service', None)
        if current is not None:
            resource_manager = getattr(self._loop, 'resource_manager', None)
            if resource_manager is not None and hasattr(current, 'bind_resource_manager'):
                current.bind_resource_manager(resource_manager)
            self._loop.org_graph_monitor_service = getattr(current, 'monitor_service', None)
            return
        config = getattr(self._loop, 'app_config', None)
        if config is None or not bool(getattr(config.org_graph, 'enabled', True)):
            self._loop.org_graph_service = None
            self._loop.org_graph_monitor_service = None
            return
        try:
            service = ProjectService(resolve_org_graph_config(config))
            self._loop.org_graph_service = service
            self._loop.org_graph_monitor_service = getattr(service, 'monitor_service', None)
        except Exception as exc:
            self._loop.org_graph_service = None
            self._loop.org_graph_monitor_service = None
            logger.warning('org-graph runtime init failed: {}', exc)

    def register_default_tools(self) -> None:
        self.init_resource_runtime()

    def _resource_services(self) -> dict[str, object]:
        return {
            "loop": self._loop,
            "app_config": getattr(self._loop, "app_config", None),
            "bus": getattr(self._loop, "bus", None),
            "cron_service": getattr(self._loop, "cron_service", None),
            "file_vault": getattr(self._loop, "file_vault", None),
            "memory_manager": getattr(self._loop, "memory_manager", None),
            "org_graph_service": getattr(self._loop, "org_graph_service", None),
            "temp_dir": getattr(self._loop, "temp_dir", None),
        }

    def _on_resource_snapshot(self, snapshot) -> None:
        self._loop.tools.replace_dynamic_tools(snapshot.tool_instances)


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

