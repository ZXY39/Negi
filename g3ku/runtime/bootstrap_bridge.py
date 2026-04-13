from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace

from loguru import logger

from g3ku.agent.tools.tool_execution_control import StopToolExecutionTool, WaitToolExecutionTool
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
        logger.info('Memory self-check: mode={}.', mode)

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

    def _active_task_session_keys(self) -> list[str]:
        active_tasks = getattr(self._loop, '_active_tasks', None)
        if not isinstance(active_tasks, dict):
            return []
        return sorted(str(key or '').strip() for key in active_tasks.keys() if str(key or '').strip())

    def _checkpointer_diagnostics(self) -> tuple[str, str]:
        checkpointer = getattr(self._loop, '_checkpointer', None)
        if checkpointer is None:
            return '', 'unknown'
        is_active = getattr(self._loop, '_sqlite_checkpointer_is_active', None)
        if callable(is_active):
            try:
                return str(id(checkpointer)), str(bool(is_active(checkpointer)))
            except Exception:
                return str(id(checkpointer)), 'error'
        return str(id(checkpointer)), 'unknown'

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
                self._reset_memory_runtime(reason=reason)
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

        self._reset_memory_runtime(reason=reason)
        fingerprints['memory_runtime'] = fingerprint
        self.init_memory_runtime(cfg)
        logger.info('memory runtime synced from resource settings (reason={})', reason)
        return True

    def _reset_memory_runtime(self, *, reason: str = 'runtime') -> None:
        active_task_sessions = self._active_task_session_keys()
        checkpointer_id, checkpointer_active = self._checkpointer_diagnostics()
        if active_task_sessions:
            logger.warning(
                'Resetting memory runtime while active sessions exist '
                '(reason={}, active_task_sessions={}, checkpointer_id={}, checkpointer_active={})',
                reason,
                ','.join(active_task_sessions),
                checkpointer_id,
                checkpointer_active,
            )
        else:
            logger.info(
                'Resetting memory runtime '
                '(reason={}, active_task_sessions={}, checkpointer_id={}, checkpointer_active={})',
                reason,
                '',
                checkpointer_id,
                checkpointer_active,
            )
        commit_service = getattr(self._loop, 'commit_service', None)
        if commit_service is not None:
            self._close_value(commit_service)
        self._loop.commit_service = None

        memory_cfg = getattr(self._loop, '_memory_runtime_settings', None)
        memory_manager = getattr(self._loop, 'memory_manager', None)
        if memory_manager is not None:
            self._close_value(memory_manager)
        self._loop.memory_manager = None
        self._purge_stale_dense_backends(memory_cfg)

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

        runner = getattr(self._loop, 'multi_agent_runner', None)
        invalidate = getattr(runner, 'invalidate_runtime_bindings', None)
        if callable(invalidate):
            invalidate()

    def _purge_stale_dense_backends(self, memory_cfg: Any | None) -> None:
        store_cfg = getattr(memory_cfg, 'store', None) if memory_cfg is not None else None
        if store_cfg is None:
            return
        try:
            from g3ku.agent.rag_memory import G3kuHybridStore

            qdrant_path = resolve_path_in_workspace(
                getattr(store_cfg, 'qdrant_path', 'memory/qdrant'),
                self._loop.workspace,
            )
            qdrant_collection = str(getattr(store_cfg, 'qdrant_collection', '') or '').strip()
            purged = G3kuHybridStore.purge_process_local_dense_backends(
                qdrant_path=qdrant_path,
                qdrant_collection=qdrant_collection,
            )
            if purged:
                logger.info(
                    'Purged {} stale process-local dense backend(s) for {} ({})',
                    purged,
                    qdrant_path,
                    qdrant_collection or 'default',
                )
        except Exception as exc:
            logger.debug('stale dense backend purge skipped: {}', exc)

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

        try:
            manager_cls = getattr(self._loop, '_memory_manager_cls', None)
            if manager_cls is None:
                raise RuntimeError('memory manager class is not configured')
            self._loop.memory_manager = manager_cls(self._loop.workspace, cfg)
            self._loop._store = getattr(self._loop.memory_manager, 'store', None)
            self._loop._store_enabled = self._loop._store is not None
            if getattr(self._loop, 'main_task_service', None) is not None:
                self._loop.main_task_service.memory_manager = self._loop.memory_manager
            if self._loop._store is not None:
                logger.info('Memory runtime enabled with RAG store ({})', type(self._loop._store).__name__)
            else:
                logger.warning('Memory runtime enabled in legacy fallback mode (RAG store unavailable).')
        except Exception as exc:
            logger.warning('Memory runtime init failed: {}', exc)
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
