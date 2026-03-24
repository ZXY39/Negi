from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace

from loguru import logger

from g3ku.agent.tools.tool_execution_control import StopToolExecutionTool, WaitToolExecutionTool
from g3ku.agent.session_commit import SessionCommitService
from g3ku.resources import get_shared_resource_manager
from g3ku.resources.tool_settings import (
    MemoryRuntimeSettings,
    raw_tool_settings_from_descriptor,
    validate_tool_settings,
)
from g3ku.runtime.frontdoor import CeoFrontDoorRunner
from g3ku.utils.helpers import ensure_dir, resolve_path_in_workspace
from main.runtime.chat_backend import ConfigChatBackend
from main.service.runtime_service import MainRuntimeService


class RuntimeBootstrapBridge:
    """Bootstrap and runtime initialization bridge for AgentLoop."""

    def __init__(self, loop):
        self._loop = loop
        self._internal_reload_guard = False

    def log_memory_self_check(self) -> None:
        cfg = getattr(self._loop, '_memory_runtime_settings', None)
        if cfg is None or not getattr(cfg, 'enabled', False):
            logger.warning('Memory self-check: tools/memory_runtime is disabled.')
            return
        mode = str(getattr(cfg, 'mode', 'legacy') or 'legacy').lower()
        if mode != 'rag':
            logger.warning(
                "Memory self-check alert: tools/memory_runtime mode='{}' (expected 'rag').",
                mode,
            )
        else:
            logger.info('Memory self-check: mode=rag.')

    def init_resource_runtime(self) -> None:
        cfg = getattr(self._loop, 'resource_config', None)
        if cfg is None or not bool(getattr(cfg, 'enabled', True)):
            return
        try:
            self.init_main_runtime()
            manager = get_shared_resource_manager(
                self._loop.workspace,
                app_config=getattr(self._loop, 'app_config', None),
                service_getter=self._resource_services,
                on_change=self._on_resource_snapshot,
            )
            self._loop.resource_manager = manager
            manager.start()
            manager.reload_now(trigger='bootstrap')
            service = getattr(self._loop, 'main_task_service', None)
            if service is not None and hasattr(service, 'bind_resource_manager'):
                service.bind_resource_manager(manager)
                try:
                    service.resource_registry.refresh()
                    service.policy_engine.sync_default_role_policies()
                except Exception as refresh_exc:
                    logger.debug('main-runtime resource refresh after bind skipped: {}', refresh_exc)
            logger.info(
                'Resource runtime initialized (skills_dir={}, tools_dir={})',
                cfg.skills_dir,
                cfg.tools_dir,
            )
        except Exception as exc:
            self._loop.resource_manager = None
            logger.warning('Resource runtime init failed: {}', exc)

    def init_main_runtime(self) -> None:
        current = getattr(self._loop, 'main_task_service', None)
        if current is not None:
            return
        config = getattr(self._loop, 'app_config', None)
        if config is None:
            self._loop.main_task_service = None
            return
        try:
            main_runtime_cfg = getattr(config, 'main_runtime', None) or SimpleNamespace()
            get_role_max_iterations = getattr(config, 'get_role_max_iterations', None)
            service = MainRuntimeService(
                chat_backend=ConfigChatBackend(config),
                app_config=config,
                store_path=getattr(main_runtime_cfg, 'store_path', None),
                files_base_dir=getattr(main_runtime_cfg, 'files_base_dir', None),
                artifact_dir=getattr(main_runtime_cfg, 'artifact_dir', None),
                governance_store_path=getattr(main_runtime_cfg, 'governance_store_path', None),
                resource_manager=getattr(self._loop, 'resource_manager', None),
                execution_model_refs=config.get_role_model_keys('execution'),
                acceptance_model_refs=config.get_role_model_keys('inspection'),
                default_max_depth=getattr(main_runtime_cfg, 'default_max_depth', 1),
                hard_max_depth=getattr(main_runtime_cfg, 'hard_max_depth', 4),
                execution_max_iterations=(get_role_max_iterations('execution') if callable(get_role_max_iterations) else None),
                acceptance_max_iterations=(get_role_max_iterations('inspection') if callable(get_role_max_iterations) else None),
                execution_mode=str(os.getenv('G3KU_TASK_RUNTIME_ROLE', 'embedded') or 'embedded').strip().lower(),
            )
            service.bind_runtime_loop(self._loop)
            self._loop.main_task_service = service
        except Exception as exc:
            self._loop.main_task_service = None
            logger.warning('main runtime init failed: {}', exc)

    def init_multi_agent_runtime(self) -> None:
        cfg = getattr(self._loop, 'multi_agent_config', None)
        self._loop.multi_agent_role_registry = None
        self._loop.multi_agent_runner = None
        self._loop.blackboard_store = None
        self._loop.dynamic_subagent_session_store = None
        self._loop.background_task_store = None
        self._loop.dynamic_subagent_controller = None
        self._loop.background_pool = None
        if cfg is not None:
            default_cfg = type(cfg)()
            if cfg.model_dump(mode='python') != default_cfg.model_dump(mode='python'):
                logger.warning(
                    'agents.multiAgent settings are deprecated and ignored; top-level split mode is disabled. Use orggraph_create_project for complex work.'
                )
        self._loop.multi_agent_runner = CeoFrontDoorRunner(loop=self._loop)

    def register_default_tools(self) -> None:
        self.init_resource_runtime()
        manager_getter = lambda: getattr(self._loop, "tool_execution_manager", None)
        task_service_getter = lambda: getattr(self._loop, "main_task_service", None)
        self._loop.tools.register(WaitToolExecutionTool(manager_getter))
        self._loop.tools.register(StopToolExecutionTool(manager_getter, task_service_getter))

    def _resource_services(self) -> dict[str, object]:
        return {
            'loop': self._loop,
            'app_config': getattr(self._loop, 'app_config', None),
            'bus': getattr(self._loop, 'bus', None),
            'cron_service': getattr(self._loop, 'cron_service', None),
            'memory_manager': getattr(self._loop, 'memory_manager', None),
            'main_task_service': getattr(self._loop, 'main_task_service', None),
            'temp_dir': getattr(self._loop, 'temp_dir', None),
        }

    def _on_resource_snapshot(self, snapshot) -> None:
        changed = self.sync_internal_tool_runtimes(reason='resource_snapshot')
        manager = getattr(self._loop, 'resource_manager', None)
        if changed and manager is not None and not self._internal_reload_guard:
            self._internal_reload_guard = True
            try:
                manager.reload_now(trigger='internal-runtime-sync')
            finally:
                self._internal_reload_guard = False
            return

        self._loop.tools.replace_dynamic_tools(snapshot.tool_instances)
        service = getattr(self._loop, 'main_task_service', None)
        if service is None:
            return
        resource_registry = getattr(service, 'resource_registry', None)
        if resource_registry is None or not hasattr(resource_registry, 'refresh_from_current_resources'):
            return
        try:
            resource_registry.refresh_from_current_resources()
            policy_engine = getattr(service, 'policy_engine', None)
            if policy_engine is not None and hasattr(policy_engine, 'sync_default_role_policies'):
                policy_engine.sync_default_role_policies()
        except Exception as exc:
            logger.debug('main-runtime resource sync on snapshot skipped: {}', exc)

    def sync_internal_tool_runtimes(self, *, force: bool = False, reason: str = 'runtime') -> bool:
        return self._sync_memory_runtime(force=force, reason=reason)

    def _sync_memory_runtime(self, *, force: bool = False, reason: str = 'runtime') -> bool:
        manager = getattr(self._loop, 'resource_manager', None)
        descriptor = manager.get_tool_descriptor('memory_runtime') if manager is not None else None
        fingerprints = getattr(self._loop, '_internal_tool_settings_fingerprints', None)
        if not isinstance(fingerprints, dict):
            fingerprints = {}
            self._loop._internal_tool_settings_fingerprints = fingerprints

        if descriptor is None:
            had_runtime = bool(getattr(self._loop, '_memory_runtime_settings', None) or self._loop.memory_manager is not None)
            if had_runtime:
                self._reset_memory_runtime()
                fingerprints.pop('memory_runtime', None)
            return had_runtime

        cfg = validate_tool_settings(
            MemoryRuntimeSettings,
            raw_tool_settings_from_descriptor(descriptor),
            tool_name='memory_runtime',
        )
        fingerprint = str(descriptor.fingerprint or '')
        if not force and fingerprints.get('memory_runtime') == fingerprint and getattr(self._loop, '_memory_runtime_settings', None) is not None:
            return False

        self._reset_memory_runtime()
        fingerprints['memory_runtime'] = fingerprint
        self.init_memory_runtime(cfg)
        self.init_commit_service(cfg)
        logger.info('memory runtime synced from resource settings (reason={})', reason)
        return True

    def _reset_memory_runtime(self) -> None:
        commit_service = getattr(self._loop, 'commit_service', None)
        if commit_service is not None:
            self._close_value(commit_service)
        self._loop.commit_service = None

        memory_manager = getattr(self._loop, 'memory_manager', None)
        if memory_manager is not None:
            self._close_value(memory_manager)
        self._loop.memory_manager = None

        checkpointer = getattr(self._loop, '_checkpointer', None)
        if checkpointer is not None:
            self._close_value(checkpointer)
        checkpointer_cm = getattr(self._loop, '_checkpointer_cm', None)
        if checkpointer_cm is not None and hasattr(checkpointer_cm, '__aexit__'):
            self._close_async(checkpointer_cm.__aexit__(None, None, None))

        self._loop._memory_runtime_settings = None
        self._loop._store = None
        self._loop._store_enabled = False
        self._loop._checkpointer_enabled = False
        self._loop._checkpointer_backend = 'disabled'
        self._loop._checkpointer_path = None
        self._loop._checkpointer = None
        self._loop._checkpointer_cm = None

    def _close_value(self, value) -> None:
        close = getattr(value, 'close', None)
        if close is None:
            return
        try:
            result = close()
        except Exception as exc:
            logger.debug('resource close skipped: {}', exc)
            return
        self._close_async(result)

    @staticmethod
    def _close_async(result) -> None:
        if not inspect.isawaitable(result):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(result)
            return
        loop.create_task(result)

    def init_memory_runtime(self, cfg: MemoryRuntimeSettings | None = None) -> None:
        cfg = cfg or getattr(self._loop, '_memory_runtime_settings', None)
        self._loop._memory_runtime_settings = cfg
        if cfg is None or not getattr(cfg, 'enabled', False):
            return

        if self._loop._use_rag_memory():
            try:
                manager_cls = getattr(self._loop, '_memory_manager_cls', None)
                if manager_cls is None:
                    raise RuntimeError('memory manager class is not configured')
                self._loop.memory_manager = manager_cls(self._loop.workspace, cfg)
                self._loop._store = self._loop.memory_manager.store
                self._loop._store_enabled = True
                if getattr(self._loop, 'main_task_service', None) is not None:
                    self._loop.main_task_service.memory_manager = self._loop.memory_manager
                logger.info('RAG memory store enabled ({})', type(self._loop._store).__name__)
            except Exception as exc:
                logger.warning('RAG memory store init failed, fallback to legacy memory: {}', exc)
                self._loop.memory_manager = None
                self._loop._store = None
                self._loop._store_enabled = False

        try:
            cp_cfg = cfg.checkpointer
            backend = str(cp_cfg.backend or 'sqlite').lower()
            self._loop._checkpointer_backend = backend
            if backend == 'memory':
                from langgraph.checkpoint.memory import InMemorySaver

                self._loop._checkpointer = InMemorySaver()
                self._loop._checkpointer_enabled = True
                self._loop._checkpointer_path = None
            elif backend == 'sqlite':
                try:
                    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                    cp_path = resolve_path_in_workspace(cp_cfg.path, self._loop.workspace)
                    ensure_dir(cp_path.parent)
                    self._loop._checkpointer_path = cp_path
                    self._loop._checkpointer_cm = None
                    self._loop._checkpointer = None
                    self._loop._checkpointer_enabled = True
                    _ = AsyncSqliteSaver
                except Exception:
                    logger.warning(
                        'SQLite async checkpointer unavailable; fallback to session-file history '
                        '(install langgraph-checkpoint-sqlite and aiosqlite to enable persistent thread checkpoints)'
                    )
                    self._loop._checkpointer = None
                    self._loop._checkpointer_cm = None
                    self._loop._checkpointer_path = None
                    self._loop._checkpointer_backend = 'disabled'
                    self._loop._checkpointer_enabled = False
        except Exception as exc:
            logger.warning('Checkpointer init failed, disable short-term persistence: {}', exc)
            self._loop._checkpointer = None
            self._loop._checkpointer_cm = None
            self._loop._checkpointer_path = None
            self._loop._checkpointer_backend = 'disabled'
            self._loop._checkpointer_enabled = False

    def init_commit_service(self, cfg: MemoryRuntimeSettings | None = None) -> None:
        cfg = cfg or getattr(self._loop, '_memory_runtime_settings', None)
        if cfg is None or self._loop.memory_manager is None:
            return
        features = getattr(cfg, 'features', None)
        commit_enabled = True if features is None else bool(getattr(features, 'commit_pipeline', False))
        if not commit_enabled:
            return
        commit_cfg = getattr(cfg, 'commit', None)
        turn_trigger = int(getattr(commit_cfg, 'turn_trigger', 20) if commit_cfg else 20)
        idle_trigger = int(getattr(commit_cfg, 'idle_minutes_trigger', 360) if commit_cfg else 360)
        self._loop.commit_service = SessionCommitService(
            memory_manager=self._loop.memory_manager,
            turn_trigger=turn_trigger,
            idle_minutes_trigger=idle_trigger,
        )
