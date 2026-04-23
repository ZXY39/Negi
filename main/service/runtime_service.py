from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import httpx
from loguru import logger

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.tool_execution_control import StopToolExecutionTool, WaitToolExecutionTool
from g3ku.config.live_runtime import get_runtime_config
from g3ku.config.loader import get_config_path
from g3ku.content import ContentNavigationService
from g3ku.llm_config.runtime_resolver import resolve_chat_target
from g3ku.resources.models import ResourceKind
from g3ku.resources.tool_settings import (
    MemoryRuntimeSettings,
    raw_tool_settings_from_descriptor,
    validate_tool_settings,
)
from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection
from g3ku.runtime.context.node_context_selection import (
    NodeContextSelectionResult,
    build_node_context_selection,
)
from g3ku.runtime.context.summarizer import layered_body_payload, score_query
from g3ku.runtime.core_tools import configured_core_tools, resolve_core_tool_targets
from g3ku.runtime.memory_scope import DEFAULT_WEB_MEMORY_SCOPE, normalize_memory_scope
from g3ku.runtime.tool_visibility import (
    NODE_FIXED_BUILTIN_TOOL_NAMES,
    fixed_builtin_tool_name_set_for_actor_role,
)
from g3ku.runtime.tool_watchdog import ToolExecutionManager
from g3ku.security import get_bootstrap_security_service
from g3ku.utils.api_keys import parse_api_keys, resolve_api_key_concurrency_layout
from g3ku.web.worker_control import managed_worker_snapshot
from main.governance import (
    GovernanceStore,
    MainRuntimePolicyEngine,
    MainRuntimeResourceRegistry,
    PermissionSubject,
    list_effective_skill_ids,
    list_effective_tool_names,
)
from main.governance.action_mapper import get_governance_tool_id
from main.governance.exec_tool_policy import (
    EXEC_TOOL_EXECUTOR_NAME,
    EXEC_TOOL_FAMILY_ID,
    exec_tool_supports_execution_mode,
    merge_exec_execution_mode_metadata,
    resolve_exec_runtime_policy_payload,
)
from main.governance.roles import normalize_public_allowed_roles
from main.governance.tool_context import (
    apply_runtime_tool_context_projection,
    build_tool_context_fingerprint,
    build_tool_toolskill_payload,
    resolve_primary_executor_name,
)
from main.ids import new_command_id, new_node_id, new_task_id, new_worker_id
from main.models import (
    FAILURE_CLASS_BUSINESS_UNPASSED,
    FAILURE_CLASS_ENGINE,
    FAILURE_CLASS_NON_RETRYABLE_BLOCKED,
    NodeRecord,
    TaskMessageDistributionEpoch,
    TaskNodeNotification,
    TaskArtifactRecord,
    TaskRecord,
    TokenUsageSummary,
    normalize_execution_policy_metadata,
    normalize_failure_class,
    normalize_final_acceptance_metadata,
)
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.monitoring.query_service_v2 import TaskQueryServiceV2
from main.prompts import load_prompt
from main.protocol import build_envelope, now_iso
from main.runtime.adaptive_tool_budget import AdaptiveToolBudgetController
from main.runtime.chat_backend import ChatBackend
from main.runtime.debug_recorder import RuntimeDebugRecorder
from main.runtime.execution_trace_compaction import compact_tool_step_for_summary
from main.runtime.global_scheduler import GlobalScheduler
from main.runtime.internal_tools import build_detail_level_schema
from main.runtime.node_prompt_contract import (
    NodeRuntimeToolContract,
    extract_node_dynamic_contract_payload,
    inject_node_dynamic_contract_message,
)
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.node_runner import NodeRunner
from main.runtime.node_turn_controller import NodeTurnController
from main.runtime.react_loop import ReActToolLoop
from main.runtime.stage_budget import STAGE_TOOL_NAME, callable_tool_names_for_stage_iteration
from main.runtime.task_actor_service import TaskActorService
from main.runtime.tool_pressure_monitor import WorkerPressureMonitor
from main.service.create_async_task_contract import (
    CREATE_ASYNC_TASK_DESCRIPTION,
    build_create_async_task_parameters,
)
from main.service.task_append_notice_contract import (
    TASK_APPEND_NOTICE_DESCRIPTION,
    build_task_append_notice_parameters,
)
from main.service.event_registry import TaskEventRegistry
from main.service.task_event_callback import (
    TASK_EVENT_BATCH_CALLBACK_PATH,
    TASK_EVENT_CALLBACK_PATH,
    normalize_task_event_payload,
    resolve_task_event_batch_callback_url,
    resolve_task_event_callback_token,
    resolve_task_event_callback_url,
)
from main.service.task_stall_callback import (
    TASK_STALL_CALLBACK_PATH,
    TASK_STALL_REASON_CANCEL_REQUESTED,
    TASK_STALL_REASON_MISSING_TASK,
    TASK_STALL_REASON_NOT_IN_PROGRESS,
    TASK_STALL_REASON_SUSPECTED_STALL,
    TASK_STALL_REASON_USER_PAUSED,
    TASK_STALL_REASON_WORKER_UNAVAILABLE,
    normalize_task_stall_payload,
    resolve_task_stall_callback_token,
    resolve_task_stall_callback_url,
)
from main.service.task_stall_notifier import (
    TaskStallNotifier,
    stall_bucket_minutes,
    stalled_minutes_since,
)
from main.service.task_terminal_callback import (
    TASK_TERMINAL_CALLBACK_PATH,
    build_task_terminal_payload,
    enrich_task_terminal_payload,
    load_task_terminal_callback_config,
    resolve_task_terminal_callback_token,
    resolve_task_terminal_callback_url,
)
from main.service.worker_heartbeat_service_v2 import WorkerHeartbeatServiceV2
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore

_UNSET = object()
GOVERNANCE_MODE_META_KEY = 'ceo_frontdoor_regulatory_mode_enabled'
GOVERNANCE_MODE_UPDATED_AT_META_KEY = 'ceo_frontdoor_regulatory_mode_enabled:updated_at'
_WORKER_STATUS_STALE_AFTER_SECONDS = 15.0
_WORKER_STATUS_ACTIVE_TASK_STALE_AFTER_SECONDS = 60.0
_WORKER_STATUS_STARTING_GRACE_SECONDS = 10.0
_WORKER_STATUS_CALLBACK_RETRY_DELAYS = [0.0, 0.5, 2.0, 5.0]
_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS = 2.0
_WORKER_RUNTIME_REFRESH_TIMEOUT_SECONDS = 5.0
_WORKER_RUNTIME_REFRESH_POLL_SECONDS = 0.1
_WORKER_LEASE_ROLE = 'task_worker'
_WORKER_LEASE_TTL_SECONDS = 20.0
_TASK_SUMMARY_DEBOUNCE_SECONDS = 0.5
_TASK_SUMMARY_MAX_WAIT_SECONDS = 1.0
_TASK_SUMMARY_BATCH_WINDOW_SECONDS = 0.25
_TASK_SUMMARY_BATCH_MAX_ITEMS = 128
_TASK_SUMMARY_BATCH_MAX_BYTES = 64 * 1024
_TASK_SUMMARY_RECONCILE_IDLE_SECONDS = 15.0
_TASK_DELETE_CONFIRM_TTL_SECONDS = 600.0
_WORKER_STATE_STARTING = 'starting'
_WORKER_STATE_ONLINE = 'online'
_WORKER_STATE_STALE = 'stale'
_WORKER_STATE_STOPPED = 'stopped'
_WORKER_STATE_OFFLINE = 'offline'
_WORKER_STATUS_TERMINAL_STATES = frozenset({'stopped', 'offline', 'dead'})
_TASK_RECOVERY_NOTICE_KEY = 'recovery_notice'
_TASK_RECOVERY_NOTICE_TEXT = '本任务遇到异常停止，已回退到稳定步骤继续。'
_TASK_RUNTIME_V3_MARKER = '.task-runtime-v3'
_NODE_FIXED_BUILTIN_TOOL_NAMES = NODE_FIXED_BUILTIN_TOOL_NAMES


def _prepare_task_runtime_v3_root(
    *,
    store_path: Path,
    files_base_dir: Path,
    artifact_dir: Path,
    event_history_dir: Path,
) -> None:
    runtime_root = store_path.parent
    marker_path = runtime_root / _TASK_RUNTIME_V3_MARKER
    if marker_path.exists():
        return
    runtime_root.mkdir(parents=True, exist_ok=True)
    for candidate in (
        store_path,
        store_path.with_name(f'{store_path.name}-wal'),
        store_path.with_name(f'{store_path.name}-shm'),
    ):
        try:
            if candidate.exists():
                candidate.unlink()
        except FileNotFoundError:
            continue
    for directory in (files_base_dir, artifact_dir, event_history_dir):
        try:
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
        except Exception:
            logger.debug('task runtime v3 cleanup skipped for {}', directory)
    marker_path.write_text('task-runtime-v3\n', encoding='utf-8')


class ResourceDeleteBlockedError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        resource_kind: str,
        resource_id: str,
        usage: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.payload = {
            'code': str(code or '').strip(),
            'message': str(message or '').strip(),
            'resource_kind': str(resource_kind or '').strip(),
            'resource_id': str(resource_id or '').strip(),
            'usage': dict(usage or {}),
        }


class ResourceMutationBlockedError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        resource_kind: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.payload = {
            'code': str(code or '').strip(),
            'message': str(message or '').strip(),
            'resource_kind': str(resource_kind or '').strip(),
            'resource_id': str(resource_id or '').strip(),
            'details': dict(details or {}),
        }


class MainRuntimeService:
    @staticmethod
    def _external_tool_family_id(tool_id: str) -> str:
        normalized = str(tool_id or '').strip()
        if normalized == 'content_navigation':
            return 'content'
        return normalized

    @staticmethod
    def _resolve_tool_family_alias(tool_id: str) -> str:
        normalized = str(tool_id or '').strip()
        if normalized == 'content':
            return 'content_navigation'
        return normalized

    def __init__(
        self,
        *,
        chat_backend: ChatBackend,
        app_config: Any | None = None,
        store_path: Path | str | None = None,
        files_base_dir: Path | str | None = None,
        artifact_dir: Path | str | None = None,
        governance_store_path: Path | str | None = None,
        resource_manager=None,
        tool_provider: Callable[[NodeRecord], dict[str, Tool]] | None = None,
        execution_model_refs: list[str] | None = None,
        acceptance_model_refs: list[str] | None = None,
        default_max_depth: int = 1,
        hard_max_depth: int = 4,
        max_iterations: int | None | object = _UNSET,
        execution_max_iterations: int | None | object = _UNSET,
        acceptance_max_iterations: int | None | object = _UNSET,
        execution_mode: str = 'embedded',
        worker_id: str | None = None,
    ) -> None:
        self._chat_backend = chat_backend
        self._app_config = app_config
        resolved_max_iterations = self._normalize_optional_limit(max_iterations, default=16)
        resolved_execution_max_iterations = self._normalize_optional_limit(
            execution_max_iterations,
            default=resolved_max_iterations,
        )
        resolved_acceptance_max_iterations = self._normalize_optional_limit(
            acceptance_max_iterations,
            default=resolved_execution_max_iterations,
        )
        normalized_mode = str(execution_mode or 'embedded').strip().lower() or 'embedded'
        if normalized_mode not in {'embedded', 'web', 'worker'}:
            normalized_mode = 'embedded'
        self.execution_mode = normalized_mode
        self.worker_id = str(worker_id or (new_worker_id() if normalized_mode == 'worker' else '')).strip()
        resolved_store_path = Path(store_path or (Path.cwd() / '.g3ku' / 'main-runtime' / 'runtime.sqlite3'))
        resolved_files_base_dir = Path(files_base_dir or (Path.cwd() / '.g3ku' / 'main-runtime' / 'tasks'))
        resolved_artifact_dir = Path(artifact_dir or (Path.cwd() / '.g3ku' / 'main-runtime' / 'artifacts'))
        event_history_settings = self._event_history_settings(app_config)
        configured_event_history_dir = str(event_history_settings.get('dir') or '').strip()
        resolved_event_history_dir = Path(
            configured_event_history_dir or (resolved_store_path.parent / 'event-history')
        )
        _prepare_task_runtime_v3_root(
            store_path=resolved_store_path,
            files_base_dir=resolved_files_base_dir,
            artifact_dir=resolved_artifact_dir,
            event_history_dir=resolved_event_history_dir,
        )
        self.runtime_debug_recorder = RuntimeDebugRecorder()
        self.store = SQLiteTaskStore(
            resolved_store_path,
            debug_recorder=self.runtime_debug_recorder,
            event_history_dir=resolved_event_history_dir,
            event_history_enabled=bool(event_history_settings.get('enabled', True)),
            event_history_archive_encoding=str(event_history_settings.get('archive_encoding') or 'gzip'),
        )
        self.file_store = TaskFileStore(resolved_files_base_dir)
        self.artifact_store = TaskArtifactStore(artifact_dir=resolved_artifact_dir, store=self.store)
        self.content_store = ContentNavigationService(
            workspace=Path.cwd(),
            artifact_store=self.artifact_store,
            artifact_lookup=self.store,
        )
        self.registry = TaskEventRegistry()
        self.log_service = TaskLogService(
            store=self.store,
            file_store=self.file_store,
            registry=self.registry,
            content_store=self.content_store,
            debug_recorder=self.runtime_debug_recorder,
            event_history_enabled=bool(event_history_settings.get('enabled', True)),
            live_patch_persist_window_ms=int(event_history_settings.get('live_patch_persist_window_ms') or 0),
        )
        self.log_service.add_live_snapshot_publisher(self._publish_live_snapshot)
        self.log_service.add_summary_metric_reporter(self._increment_summary_stat)
        self.task_stall_notifier = TaskStallNotifier(service=self)
        self.log_service.add_task_visible_output_listener(self.task_stall_notifier.reset_visible_output)
        self.log_service.add_task_terminal_listener(self._cleanup_terminal_task_temp_dir_if_empty)
        self.log_service.add_task_terminal_listener(self.task_stall_notifier.terminal_task)
        self.query_service = TaskQueryServiceV2(store=self.store, file_store=self.file_store, log_service=self.log_service, debug_recorder=self.runtime_debug_recorder)
        self.governance_store = GovernanceStore(governance_store_path or (Path.cwd() / '.g3ku' / 'main-runtime' / 'governance.sqlite3'))
        self.resource_registry = MainRuntimeResourceRegistry(workspace_root=Path.cwd(), store=self.governance_store, resource_manager=resource_manager)
        self.policy_engine = MainRuntimePolicyEngine(store=self.governance_store, resource_registry=self.resource_registry)
        self._external_tool_provider = tool_provider or (lambda _node: {})
        self._resource_manager = resource_manager
        self._resource_tree_state_cache: dict[str, dict[str, str]] | None = None
        self._resource_tree_state_checked_at: float = 0.0
        self._resource_tree_state_poll_interval_ms = self._resource_reload_poll_interval_ms(app_config)
        self.memory_manager = None
        self._default_max_depth = max(0, int(default_max_depth or 0))
        self._hard_max_depth = max(self._default_max_depth, int(hard_max_depth or self._default_max_depth))
        self.tool_execution_manager = ToolExecutionManager()
        self._builtin_tool_cache: dict[str, Tool] | None = None
        self._node_context_selection_cache: dict[tuple[str, str], dict[str, Any]] = {}
        parallel_enabled, max_parallel_tool_calls, max_parallel_child_pipelines = self._node_parallelism_settings(app_config)
        adaptive_budget_settings = self._adaptive_tool_budget_settings(app_config)
        node_dispatch_limits = self._node_dispatch_concurrency_settings(app_config)
        execution_runtime_enabled = self.execution_mode == 'worker'
        execution_max_concurrency = (
            app_config.get_role_max_concurrency('execution')
            if app_config is not None and hasattr(app_config, 'get_role_max_concurrency')
            else None
        )
        acceptance_max_concurrency = (
            app_config.get_role_max_concurrency('inspection')
            if app_config is not None and hasattr(app_config, 'get_role_max_concurrency')
            else None
        )
        react_loop = ReActToolLoop(
            chat_backend=chat_backend,
            log_service=self.log_service,
            max_iterations=resolved_max_iterations,
            parallel_tool_calls_enabled=parallel_enabled,
            max_parallel_tool_calls=max_parallel_tool_calls,
        )
        react_loop._runtime_config_refresh_for_retry_invalidation = (
            lambda: self.ensure_runtime_config_current(force=False, reason='provider_retry_invalidation')
        )
        self._chat_backend = chat_backend
        react_loop._tool_execution_manager = None
        self.model_key_concurrency_controller = ModelKeyConcurrencyController(
            resolve_model_limits=self._resolve_model_limit_payload,
        ) if execution_runtime_enabled else None
        self.node_turn_controller = NodeTurnController(
            model_concurrency_controller=self.model_key_concurrency_controller,
            gate_supplier=self._node_turn_gate_allowed,
            freeze_supplier=self._node_turn_task_frozen,
        ) if execution_runtime_enabled and self.model_key_concurrency_controller is not None else None
        if self.model_key_concurrency_controller is not None and self.node_turn_controller is not None:
            self.model_key_concurrency_controller.configure(
                resolve_model_limits=self._resolve_model_limit_payload,
                on_availability_changed=self.node_turn_controller.poke,
            )
        react_loop._model_concurrency_controller = self.model_key_concurrency_controller
        react_loop._node_turn_controller = self.node_turn_controller
        adaptive_budget_enabled = bool(adaptive_budget_settings.get('enabled')) and self.execution_mode == 'worker'
        self.adaptive_tool_budget_controller = AdaptiveToolBudgetController(
            normal_limit=int(adaptive_budget_settings['normal_limit']),
            throttled_limit=int(adaptive_budget_settings['throttled_limit']),
            critical_limit=int(adaptive_budget_settings['critical_limit']),
            step_up=int(adaptive_budget_settings['step_up']),
        ) if adaptive_budget_enabled else None
        react_loop._adaptive_tool_budget_controller = self.adaptive_tool_budget_controller
        react_loop._model_visible_tool_schema_selector = self._select_model_visible_tool_schema_payload
        react_loop._tool_context_hydration_promoter = self._promote_tool_context_hydration
        self._pending_task_delete_confirmations: dict[str, dict[str, Any]] = {}
        self._react_loop = react_loop
        self.node_runner = NodeRunner(
            store=self.store,
            log_service=self.log_service,
            react_loop=react_loop,
            tool_provider=self._tool_provider,
            execution_model_refs=list(execution_model_refs or ['execution']),
            acceptance_model_refs=list(acceptance_model_refs or execution_model_refs or ['inspection']),
            execution_max_iterations=resolved_execution_max_iterations,
            acceptance_max_iterations=resolved_acceptance_max_iterations,
            max_parallel_child_pipelines=max_parallel_child_pipelines,
            execution_max_concurrency=execution_max_concurrency,
            acceptance_max_concurrency=acceptance_max_concurrency,
            context_enricher=self._enrich_node_messages,
            context_preparer=self._prepare_node_context_selection,
            context_finalizer=self._clear_node_context_selection,
            workspace_root_getter=lambda: self._workspace_root(),
        )
        self.node_runner._tool_snapshot_supplier = lambda task_id: self.get_task_detail_payload(task_id, mark_read=False)
        self.node_runner.governance_child_created_observer = self._on_governance_child_created
        self.node_runner.governance_spawn_refusal_supplier = self._governance_spawn_refusal_message
        self.node_runner.distribution_delivery_callback = self._deliver_distribution_message
        self.task_actor_service = TaskActorService(
            store=self.store,
            log_service=self.log_service,
            node_runner=self.node_runner,
            stall_notifier=self.task_stall_notifier,
            node_dispatch_execution_limit=None if execution_runtime_enabled else int(node_dispatch_limits['execution']),
            node_dispatch_inspection_limit=None if execution_runtime_enabled else int(node_dispatch_limits['inspection']),
        )
        self.global_scheduler = GlobalScheduler(
            runner=self.task_actor_service,
            max_concurrent_tasks=None if execution_runtime_enabled else 4,
            per_task_limit=1,
        )
        self.task_actor_service.distribution_resume_callback = (
            lambda task_id: asyncio.get_running_loop().call_soon(
                lambda normalized_task_id=str(task_id or '').strip(): asyncio.create_task(
                    self.global_scheduler.enqueue_task(normalized_task_id)
                )
            )
        )
        self.tool_pressure_monitor = WorkerPressureMonitor(
            controller=self.adaptive_tool_budget_controller,
            store=self.store,
            sample_seconds=float(adaptive_budget_settings['sample_seconds']),
            recover_window_seconds=float(adaptive_budget_settings['recover_window_seconds']),
            warn_consecutive_samples=int(adaptive_budget_settings['warn_consecutive_samples']),
            safe_consecutive_samples=int(adaptive_budget_settings['safe_consecutive_samples']),
            pressure_snapshot_stale_after_seconds=float(adaptive_budget_settings['pressure_snapshot_stale_after_seconds']),
            event_loop_warn_ms=float(adaptive_budget_settings['event_loop_warn_ms']),
            event_loop_safe_ms=float(adaptive_budget_settings['event_loop_safe_ms']),
            event_loop_critical_ms=float(adaptive_budget_settings['event_loop_critical_ms']),
            writer_queue_warn=int(adaptive_budget_settings['writer_queue_warn']),
            writer_queue_safe=int(adaptive_budget_settings['writer_queue_safe']),
            writer_queue_critical=int(adaptive_budget_settings['writer_queue_critical']),
            sqlite_write_wait_warn_ms=float(adaptive_budget_settings['sqlite_write_wait_warn_ms']),
            sqlite_write_wait_safe_ms=float(adaptive_budget_settings['sqlite_write_wait_safe_ms']),
            sqlite_write_wait_critical_ms=float(adaptive_budget_settings['sqlite_write_wait_critical_ms']),
            sqlite_query_warn_ms=float(adaptive_budget_settings['sqlite_query_warn_ms']),
            sqlite_query_safe_ms=float(adaptive_budget_settings['sqlite_query_safe_ms']),
            sqlite_query_critical_ms=float(adaptive_budget_settings['sqlite_query_critical_ms']),
            machine_cpu_warn_percent=float(adaptive_budget_settings['machine_cpu_warn_percent']),
            machine_cpu_safe_percent=float(adaptive_budget_settings['machine_cpu_safe_percent']),
            machine_cpu_critical_percent=float(adaptive_budget_settings['machine_cpu_critical_percent']),
            machine_memory_warn_percent=float(adaptive_budget_settings['machine_memory_warn_percent']),
            machine_memory_safe_percent=float(adaptive_budget_settings['machine_memory_safe_percent']),
            machine_memory_critical_percent=float(adaptive_budget_settings['machine_memory_critical_percent']),
            machine_disk_busy_warn_percent=float(adaptive_budget_settings['machine_disk_busy_warn_percent']),
            machine_disk_busy_safe_percent=float(adaptive_budget_settings['machine_disk_busy_safe_percent']),
            machine_disk_busy_critical_percent=float(adaptive_budget_settings['machine_disk_busy_critical_percent']),
            process_cpu_warn_ratio=float(adaptive_budget_settings['process_cpu_warn_ratio']),
            process_cpu_safe_ratio=float(adaptive_budget_settings['process_cpu_safe_ratio']),
        ) if self.adaptive_tool_budget_controller is not None else None
        self.node_runner._adaptive_tool_budget_controller = self.adaptive_tool_budget_controller
        self.worker_heartbeat_service = WorkerHeartbeatServiceV2(
            store=self.store,
            scheduler=self.global_scheduler,
            execution_mode=self.execution_mode,
            worker_id=self.worker_id or 'worker',
            publish_status=self._publish_worker_status_from_any_thread,
            pressure_snapshot_supplier=self._tool_pressure_snapshot,
            debug_snapshot_supplier=lambda: {'recent_long_blocks': self.runtime_debug_recorder.snapshot()},
            lease_heartbeat=self._renew_worker_lease_from_thread,
        )
        self._started = False
        self._runtime_loop = None
        self._worker_lease_takeover = False
        self._worker_lease_acquired = False
        self._command_poller_task: asyncio.Task[Any] | None = None
        self._worker_heartbeat_task: asyncio.Task[Any] | None = None
        self._task_terminal_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_stall_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_worker_status_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_summary_delivery_task: asyncio.Task[Any] | None = None
        self._task_event_dispatch_tasks: set[asyncio.Task[Any]] = set()
        self._governance_review_tasks: dict[str, asyncio.Task[Any]] = {}
        self._callback_client: httpx.AsyncClient | None = None
        self._pending_task_summaries: dict[str, dict[str, Any]] = {}
        self._task_summary_flush_tasks: dict[str, asyncio.Task[Any]] = {}
        self._last_summary_payloads: dict[str, dict[str, Any]] = {}
        self._task_summary_stats: dict[str, float] = {
            'task_summary_dirty_count': 0.0,
            'task_summary_flush_count': 0.0,
            'task_summary_skip_unchanged_count': 0.0,
            'task_summary_outbox_write_count': 0.0,
            'task_summary_batch_request_count': 0.0,
            'task_summary_batch_item_count': 0.0,
        }
        if self.execution_mode == 'worker':
            self.log_service.add_task_terminal_listener(self._enqueue_task_terminal_callback)

    @staticmethod
    def _bool_env(name: str, *, default: bool) -> bool:
        raw = str(os.getenv(name, '') or '').strip().lower()
        if not raw:
            return bool(default)
        return raw not in {'0', 'false', 'no', 'off'}

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        if self._runtime_loop is None:
            self._runtime_loop = asyncio.get_running_loop()
        if self.execution_mode == 'worker':
            self._acquire_worker_lease_or_raise()
            self.worker_heartbeat_service.start_background()
        self.resource_registry.refresh_from_current_resources()
        self.reconcile_core_tool_families()
        self.policy_engine.sync_default_role_policies()
        self._record_resource_tree_state()
        if self.memory_manager is not None and hasattr(self.memory_manager, 'sync_catalog'):
            try:
                await self.memory_manager.sync_catalog(self)
            except Exception:
                pass
        if self.execution_mode in {'embedded', 'worker'}:
            for task in self.store.list_tasks():
                self.log_service.sync_task_read_models(task.task_id, externalize_execution_trace=False)
                if task.status != 'in_progress':
                    continue
                if bool(task.is_paused) or bool(task.pause_requested):
                    continue
                self._recover_interrupted_task(task.task_id)
                await self.global_scheduler.enqueue_task(task.task_id)
            self.task_stall_notifier.bootstrap_running_tasks()
        if self.execution_mode == 'worker':
            if self.tool_pressure_monitor is not None:
                self.tool_pressure_monitor.start()
            self._start_worker_loops()
            self._schedule_pending_task_summary_callbacks()
            self._schedule_pending_task_worker_status_callbacks()
            self._schedule_pending_task_terminal_callbacks()
            self._schedule_pending_task_stall_callbacks()

    def _start_worker_loops(self) -> None:
        if self._command_poller_task is None or self._command_poller_task.done():
            self._command_poller_task = asyncio.create_task(self._worker_command_loop(), name=f'main-runtime-command-poller:{self.worker_id or "worker"}')
        if self._worker_heartbeat_task is None or self._worker_heartbeat_task.done():
            self._worker_heartbeat_task = asyncio.create_task(self._worker_heartbeat_loop(), name=f'main-runtime-worker-heartbeat:{self.worker_id or "worker"}')

    def _recover_interrupted_task(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if task is None:
            return
        root = self.store.get_node(task.root_node_id)
        if root is None:
            self.log_service.mark_task_failed(task.task_id, reason='missing root node during recovery')
            return

        root_status = str(root.status or '').strip().lower()

        self.store.update_task(
            task.task_id,
            lambda record: record.model_copy(
                update={
                    'status': 'in_progress',
                    'pause_requested': False,
                    'is_paused': False,
                    'finished_at': None,
                    'final_output': '',
                    'final_output_ref': '',
                    'failure_reason': '',
                    'updated_at': now_iso(),
                    'metadata': self._sanitize_recovered_task_metadata(
                        record.metadata,
                        preserve_final_acceptance=bool(root_status == 'success'),
                    ),
                }
            ),
        )

        self.log_service.update_task_runtime_meta(
            task.task_id,
            last_visible_output_at=self._stall_now_iso(),
            last_stall_notice_bucket_minutes=0,
        )
        runtime_state = self.log_service.read_runtime_state(task.task_id) or {}
        frames = [dict(item) for item in list(runtime_state.get('frames') or []) if isinstance(item, dict)]
        active_node_ids = [str(item) for item in list(runtime_state.get('active_node_ids') or []) if str(item or '').strip()]
        runnable_node_ids = [str(item) for item in list(runtime_state.get('runnable_node_ids') or []) if str(item or '').strip()]
        waiting_node_ids = [str(item) for item in list(runtime_state.get('waiting_node_ids') or []) if str(item or '').strip()]
        if not frames:
            frames = [self.log_service._default_frame(node_id=root.node_id, depth=root.depth, node_kind=root.node_kind, phase='before_model')]
            active_node_ids = [root.node_id]
            runnable_node_ids = [root.node_id]
            waiting_node_ids = []
        self.log_service.replace_runtime_frames(
            task.task_id,
            frames=frames,
            active_node_ids=active_node_ids,
            runnable_node_ids=runnable_node_ids,
            waiting_node_ids=waiting_node_ids,
            publish_snapshot=False,
        )
        self.log_service.sync_task_read_models(task.task_id, externalize_execution_trace=False)
        self.log_service.refresh_task_view(task.task_id, mark_unread=True)

    @staticmethod
    def _recovery_discard_node_ids(root_node_id: str, nodes: list[NodeRecord]) -> set[str]:
        children_by_parent: dict[str, list[str]] = {}
        for node in list(nodes or []):
            parent_id = str(node.parent_node_id or '').strip()
            if parent_id:
                children_by_parent.setdefault(parent_id, []).append(node.node_id)

        discard_ids: set[str] = set()

        def _discard_subtree(node_id: str) -> None:
            if not str(node_id or '').strip() or node_id in discard_ids:
                return
            discard_ids.add(node_id)
            for child_id in list(children_by_parent.get(node_id, [])):
                _discard_subtree(child_id)

        for node in list(nodes or []):
            if node.node_id == root_node_id:
                continue
            if str(node.status or '').strip().lower() == 'success':
                continue
            _discard_subtree(node.node_id)
        return discard_ids

    def _sanitize_recovered_node(self, node: NodeRecord) -> None:
        metadata = self._sanitize_recovered_node_metadata(dict(node.metadata or {}), clear_result_payload=False)
        self.store.upsert_node(node.model_copy(update={'metadata': metadata, 'updated_at': now_iso()}))

    def _reset_root_for_recovery(self, root: NodeRecord) -> NodeRecord:
        metadata = self._sanitize_recovered_node_metadata(dict(root.metadata or {}), clear_result_payload=True)
        return root.model_copy(
            update={
                'status': 'in_progress',
                'input': root.prompt,
                'input_ref': '',
                'output': [],
                'check_result': '',
                'check_result_ref': '',
                'final_output': '',
                'final_output_ref': '',
                'failure_reason': '',
                'finished_at': None,
                'updated_at': now_iso(),
                'metadata': metadata,
            }
        )

    @staticmethod
    def _sanitize_recovered_node_metadata(metadata: dict[str, Any], *, clear_result_payload: bool) -> dict[str, Any]:
        cleaned = dict(metadata or {})
        cleaned.pop('spawn_operations', None)
        cleaned.pop('execution_stages', None)
        if clear_result_payload:
            cleaned.pop('result_schema_version', None)
            cleaned.pop('result_payload', None)
        return cleaned

    @staticmethod
    def _sanitize_recovered_task_metadata(metadata: dict[str, Any], *, preserve_final_acceptance: bool) -> dict[str, Any]:
        cleaned = dict(metadata or {})
        cleaned.pop('final_execution_output', None)
        cleaned[_TASK_RECOVERY_NOTICE_KEY] = _TASK_RECOVERY_NOTICE_TEXT
        final_acceptance = normalize_final_acceptance_metadata(cleaned.get('final_acceptance'))
        if final_acceptance.required or str(final_acceptance.prompt or '').strip():
            cleaned['final_acceptance'] = {
                'required': bool(final_acceptance.required),
                'prompt': str(final_acceptance.prompt or ''),
                'node_id': str(final_acceptance.node_id or '').strip() if preserve_final_acceptance else '',
                'status': str(final_acceptance.status or 'pending').strip().lower() if preserve_final_acceptance else 'pending',
            }
        else:
            cleaned.pop('final_acceptance', None)
        return cleaned

    async def _worker_command_loop(self) -> None:
        idle_delays = [0.25, 0.5, 1.0, 2.0]
        idle_index = 0
        while True:
            try:
                commands = self.store.claim_pending_task_commands(
                    worker_id=self.worker_id or 'worker',
                    claimed_at=now_iso(),
                    limit=20,
                )
                if not commands:
                    await asyncio.sleep(idle_delays[min(idle_index, len(idle_delays) - 1)])
                    idle_index = min(idle_index + 1, len(idle_delays) - 1)
                    continue
                idle_index = 0
                for command in commands:
                    await self._process_worker_command(command)
            except asyncio.CancelledError:
                raise
            except Exception:
                idle_index = 0
                await asyncio.sleep(0.5)

    async def _worker_heartbeat_loop(self) -> None:
        await self.worker_heartbeat_service.run_forever()

    def _publish_worker_status_from_any_thread(self, item: dict[str, Any]) -> None:
        loop = self._runtime_loop
        payload = dict(item or {})
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(
            lambda: self.publish_worker_status_event(item=payload, bridge=False)
        )

    async def _process_worker_command(self, command: dict[str, Any]) -> None:
        command_id = str(command.get('command_id') or '').strip()
        command_type = str(command.get('command_type') or '').strip()
        task_id = self.normalize_task_id(str(command.get('task_id') or '').strip())
        payload = dict(command.get('payload') or {}) if isinstance(command.get('payload'), dict) else {}
        success = False
        error_text = ''
        result_payload: dict[str, Any] | None = None
        try:
            if command_type == 'create_task':
                task = self.get_task(task_id)
                if task is not None and not bool(task.is_paused):
                    await self.global_scheduler.enqueue_task(task.task_id)
                success = True
            elif command_type == 'resume_task':
                if task_id:
                    await self.resume_task(task_id)
                success = True
            elif command_type == 'pause_task':
                if task_id:
                    await self.pause_task(task_id)
                success = True
            elif command_type == 'cancel_task':
                if task_id:
                    await self.cancel_task(task_id)
                success = True
            elif command_type == 'refresh_runtime_config':
                changed = self.ensure_runtime_config_current(
                    force=True,
                    reason=str(payload.get('reason') or 'worker_command_refresh').strip() or 'worker_command_refresh',
                )
                result_payload = {
                    'changed': bool(changed),
                    'applied_revision': int(getattr(self, '_runtime_model_revision', 0) or 0),
                    'applied_config_mtime_ns': int(self._config_mtime_ns()),
                    'worker_id': str(self.worker_id or 'worker'),
                    'worker_pid': int(os.getpid()),
                }
                success = True
            else:
                error_text = f'unsupported_command:{command_type}'
        except Exception as exc:
            error_text = str(exc)
        finally:
            if command_id:
                self.store.finish_task_command(
                    command_id,
                    finished_at=now_iso(),
                    success=success,
                    error_text=error_text,
                    result=result_payload,
                )

    def _enqueue_task_command(
        self,
        *,
        command_type: str,
        task_id: str | None,
        session_id: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        command_id = new_command_id()
        self.store.enqueue_task_command(
            command_id=command_id,
            task_id=task_id,
            session_id=str(session_id or 'web:shared').strip() or 'web:shared',
            command_type=str(command_type or '').strip(),
            created_at=now_iso(),
            payload=dict(payload or {}),
        )
        return command_id

    @staticmethod
    def _config_mtime_ns() -> int:
        try:
            return int(Path(get_config_path()).stat().st_mtime_ns)
        except Exception:
            return 0

    @staticmethod
    def _lease_expiry_from(updated_at: str, *, ttl_seconds: float = _WORKER_LEASE_TTL_SECONDS) -> str:
        parsed = MainRuntimeService._parse_worker_timestamp(updated_at)
        if parsed is None:
            return now_iso()
        return (parsed + timedelta(seconds=max(1.0, float(ttl_seconds or _WORKER_LEASE_TTL_SECONDS)))).astimezone().isoformat(timespec='seconds')

    def _worker_lease_payload(self, *, heartbeat_at: str) -> dict[str, Any]:
        return {
            'workspace': str(Path.cwd()),
            'worker_id': str(self.worker_id or 'worker'),
            'worker_pid': int(os.getpid()),
            'heartbeat_at': str(heartbeat_at or ''),
            'execution_mode': str(self.execution_mode or ''),
            'takeover': bool(self._worker_lease_takeover),
        }

    def _acquire_worker_lease_or_raise(self) -> None:
        if self.execution_mode != 'worker':
            return
        started_at = now_iso()
        lease = self.store.acquire_worker_lease(
            role=_WORKER_LEASE_ROLE,
            worker_id=str(self.worker_id or 'worker'),
            holder_pid=int(os.getpid()),
            acquired_at=started_at,
            heartbeat_at=started_at,
            expires_at=self._lease_expiry_from(started_at),
            payload=self._worker_lease_payload(heartbeat_at=started_at),
        )
        if bool(lease.get('acquired')):
            self._worker_lease_takeover = bool(lease.get('takeover'))
            self._worker_lease_acquired = True
            return
        holder = str(lease.get('worker_id') or '').strip() or 'unknown'
        expires_at = str(lease.get('expires_at') or '').strip()
        raise RuntimeError(f'worker_lease_unavailable:{holder}:{expires_at}')

    def _renew_worker_lease_from_thread(self, heartbeat_at: str, payload: dict[str, Any]) -> None:
        if self.execution_mode != 'worker' or not self._worker_lease_acquired:
            return
        merged_payload = {
            **self._worker_lease_payload(heartbeat_at=heartbeat_at),
            'status_payload': dict(payload or {}),
        }
        self.store.renew_worker_lease(
            role=_WORKER_LEASE_ROLE,
            worker_id=str(self.worker_id or 'worker'),
            heartbeat_at=heartbeat_at,
            expires_at=self._lease_expiry_from(heartbeat_at),
            payload=merged_payload,
        )

    async def request_worker_runtime_refresh(
        self,
        *,
        reason: str,
        timeout_s: float = _WORKER_RUNTIME_REFRESH_TIMEOUT_SECONDS,
    ) -> dict[str, object]:
        if self.execution_mode != 'web':
            changed = self.ensure_runtime_config_current(force=True, reason=reason)
            return {
                'changed': bool(changed),
                'worker_refresh_acked': True,
                'worker_id': str(self.worker_id or ''),
                'worker_pid': int(os.getpid()),
                'applied_config_mtime_ns': int(self._config_mtime_ns()),
            }
        command_id = self._enqueue_task_command(
            command_type='refresh_runtime_config',
            task_id=None,
            session_id='web:shared',
            payload={
                'reason': str(reason or '').strip() or 'runtime_refresh',
                'expected_config_mtime_ns': int(self._config_mtime_ns()),
            },
        )
        deadline = time.monotonic() + max(0.1, float(timeout_s or _WORKER_RUNTIME_REFRESH_TIMEOUT_SECONDS))
        while True:
            current = self.store.get_task_command(command_id)
            if current and str(current.get('status') or '').strip().lower() in {'completed', 'failed'}:
                if str(current.get('status') or '').strip().lower() == 'failed':
                    raise RuntimeError(str(current.get('error_text') or 'worker_runtime_refresh_failed').strip() or 'worker_runtime_refresh_failed')
                result = dict(current.get('result') or {}) if isinstance(current.get('result'), dict) else {}
                return {
                    'worker_refresh_acked': True,
                    'command_id': command_id,
                    **result,
                }
            if time.monotonic() >= deadline:
                raise TimeoutError('worker_runtime_refresh_timeout')
            await asyncio.sleep(_WORKER_RUNTIME_REFRESH_POLL_SECONDS)

    def enqueue_worker_runtime_refresh(self, *, reason: str) -> dict[str, object]:
        normalized_reason = str(reason or '').strip() or 'runtime_refresh'
        if self.execution_mode != 'web':
            changed = self.ensure_runtime_config_current(force=True, reason=normalized_reason)
            return {
                'worker_refresh_requested': True,
                'worker_refresh_acked': True,
                'worker_refresh_command_id': '',
                'worker_refresh_status': 'completed',
                'reason': normalized_reason,
                'changed': bool(changed),
                'worker_id': str(self.worker_id or ''),
                'worker_pid': int(os.getpid()),
                'applied_config_mtime_ns': int(self._config_mtime_ns()),
            }
        command_id = self._enqueue_task_command(
            command_type='refresh_runtime_config',
            task_id=None,
            session_id='web:shared',
            payload={
                'reason': normalized_reason,
                'expected_config_mtime_ns': int(self._config_mtime_ns()),
            },
        )
        return {
            'worker_refresh_requested': True,
            'worker_refresh_acked': False,
            'worker_refresh_command_id': command_id,
            'worker_refresh_status': 'pending',
            'reason': normalized_reason,
        }

    def get_task_command_status(self, command_id: str) -> dict[str, object] | None:
        normalized = str(command_id or '').strip()
        if not normalized:
            return None
        current = self.store.get_task_command(normalized)
        if not current:
            return None
        return dict(current)

    def _build_task_record(
        self,
        *,
        task: str,
        session_id: str,
        max_depth: int | None,
        title: str | None,
        metadata: dict[str, Any] | None,
    ) -> tuple[TaskRecord, NodeRecord]:
        prompt = str(task or '').strip()
        if not prompt:
            raise ValueError('task must not be empty')
        effective_max_depth = self._clamp_depth(max_depth)
        task_id = new_task_id()
        root_node_id = new_node_id()
        now = now_iso()
        task_metadata = self._normalize_task_metadata(
            session_id=session_id,
            metadata=metadata,
            task_prompt=prompt,
        )
        record = TaskRecord(
            task_id=task_id,
            session_id=str(session_id or 'web:shared').strip() or 'web:shared',
            title=(str(title or '').strip() or prompt[:60] or task_id),
            user_request=prompt,
            status='in_progress',
            root_node_id=root_node_id,
            max_depth=effective_max_depth,
            cancel_requested=False,
            pause_requested=False,
            is_paused=False,
            is_unread=True,
            brief_text='',
            created_at=now,
            updated_at=now,
            finished_at=None,
            final_output='',
            failure_reason='',
            token_usage=TokenUsageSummary(tracked=True),
            metadata=task_metadata,
        )
        root_goal = str(task_metadata.get('core_requirement') or prompt).strip() or prompt
        root = NodeRecord(
            node_id=root_node_id,
            task_id=task_id,
            parent_node_id=None,
            root_node_id=root_node_id,
            depth=0,
            node_kind='execution',
            status='in_progress',
            goal=root_goal,
            prompt=prompt,
            input=prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=0 < effective_max_depth,
            created_at=now,
            updated_at=now,
            token_usage=TokenUsageSummary(tracked=True),
            token_usage_by_model=[],
            metadata={
                'execution_policy': dict(task_metadata.get('execution_policy') or {}),
            },
        )
        return record, root

    async def create_task(
        self,
        task: str,
        *,
        session_id: str = 'web:shared',
        max_depth: int | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        await self.startup()
        if self.execution_mode == 'web':
            self._assert_worker_available()
        record, root = self._build_task_record(
            task=task,
            session_id=session_id,
            max_depth=max_depth,
            title=title,
            metadata=metadata,
        )
        record, root = self.log_service.initialize_task(record, root)
        self.log_service.update_task_runtime_meta(
            record.task_id,
            task_temp_dir=str(self._task_temp_dir(record.task_id)),
        )
        if self.execution_mode in {'embedded', 'worker'}:
            await self.global_scheduler.enqueue_task(record.task_id)
        else:
            self._enqueue_task_command(
                command_type='create_task',
                task_id=record.task_id,
                session_id=record.session_id,
                payload={
                    'task_id': record.task_id,
                    'session_id': record.session_id,
                },
            )
        return self.store.get_task(record.task_id) or record

    async def cancel_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        if self.execution_mode in {'embedded', 'worker'}:
            self.task_actor_service.request_cancel(task_id)
            await self.global_scheduler.cancel_task(task_id)
            current = self.get_task(task_id)
            if current is not None and current.status == 'in_progress' and not bool(current.is_paused):
                self.log_service.mark_task_failed(task_id, reason='canceled')
        else:
            self._assert_worker_available()
            self.log_service.request_cancel(task_id)
            task = self.get_task(task_id)
            if task is not None:
                self._enqueue_task_command(
                    command_type='cancel_task',
                    task_id=task.task_id,
                    session_id=task.session_id,
                    payload={'task_id': task.task_id},
                )
        return self.get_task(task_id)

    async def pause_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        if self.execution_mode in {'embedded', 'worker'}:
            self.task_actor_service.request_pause(task_id)
            await self.global_scheduler.cancel_task(task_id)
        else:
            self._assert_worker_available()
            self.log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            task = self.get_task(task_id)
            if task is not None:
                self._enqueue_task_command(
                    command_type='pause_task',
                    task_id=task.task_id,
                    session_id=task.session_id,
                    payload={'task_id': task.task_id},
                )
        return self.get_task(task_id)

    async def resume_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        if self.execution_mode in {'embedded', 'worker'}:
            task = self.get_task(task_id)
            if task is None:
                return None
            self.task_actor_service.clear_pause(task_id)
            await self.global_scheduler.enqueue_task(task_id)
        else:
            self._assert_worker_available()
            task = self.get_task(task_id)
            if task is None:
                return None
            self.log_service.set_pause_state(task_id, pause_requested=False, is_paused=False)
            self._enqueue_task_command(
                command_type='resume_task',
                task_id=task.task_id,
                session_id=task.session_id,
                payload={'task_id': task.task_id},
            )
        return self.get_task(task_id)

    @staticmethod
    def _runtime_session_context(session_id: str) -> dict[str, str]:
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        channel, _, chat_id = normalized_session_id.partition(':')
        normalized_channel = str(channel or 'web').strip() or 'web'
        normalized_chat_id = str(chat_id or normalized_session_id).strip() or normalized_session_id
        return {
            'session_key': normalized_session_id,
            'channel': normalized_channel,
            'chat_id': normalized_chat_id,
        }

    async def delete_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        task = self.get_task(task_id)
        if task is None:
            return None
        if not (bool(task.is_paused) or task.status in {'success', 'failed'}):
            raise ValueError('task_not_paused')
        self._cancel_distribution_for_force_delete(task_id=task_id, reason='task_delete')
        if self.global_scheduler.is_active(task_id):
            try:
                await asyncio.wait_for(self.global_scheduler.wait(task_id), timeout=2.0)
            except asyncio.TimeoutError as exc:
                raise ValueError('task_still_stopping') from exc
        if self.global_scheduler.is_active(task_id) or self.global_scheduler.is_queued(task_id):
            raise ValueError('task_still_stopping')
        artifacts = self.list_artifacts(task_id)
        self.artifact_store.delete_artifacts_for_task(task_id, artifacts=artifacts)
        self.file_store.delete_task_files(task_id)
        shutil.rmtree(self._task_temp_dir(task_id, create=False), ignore_errors=True)
        self.store.delete_task(task_id)
        shutil.rmtree(self._task_event_history_dir(task_id), ignore_errors=True)
        self._publish_task_deleted_event(session_id=task.session_id, task_id=task.task_id)
        await self.registry.forget_task(task.session_id, task_id)
        return task

    async def _delete_task_with_task_delete_semantics(self, task_id: str) -> TaskRecord | None:
        task = self.get_task(task_id)
        if task is None:
            return None
        if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress' and not bool(getattr(task, 'is_paused', False)):
            await self.pause_task(task_id)
        return await self.delete_task(task_id)

    async def wait_for_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        await self.global_scheduler.wait(task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        return self.store.get_task(task_id)

    def task_stats(
        self,
        *,
        mode: str,
        task_keywords: list[str] | None = None,
        date_from: str = '',
        date_to: str = '',
        task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        normalized_mode = str(mode or '').strip().lower()
        if normalized_mode not in {'list', 'id'}:
            raise ValueError('invalid_mode')
        if normalized_mode == 'list':
            start_at = self._parse_task_stats_date(date_from, field_name='from')
            end_at = self._parse_task_stats_date(date_to, field_name='to') + timedelta(days=1) - timedelta(milliseconds=1)
            if end_at < start_at:
                raise ValueError('invalid_date_range')
            keywords = self._normalize_task_keyword_list(task_keywords)
            matched: list[tuple[float, dict[str, Any]]] = []
            for task in list(self.store.list_tasks()):
                created_at = self._parse_task_timestamp(str(getattr(task, 'created_at', '') or ''))
                if created_at is None or created_at < start_at or created_at > end_at:
                    continue
                prompt = str(getattr(task, 'user_request', '') or '')
                if keywords and not any(keyword.casefold() in prompt.casefold() for keyword in keywords):
                    continue
                matched.append((created_at.timestamp(), self._task_stats_item(task)))
            matched.sort(key=lambda item: item[0], reverse=True)
            return {
                'mode': 'list',
                'from': str(date_from or '').strip(),
                'to': str(date_to or '').strip(),
                'items': [item for _, item in matched],
            }
        normalized_task_ids = self._normalize_task_id_list(task_ids, allow_empty=False)
        items: list[dict[str, Any]] = []
        for task_id in normalized_task_ids:
            task = self.get_task(task_id)
            if task is None:
                items.append(self._task_not_found_item(task_id))
                continue
            item = self._task_stats_item(task)
            item['result'] = 'ok'
            items.append(item)
        return {
            'mode': 'id',
            'items': items,
        }

    def task_delete_preview(self, *, task_ids: list[str] | None, session_id: str = 'web:shared') -> dict[str, Any]:
        normalized_task_ids = self._normalize_task_id_list(task_ids, allow_empty=False)
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        items: list[dict[str, Any]] = []
        for task_id in normalized_task_ids:
            task = self.get_task(task_id)
            if task is None:
                items.append(self._task_not_found_item(task_id))
                continue
            items.append(self._task_stats_item(task))
        self._prune_pending_task_delete_confirmations()
        confirmation_token = new_command_id()
        self._pending_task_delete_confirmations[confirmation_token] = {
            'task_ids': list(normalized_task_ids),
            'session_id': normalized_session_id,
            'created_mono': time.monotonic(),
            'preview_items': list(items),
        }
        return {
            'mode': 'preview',
            'confirmation_token': confirmation_token,
            'items': items,
        }

    async def task_delete_confirm(
        self,
        *,
        task_ids: list[str] | None,
        confirmation_token: str,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        normalized_task_ids = self._normalize_task_id_list(task_ids, allow_empty=False)
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        normalized_token = str(confirmation_token or '').strip()
        if not normalized_token:
            raise ValueError('confirmation_token_required')
        self._prune_pending_task_delete_confirmations()
        pending = self._pending_task_delete_confirmations.pop(normalized_token, None)
        if pending is None:
            return {
                'mode': 'confirm',
                'items': [self._task_token_mismatch_item(task_id) for task_id in normalized_task_ids],
            }
        if list(pending.get('task_ids') or []) != normalized_task_ids or str(pending.get('session_id') or '') != normalized_session_id:
            return {
                'mode': 'confirm',
                'items': [self._task_token_mismatch_item(task_id) for task_id in normalized_task_ids],
            }
        items: list[dict[str, Any]] = []
        for task_id in normalized_task_ids:
            items.append(await self._delete_task_with_confirmation(task_id))
        return {
            'mode': 'confirm',
            'items': items,
        }

    async def _delete_task_with_confirmation(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            return self._task_not_found_item(task_id)
        snapshot = self._task_stats_item(task)
        try:
            deleted = await self._delete_task_with_task_delete_semantics(task_id)
        except ValueError as exc:
            if str(exc) in {'task_not_paused', 'task_still_stopping'}:
                snapshot['result'] = 'still_stopping'
                return snapshot
            raise
        if deleted is None:
            return self._task_not_found_item(task_id)
        snapshot['result'] = 'deleted'
        return snapshot

    def _prune_pending_task_delete_confirmations(self) -> None:
        now_mono = time.monotonic()
        expired = [
            token
            for token, entry in list(self._pending_task_delete_confirmations.items())
            if (now_mono - float(entry.get('created_mono') or 0.0)) > _TASK_DELETE_CONFIRM_TTL_SECONDS
        ]
        for token in expired:
            self._pending_task_delete_confirmations.pop(token, None)

    @staticmethod
    def _normalize_task_keyword_list(raw_keywords: list[str] | None) -> list[str]:
        if raw_keywords in (None, ''):
            return []
        if not isinstance(raw_keywords, list):
            raise ValueError('任务关键词 must be an array')
        return [str(item or '').strip() for item in list(raw_keywords or []) if str(item or '').strip()]

    def _normalize_task_id_list(self, raw_task_ids: list[str] | None, *, allow_empty: bool) -> list[str]:
        if not isinstance(raw_task_ids, list):
            raise ValueError('任务id列表 must be an array')
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_task_id in list(raw_task_ids or []):
            task_id = self.normalize_task_id(str(raw_task_id or '').strip())
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            normalized.append(task_id)
        if not allow_empty and not normalized:
            raise ValueError('任务id列表 must not be empty')
        return normalized

    @staticmethod
    def _parse_task_stats_date(value: str, *, field_name: str) -> datetime:
        text = str(value or '').strip()
        parts = text.split('/')
        if len(parts) != 3:
            raise ValueError(f'{field_name}_invalid')
        try:
            year, month, day = (int(part) for part in parts)
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            return datetime(year, month, day, tzinfo=local_tz)
        except ValueError as exc:
            raise ValueError(f'{field_name}_invalid') from exc

    @staticmethod
    def _parse_task_timestamp(value: str) -> datetime | None:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(local_tz)

    @staticmethod
    def _prompt_preview_100(value: Any) -> str:
        text = ' '.join(str(value or '').split())
        return text[:100]

    def _task_stats_item(self, task: TaskRecord) -> dict[str, Any]:
        return {
            'task_id': task.task_id,
            'created_at': str(getattr(task, 'created_at', '') or ''),
            'status': str(getattr(task, 'status', '') or ''),
            'prompt_preview_100': self._prompt_preview_100(getattr(task, 'user_request', '')),
            'disk_usage_bytes': self._task_disk_usage_bytes(task.task_id),
        }

    def _task_not_found_item(self, task_id: str) -> dict[str, Any]:
        return {
            'task_id': str(task_id or '').strip(),
            'created_at': '',
            'status': 'not_found',
            'prompt_preview_100': '',
            'disk_usage_bytes': 0,
            'result': 'not_found',
        }

    def _task_token_mismatch_item(self, task_id: str) -> dict[str, Any]:
        return {
            'task_id': str(task_id or '').strip(),
            'result': 'token_mismatch',
        }

    def _task_disk_usage_bytes(self, task_id: str) -> int:
        normalized_task_id = self.normalize_task_id(task_id)
        return sum(
            self._directory_size_bytes(path)
            for path in (
                self._task_file_dir_path(normalized_task_id),
                self._task_artifact_dir(normalized_task_id),
                self._task_event_history_dir(normalized_task_id),
                self._effective_task_temp_dir(normalized_task_id),
            )
        )

    @staticmethod
    def _directory_size_bytes(path: Path) -> int:
        target = Path(path)
        if not target.exists():
            return 0
        if target.is_file():
            try:
                return int(target.stat().st_size)
            except OSError:
                return 0
        total = 0
        for child in target.rglob('*'):
            try:
                if child.is_file():
                    total += int(child.stat().st_size)
            except OSError:
                continue
        return total

    def latest_worker_status(self) -> dict[str, Any] | None:
        items = self.store.list_worker_status(role='task_worker')
        return items[0] if items else None

    @staticmethod
    def _parse_worker_timestamp(value: Any) -> datetime | None:
        text = str(value or '').strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _managed_worker_runtime_snapshot(self) -> dict[str, object]:
        if self.execution_mode != 'web':
            return {}
        try:
            snapshot = managed_worker_snapshot(starting_grace_s=_WORKER_STATUS_STARTING_GRACE_SECONDS)
        except Exception:
            return {}
        return dict(snapshot or {}) if isinstance(snapshot, dict) else {}

    def _worker_status_stale_after_seconds(
        self,
        item: dict[str, Any] | None,
        *,
        override_seconds: float | None = None,
    ) -> float:
        if override_seconds is not None:
            return max(0.0, float(override_seconds))
        payload = item.get('payload') if isinstance(item, dict) else {}
        raw_active_task_count = payload.get('active_task_count') if isinstance(payload, dict) else 0
        try:
            active_task_count = max(0, int(raw_active_task_count or 0))
        except (TypeError, ValueError):
            active_task_count = 0
        if active_task_count > 0:
            return _WORKER_STATUS_ACTIVE_TASK_STALE_AFTER_SECONDS
        return _WORKER_STATUS_STALE_AFTER_SECONDS

    def worker_status_stale_after_seconds(
        self,
        *,
        item: dict[str, Any] | None = None,
        override_seconds: float | None = None,
    ) -> float:
        return float(self._worker_status_stale_after_seconds(item, override_seconds=override_seconds))

    def _worker_online_from_item(
        self,
        item: dict[str, Any] | None,
        *,
        stale_after_seconds: float | None = None,
    ) -> bool:
        if not item:
            return self.execution_mode in {'embedded', 'worker'}
        status = str(item.get('status') or item.get('state') or '').strip().lower()
        if status in _WORKER_STATUS_TERMINAL_STATES:
            return False
        if self.execution_mode in {'embedded', 'worker'}:
            return True
        updated_dt = self._parse_worker_timestamp(item.get('updated_at'))
        if updated_dt is None:
            return False
        age_seconds = max(0.0, (datetime.now(timezone.utc) - updated_dt).total_seconds())
        stale_window_seconds = self._worker_status_stale_after_seconds(item, override_seconds=stale_after_seconds)
        return age_seconds <= stale_window_seconds

    def _managed_worker_is_starting(
        self,
        *,
        item: dict[str, Any] | None,
        snapshot: dict[str, object] | None = None,
    ) -> bool:
        current = item if isinstance(item, dict) else {}
        worker_snapshot = dict(snapshot or {}) if isinstance(snapshot, dict) else self._managed_worker_runtime_snapshot()
        if not worker_snapshot:
            return False
        if not bool(worker_snapshot.get('active')) or not bool(worker_snapshot.get('auto_worker_enabled')):
            return False
        if not bool(worker_snapshot.get('starting')):
            return False
        started_at_dt = self._parse_worker_timestamp(worker_snapshot.get('started_at'))
        if not current:
            return True
        current_updated_at = self._parse_worker_timestamp(current.get('updated_at'))
        if started_at_dt is None or current_updated_at is None:
            return True
        return current_updated_at < started_at_dt

    def worker_state(
        self,
        *,
        item: dict[str, Any] | None = None,
        stale_after_seconds: float | None = None,
    ) -> str:
        current = dict(item or self.latest_worker_status() or {})
        if self._managed_worker_is_starting(item=current if current else None):
            return _WORKER_STATE_STARTING
        if not current:
            return _WORKER_STATE_ONLINE if self.execution_mode in {'embedded', 'worker'} else _WORKER_STATE_OFFLINE
        status = str(current.get('status') or current.get('state') or '').strip().lower()
        if status in _WORKER_STATUS_TERMINAL_STATES:
            return _WORKER_STATE_STOPPED
        if self._worker_online_from_item(current, stale_after_seconds=stale_after_seconds):
            return _WORKER_STATE_ONLINE
        return _WORKER_STATE_STALE

    def is_worker_online(self, *, stale_after_seconds: float | None = None) -> bool:
        return self.worker_state(stale_after_seconds=stale_after_seconds) == _WORKER_STATE_ONLINE

    def worker_status_payload(
        self,
        *,
        item: dict[str, Any] | None = None,
        stale_after_seconds: float | None = None,
    ) -> dict[str, object]:
        current = dict(item or self.latest_worker_status() or {})
        window_seconds = self.worker_status_stale_after_seconds(
            item=current if current else None,
            override_seconds=stale_after_seconds,
        )
        state = self.worker_state(
            item=current if current else None,
            stale_after_seconds=window_seconds,
        )
        last_seen_at = str(current.get('updated_at') or '').strip()
        return {
            'worker': current or None,
            'worker_online': state == _WORKER_STATE_ONLINE,
            'worker_state': state,
            'worker_last_seen_at': last_seen_at,
            'worker_control_available': state == _WORKER_STATE_ONLINE,
            'worker_stale_after_seconds': window_seconds,
            **self._tool_pressure_status_payload(current if current else None),
        }

    def _publish_task_list_envelope(
        self,
        *,
        target_session_id: str,
        session_id: str,
        event_type: str,
        data: dict[str, Any],
        task_id: str | None = None,
    ) -> dict[str, Any]:
        payload = build_envelope(
            channel='task',
            session_id=session_id,
            task_id=(str(task_id or '').strip() or None),
            seq=self.registry.next_task_list_seq(target_session_id),
            type=event_type,
            data=data,
        )
        self.registry.publish_task_list(target_session_id, payload)
        return payload

    def _callback_headers(self, *, token: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        normalized_token = str(token or '').strip()
        if normalized_token:
            headers['x-g3ku-internal-token'] = normalized_token
        return headers

    @staticmethod
    def _replace_internal_callback_path(url: str, *, target_path: str) -> str:
        text = str(url or '').strip()
        if not text:
            return ''
        parsed = urlparse(text)
        path = str(parsed.path or '').strip()
        if path.endswith(TASK_TERMINAL_CALLBACK_PATH):
            next_path = f'{path[: -len(TASK_TERMINAL_CALLBACK_PATH)]}{target_path}'
        elif not path or path == '/':
            next_path = target_path
        else:
            return ''
        return urlunparse(parsed._replace(path=next_path))

    def _internal_callback_targets(
        self,
        *,
        workspace: Path,
        callback_kind: str,
    ) -> list[tuple[str, str]]:
        normalized_kind = str(callback_kind or '').strip().lower()
        if normalized_kind == 'terminal':
            target_path = TASK_TERMINAL_CALLBACK_PATH
            primary_url = resolve_task_terminal_callback_url(workspace=workspace)
            primary_token = resolve_task_terminal_callback_token(workspace=workspace)
        elif normalized_kind == 'event':
            target_path = TASK_EVENT_CALLBACK_PATH
            primary_url = resolve_task_event_callback_url(workspace=workspace)
            primary_token = resolve_task_event_callback_token(workspace=workspace)
        elif normalized_kind == 'event_batch':
            target_path = TASK_EVENT_BATCH_CALLBACK_PATH
            primary_url = resolve_task_event_batch_callback_url(workspace=workspace)
            primary_token = resolve_task_event_callback_token(workspace=workspace)
        elif normalized_kind == 'stall':
            target_path = TASK_STALL_CALLBACK_PATH
            primary_url = resolve_task_stall_callback_url(workspace=workspace)
            primary_token = resolve_task_stall_callback_token(workspace=workspace)
        else:
            return []

        candidates: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def _append(url: str, token: str) -> None:
            normalized_url = str(url or '').strip()
            normalized_token = str(token or '').strip()
            if not normalized_url:
                return
            item = (normalized_url, normalized_token)
            if item in seen:
                return
            seen.add(item)
            candidates.append(item)

        _append(primary_url, primary_token)

        file_config = load_task_terminal_callback_config(workspace=workspace)
        file_terminal_url = str(file_config.get('url') or '').strip()
        if target_path != TASK_TERMINAL_CALLBACK_PATH:
            file_url = self._replace_internal_callback_path(file_terminal_url, target_path=target_path)
        else:
            file_url = file_terminal_url
        _append(file_url, str(file_config.get('token') or '').strip())

        return candidates

    def _get_callback_client(self) -> httpx.AsyncClient:
        client = self._callback_client
        if client is None:
            client = httpx.AsyncClient()
            self._callback_client = client
        return client

    async def _post_internal_callback(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> httpx.Response:
        client = self._get_callback_client()
        return await client.post(url, json=payload, headers=headers, timeout=float(timeout or _WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS))

    def _schedule_task_event_callback(self, payload: dict[str, Any] | None) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_event_payload(payload)
        if not normalized:
            return
        if str(normalized.get('event_type') or '').strip() == 'task.summary.patch':
            self._enqueue_task_summary_callback(normalized)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        suffix = str(normalized.get('task_id') or normalized.get('event_type') or 'task-event').strip() or 'task-event'
        task = loop.create_task(
            self._deliver_task_event_callback(normalized),
            name=f'main-runtime-task-event:{suffix}',
        )
        self._task_event_dispatch_tasks.add(task)
        task.add_done_callback(self._task_event_dispatch_tasks.discard)

    async def _deliver_task_event_callback(self, payload: dict[str, Any]) -> None:
        callback_targets = self._internal_callback_targets(workspace=Path.cwd(), callback_kind='event')
        if not callback_targets:
            return
        for index, (callback_url, callback_token) in enumerate(callback_targets):
            headers = self._callback_headers(token=callback_token)
            try:
                response = await self._post_internal_callback(
                    callback_url,
                    payload=payload,
                    headers=headers,
                    timeout=1.5,
                )
                if 200 <= int(response.status_code or 0) < 300:
                    return
                if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                if index + 1 < len(callback_targets):
                    continue
                continue
            break
        logger.debug('task event callback delivery skipped: {}', payload.get('event_type'))

    def publish_worker_status_event(
        self,
        *,
        item: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        bridge: bool = False,
    ) -> dict[str, object]:
        data = dict(payload or self.worker_status_payload(item=item))
        channels = self.registry.task_list_channels() or ['all']
        for channel in channels:
            self._publish_task_list_envelope(
                target_session_id=channel,
                session_id=channel,
                event_type='task.worker.status',
                data=data,
            )
        if self.execution_mode == 'worker' and not bridge:
            self._enqueue_task_worker_status_callback(data)
        return data

    def _publish_task_list_patch_event(
        self,
        *,
        session_id: str,
        task_payload: dict[str, Any],
        bridge: bool = False,
    ) -> None:
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        normalized_task_payload = dict(task_payload or {})
        normalized_task_id = self.normalize_task_id(
            str(
                normalized_task_payload.get('task_id')
                or normalized_task_payload.get('taskId')
                or ''
            ).strip()
        )
        normalized_task_payload['task_id'] = normalized_task_id
        data = {'task': normalized_task_payload}
        for target_session_id in {normalized_session_id, 'all'}:
            self._publish_task_list_envelope(
                target_session_id=target_session_id,
                session_id=normalized_session_id,
                task_id=normalized_task_id,
                event_type='task.summary.patch',
                data=data,
            )
        if self.execution_mode == 'worker' and not bridge:
            self._schedule_task_event_callback(
                {
                    'event_type': 'task.summary.patch',
                    'session_id': normalized_session_id,
                    'task_id': normalized_task_id,
                    'data': data,
                }
            )

    def _publish_task_deleted_event(self, *, session_id: str, task_id: str) -> None:
        normalized_session_id = str(session_id or 'web:shared').strip() or 'web:shared'
        normalized_task_id = self.normalize_task_id(task_id)
        data = {'task_id': normalized_task_id}
        for target_session_id in {normalized_session_id, 'all'}:
            self._publish_task_list_envelope(
                target_session_id=target_session_id,
                session_id=normalized_session_id,
                task_id=normalized_task_id,
                event_type='task.deleted',
                data=data,
            )
        payload = build_envelope(
            channel='task',
            session_id=normalized_session_id,
            task_id=normalized_task_id,
            seq=self.registry.next_global_task_seq(normalized_task_id),
            type='task.deleted',
            data=data,
        )
        self.registry.publish_global_task(normalized_task_id, payload)

    def _publish_task_artifact_applied_event(self, *, task: TaskRecord, artifact_id: str, path: str) -> None:
        payload = build_envelope(
            channel='task',
            session_id=task.session_id,
            task_id=task.task_id,
            seq=self.registry.next_global_task_seq(task.task_id),
            type='task.artifact.applied',
            data={'artifact_id': artifact_id, 'path': path, 'applied': True, 'task_id': task.task_id},
        )
        self.registry.publish_global_task(task.task_id, payload)

    def _publish_live_snapshot(self, task: TaskRecord, payload: dict[str, Any], publish_summary: bool) -> None:
        started_at = now_iso()
        started_mono = time.perf_counter()
        event_type = str(payload.get('event_type') or '').strip()
        data = dict(payload.get('data') or {})
        if event_type == 'task.summary.patch':
            if self.execution_mode == 'worker':
                self._enqueue_task_summary_callback(
                    {
                        'event_type': event_type,
                        'session_id': task.session_id,
                        'task_id': task.task_id,
                        'data': data,
                    },
                    immediate=bool(payload.get('dispatch_immediate')),
                )
                self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
                return
            self._publish_task_list_envelope(
                target_session_id=task.session_id,
                session_id=task.session_id,
                task_id=task.task_id,
                event_type=event_type,
                data=data,
            )
            self._publish_task_list_envelope(
                target_session_id='all',
                session_id=task.session_id,
                task_id=task.task_id,
                event_type=event_type,
                data=data,
            )
            detail_payload = build_envelope(
                channel='task',
                session_id=task.session_id,
                task_id=task.task_id,
                seq=self.registry.next_global_task_seq(task.task_id),
                type=event_type,
                data=data,
            )
            self.registry.publish_global_task(task.task_id, detail_payload)
            self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
            return
        if event_type == 'task.token.patch':
            if self.execution_mode == 'worker':
                self._schedule_task_event_callback(
                    {
                        'event_type': event_type,
                        'session_id': task.session_id,
                        'task_id': task.task_id,
                        'data': data,
                    }
                )
                self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
                return
            self._publish_task_list_envelope(
                target_session_id=task.session_id,
                session_id=task.session_id,
                task_id=task.task_id,
                event_type=event_type,
                data=data,
            )
            self._publish_task_list_envelope(
                target_session_id='all',
                session_id=task.session_id,
                task_id=task.task_id,
                event_type=event_type,
                data=data,
            )
            self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
            return
        if event_type in {'task.node.patch', 'task.live.patch', 'task.governance.patch', 'task.model.call', 'task.terminal'}:
            detail_payload = build_envelope(
                channel='task',
                session_id=task.session_id,
                task_id=task.task_id,
                seq=self.registry.next_global_task_seq(task.task_id),
                type=event_type,
                data=data,
            )
            self.registry.publish_global_task(task.task_id, detail_payload)
            if self.execution_mode == 'worker':
                self._schedule_task_event_callback(
                    {
                        'event_type': event_type,
                        'session_id': task.session_id,
                        'task_id': task.task_id,
                        'data': data,
                    }
                )
            self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
            return
        self.runtime_debug_recorder.record(section='runtime_service.publish_live_snapshot', elapsed_ms=(time.perf_counter() - started_mono) * 1000.0, started_at=started_at)
        return

    def forward_live_task_event(self, payload: dict[str, Any] | None) -> bool:
        normalized = normalize_task_event_payload(payload)
        if not normalized:
            return False
        event_type = str(normalized.get('event_type') or '').strip()
        session_id = str(normalized.get('session_id') or 'web:shared').strip() or 'web:shared'
        task_id = self.normalize_task_id(str(normalized.get('task_id') or '').strip()) if normalized.get('task_id') else ''
        data = dict(normalized.get('data') or {})
        if event_type in {'task.node.patch', 'task.live.patch', 'task.governance.patch', 'task.model.call', 'task.terminal'} and task_id:
            payload = build_envelope(
                channel='task',
                session_id=session_id,
                task_id=task_id,
                seq=self.registry.next_global_task_seq(task_id),
                type=event_type,
                data=data,
            )
            self.registry.publish_global_task(task_id, payload)
            return True
        if event_type == 'task.token.patch' and task_id:
            for target_session_id in {session_id, 'all'}:
                self._publish_task_list_envelope(
                    target_session_id=target_session_id,
                    session_id=session_id,
                    task_id=task_id,
                    event_type='task.token.patch',
                    data=data,
                )
            return True
        if event_type == 'task.summary.patch':
            normalized_task_payload = dict(data.get('task') or {})
            normalized_task_id = self.normalize_task_id(str(normalized_task_payload.get('task_id') or task_id or '').strip())
            normalized_task_payload['task_id'] = normalized_task_id
            for target_session_id in {session_id, 'all'}:
                self._publish_task_list_envelope(
                    target_session_id=target_session_id,
                    session_id=session_id,
                    task_id=normalized_task_id,
                    event_type='task.summary.patch',
                    data={'task': normalized_task_payload},
                )
            payload = build_envelope(
                channel='task',
                session_id=session_id,
                task_id=normalized_task_id,
                seq=self.registry.next_global_task_seq(normalized_task_id),
                type='task.summary.patch',
                data={'task': normalized_task_payload},
            )
            self.registry.publish_global_task(normalized_task_id, payload)
            return True
        if event_type == 'task.worker.status':
            self.publish_worker_status_event(
                item=dict(data.get('worker') or {}) if isinstance(data.get('worker'), dict) else None,
                bridge=True,
            )
            return True
        return False

    def _assert_worker_available(self) -> None:
        if self.execution_mode != 'web':
            return
        state = self.worker_state()
        if state == _WORKER_STATE_ONLINE:
            return
        if state == _WORKER_STATE_STARTING:
            raise ValueError('task_worker_starting')
        if state == _WORKER_STATE_STALE:
            raise ValueError('task_worker_stale')
        raise ValueError('task_worker_offline')

    @staticmethod
    def _normalize_session_key(session_id: str | None) -> str:
        return str(session_id or 'web:shared').strip() or 'web:shared'

    def _task_origin_session_id(self, task: TaskRecord | None) -> str:
        if task is None:
            return 'web:shared'
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        origin_session_id = str(metadata.get('origin_session_id') or '').strip()
        if origin_session_id:
            return origin_session_id
        return self._normalize_session_key(getattr(task, 'session_id', 'web:shared'))

    def list_tasks_for_session(self, session_id: str) -> list[TaskRecord]:
        key = self._normalize_session_key(session_id)
        return [task for task in self.store.list_tasks() if self._task_origin_session_id(task) == key]

    def list_unfinished_tasks_for_session(self, session_id: str) -> list[TaskRecord]:
        return [
            task
            for task in self.list_tasks_for_session(session_id)
            if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress'
        ]

    @staticmethod
    def _normalize_async_task_target_text(value: Any) -> str:
        text = str(value or '').strip().casefold()
        if not text:
            return ''
        text = re.sub(r'\s+', ' ', text)
        return (
            text.replace('，', ',')
            .replace('。', '.')
            .replace('：', ':')
            .replace('（', '(')
            .replace('）', ')')
        )

    def _async_task_keyword_fingerprint(self, value: Any) -> tuple[str, ...]:
        normalized = self._normalize_async_task_target_text(value)
        if not normalized:
            return ()
        tokens = re.findall(r"[a-z0-9_:/.-]+|[\u4e00-\u9fff]+", normalized)
        unique_tokens: list[str] = []
        for raw_token in tokens:
            token = str(raw_token or '').strip()
            if len(token) <= 1 or token in unique_tokens:
                continue
            unique_tokens.append(token)
        return tuple(unique_tokens)

    def _async_task_precheck_pool(self, session_id: str) -> list[dict[str, Any]]:
        pool: list[dict[str, Any]] = []
        for task in self.list_unfinished_tasks_for_session(session_id):
            metadata = task.metadata if isinstance(task.metadata, dict) else {}
            task_text = str(getattr(task, 'user_request', '') or '').strip()
            core_requirement = str(metadata.get('core_requirement') or '').strip()
            target_text = core_requirement or task_text
            pool.append(
                {
                    'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                    'task_text': task_text,
                    'core_requirement': core_requirement,
                    'execution_policy': dict(metadata.get('execution_policy') or {}),
                    'status': str(getattr(task, 'status', '') or '').strip(),
                    'is_paused': bool(getattr(task, 'is_paused', False)),
                    'target_text': self._normalize_async_task_target_text(target_text),
                    'keyword_fingerprint': self._async_task_keyword_fingerprint(target_text),
                }
            )
        return pool

    def _rule_precheck_async_task_creation(
        self,
        *,
        session_id: str,
        task_text: str,
        core_requirement: str,
        execution_policy: dict[str, Any] | None,
        requires_final_acceptance: bool,
        final_acceptance_prompt: str,
    ) -> dict[str, Any]:
        _ = execution_policy, requires_final_acceptance, final_acceptance_prompt
        candidate_target = self._normalize_async_task_target_text(core_requirement or task_text)
        candidate_keywords = self._async_task_keyword_fingerprint(core_requirement or task_text)
        for item in self._async_task_precheck_pool(session_id):
            if candidate_target and candidate_target == str(item.get('target_text') or '').strip():
                return {
                    'decision': 'reject_duplicate',
                    'matched_task_id': str(item.get('task_id') or '').strip(),
                    'reason': 'core_requirement exact match',
                    'decision_source': 'rule',
                }
            if candidate_keywords and candidate_keywords == tuple(item.get('keyword_fingerprint') or ()):
                return {
                    'decision': 'reject_duplicate',
                    'matched_task_id': str(item.get('task_id') or '').strip(),
                    'reason': 'keyword fingerprint exact match',
                    'decision_source': 'rule',
                }
        return {
            'decision': 'approve_new',
            'matched_task_id': '',
            'reason': 'rule precheck found no exact duplicate',
            'decision_source': 'rule',
        }

    def _unfinished_async_task_review_payload(self, session_id: str) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for item in self._async_task_precheck_pool(session_id):
            payload.append(
                {
                    'task_id': str(item.get('task_id') or '').strip(),
                    'task_text': str(item.get('task_text') or '').strip(),
                    'core_requirement': str(item.get('core_requirement') or '').strip(),
                    'execution_policy': dict(item.get('execution_policy') or {}),
                    'status': str(item.get('status') or '').strip(),
                    'is_paused': bool(item.get('is_paused', False)),
                }
            )
        return payload

    @staticmethod
    def _parse_async_task_duplicate_precheck_response(response: Any) -> dict[str, Any] | None:
        tool_calls = list(getattr(response, 'tool_calls', []) or [])
        for call in tool_calls:
            if isinstance(call, dict):
                name = str(call.get('name') or '').strip()
                arguments: Any = call.get('arguments')
            else:
                name = str(getattr(call, 'name', '') or '').strip()
                arguments = getattr(call, 'arguments', None)
            if name != 'review_async_task_duplicate_precheck':
                continue
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    return None
            if not isinstance(arguments, dict):
                return None
            decision = str(arguments.get('decision') or '').strip()
            if decision not in {'approve_new', 'reject_duplicate', 'reject_use_append_notice'}:
                return None
            matched_task_id = str(arguments.get('matched_task_id') or '').strip()
            if decision != 'approve_new' and not matched_task_id.startswith('task:'):
                return None
            return {
                'decision': decision,
                'matched_task_id': matched_task_id,
                'reason': str(arguments.get('reason') or '').strip(),
                'decision_source': 'llm',
            }
        return None

    async def _execute_async_task_duplicate_precheck_review(
        self,
        *,
        session_id: str,
        task_text: str,
        core_requirement: str,
        execution_policy: dict[str, Any] | None,
        requires_final_acceptance: bool,
        final_acceptance_prompt: str,
    ) -> dict[str, Any] | None:
        model_refs = list(self.node_runner._acceptance_model_refs or self.node_runner._execution_model_refs)
        if not model_refs:
            return None
        backend = self._chat_backend
        if backend is None or not callable(getattr(backend, 'chat', None)):
            return None
        response = await backend.chat(
            messages=[
                {'role': 'system', 'content': load_prompt('async_task_duplicate_precheck.md').strip()},
                {
                    'role': 'user',
                    'content': json.dumps(
                        {
                            'candidate_task': {
                                'task_text': str(task_text or '').strip(),
                                'core_requirement': str(core_requirement or '').strip(),
                                'execution_policy': dict(execution_policy or {}),
                                'requires_final_acceptance': bool(requires_final_acceptance),
                                'final_acceptance_prompt': str(final_acceptance_prompt or '').strip(),
                            },
                            'unfinished_session_tasks': self._unfinished_async_task_review_payload(session_id),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
            ],
            tools=[
                {
                    'type': 'function',
                    'function': {
                        'name': 'review_async_task_duplicate_precheck',
                        'description': 'Decide whether a new async task should be created or blocked.',
                        'parameters': {
                            'type': 'object',
                            'properties': {
                                'decision': {
                                    'type': 'string',
                                    'enum': ['approve_new', 'reject_duplicate', 'reject_use_append_notice'],
                                },
                                'matched_task_id': {'type': 'string'},
                                'reason': {'type': 'string'},
                            },
                            'required': ['decision', 'reason'],
                            'additionalProperties': False,
                        },
                    },
                }
            ],
            model_refs=model_refs,
        )
        return self._parse_async_task_duplicate_precheck_response(response)

    async def precheck_async_task_creation(
        self,
        *,
        session_id: str,
        task_text: str,
        core_requirement: str,
        execution_policy: dict[str, Any] | None,
        requires_final_acceptance: bool,
        final_acceptance_prompt: str,
    ) -> dict[str, Any]:
        normalized_session_id = self._normalize_session_key(session_id)
        rule_decision = self._rule_precheck_async_task_creation(
            session_id=normalized_session_id,
            task_text=task_text,
            core_requirement=core_requirement,
            execution_policy=dict(execution_policy or {}),
            requires_final_acceptance=bool(requires_final_acceptance),
            final_acceptance_prompt=str(final_acceptance_prompt or '').strip(),
        )
        if str(rule_decision.get('decision') or '').strip() != 'approve_new':
            return rule_decision
        if not self._async_task_precheck_pool(normalized_session_id):
            return rule_decision
        try:
            llm_decision = await self._execute_async_task_duplicate_precheck_review(
                session_id=normalized_session_id,
                task_text=task_text,
                core_requirement=core_requirement,
                execution_policy=dict(execution_policy or {}),
                requires_final_acceptance=bool(requires_final_acceptance),
                final_acceptance_prompt=str(final_acceptance_prompt or '').strip(),
            )
        except Exception:
            llm_decision = None
        if llm_decision is None:
            return {
                'decision': 'approve_new',
                'matched_task_id': '',
                'reason': 'llm review unavailable; allow new task',
                'decision_source': 'fallback',
            }
        return llm_decision

    async def task_append_notice(
        self,
        *,
        task_ids: list[str] | None,
        node_ids: list[str] | None,
        message: str,
        session_id: str,
    ) -> str:
        normalized_message = str(message or '').strip()
        normalized_session_id = self._normalize_session_key(session_id)
        if not normalized_message:
            raise ValueError('message_required')
        normalized_task_ids = self._normalize_task_id_list(task_ids or [], allow_empty=True)
        normalized_node_ids: list[str] = []
        seen_node_ids: set[str] = set()
        for raw_node_id in list(node_ids or []):
            node_id = str(raw_node_id or '').strip()
            if not node_id or node_id in seen_node_ids:
                continue
            seen_node_ids.add(node_id)
            normalized_node_ids.append(node_id)
        if not normalized_task_ids and not normalized_node_ids:
            raise ValueError('append_notice_targets_required')

        unfinished_tasks = {
            task.task_id: task
            for task in self.list_unfinished_tasks_for_session(normalized_session_id)
        }
        target_tasks: dict[str, TaskRecord] = {}
        for task_id in normalized_task_ids:
            task = unfinished_tasks.get(task_id)
            if task is None:
                raise ValueError('append_notice_invalid_task_target')
            target_tasks[task.task_id] = task
        for node_id in normalized_node_ids:
            node = self.store.get_node(node_id)
            if node is None or str(getattr(node, 'node_kind', '') or '').strip().lower() == 'acceptance':
                raise ValueError('append_notice_invalid_node_target')
            task = unfinished_tasks.get(str(getattr(node, 'task_id', '') or '').strip())
            if task is None:
                raise ValueError('append_notice_invalid_task_target')
            snapshot = self.query_service.get_tree_subtree(task.task_id, task.root_node_id)
            visible_ids = set((snapshot.nodes_by_id or {}).keys()) if snapshot is not None else set()
            if node_id not in visible_ids:
                raise ValueError('append_notice_invalid_node_target')
            target_tasks[task.task_id] = task
        if not target_tasks:
            raise ValueError('append_notice_invalid_task_target')

        updated_task_ids: list[str] = []
        for task_id, task in sorted(target_tasks.items()):
            current_state = self._task_distribution_state(task_id)
            active_epoch_id = str(current_state.get('active_epoch_id') or '').strip()
            active_state = str(current_state.get('state') or '').strip()
            queued_epoch: dict[str, Any] | None = None
            if active_epoch_id and active_state in {'pause_requested', 'barrier_requested'}:
                self._coalesce_pending_root_message(task_id=task_id, root_message=normalized_message)
            else:
                queued_epoch = self._queue_distribution_epoch(
                    task_id=task_id,
                    root_node_id=task.root_node_id,
                    root_message=normalized_message,
                )
            latest_task = self.get_task(task_id)
            if latest_task is not None and not bool(latest_task.pause_requested):
                await self.pause_task(task_id)
            self.log_service.update_task_runtime_meta(
                task_id,
                **self._distribution_runtime_meta_payload(task_id=task_id),
            )
            if queued_epoch is not None and str(queued_epoch.get('state') or '').strip() == 'pause_requested':
                await self._schedule_distribution_epoch(task)
            updated_task_ids.append(task_id)
        joined_task_ids = ', '.join(updated_task_ids)
        return f'已向任务 {joined_task_ids} 追加通知。'

    def _task_distribution_epochs(self, task_id: str) -> list[TaskMessageDistributionEpoch]:
        terminal_states = {'completed', 'failed', 'cancelled', 'cancelled_by_task_delete'}
        return [
            epoch
            for epoch in list(self.store.list_active_task_message_distribution_epochs(task_id) or [])
            if str(epoch.state or '').strip().lower() not in terminal_states
        ]

    def _task_distribution_state(self, task_id: str) -> dict[str, Any]:
        epochs = self._task_distribution_epochs(task_id)
        active = next(
            (
                epoch
                for epoch in epochs
                if str(epoch.state or '').strip() in {'pause_requested', 'paused', 'distributing', 'resuming'}
            ),
            None,
        )
        if active is None:
            active = next((epoch for epoch in epochs if str(epoch.state or '').strip() == 'queued'), None)
        queued_epoch_count = sum(
            1
            for epoch in epochs
            if str(epoch.state or '').strip() in {'queued', 'pause_requested'}
        )
        pending_mailbox_count = 0
        if active is not None:
            pending_mailbox_count = sum(
                1
                for item in list(self.store.list_task_epoch_notifications(task_id, active.epoch_id) or [])
                if str(item.status or '').strip() in {'pending_distribution', 'delivered'}
            )
        frontier_node_ids = []
        blocked_node_ids = []
        pending_notice_node_ids = []
        mode = ''
        state = str(active.state or '').strip() if active is not None else ''
        if active is not None and isinstance(active.payload, dict):
            frontier_node_ids = [
                str(item or '').strip()
                for item in list(active.payload.get('frontier_node_ids') or [])
                if str(item or '').strip()
            ]
            blocked_node_ids = [
                str(item or '').strip()
                for item in list(active.payload.get('barrier_node_ids') or [])
                if str(item or '').strip()
            ]
            if blocked_node_ids:
                mode = 'task_wide_barrier'
                if state == 'pause_requested':
                    state = 'barrier_requested'
                pending_notice_node_ids = [
                    str(item or '').strip()
                    for item in list(active.payload.get('pending_notice_node_ids') or [])
                    if str(item or '').strip()
                ]
                if not pending_notice_node_ids:
                    root_node_id = str(active.root_node_id or '').strip()
                    if root_node_id:
                        pending_notice_node_ids = [root_node_id]
        return {
            'active_epoch_id': str(active.epoch_id or '').strip() if active is not None else '',
            'state': state,
            'mode': mode,
            'frontier_node_ids': frontier_node_ids,
            'blocked_node_ids': blocked_node_ids,
            'pending_notice_node_ids': pending_notice_node_ids,
            'queued_epoch_count': queued_epoch_count,
            'pending_mailbox_count': pending_mailbox_count,
        }

    def _queue_distribution_epoch(self, *, task_id: str, root_node_id: str, root_message: str) -> dict[str, Any]:
        epochs = self._task_distribution_epochs(task_id)
        active_states = {'pause_requested', 'paused', 'distributing', 'resuming'}
        next_state = 'queued' if any(str(epoch.state or '').strip() in active_states for epoch in epochs) else 'pause_requested'
        epoch_id = new_command_id().replace('command:', 'epoch:', 1)
        barrier_node_ids = list(
            dict.fromkeys(
                list(getattr(self.node_runner, 'live_distribution_tree_node_ids', lambda **_: [])(task_id=task_id) or [])
            )
        )
        normalized_root_node_id = str(root_node_id or '').strip()
        if normalized_root_node_id and normalized_root_node_id not in barrier_node_ids:
            barrier_node_ids.insert(0, normalized_root_node_id)
        record = self.store.upsert_task_message_distribution_epoch(
            TaskMessageDistributionEpoch(
                epoch_id=epoch_id,
                task_id=task_id,
                root_node_id=normalized_root_node_id,
                root_message=str(root_message or '').strip(),
                state=next_state,
                created_at=now_iso(),
                payload={
                    'barrier_root_node_id': normalized_root_node_id,
                    'barrier_node_ids': list(barrier_node_ids),
                    'drain_pending_node_ids': list(barrier_node_ids),
                    'frontier_node_ids': [],
                    'queued_root_messages': [str(root_message or '').strip()],
                    'distributed_node_ids': [],
                    'decision_records': [],
                },
            )
        )
        return record.model_dump(mode='json')

    def _coalesce_pending_root_message(self, *, task_id: str, root_message: str) -> dict[str, Any]:
        epochs = self._task_distribution_epochs(task_id)
        target = next(
            (epoch for epoch in epochs if str(epoch.state or '').strip() == 'pause_requested'),
            None,
        )
        if target is None:
            return self._queue_distribution_epoch(
                task_id=task_id,
                root_node_id=str((self.get_task(task_id) or SimpleNamespace(root_node_id='')).root_node_id or '').strip(),
                root_message=root_message,
            )
        payload = dict(target.payload or {})
        queued_root_messages = [
            str(item or '').strip()
            for item in list(payload.get('queued_root_messages') or [])
            if str(item or '').strip()
        ]
        if not queued_root_messages:
            queued_root_messages.append(str(target.root_message or '').strip())
        queued_root_messages.append(str(root_message or '').strip())
        payload['queued_root_messages'] = queued_root_messages
        updated = target.model_copy(update={'payload': payload})
        stored = self.store.upsert_task_message_distribution_epoch(updated)
        return stored.model_dump(mode='json')

    def _distribution_runtime_meta_payload(self, *, task_id: str) -> dict[str, Any]:
        return {
            'distribution': self._task_distribution_state(task_id),
        }

    def _cancel_distribution_for_force_delete(self, *, task_id: str, reason: str) -> None:
        epochs = self._task_distribution_epochs(task_id)
        for epoch in epochs:
            updated_epoch = epoch.model_copy(
                update={
                    'state': 'cancelled_by_task_delete',
                    'error_text': str(reason or '').strip(),
                    'completed_at': now_iso(),
                }
            )
            self.store.upsert_task_message_distribution_epoch(updated_epoch)
            for notification in list(self.store.list_task_epoch_notifications(task_id, epoch.epoch_id) or []):
                updated_notification = notification.model_copy(
                    update={
                        'status': 'cancelled',
                        'consumed_at': str(notification.consumed_at or now_iso()).strip() or now_iso(),
                    }
                )
                self.store.upsert_task_node_notification(updated_notification)
        self.log_service.update_task_runtime_meta(
            task_id,
            distribution={
                'active_epoch_id': '',
                'state': '',
                'mode': '',
                'frontier_node_ids': [],
                'blocked_node_ids': [],
                'pending_notice_node_ids': [],
                'queued_epoch_count': 0,
                'pending_mailbox_count': 0,
            },
        )

    async def _schedule_distribution_epoch(self, task: TaskRecord) -> None:
        if self.execution_mode in {'embedded', 'worker'}:
            await self.global_scheduler.enqueue_task(task.task_id)
            return
        self._enqueue_task_command(
            command_type='resume_task',
            task_id=task.task_id,
            session_id=task.session_id,
            payload={'task_id': task.task_id},
        )

    def _deliver_distribution_message(
        self,
        *,
        task_id: str,
        epoch_id: str,
        source_node_id: str,
        target_node_id: str,
        message: str,
    ) -> dict[str, Any]:
        target = self.store.get_node(target_node_id)
        if target is None or str(target.task_id or '').strip() != str(task_id or '').strip():
            raise ValueError('distribution_target_missing')
        if str(target.node_kind or '').strip().lower() != 'execution':
            raise ValueError('distribution_target_must_be_execution_node')
        notification = self.store.upsert_task_node_notification(
            next(
                (
                    item
                    for item in list(self.store.list_task_node_notifications(task_id, target_node_id) or [])
                    if str(item.epoch_id or '').strip() == str(epoch_id or '').strip()
                    and str(item.source_node_id or '').strip() == str(source_node_id or '').strip()
                    and str(item.message or '').strip() == str(message or '').strip()
                ),
                TaskNodeNotification(
                    notification_id=new_command_id().replace('command:', 'notif:', 1),
                    task_id=str(task_id or '').strip(),
                    node_id=str(target_node_id or '').strip(),
                    epoch_id=str(epoch_id or '').strip(),
                    source_node_id=str(source_node_id or '').strip(),
                    message=str(message or '').strip(),
                    status='delivered',
                    created_at=now_iso(),
                    delivered_at=now_iso(),
                    consumed_at='',
                    payload={},
                ),
            )
        )
        self._reactivate_execution_node_for_distribution(
            task_id=task_id,
            node_id=target_node_id,
            epoch_id=epoch_id,
        )
        self._invalidate_acceptance_for_reactivated_execution_node(
            task_id=task_id,
            execution_node_id=target_node_id,
            epoch_id=epoch_id,
        )
        self.log_service.update_task_runtime_meta(
            task_id,
            **self._distribution_runtime_meta_payload(task_id=task_id),
        )
        return notification.model_dump(mode='json')

    def _reactivate_execution_node_for_distribution(
        self,
        *,
        task_id: str,
        node_id: str,
        epoch_id: str,
    ) -> None:
        _ = epoch_id
        node = self.store.get_node(node_id)
        if node is None or str(node.task_id or '').strip() != str(task_id or '').strip():
            return
        if str(node.node_kind or '').strip().lower() != 'execution':
            return
        if str(node.status or '').strip().lower() not in {'success', 'failed'}:
            return
        updated = self.store.update_node(
            node_id,
            lambda record: record.model_copy(
                update={
                    'status': 'in_progress',
                    'updated_at': now_iso(),
                    'final_output': '',
                    'final_output_ref': '',
                    'failure_reason': '',
                    'check_result': '',
                    'check_result_ref': '',
                }
            ),
        )
        if updated is not None:
            self.log_service.sync_node_read_model(task_id, node_id)
            self.log_service.refresh_task_view(task_id, mark_unread=True)

    def _invalidate_acceptance_for_reactivated_execution_node(
        self,
        *,
        task_id: str,
        execution_node_id: str,
        epoch_id: str,
    ) -> None:
        execution_node = self.store.get_node(execution_node_id)
        if execution_node is None or str(execution_node.task_id or '').strip() != str(task_id or '').strip():
            return
        metadata = dict(execution_node.metadata or {})
        owner_parent_node_id = str(metadata.get('spawn_owner_parent_node_id') or '').strip()
        owner_round_id = str(metadata.get('spawn_owner_round_id') or '').strip()
        owner_entry_index = int(metadata.get('spawn_owner_entry_index') or 0)
        if not owner_parent_node_id or not owner_round_id:
            return
        parent = self.store.get_node(owner_parent_node_id)
        if parent is None or str(parent.task_id or '').strip() != str(task_id or '').strip():
            return
        operations = dict((parent.metadata or {}).get('spawn_operations') or {})
        payload = dict(operations.get(owner_round_id) or {})
        entries = list(payload.get('entries') or [])
        if owner_entry_index < 0 or owner_entry_index >= len(entries) or not isinstance(entries[owner_entry_index], dict):
            return
        entry = dict(entries[owner_entry_index] or {})
        acceptance_node_id = str(entry.get('acceptance_node_id') or '').strip()
        if not acceptance_node_id:
            return
        self.log_service.invalidate_acceptance_node(
            task_id,
            acceptance_node_id,
            epoch_id=epoch_id,
            reason='task_message_distribution',
        )

    def list_active_task_snapshots_for_session(self, session_id: str, *, limit: int = 3) -> list[dict[str, str]]:
        unfinished = list(self.list_unfinished_tasks_for_session(session_id))
        unfinished.sort(
            key=lambda item: (
                str(getattr(item, 'updated_at', '') or ''),
                str(getattr(item, 'created_at', '') or ''),
                str(getattr(item, 'task_id', '') or ''),
            ),
            reverse=True,
        )
        snapshots: list[dict[str, str]] = []
        for task in unfinished[: max(1, int(limit or 3))]:
            metadata = task.metadata if isinstance(task.metadata, dict) else {}
            item = {
                'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                'title': str(getattr(task, 'title', '') or '').strip(),
                'core_requirement': str(metadata.get('core_requirement') or '').strip(),
                'status': str(getattr(task, 'status', '') or '').strip(),
                'updated_at': str(getattr(task, 'updated_at', '') or '').strip(),
            }
            snapshots.append({key: value for key, value in item.items() if value})
        return snapshots

    def get_session_task_counts(self, session_id: str) -> dict[str, int]:
        tasks = self.list_tasks_for_session(session_id)
        in_progress = 0
        paused = 0
        terminal = 0
        for task in tasks:
            status = str(getattr(task, 'status', '') or '').strip().lower()
            if status != 'in_progress':
                terminal += 1
            elif bool(getattr(task, 'is_paused', False)):
                paused += 1
            else:
                in_progress += 1
        return {
            'total': len(tasks),
            'unfinished': in_progress,
            'in_progress': in_progress,
            'paused': paused,
            'terminal': terminal,
            'deletable': terminal + paused,
        }

    async def delete_task_records_for_session(self, session_id: str) -> int:
        deleted = 0
        for task in list(self.list_tasks_for_session(session_id)):
            try:
                delete_helper = getattr(self, '_delete_task_with_task_delete_semantics', None)
                if callable(delete_helper):
                    removed = await delete_helper(task.task_id)
                else:
                    status = str(getattr(task, 'status', '') or '').strip().lower()
                    if status == 'in_progress' and not bool(getattr(task, 'is_paused', False)):
                        pause = getattr(self, 'pause_task', None)
                        if callable(pause):
                            await pause(task.task_id)
                    removed = await self.delete_task(task.task_id)
            except ValueError as exc:
                if str(exc) in {'task_not_paused', 'task_still_stopping'}:
                    continue
                raise
            if removed is not None:
                deleted += 1
        return deleted

    def get_node(self, node_id: str) -> NodeRecord | None:
        return self.store.get_node(node_id)

    def list_nodes(self, task_id: str) -> list[NodeRecord]:
        task_id = self.normalize_task_id(task_id)
        return self.store.list_nodes(task_id)

    def normalize_task_id(self, task_id: str) -> str:
        raw = str(task_id or '').strip()
        if not raw or raw.startswith('task:') or ':' in raw:
            return raw
        return f'task:{raw}'

    def bind_resource_manager(self, resource_manager) -> None:
        self._resource_manager = resource_manager
        self.resource_registry.bind_resource_manager(resource_manager)
        self._record_resource_tree_state()

    def bind_runtime_loop(self, loop: Any | None) -> None:
        self._runtime_loop = loop
        if loop is None:
            return
        manager = getattr(loop, 'tool_execution_manager', None)
        if manager is None:
            setattr(loop, 'tool_execution_manager', self.tool_execution_manager)
            manager = self.tool_execution_manager
        self.tool_execution_manager = manager
        if hasattr(self, '_react_loop') and self._react_loop is not None:
            setattr(self._react_loop, '_tool_execution_manager', None)

    @staticmethod
    def _stall_now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec='microseconds')

    def _enqueue_task_terminal_callback(self, task: TaskRecord) -> None:
        if self.execution_mode != 'worker':
            return
        payload = enrich_task_terminal_payload(
            build_task_terminal_payload(task),
            task=task,
            node_detail_getter=self.get_node_detail_payload,
        )
        if not payload:
            return
        dedupe_key = str(payload.get('dedupe_key') or '').strip()
        if not dedupe_key:
            return
        created_at = str(payload.get('finished_at') or now_iso()).strip() or now_iso()
        try:
            self.store.put_task_terminal_outbox(
                dedupe_key=dedupe_key,
                task_id=str(payload.get('task_id') or '').strip(),
                session_id=str(payload.get('session_id') or '').strip() or 'web:shared',
                created_at=created_at,
                payload=payload,
            )
        except Exception:
            logger.exception('failed to persist task terminal outbox for {}', dedupe_key)
            return
        self._schedule_task_terminal_delivery(dedupe_key)

    def emit_task_stall(self, payload: dict[str, Any] | None) -> bool:
        normalized = normalize_task_stall_payload(payload)
        if not normalized:
            return False
        if self.execution_mode == 'worker':
            self._enqueue_task_stall_callback(normalized)
            return True
        loop = getattr(self, '_runtime_loop', None)
        heartbeat = getattr(loop, 'web_session_heartbeat', None) if loop is not None else None
        if heartbeat is None or not hasattr(heartbeat, 'enqueue_task_stall_payload'):
            return False
        return bool(heartbeat.enqueue_task_stall_payload(normalized))

    def classify_task_stall_reason(
        self,
        task_id: str,
        *,
        runtime_state: dict[str, Any] | None = None,
    ) -> str:
        task = self.get_task(task_id)
        if task is None:
            return TASK_STALL_REASON_MISSING_TASK
        if str(getattr(task, 'status', '') or '').strip().lower() != 'in_progress':
            return TASK_STALL_REASON_NOT_IN_PROGRESS
        current_runtime_state = runtime_state if isinstance(runtime_state, dict) else (self.log_service.read_runtime_state(task.task_id) or {})
        if bool(getattr(task, 'is_paused', False)) or bool(getattr(task, 'pause_requested', False)):
            return TASK_STALL_REASON_USER_PAUSED
        if bool(current_runtime_state.get('paused')) or bool(current_runtime_state.get('pause_requested')):
            return TASK_STALL_REASON_USER_PAUSED
        if bool(getattr(task, 'cancel_requested', False)):
            return TASK_STALL_REASON_CANCEL_REQUESTED
        if bool(current_runtime_state.get('cancel_requested')):
            return TASK_STALL_REASON_CANCEL_REQUESTED
        governance = dict((self.log_service.read_task_runtime_meta(task.task_id) or {}).get('governance') or {})
        if bool(governance.get('review_inflight')) or bool(governance.get('frozen')):
            return TASK_STALL_REASON_NOT_IN_PROGRESS
        if self.execution_mode == 'web':
            if not self.is_worker_online():
                return TASK_STALL_REASON_WORKER_UNAVAILABLE
        return TASK_STALL_REASON_SUSPECTED_STALL

    def is_task_stall_actionable(
        self,
        task_id: str,
        *,
        runtime_state: dict[str, Any] | None = None,
    ) -> bool:
        return self.classify_task_stall_reason(
            task_id,
            runtime_state=runtime_state,
        ) == TASK_STALL_REASON_SUSPECTED_STALL

    def build_task_stall_payload(
        self,
        task_id: str,
        *,
        bucket_minutes: int,
        last_visible_output_at: str | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            return {}
        origin_session_id = self._task_origin_session_id(task)
        if not origin_session_id.startswith('web:'):
            return {}
        runtime_state = self.log_service.read_runtime_state(task.task_id) or {}
        stall_reason = self.classify_task_stall_reason(task.task_id, runtime_state=runtime_state)
        if stall_reason != TASK_STALL_REASON_SUSPECTED_STALL:
            return {}
        visible_at = str(last_visible_output_at or runtime_state.get('last_visible_output_at') or task.created_at or '').strip()
        minute_seconds = float(getattr(self.task_stall_notifier, 'minute_seconds', 60.0) or 60.0)
        current_bucket = stall_bucket_minutes(visible_at, minute_seconds=minute_seconds)
        if current_bucket <= 0:
            return {}
        active_bucket = max(current_bucket, max(0, int(bucket_minutes or 0)))
        stalled_minutes = max(
            stalled_minutes_since(visible_at, minute_seconds=minute_seconds),
            active_bucket,
        )
        detail = self.get_task_detail_payload(task.task_id, mark_read=False) or {}
        return normalize_task_stall_payload(
            {
                'task_id': task.task_id,
                'session_id': origin_session_id,
                'title': str(getattr(task, 'title', '') or task.task_id).strip() or task.task_id,
                'reason': stall_reason,
                'stalled_minutes': stalled_minutes,
                'bucket_minutes': active_bucket,
                'last_visible_output_at': visible_at,
                'brief_text': str(getattr(task, 'brief_text', '') or '').strip(),
                'latest_node_summary': self._task_stall_latest_node_summary(detail),
                'runtime_summary_excerpt': self._task_stall_runtime_summary(detail),
            }
        )

    def _task_stall_latest_node_summary(self, detail: dict[str, Any]) -> str:
        payload = detail if isinstance(detail, dict) else {}
        task_id = str((payload.get('task') or {}).get('task_id') or '').strip()
        root_node = payload.get('root_node') if isinstance(payload.get('root_node'), dict) else {}
        frontier = [item for item in list(payload.get('frontier') or []) if isinstance(item, dict)]
        candidate = root_node
        frontier_node_id = str(frontier[0].get('node_id') or '').strip() if frontier else ''
        root_node_id = str(root_node.get('node_id') or '').strip()
        if task_id and frontier_node_id and frontier_node_id != root_node_id:
            detail_payload = self.get_node_detail_payload(task_id, frontier_node_id) or {}
            candidate = detail_payload.get('item') if isinstance(detail_payload.get('item'), dict) else candidate
        if not candidate:
            return ''
        title = str(self._repair_legacy_display_text(candidate.get('goal') or candidate.get('title') or candidate.get('node_id') or 'node')).strip() or 'node'
        status = str(candidate.get('status') or 'in_progress').strip() or 'in_progress'
        output = str(self._repair_legacy_display_text(
            candidate.get('final_output')
            or candidate.get('output')
            or candidate.get('failure_reason')
            or ''
        )).strip()
        if not output and frontier:
            output = str(self._repair_legacy_display_text(frontier[0].get('stage_goal') or frontier[0].get('phase') or '')).strip()
        text = f'{title} [{status}]'
        if output:
            text = f'{text}: {output}'
        return text[:240]

    def _task_stall_runtime_summary(self, detail: dict[str, Any]) -> str:
        payload = detail if isinstance(detail, dict) else {}
        frames = [item for item in list(payload.get('frontier') or []) if isinstance(item, dict)]
        parts: list[str] = []
        for frame in frames[:3]:
            node_id = str(frame.get('node_id') or '').strip() or 'node'
            phase = str(frame.get('phase') or '').strip() or 'waiting'
            tool_calls = [item for item in list(frame.get('tool_calls') or []) if isinstance(item, dict)]
            child_pipelines = [item for item in list(frame.get('child_pipelines') or []) if isinstance(item, dict)]
            running_tools = sum(
                1
                for item in tool_calls
                if str(item.get('status') or '').strip().lower() in {'queued', 'running'}
            )
            running_children = sum(
                1
                for item in child_pipelines
                if str(item.get('status') or '').strip().lower() in {'queued', 'running'}
            )
            summary = f'{node_id} phase={phase}'
            if tool_calls:
                summary = f'{summary} tools={running_tools}/{len(tool_calls)}'
            if child_pipelines:
                summary = f'{summary} children_running={running_children}/{len(child_pipelines)}'
            parts.append(summary)
        return '; '.join(parts)[:320]

    def _enqueue_task_worker_status_callback(self, payload: dict[str, Any] | None) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_event_payload(
            {
                'event_type': 'task.worker.status',
                'session_id': 'all',
                'data': dict(payload or {}),
            }
        )
        if not normalized:
            return
        data = dict(normalized.get('data') or {})
        worker = dict(data.get('worker') or {}) if isinstance(data.get('worker'), dict) else {}
        worker_id = str(worker.get('worker_id') or '').strip()
        if not worker_id:
            return
        created_at = str(worker.get('updated_at') or now_iso()).strip() or now_iso()
        try:
            self.store.put_task_worker_status_outbox(
                worker_id=worker_id,
                created_at=created_at,
                payload=normalized,
            )
        except Exception:
            logger.exception('failed to persist worker status outbox for {}', worker_id)
            return
        self._schedule_task_worker_status_delivery(worker_id)

    def _summary_stats_snapshot(self) -> dict[str, float]:
        snapshot = {key: float(value or 0.0) for key, value in dict(self._task_summary_stats or {}).items()}
        snapshot['task_summary_pending_count'] = float(len(self._pending_task_summaries))
        return snapshot

    def _increment_summary_stat(self, key: str, amount: float = 1.0) -> None:
        normalized_key = str(key or '').strip()
        if not normalized_key:
            return
        current = float(self._task_summary_stats.get(normalized_key, 0.0) or 0.0)
        self._task_summary_stats[normalized_key] = current + float(amount or 0.0)

    @staticmethod
    def _summary_payload_task_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
        data = dict((payload or {}).get('data') or {})
        return dict(data.get('task') or {}) if isinstance(data.get('task'), dict) else {}

    def _summary_payload_requires_immediate(self, task_id: str, task_payload: dict[str, Any]) -> bool:
        previous = dict(self._last_summary_payloads.get(task_id) or {})
        if not previous:
            return True
        for key in ('status', 'is_paused', 'is_unread'):
            if previous.get(key) != task_payload.get(key):
                return True
        return False

    def _ensure_task_summary_flush_task(self, task_id: str) -> None:
        key = self.normalize_task_id(str(task_id or '').strip())
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_summary_flush_tasks.get(key)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._flush_task_summary_to_outbox(key)
            return
        task = loop.create_task(self._run_task_summary_flush(key), name=f'main-runtime-task-summary-flush:{key}')
        self._task_summary_flush_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_summary_flush_task(stored_key, done_task))

    def _clear_task_summary_flush_task(self, task_id: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_summary_flush_tasks.get(task_id)
        if current is done_task:
            self._task_summary_flush_tasks.pop(task_id, None)

    def _enqueue_task_summary_callback(self, payload: dict[str, Any] | None, *, immediate: bool = False) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_event_payload(payload)
        if not normalized or str(normalized.get('event_type') or '').strip() != 'task.summary.patch':
            return
        task_id = self.normalize_task_id(str(normalized.get('task_id') or '').strip())
        if not task_id:
            return
        task_payload = self._summary_payload_task_payload(normalized)
        if not task_payload:
            return
        self._increment_summary_stat('task_summary_dirty_count')
        if immediate or self._summary_payload_requires_immediate(task_id, task_payload):
            self._flush_task_summary_to_outbox(task_id, normalized)
            return
        now_mono = time.monotonic()
        current = self._pending_task_summaries.get(task_id)
        if current is None:
            self._pending_task_summaries[task_id] = {
                'payload': normalized,
                'first_dirty_mono': now_mono,
                'last_dirty_mono': now_mono,
            }
        else:
            current['payload'] = normalized
            current['last_dirty_mono'] = now_mono
        self._ensure_task_summary_flush_task(task_id)

    def _schedule_pending_task_worker_status_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_worker_status_outbox(limit=500):
            worker_id = str(entry.get('worker_id') or '').strip()
            if worker_id:
                self._schedule_task_worker_status_delivery(worker_id)

    def _schedule_pending_task_summary_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_summary_outbox(limit=500):
            task_id = str(entry.get('task_id') or '').strip()
            if task_id:
                self._schedule_task_summary_delivery(task_id)

    async def _run_task_summary_flush(self, task_id: str) -> None:
        key = self.normalize_task_id(str(task_id or '').strip())
        if not key:
            return
        while True:
            current = dict(self._pending_task_summaries.get(key) or {})
            if not current:
                return
            first_dirty_mono = float(current.get('first_dirty_mono') or time.monotonic())
            last_dirty_mono = float(current.get('last_dirty_mono') or first_dirty_mono)
            due_mono = min(
                last_dirty_mono + _TASK_SUMMARY_DEBOUNCE_SECONDS,
                first_dirty_mono + _TASK_SUMMARY_MAX_WAIT_SECONDS,
            )
            delay_seconds = max(0.0, due_mono - time.monotonic())
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
                continue
            self._flush_task_summary_to_outbox(key)

    def _flush_task_summary_to_outbox(self, task_id: str, payload: dict[str, Any] | None = None) -> None:
        key = self.normalize_task_id(str(task_id or '').strip())
        if self.execution_mode != 'worker' or not key:
            return
        pending_entry = self._pending_task_summaries.pop(key, None)
        normalized = normalize_task_event_payload(payload) if payload is not None else dict(
            (pending_entry or {}).get('payload') or {}
        )
        if not normalized or str(normalized.get('event_type') or '').strip() != 'task.summary.patch':
            return
        session_id = str(normalized.get('session_id') or 'web:shared').strip() or 'web:shared'
        task_payload = self._summary_payload_task_payload(normalized)
        created_at = str(task_payload.get('updated_at') or now_iso()).strip() or now_iso()
        try:
            self.store.put_task_summary_outbox(
                task_id=key,
                session_id=session_id,
                created_at=created_at,
                payload=normalized,
            )
        except Exception:
            logger.exception('failed to persist task summary outbox for {}', key)
            return
        self._last_summary_payloads[key] = dict(task_payload)
        self._increment_summary_stat('task_summary_flush_count')
        self._increment_summary_stat('task_summary_outbox_write_count')
        self._schedule_task_summary_delivery(key)

    def _schedule_task_summary_delivery(self, _task_id: str | None = None) -> None:
        if self.execution_mode != 'worker':
            return
        current = self._task_summary_delivery_task
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task_summary_delivery_task = loop.create_task(
            self._deliver_task_summary_batches(),
            name=f'main-runtime-task-summary-batch:{self.worker_id or "worker"}',
        )

    async def _deliver_task_summary_batches(self) -> None:
        retry_delays = list(_WORKER_STATUS_CALLBACK_RETRY_DELAYS or [0.0, 0.5, 2.0, 5.0])
        if not retry_delays:
            retry_delays = [0.0]
        attempt_index = 0
        while True:
            delay_seconds = _TASK_SUMMARY_BATCH_WINDOW_SECONDS if attempt_index == 0 else float(
                retry_delays[min(attempt_index, len(retry_delays) - 1)] or 0.0
            )
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entries = list(self.store.list_pending_task_summary_outbox(limit=max(_TASK_SUMMARY_BATCH_MAX_ITEMS * 2, 256)) or [])
            if not entries:
                return
            batch_entries: list[dict[str, Any]] = []
            batch_items: list[dict[str, Any]] = []
            bytes_total = 0
            for entry in entries:
                payload = dict(entry.get('payload') or {})
                encoded_size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                if batch_entries and (
                    len(batch_entries) >= _TASK_SUMMARY_BATCH_MAX_ITEMS
                    or bytes_total + encoded_size > _TASK_SUMMARY_BATCH_MAX_BYTES
                ):
                    break
                batch_entries.append(entry)
                batch_items.append(payload)
                bytes_total += encoded_size
            if not batch_entries:
                return
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='event_batch')
            if not callback_targets:
                for entry in batch_entries:
                    self.store.mark_task_summary_outbox_attempt(
                        str(entry.get('task_id') or ''),
                        attempted_at=now_iso(),
                        error_text='task_event_batch_callback_url_unavailable',
                        expected_version=int(entry.get('version') or 0),
                    )
                attempt_index += 1
                continue
            error_text = 'task_event_batch_callback_failed'
            delivered = False
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload={'items': batch_items},
                        headers=headers,
                        timeout=_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        for entry in batch_entries:
                            self.store.mark_task_summary_outbox_delivered(
                                str(entry.get('task_id') or ''),
                                delivered_at=now_iso(),
                                expected_version=int(entry.get('version') or 0),
                            )
                        self._increment_summary_stat('task_summary_batch_request_count')
                        self._increment_summary_stat('task_summary_batch_item_count', float(len(batch_entries)))
                        attempt_index = 0
                        delivered = True
                        break
                    error_text = f'task_event_batch_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_event_batch_callback_failed').strip() or 'task_event_batch_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            if delivered:
                continue
            for entry in batch_entries:
                self.store.mark_task_summary_outbox_attempt(
                    str(entry.get('task_id') or ''),
                    attempted_at=now_iso(),
                    error_text=error_text,
                    expected_version=int(entry.get('version') or 0),
                )
            attempt_index += 1

    async def _deliver_task_summary_outbox(self, task_id: str) -> None:
        key = self.normalize_task_id(str(task_id or '').strip())
        if not key:
            return
        if self.store.get_task_summary_outbox(key) is None:
            return
        await self._deliver_task_summary_batches()

    def _schedule_task_worker_status_delivery(self, worker_id: str) -> None:
        key = str(worker_id or '').strip()
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_worker_status_delivery_tasks.get(key)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._deliver_task_worker_status_outbox(key), name=f'main-runtime-worker-status:{key}')
        self._task_worker_status_delivery_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_worker_status_delivery_task(stored_key, done_task))

    def _clear_task_worker_status_delivery_task(self, worker_id: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_worker_status_delivery_tasks.get(worker_id)
        if current is done_task:
            self._task_worker_status_delivery_tasks.pop(worker_id, None)

    async def _deliver_task_worker_status_outbox(self, worker_id: str) -> None:
        for delay_seconds in _WORKER_STATUS_CALLBACK_RETRY_DELAYS:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entry = self.store.get_task_worker_status_outbox(worker_id)
            if not entry:
                return
            if str(entry.get('delivery_state') or '').strip().lower() == 'delivered':
                return
            payload = dict(entry.get('payload') or {})
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='event')
            if not callback_targets:
                self.store.mark_task_worker_status_outbox_attempt(
                    worker_id,
                    attempted_at=now_iso(),
                    error_text='task_event_callback_url_unavailable',
                )
                return
            error_text = 'task_event_callback_failed'
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload=payload,
                        headers=headers,
                        timeout=_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        self.store.mark_task_worker_status_outbox_delivered(worker_id, delivered_at=now_iso())
                        return
                    error_text = f'task_event_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_event_callback_failed').strip() or 'task_event_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            self.store.mark_task_worker_status_outbox_attempt(
                worker_id,
                attempted_at=now_iso(),
                error_text=error_text,
            )

    def _enqueue_task_stall_callback(self, payload: dict[str, Any]) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_stall_payload(payload)
        if not normalized:
            return
        dedupe_key = str(normalized.get('dedupe_key') or '').strip()
        if not dedupe_key:
            return
        created_at = str(normalized.get('last_visible_output_at') or now_iso()).strip() or now_iso()
        try:
            self.store.put_task_stall_outbox(
                dedupe_key=dedupe_key,
                task_id=str(normalized.get('task_id') or '').strip(),
                session_id=str(normalized.get('session_id') or '').strip() or 'web:shared',
                created_at=created_at,
                payload=normalized,
            )
        except Exception:
            logger.exception('failed to persist task stall outbox for {}', dedupe_key)
            return
        self._schedule_task_stall_delivery(dedupe_key)

    def _schedule_pending_task_stall_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_stall_outbox(limit=500):
            dedupe_key = str(entry.get('dedupe_key') or '').strip()
            if dedupe_key:
                self._schedule_task_stall_delivery(dedupe_key)

    def _schedule_task_stall_delivery(self, dedupe_key: str) -> None:
        key = str(dedupe_key or '').strip()
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_stall_delivery_tasks.get(key)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._deliver_task_stall_outbox(key), name=f'main-runtime-task-stall:{key}')
        self._task_stall_delivery_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_stall_delivery_task(stored_key, done_task))

    def _clear_task_stall_delivery_task(self, dedupe_key: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_stall_delivery_tasks.get(dedupe_key)
        if current is done_task:
            self._task_stall_delivery_tasks.pop(dedupe_key, None)

    async def _deliver_task_stall_outbox(self, dedupe_key: str) -> None:
        retry_delays = [0.0, 0.5, 2.0, 5.0]
        for delay_seconds in retry_delays:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entry = self.store.get_task_stall_outbox(dedupe_key)
            if not entry:
                return
            if str(entry.get('delivery_state') or '').strip().lower() == 'delivered':
                return
            payload = dict(entry.get('payload') or {})
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='stall')
            if not callback_targets:
                self.store.mark_task_stall_outbox_attempt(
                    dedupe_key,
                    attempted_at=now_iso(),
                    error_text='task_stall_callback_url_unavailable',
                )
                return
            error_text = 'task_stall_callback_failed'
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload=payload,
                        headers=headers,
                        timeout=2.0,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        self.store.mark_task_stall_outbox_delivered(dedupe_key, delivered_at=now_iso())
                        return
                    error_text = f'task_stall_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_stall_callback_failed').strip() or 'task_stall_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            self.store.mark_task_stall_outbox_attempt(
                dedupe_key,
                attempted_at=now_iso(),
                error_text=error_text,
            )

    def _schedule_pending_task_terminal_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_terminal_outbox(limit=500):
            dedupe_key = str(entry.get('dedupe_key') or '').strip()
            if dedupe_key:
                self._schedule_task_terminal_delivery(dedupe_key)

    def _schedule_task_terminal_delivery(self, dedupe_key: str) -> None:
        key = str(dedupe_key or '').strip()
        if self.execution_mode != 'worker' or not key:
            return
        current = self._task_terminal_delivery_tasks.get(key)
        if current is not None and not current.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._deliver_task_terminal_outbox(key), name=f'main-runtime-task-terminal:{key}')
        self._task_terminal_delivery_tasks[key] = task
        task.add_done_callback(lambda done_task, stored_key=key: self._clear_task_terminal_delivery_task(stored_key, done_task))

    def _clear_task_terminal_delivery_task(self, dedupe_key: str, done_task: asyncio.Task[Any]) -> None:
        current = self._task_terminal_delivery_tasks.get(dedupe_key)
        if current is done_task:
            self._task_terminal_delivery_tasks.pop(dedupe_key, None)

    async def _deliver_task_terminal_outbox(self, dedupe_key: str) -> None:
        retry_delays = [0.0, 0.5, 2.0, 5.0]
        for delay_seconds in retry_delays:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            entry = self.store.get_task_terminal_outbox(dedupe_key)
            if not entry:
                return
            if str(entry.get('delivery_state') or '').strip().lower() == 'delivered':
                return
            payload = dict(entry.get('payload') or {})
            workspace = Path.cwd()
            callback_targets = self._internal_callback_targets(workspace=workspace, callback_kind='terminal')
            if not callback_targets:
                self.store.mark_task_terminal_outbox_attempt(
                    dedupe_key,
                    attempted_at=now_iso(),
                    error_text='task_terminal_callback_url_unavailable',
                )
                return
            error_text = 'task_terminal_callback_failed'
            for index, (callback_url, callback_token) in enumerate(callback_targets):
                headers = self._callback_headers(token=callback_token)
                try:
                    response = await self._post_internal_callback(
                        callback_url,
                        payload=payload,
                        headers=headers,
                        timeout=2.0,
                    )
                    if 200 <= int(response.status_code or 0) < 300:
                        self.store.mark_task_terminal_outbox_delivered(dedupe_key, delivered_at=now_iso())
                        return
                    error_text = f'task_terminal_callback_http_{int(response.status_code or 0)}'
                    if int(response.status_code or 0) in {401, 403, 404} and index + 1 < len(callback_targets):
                        continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error_text = str(exc or 'task_terminal_callback_failed').strip() or 'task_terminal_callback_failed'
                    if index + 1 < len(callback_targets):
                        continue
                break
            self.store.mark_task_terminal_outbox_attempt(
                dedupe_key,
                attempted_at=now_iso(),
                error_text=error_text,
            )

    @staticmethod
    def _node_parallelism_settings(config: Any | None) -> tuple[bool, int | None, int | None]:
        agents = getattr(config, 'agents', None) if config is not None else None
        parallelism = getattr(agents, 'node_parallelism', None) if agents is not None else None
        enabled = bool(getattr(parallelism, 'enabled', True)) if parallelism is not None else True
        max_parallel_tool_calls = MainRuntimeService._normalize_optional_parallel_limit(
            getattr(parallelism, 'max_parallel_tool_calls_per_node', None) if parallelism is not None else None
        )
        max_parallel_child_pipelines = MainRuntimeService._normalize_optional_parallel_limit(
            getattr(parallelism, 'max_parallel_child_pipelines_per_node', None) if parallelism is not None else None
        )
        if not enabled:
            return False, 1, 1
        return True, max_parallel_tool_calls, max_parallel_child_pipelines

    @staticmethod
    def _node_dispatch_concurrency_settings(config: Any | None) -> dict[str, int]:
        if config is not None and hasattr(config, 'get_node_dispatch_concurrency'):
            try:
                execution = int(config.get_node_dispatch_concurrency('execution'))
                inspection = int(config.get_node_dispatch_concurrency('inspection'))
                return {
                    'execution': max(1, execution),
                    'inspection': max(1, inspection),
                }
            except Exception:
                pass
        main_runtime = getattr(config, 'main_runtime', None) if config is not None else None
        dispatch_config = getattr(main_runtime, 'node_dispatch_concurrency', None) if main_runtime is not None else None
        execution = getattr(dispatch_config, 'execution', 8) if dispatch_config is not None else 8
        inspection = getattr(dispatch_config, 'inspection', 4) if dispatch_config is not None else 4
        return {
            'execution': max(1, int(execution or 8)),
            'inspection': max(1, int(inspection or 4)),
        }

    @staticmethod
    def _event_history_settings(config: Any | None) -> dict[str, Any]:
        main_runtime = getattr(config, 'main_runtime', None) if config is not None else None
        history = getattr(main_runtime, 'event_history', None) if main_runtime is not None else None
        return {
            'enabled': bool(getattr(history, 'enabled', True)) if history is not None else True,
            'dir': str(getattr(history, 'dir', '') or ''),
            'live_patch_persist_window_ms': max(
                0,
                int(getattr(history, 'live_patch_persist_window_ms', 1000) or 1000),
            ),
            'archive_encoding': str(getattr(history, 'archive_encoding', 'gzip') or 'gzip').strip().lower() or 'gzip',
        }

    @staticmethod
    def _adaptive_tool_budget_settings(config: Any | None) -> dict[str, Any]:
        agents = getattr(config, 'agents', None) if config is not None else None
        parallelism = getattr(agents, 'node_parallelism', None) if agents is not None else None
        return {
            'enabled': bool(getattr(parallelism, 'adaptive_total_tool_budget_enabled', True)) if parallelism is not None else True,
            'normal_limit': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_normal_limit', 6) or 6)),
            'throttled_limit': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_throttled_limit', 2) or 2)),
            'critical_limit': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_critical_limit', getattr(parallelism, 'adaptive_total_tool_budget_safe_limit', 1)) or 1)),
            'step_up': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_step_up', 1) or 1)),
            'sample_seconds': max(0.1, float(getattr(parallelism, 'adaptive_total_tool_budget_sample_seconds', 1.0) or 1.0)),
            'recover_window_seconds': max(0.1, float(getattr(parallelism, 'adaptive_total_tool_budget_recover_window_seconds', 1.0) or 1.0)),
            'warn_consecutive_samples': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_warn_consecutive_samples', 3) or 3)),
            'safe_consecutive_samples': max(1, int(getattr(parallelism, 'adaptive_total_tool_budget_safe_consecutive_samples', 3) or 3)),
            'pressure_snapshot_stale_after_seconds': max(0.1, float(getattr(parallelism, 'adaptive_pressure_snapshot_stale_after_seconds', 3.0) or 3.0)),
            'event_loop_warn_ms': max(0.0, float(getattr(parallelism, 'adaptive_event_loop_warn_ms', 250.0) or 250.0)),
            'event_loop_safe_ms': max(0.0, float(getattr(parallelism, 'adaptive_event_loop_safe_ms', 100.0) or 100.0)),
            'event_loop_critical_ms': max(0.0, float(getattr(parallelism, 'adaptive_event_loop_critical_ms', 1500.0) or 1500.0)),
            'writer_queue_warn': max(1, int(getattr(parallelism, 'adaptive_writer_queue_warn', 50) or 50)),
            'writer_queue_safe': max(1, int(getattr(parallelism, 'adaptive_writer_queue_safe', 10) or 10)),
            'writer_queue_critical': max(1, int(getattr(parallelism, 'adaptive_writer_queue_critical', 100) or 100)),
            'sqlite_write_wait_warn_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_write_wait_warn_ms', 200.0) or 200.0)),
            'sqlite_write_wait_safe_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_write_wait_safe_ms', 50.0) or 50.0)),
            'sqlite_write_wait_critical_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_write_wait_critical_ms', 250.0) or 250.0)),
            'sqlite_query_warn_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_query_warn_ms', 150.0) or 150.0)),
            'sqlite_query_safe_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_query_safe_ms', 30.0) or 30.0)),
            'sqlite_query_critical_ms': max(0.0, float(getattr(parallelism, 'adaptive_sqlite_query_critical_ms', 250.0) or 250.0)),
            'machine_cpu_warn_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_cpu_warn_percent', 85.0) or 85.0)),
            'machine_cpu_safe_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_cpu_safe_percent', 55.0) or 55.0)),
            'machine_cpu_critical_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_cpu_critical_percent', 95.0) or 95.0)),
            'machine_memory_warn_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_memory_warn_percent', 88.0) or 88.0)),
            'machine_memory_safe_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_memory_safe_percent', 95.0) or 95.0)),
            'machine_memory_critical_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_memory_critical_percent', 94.0) or 94.0)),
            'machine_disk_busy_warn_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_disk_busy_warn_percent', 70.0) or 70.0)),
            'machine_disk_busy_safe_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_disk_busy_safe_percent', 35.0) or 35.0)),
            'machine_disk_busy_critical_percent': max(0.0, float(getattr(parallelism, 'adaptive_machine_disk_busy_critical_percent', 90.0) or 90.0)),
            'process_cpu_warn_ratio': max(0.0, float(getattr(parallelism, 'adaptive_process_cpu_warn_ratio', 0.85) or 0.85)),
            'process_cpu_safe_ratio': max(0.0, float(getattr(parallelism, 'adaptive_process_cpu_safe_ratio', 0.50) or 0.50)),
        }

    @staticmethod
    def _resource_reload_poll_interval_ms(config: Any | None) -> int:
        resources = getattr(config, 'resources', None) if config is not None else None
        reload_cfg = getattr(resources, 'reload', None) if resources is not None else None
        return max(0, int(getattr(reload_cfg, 'poll_interval_ms', 1000) or 1000))

    @staticmethod
    def _resource_state_names_changed(
        before_state: dict[str, dict[str, str]] | None,
        after_state: dict[str, dict[str, str]] | None,
        section: str,
    ) -> list[str]:
        previous = dict((before_state or {}).get(section) or {})
        current = dict((after_state or {}).get(section) or {})
        return sorted(
            name
            for name in set(previous) | set(current)
            if previous.get(name) != current.get(name)
        )

    def _resource_tree_state_snapshot(self) -> dict[str, dict[str, str]]:
        manager = getattr(self, '_resource_manager', None)
        if manager is None or not hasattr(manager, 'capture_resource_tree_state'):
            return {}
        try:
            snapshot = manager.capture_resource_tree_state()
        except Exception:
            return {}
        return dict(snapshot or {})

    def _record_resource_tree_state(
        self,
        state: dict[str, dict[str, str]] | None = None,
        *,
        checked_at: float | None = None,
    ) -> None:
        self._resource_tree_state_cache = dict(state or self._resource_tree_state_snapshot() or {})
        self._resource_tree_state_checked_at = float(time.perf_counter() if checked_at is None else checked_at)

    async def _sync_catalog_targets(
        self,
        *,
        skill_ids: set[str] | None = None,
        tool_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        normalized_skill_ids = {
            str(item or '').strip()
            for item in (skill_ids or set())
            if str(item or '').strip()
        }
        normalized_tool_ids = {
            str(item or '').strip()
            for item in (tool_ids or set())
            if str(item or '').strip()
        }
        if not normalized_skill_ids and not normalized_tool_ids:
            return {
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        memory_manager = getattr(self, 'memory_manager', None)
        if memory_manager is None or not hasattr(memory_manager, 'sync_catalog'):
            return {
                'catalog_synced': False,
                'skill_ids': sorted(normalized_skill_ids),
                'tool_ids': sorted(normalized_tool_ids),
            }
        try:
            result = await memory_manager.sync_catalog(
                self,
                skill_ids=normalized_skill_ids or None,
                tool_ids=normalized_tool_ids or None,
            )
        except Exception:
            return {
                'catalog_synced': False,
                'skill_ids': sorted(normalized_skill_ids),
                'tool_ids': sorted(normalized_tool_ids),
            }
        return {
            'catalog_synced': True,
            'catalog': dict(result or {}),
            'skill_ids': sorted(normalized_skill_ids),
            'tool_ids': sorted(normalized_tool_ids),
        }

    async def maybe_refresh_external_resource_changes(
        self,
        *,
        session_id: str = 'web:shared',
        force: bool = False,
    ) -> dict[str, Any]:
        manager = getattr(self, '_resource_manager', None)
        if manager is None or not hasattr(manager, 'capture_resource_tree_state'):
            return {
                'refreshed': False,
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        now = time.perf_counter()
        interval_ms = max(0, int(getattr(self, '_resource_tree_state_poll_interval_ms', 0) or 0))
        before_state = dict(getattr(self, '_resource_tree_state_cache', None) or {})
        last_checked_at = float(getattr(self, '_resource_tree_state_checked_at', 0.0) or 0.0)
        if not before_state:
            self._record_resource_tree_state(checked_at=now)
            return {
                'refreshed': False,
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        if (
            not force
            and interval_ms > 0
            and last_checked_at > 0.0
            and ((now - last_checked_at) * 1000.0) < interval_ms
        ):
            return {
                'refreshed': False,
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        after_state = self._resource_tree_state_snapshot()
        if after_state == before_state:
            self._record_resource_tree_state(after_state, checked_at=now)
            return {
                'refreshed': False,
                'catalog_synced': False,
                'skill_ids': [],
                'tool_ids': [],
            }
        skill_ids = self._resource_state_names_changed(before_state, after_state, 'skills')
        tool_ids = self._resource_state_names_changed(before_state, after_state, 'tools')
        refresh_result = self.refresh_changed_resources(
            before_state,
            trigger='external-resource-generation-check',
            session_id=session_id,
        )
        self._node_context_selection_cache = {}
        self._record_resource_tree_state(after_state, checked_at=now)
        sync_result = await self._sync_catalog_targets(
            skill_ids=set(skill_ids),
            tool_ids=set(tool_ids),
        )
        return {
            'refreshed': True,
            'resources': refresh_result,
            **sync_result,
        }

    @staticmethod
    def _repair_legacy_display_text(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = str(value or '')
        if not text:
            return text
        text = re.sub(r'^鏈€缁堥獙鏀(?:讹細|:|：|\?)?', '最终验收:', text)
        replacements = (
            ('鏍稿\ue1ee鏈€缁堢粨鏋滄槸鍚︽弧瓒宠\ue6e6姹傘€?', '核对最终结果是否满足要求。'),
            ('妫€鏌ユ渶缁堢粨鏋滄槸鍚︽弧瓒宠\ue6e6姹傘€?', '检查最终结果是否满足要求。'),
            ('妫€鏌?child 杈撳嚭銆?', '检查 child 输出。'),
            ('妫€鏌ュ叕鍛婅崏绋挎槸鍚︽弧瓒充氦浠樿\ue6e6姹傘€?', '检查公告草稿是否满足交付要求。'),
            ('楠屾敹閫氳繃', '验收通过'),
            ('鑷\ue043富鎵ц\ue511', '自主执行'),
            ('杩涜\ue511涓?', '进行中'),
            ('鏈€鏂伴樁娈电洰鏍?', '最新阶段目标'),
        )
        for source, target in replacements:
            text = text.replace(source, target)
        return text

    @classmethod
    def _repair_legacy_display_payload(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._repair_legacy_display_payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._repair_legacy_display_payload(item) for item in value]
        return cls._repair_legacy_display_text(value)

    @staticmethod
    def _node_latest_context_display_payload(value: Any) -> Any | None:
        if isinstance(value, list):
            return value
        if not isinstance(value, dict):
            return None
        request_messages = value.get('request_messages')
        provider_request_body = value.get('provider_request_body')
        provider_input = provider_request_body.get('input') if isinstance(provider_request_body, dict) else None
        model_messages = value.get('model_messages')
        for candidate in (request_messages, provider_input, model_messages):
            if isinstance(candidate, list) and candidate:
                return candidate
            if isinstance(candidate, (dict, str)) and candidate:
                return candidate
        if isinstance(request_messages, list):
            return request_messages
        if isinstance(provider_input, list):
            return provider_input
        if isinstance(model_messages, list):
            return model_messages
        return None

    @classmethod
    def _render_node_latest_context_content(cls, raw_content: Any) -> str:
        text = str(raw_content or '')
        if not text.strip():
            return ''
        try:
            parsed = json.loads(text)
        except Exception:
            return str(cls._repair_legacy_display_text(text))
        display_payload = cls._node_latest_context_display_payload(parsed)
        rendered_payload = cls._repair_legacy_display_payload(display_payload if display_payload is not None else parsed)
        if isinstance(rendered_payload, str):
            return str(rendered_payload)
        return json.dumps(rendered_payload, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _normalize_optional_parallel_limit(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return max(0, int(value))

    @staticmethod
    def _normalize_optional_limit(value: Any, *, default: int | None) -> int | None:
        if value is _UNSET:
            value = default
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return max(0, int(value))

    def ensure_runtime_config_current(self, force: bool = False, reason: str = 'runtime') -> bool:
        if force:
            security = get_bootstrap_security_service(
                getattr(getattr(self, '_app_config', None), 'workspace_path', None)
            )
            security.reload_overlay_from_disk()
        config, revision, changed = get_runtime_config(force=force)
        if not changed and int(getattr(self, '_runtime_model_revision', 0) or 0) == int(revision or 0):
            return False
        self._app_config = config
        if hasattr(self._chat_backend, '_config'):
            self._chat_backend._config = config
        self._default_max_depth = max(0, int(getattr(config.main_runtime, 'default_max_depth', 1) or 0))
        self._hard_max_depth = max(self._default_max_depth, int(getattr(config.main_runtime, 'hard_max_depth', self._default_max_depth) or self._default_max_depth))
        self.node_runner._execution_model_refs = list(config.get_role_model_keys('execution'))
        self.node_runner._acceptance_model_refs = list(config.get_role_model_keys('inspection') or config.get_role_model_keys('execution'))
        self.node_runner._execution_max_iterations = config.get_role_max_iterations('execution')
        self.node_runner._acceptance_max_iterations = config.get_role_max_iterations('inspection')
        self.node_runner._execution_max_concurrency = config.get_role_max_concurrency('execution')
        self.node_runner._acceptance_max_concurrency = config.get_role_max_concurrency('inspection')
        self.task_actor_service.configure_node_dispatch_limits(
            execution=None,
            inspection=None,
        )
        parallel_enabled, max_parallel_tool_calls, max_parallel_child_pipelines = self._node_parallelism_settings(config)
        self._react_loop._parallel_tool_calls_enabled = parallel_enabled
        self._react_loop._max_parallel_tool_calls = max_parallel_tool_calls
        self.node_runner._max_parallel_child_pipelines = max_parallel_child_pipelines
        self.node_runner._parallel_child_pipelines_enabled = parallel_enabled
        adaptive_budget_settings = self._adaptive_tool_budget_settings(config)
        if self.adaptive_tool_budget_controller is not None:
            self.adaptive_tool_budget_controller.configure(
                normal_limit=int(adaptive_budget_settings['normal_limit']),
                throttled_limit=int(adaptive_budget_settings['throttled_limit']),
                critical_limit=int(adaptive_budget_settings['critical_limit']),
                step_up=int(adaptive_budget_settings['step_up']),
            )
        if self.model_key_concurrency_controller is not None:
            self.model_key_concurrency_controller.configure(
                resolve_model_limits=self._resolve_model_limit_payload,
                on_availability_changed=self.node_turn_controller.poke if self.node_turn_controller is not None else None,
            )
        if self.node_turn_controller is not None:
            self.node_turn_controller.configure(
                gate_supplier=self._node_turn_gate_allowed,
                freeze_supplier=self._node_turn_task_frozen,
            )
        if self.tool_pressure_monitor is not None:
            self.tool_pressure_monitor.configure(
                sample_seconds=float(adaptive_budget_settings['sample_seconds']),
                recover_window_seconds=float(adaptive_budget_settings['recover_window_seconds']),
                warn_consecutive_samples=int(adaptive_budget_settings['warn_consecutive_samples']),
                safe_consecutive_samples=int(adaptive_budget_settings['safe_consecutive_samples']),
                pressure_snapshot_stale_after_seconds=float(adaptive_budget_settings['pressure_snapshot_stale_after_seconds']),
                event_loop_warn_ms=float(adaptive_budget_settings['event_loop_warn_ms']),
                event_loop_safe_ms=float(adaptive_budget_settings['event_loop_safe_ms']),
                event_loop_critical_ms=float(adaptive_budget_settings['event_loop_critical_ms']),
                writer_queue_warn=int(adaptive_budget_settings['writer_queue_warn']),
                writer_queue_safe=int(adaptive_budget_settings['writer_queue_safe']),
                writer_queue_critical=int(adaptive_budget_settings['writer_queue_critical']),
                sqlite_write_wait_warn_ms=float(adaptive_budget_settings['sqlite_write_wait_warn_ms']),
                sqlite_write_wait_safe_ms=float(adaptive_budget_settings['sqlite_write_wait_safe_ms']),
                sqlite_write_wait_critical_ms=float(adaptive_budget_settings['sqlite_write_wait_critical_ms']),
                sqlite_query_warn_ms=float(adaptive_budget_settings['sqlite_query_warn_ms']),
                sqlite_query_safe_ms=float(adaptive_budget_settings['sqlite_query_safe_ms']),
                sqlite_query_critical_ms=float(adaptive_budget_settings['sqlite_query_critical_ms']),
                machine_cpu_warn_percent=float(adaptive_budget_settings['machine_cpu_warn_percent']),
                machine_cpu_safe_percent=float(adaptive_budget_settings['machine_cpu_safe_percent']),
                machine_cpu_critical_percent=float(adaptive_budget_settings['machine_cpu_critical_percent']),
                machine_memory_warn_percent=float(adaptive_budget_settings['machine_memory_warn_percent']),
                machine_memory_safe_percent=float(adaptive_budget_settings['machine_memory_safe_percent']),
                machine_memory_critical_percent=float(adaptive_budget_settings['machine_memory_critical_percent']),
                machine_disk_busy_warn_percent=float(adaptive_budget_settings['machine_disk_busy_warn_percent']),
                machine_disk_busy_safe_percent=float(adaptive_budget_settings['machine_disk_busy_safe_percent']),
                machine_disk_busy_critical_percent=float(adaptive_budget_settings['machine_disk_busy_critical_percent']),
                process_cpu_warn_ratio=float(adaptive_budget_settings['process_cpu_warn_ratio']),
                process_cpu_safe_ratio=float(adaptive_budget_settings['process_cpu_safe_ratio']),
            )
        # Resource manager config binding and reload are handled by refresh_loop_runtime_config().
        # Rebinding here clears dynamic tool instances before CEO exposure is assembled.
        self.resource_registry.refresh_from_current_resources()
        self.reconcile_core_tool_families()
        self.policy_engine.sync_default_role_policies()
        self._runtime_model_revision = int(revision or 0)
        return True

    def _normalize_task_metadata(
        self,
        *,
        session_id: str,
        metadata: dict[str, Any] | None,
        task_prompt: str = '',
    ) -> dict[str, Any]:
        payload = dict(metadata or {})
        raw_session_id = self._normalize_session_key(session_id)
        payload.setdefault('origin_session_id', raw_session_id)
        if raw_session_id.startswith('web:'):
            payload['memory_scope'] = normalize_memory_scope(
                payload.get('memory_scope'),
                fallback_channel=DEFAULT_WEB_MEMORY_SCOPE['channel'],
                fallback_chat_id=DEFAULT_WEB_MEMORY_SCOPE['chat_id'],
            )
        else:
            payload['memory_scope'] = normalize_memory_scope(
                payload.get('memory_scope'),
                fallback_session_key=raw_session_id,
            )
        payload.pop('continuation_of_task_id', None)
        payload.pop('created_by_source', None)
        core_requirement = str(payload.get('core_requirement') or '').strip() or str(task_prompt or '').strip()
        if core_requirement:
            payload['core_requirement'] = core_requirement
        else:
            payload.pop('core_requirement', None)
        payload['execution_policy'] = normalize_execution_policy_metadata(payload.get('execution_policy')).model_dump(mode='json')
        payload['final_acceptance'] = normalize_final_acceptance_metadata(payload.get('final_acceptance')).model_dump(mode='json')
        return payload

    def _task_memory_scope(self, task: TaskRecord | None) -> dict[str, str]:
        if task is None:
            return dict(DEFAULT_WEB_MEMORY_SCOPE)
        return normalize_memory_scope(
            (task.metadata or {}).get('memory_scope') if isinstance(task.metadata, dict) else None,
            fallback_session_key=task.session_id,
        )

    def _subject(self, *, actor_role: str, session_id: str, task_id: str | None = None, node_id: str | None = None) -> PermissionSubject:
        return PermissionSubject(user_key=session_id, session_id=session_id, task_id=task_id, node_id=node_id, actor_role=actor_role)

    def list_effective_tool_names(self, *, actor_role: str, session_id: str) -> list[str]:
        resource_manager = getattr(self, '_resource_manager', None)
        supported = sorted((resource_manager.tool_instances().keys() if resource_manager is not None else []))
        resource_registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        if resource_registry is None or policy_engine is None:
            return list(supported)
        return list_effective_tool_names(subject=self._subject(actor_role=actor_role, session_id=session_id), supported_tool_names=supported, resource_registry=resource_registry, policy_engine=policy_engine, mutation_allowed=True)

    def list_visible_skill_resources(self, *, actor_role: str, session_id: str):
        visible_ids = set(list_effective_skill_ids(subject=self._subject(actor_role=actor_role, session_id=session_id), available_skill_ids=[item.skill_id for item in self.resource_registry.list_skill_resources()], policy_engine=self.policy_engine))
        return [item for item in self.resource_registry.list_skill_resources() if item.skill_id in visible_ids]

    def list_contract_visible_skill_resources(self, *, actor_role: str, session_id: str):
        resource_registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        if resource_registry is None or policy_engine is None:
            return list(self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id) or [])
        subject = self._subject(actor_role=actor_role, session_id=session_id)
        items = []
        find_role_policy = getattr(policy_engine, '_find_role_policy', None)
        for record in list(resource_registry.list_skill_resources() or []):
            skill_id = str(getattr(record, 'skill_id', '') or '').strip()
            if not skill_id or not bool(getattr(record, 'enabled', True)):
                continue
            allowed_roles = {
                str(role or '').strip()
                for role in list(getattr(record, 'allowed_roles', []) or [])
                if str(role or '').strip()
            }
            if str(actor_role or '').strip() not in allowed_roles:
                continue
            if callable(find_role_policy):
                policy = find_role_policy(
                    subject=subject,
                    resource_kind='skill',
                    resource_id=skill_id,
                    action_id='load',
                )
                if policy is not None and str(getattr(policy, 'effect', '') or '').strip().lower() != 'allow':
                    continue
            items.append(record)
        return items

    def list_visible_tool_families(self, *, actor_role: str, session_id: str):
        visible_names = set(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id))
        subject = self._subject(actor_role=actor_role, session_id=session_id)
        families = []
        for family in self.resource_registry.list_tool_families():
            callable_family = bool(getattr(family, 'callable', True))
            actions = []
            context_lookup_only = False
            for action in family.actions:
                decision = self.policy_engine.evaluate_tool_action(
                    subject=subject,
                    tool_id=family.tool_id,
                    action_id=action.action_id,
                )
                executor_visible = not callable_family or bool(set(action.executor_names) & visible_names)
                if decision.allowed and (
                    executor_visible
                ):
                    actions.append(action)
                    continue
                if self._should_expose_unavailable_tool_action(
                    actor_role=actor_role,
                    family=family,
                    action=action,
                ):
                    actions.append(action)
                    context_lookup_only = True
                    continue
                if self._should_expose_context_lookup_only_tool_action(
                    actor_role=actor_role,
                    family=family,
                    action=action,
                    decision=decision,
                    executor_visible=executor_visible,
                ):
                    actions.append(action)
                    context_lookup_only = True
            if actions:
                metadata = dict(getattr(family, 'metadata', {}) or {})
                if context_lookup_only:
                    metadata['context_lookup_only'] = True
                families.append(family.model_copy(update={'actions': actions, 'metadata': metadata}))
        return families

    def execution_visible_tool_lightweight_items(self, *, actor_role: str, session_id: str) -> list[dict[str, Any]]:
        families = list(self.list_visible_tool_families(actor_role=actor_role, session_id=session_id) or [])
        items: list[dict[str, Any]] = []
        for family in families:
            tool_id = str(getattr(family, "tool_id", "") or "").strip()
            if not tool_id:
                continue
            metadata = dict(getattr(family, "metadata", {}) or {})
            items.append(
                {
                    "tool_id": tool_id,
                    "display_name": str(getattr(family, "display_name", "") or tool_id).strip() or tool_id,
                    "description": str(getattr(family, "description", "") or "").strip(),
                    "l0": str(metadata.get("l0") or getattr(family, "l0", "") or "").strip(),
                    "l1": str(metadata.get("l1") or getattr(family, "l1", "") or "").strip(),
                    "actions": [
                        {
                            "action_id": str(getattr(action, "action_id", "") or "").strip(),
                            "executor_names": [
                                str(name or "").strip()
                                for name in list(getattr(action, "executor_names", []) or [])
                                if str(name or "").strip()
                            ],
                        }
                        for action in list(getattr(family, "actions", []) or [])
                    ],
                }
            )
        return items

    @staticmethod
    def _stable_execution_visible_tool_families(
        *,
        items: list[dict[str, Any]],
        visible_tool_names: list[str],
    ) -> list[dict[str, Any]]:
        visible_index = {
            str(name or '').strip(): index
            for index, name in enumerate(list(visible_tool_names or []))
            if str(name or '').strip()
        }
        fallback_index = len(visible_index) + 10_000

        def _executor_sort_key(name: Any) -> tuple[int, str]:
            normalized = str(name or '').strip()
            return (visible_index.get(normalized, fallback_index), normalized)

        def _action_sort_key(action: dict[str, Any]) -> tuple[int, str]:
            executors = [
                str(name or '').strip()
                for name in list((action or {}).get('executor_names') or [])
                if str(name or '').strip()
            ]
            if executors:
                return min(_executor_sort_key(name) for name in executors)
            return (fallback_index, str((action or {}).get('action_id') or '').strip())

        normalized_items: list[dict[str, Any]] = []
        for raw_item in list(items or []):
            item = dict(raw_item or {})
            normalized_actions: list[dict[str, Any]] = []
            for raw_action in list(item.get('actions') or []):
                action = dict(raw_action or {})
                action['executor_names'] = [
                    name
                    for name in sorted(
                        [
                            str(executor_name or '').strip()
                            for executor_name in list(action.get('executor_names') or [])
                            if str(executor_name or '').strip()
                        ],
                        key=_executor_sort_key,
                    )
                ]
                normalized_actions.append(action)
            item['actions'] = sorted(normalized_actions, key=_action_sort_key)
            normalized_items.append(item)

        def _family_sort_key(item: dict[str, Any]) -> tuple[int, str]:
            tool_id = str(item.get('tool_id') or '').strip()
            candidates: list[tuple[int, str]] = []
            if tool_id:
                candidates.append((visible_index.get(tool_id, fallback_index), tool_id))
            for action in list(item.get('actions') or []):
                for executor_name in list(action.get('executor_names') or []):
                    candidates.append(_executor_sort_key(executor_name))
            if candidates:
                return min(candidates)
            return (fallback_index, tool_id)

        return sorted(normalized_items, key=_family_sort_key)

    @staticmethod
    def _split_executor_prefixes_for_tool_id(tool_id: str) -> tuple[str, ...]:
        normalized_tool_id = str(tool_id or '').strip()
        prefixes = [
            prefix
            for prefix in (
                f'{normalized_tool_id}_' if normalized_tool_id else '',
                'filesystem_' if normalized_tool_id == 'filesystem' else '',
                'content_' if normalized_tool_id == 'content_navigation' else '',
            )
            if prefix
        ]
        return tuple(prefixes)

    @classmethod
    def _is_legacy_execution_monolith_name(cls, *, tool_id: str, executor_name: str) -> bool:
        normalized_tool_id = str(tool_id or '').strip()
        normalized_name = str(executor_name or '').strip()
        if not normalized_tool_id or not normalized_name:
            return False
        split_prefixes = cls._split_executor_prefixes_for_tool_id(normalized_tool_id)
        if any(normalized_name.startswith(prefix) for prefix in split_prefixes):
            return False
        return normalized_name == normalized_tool_id or get_governance_tool_id(normalized_name) == normalized_tool_id

    @classmethod
    def _filter_execution_model_visible_lightweight_items(
        cls,
        *,
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        filtered_items: list[dict[str, Any]] = []
        for raw_item in list(items or []):
            item = dict(raw_item or {})
            tool_id = str(item.get('tool_id') or '').strip()
            split_prefixes = cls._split_executor_prefixes_for_tool_id(tool_id)
            actions = [dict(raw_action or {}) for raw_action in list(item.get('actions') or [])]
            all_executor_names = [
                str(name or '').strip()
                for action in actions
                for name in list(action.get('executor_names') or [])
                if str(name or '').strip()
            ]
            split_available = any(
                any(name.startswith(prefix) for prefix in split_prefixes)
                for name in all_executor_names
            )
            if split_available and tool_id:
                normalized_actions: list[dict[str, Any]] = []
                for action in actions:
                    action['executor_names'] = [
                        name
                        for name in [
                            str(executor_name or '').strip()
                            for executor_name in list(action.get('executor_names') or [])
                            if str(executor_name or '').strip()
                        ]
                        if not cls._is_legacy_execution_monolith_name(tool_id=tool_id, executor_name=name)
                    ]
                    if action['executor_names']:
                        normalized_actions.append(action)
                item['actions'] = normalized_actions
            else:
                item['actions'] = actions
            filtered_items.append(item)
        return filtered_items

    @classmethod
    def _canonicalize_execution_promoted_tool_names(
        cls,
        *,
        tool_names: list[str],
        visible_tool_families: list[dict[str, Any]],
    ) -> list[str]:
        executor_name_set: set[str] = set()
        preferred_executor_by_family: dict[str, str] = {}
        for item in list(visible_tool_families or []):
            tool_id = str((item or {}).get('tool_id') or '').strip()
            split_executor_names: list[str] = []
            fallback_executor_names: list[str] = []
            for action in list((item or {}).get('actions') or []):
                for raw_executor_name in list((action or {}).get('executor_names') or []):
                    executor_name = str(raw_executor_name or '').strip()
                    if not executor_name:
                        continue
                    executor_name_set.add(executor_name)
                    fallback_executor_names.append(executor_name)
                    if not cls._is_legacy_execution_monolith_name(tool_id=tool_id, executor_name=executor_name):
                        split_executor_names.append(executor_name)
            preferred_executor = ""
            for candidate in split_executor_names + fallback_executor_names:
                if candidate:
                    preferred_executor = candidate
                    break
            if tool_id and preferred_executor:
                preferred_executor_by_family[tool_id] = preferred_executor
                external_tool_id = cls._external_tool_family_id(tool_id)
                if external_tool_id:
                    preferred_executor_by_family.setdefault(external_tool_id, preferred_executor)
        canonical_names: list[str] = []
        for raw_name in list(tool_names or []):
            name = str(raw_name or '').strip()
            if not name:
                continue
            if name in executor_name_set and name not in canonical_names:
                canonical_names.append(name)
                continue
            preferred_executor = preferred_executor_by_family.get(name)
            if preferred_executor and preferred_executor not in canonical_names:
                canonical_names.append(preferred_executor)
                continue
            canonical_family_name = get_governance_tool_id(name)
            preferred_executor = preferred_executor_by_family.get(canonical_family_name)
            if preferred_executor and preferred_executor not in canonical_names:
                canonical_names.append(preferred_executor)
                continue
            if name not in canonical_names:
                canonical_names.append(name)
        return canonical_names

    @staticmethod
    def _execution_fixed_builtin_tool_names(*, visible_tool_names: list[str]) -> list[str]:
        visible_name_set = {
            str(item or '').strip()
            for item in list(visible_tool_names or [])
            if str(item or '').strip()
        }
        return [
            name
            for name in _NODE_FIXED_BUILTIN_TOOL_NAMES
            if name in visible_name_set
        ]

    def _select_model_visible_tool_schema_payload(
        self,
        *,
        task_id: str,
        node_id: str,
        node_kind: str,
        visible_tools: dict[str, Tool],
        runtime_context: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_node_kind = str(node_kind or '').strip().lower()
        if normalized_node_kind not in {'execution', 'acceptance'}:
            return {
                'tool_names': list(dict(visible_tools or {}).keys()),
                'lightweight_tool_ids': [],
                'schema_chars': 0,
                'trace': {'mode': 'all_visible'},
            }
        runtime_payload = dict(runtime_context or {})
        store = getattr(self, 'store', None)
        get_task = getattr(store, 'get_task', None) if store is not None else None
        get_node = getattr(store, 'get_node', None) if store is not None else None
        task = get_task(str(task_id or '').strip()) if callable(get_task) else None
        node = get_node(str(node_id or '').strip()) if callable(get_node) else None
        session_id = str(
            runtime_payload.get('session_key')
            or getattr(task, 'session_id', '')
            or 'web:shared'
        ).strip() or 'web:shared'
        actor_role = str(
            runtime_payload.get('actor_role')
            or ('inspection' if normalized_node_kind == 'acceptance' else 'execution')
        ).strip() or ('inspection' if normalized_node_kind == 'acceptance' else 'execution')
        ordered_visible_tool_names = self._normalized_tool_name_list(list(dict(visible_tools or {}).keys()))
        visible_rbac_tool_names = self._normalized_tool_name_list(list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or []))
        if not visible_rbac_tool_names:
            visible_rbac_tool_names = list(ordered_visible_tool_names)
        stable_lightweight_items = self._stable_execution_visible_tool_families(
            items=list(self.execution_visible_tool_lightweight_items(actor_role=actor_role, session_id=session_id) or []),
            visible_tool_names=visible_rbac_tool_names,
        )
        lightweight_items = self._filter_execution_model_visible_lightweight_items(items=stable_lightweight_items)
        legacy_monolith_names: set[str] = set()
        for item in list(stable_lightweight_items or []):
            tool_id = str((item or {}).get('tool_id') or '').strip()
            actions = list((item or {}).get('actions') or [])
            all_executor_names = [
                str(executor_name or '').strip()
                for action in actions
                for executor_name in list((action or {}).get('executor_names') or [])
                if str(executor_name or '').strip()
            ]
            split_available = any(
                not self._is_legacy_execution_monolith_name(tool_id=tool_id, executor_name=executor_name)
                for executor_name in all_executor_names
            )
            if split_available and tool_id:
                legacy_monolith_names.add(tool_id)
                for executor_name in all_executor_names:
                    if self._is_legacy_execution_monolith_name(tool_id=tool_id, executor_name=executor_name):
                        legacy_monolith_names.add(executor_name)
        hydrated_executor_names = self._node_hydrated_executor_names(
            task_id=str(task_id or '').strip(),
            node_id=str(node_id or '').strip(),
            actor_role=actor_role,
            session_id=session_id,
        )
        always_callable_tool_names: list[str] = []
        lightweight_executor_names = {
            str(executor_name or '').strip()
            for item in list(lightweight_items or [])
            for action in list((item or {}).get('actions') or [])
            for executor_name in list((action or {}).get('executor_names') or [])
            if str(executor_name or '').strip()
        }
        control_tool_names = self._execution_control_tool_names(
            node_kind=normalized_node_kind,
            can_spawn_children=bool(getattr(node, 'can_spawn_children', normalized_node_kind == 'execution')),
        )
        for name in [
            *control_tool_names,
            *self._execution_fixed_builtin_tool_names(visible_tool_names=ordered_visible_tool_names),
        ]:
            normalized = str(name or '').strip()
            if normalized and normalized in ordered_visible_tool_names and normalized not in always_callable_tool_names:
                always_callable_tool_names.append(normalized)
        for name in ordered_visible_tool_names:
            if name in lightweight_executor_names or name in always_callable_tool_names or name in legacy_monolith_names:
                continue
            always_callable_tool_names.append(name)
        promoted_hydrated_executor_names = self._canonicalize_execution_promoted_tool_names(
            tool_names=list(hydrated_executor_names),
            visible_tool_families=lightweight_items,
        )
        promoted_only_hydrated_executor_names = [
            name for name in promoted_hydrated_executor_names if name not in always_callable_tool_names
        ]
        schema_size_by_executor = {
            name: len(json.dumps(tool.to_model_schema(), ensure_ascii=False, sort_keys=True))
            for name, tool in dict(visible_tools or {}).items()
        }
        selection = build_execution_tool_selection(
            prompt=str(getattr(node, 'prompt', '') or ''),
            goal=str(getattr(node, 'goal', '') or ''),
            core_requirement=str(getattr(task, 'metadata', {}).get('core_requirement') or getattr(node, 'prompt', '') or getattr(node, 'goal', '') or ''),
            visible_tool_families=list(lightweight_items),
            visible_tool_names=list(ordered_visible_tool_names),
            always_callable_tool_names=list(always_callable_tool_names),
            promoted_tool_names=list(promoted_hydrated_executor_names),
            schema_size_by_executor=schema_size_by_executor,
        )
        full_callable_tool_names = self._normalized_tool_name_list(list(selection.hydrated_tool_names or []))
        model_visible_callable_tool_names = self._model_visible_callable_tool_names_for_node(
            task_id=str(task_id or '').strip(),
            node_id=str(node_id or '').strip(),
            node_kind=normalized_node_kind,
            callable_tool_names=full_callable_tool_names,
        )
        log_service = getattr(self, 'log_service', None)
        read_runtime_frame = getattr(log_service, 'read_runtime_frame', None)
        prior_selected_tool_names: list[str] = []
        prior_provider_tool_names: list[str] = []
        prior_history_shrink_reason = ''
        if callable(read_runtime_frame):
            prior_frame = read_runtime_frame(str(task_id or '').strip(), str(node_id or '').strip())
            if isinstance(prior_frame, dict):
                prior_selected_tool_names = [
                    str(item or '').strip()
                    for item in list(prior_frame.get('model_visible_tool_names') or [])
                    if str(item or '').strip()
                ]
                prior_provider_tool_names = [
                    str(item or '').strip()
                    for item in list(
                        prior_frame.get('provider_tool_names')
                        or prior_frame.get('model_visible_tool_names')
                        or []
                    )
                    if str(item or '').strip()
                ]
                prior_history_shrink_reason = str(prior_frame.get('history_shrink_reason') or '').strip()
        selected_tool_names: list[str] = []
        for name in prior_selected_tool_names:
            if name in model_visible_callable_tool_names and name not in selected_tool_names:
                selected_tool_names.append(name)
        for name in model_visible_callable_tool_names:
            if name not in selected_tool_names:
                selected_tool_names.append(name)
        provider_visible_tool_names = [
            name
            for name in ordered_visible_tool_names
            if name not in legacy_monolith_names
        ]
        desired_provider_tool_names: list[str] = []
        provider_visible_tool_name_set = set(provider_visible_tool_names)
        for name in visible_rbac_tool_names:
            if name in provider_visible_tool_name_set and name not in desired_provider_tool_names:
                desired_provider_tool_names.append(name)
        for name in provider_visible_tool_names:
            if name not in desired_provider_tool_names:
                desired_provider_tool_names.append(name)
        exposure = self._refresh_provider_tool_bundle(
            prior_provider_tool_names=prior_provider_tool_names,
            desired_provider_tool_names=desired_provider_tool_names,
            prior_history_shrink_reason=prior_history_shrink_reason,
        )
        provider_tool_names = list(exposure.get('provider_tool_names') or [])
        provider_tool_bundle_seeded = bool(exposure.get('provider_tool_bundle_seeded'))
        final_schema_chars = sum(
            len(json.dumps(visible_tools[name].to_model_schema(), ensure_ascii=False, sort_keys=True))
            for name in selected_tool_names
            if name in visible_tools
        )
        candidate_tool_names = [
            name
            for name in self._normalized_tool_name_list(list(getattr(selection, 'candidate_tool_names', []) or []))
            if name not in set(selected_tool_names)
        ]
        return {
            'tool_names': selected_tool_names,
            'provider_tool_names': provider_tool_names,
            'candidate_tool_names': candidate_tool_names,
            'lightweight_tool_ids': list(selection.lightweight_tool_ids or []),
            'hydrated_executor_names': list(promoted_only_hydrated_executor_names),
            'pending_provider_tool_names': list(exposure.get('pending_provider_tool_names') or []),
            'provider_tool_exposure_pending': bool(exposure.get('provider_tool_exposure_pending')),
            'provider_tool_exposure_revision': str(exposure.get('provider_tool_exposure_revision') or ''),
            'provider_tool_exposure_commit_reason': str(exposure.get('provider_tool_exposure_commit_reason') or ''),
            'schema_chars': int(final_schema_chars),
            'trace': {
                'mode': 'execution_tool_selection',
                'node_kind': normalized_node_kind,
                'session_id': session_id,
                'actor_role': actor_role,
                'rbac_visible_tool_names': list(visible_rbac_tool_names),
                'callable_tool_names': list(full_callable_tool_names),
                'full_callable_tool_names': list(full_callable_tool_names),
                'stage_locked_to_submit_next_stage': (
                    list(model_visible_callable_tool_names) == [STAGE_TOOL_NAME]
                    and list(full_callable_tool_names) != [STAGE_TOOL_NAME]
                ),
                'requested_promoted_hydrated_executor_names': list(hydrated_executor_names),
                'promoted_hydrated_executor_names': list(promoted_only_hydrated_executor_names),
                'candidate_tool_names': list(candidate_tool_names),
                'desired_provider_tool_names': list(exposure.get('desired_provider_tool_names') or []),
                'prior_provider_tool_names': list(prior_provider_tool_names),
                'prior_history_shrink_reason': str(prior_history_shrink_reason or ''),
                'provider_tool_names': list(provider_tool_names),
                'pending_provider_tool_names': list(exposure.get('pending_provider_tool_names') or []),
                'provider_tool_exposure_pending': bool(exposure.get('provider_tool_exposure_pending')),
                'provider_tool_exposure_revision': str(exposure.get('provider_tool_exposure_revision') or ''),
                'provider_tool_exposure_commit_reason': str(exposure.get('provider_tool_exposure_commit_reason') or ''),
                'provider_tool_bundle_seeded': bool(provider_tool_bundle_seeded),
                'base_schema_chars': int(selection.schema_chars),
                'top_k': int((selection.trace or {}).get('top_k', 0) or 0),
                'final_schema_chars': int(final_schema_chars),
                **dict(selection.trace or {}),
            },
        }

    @staticmethod
    def _should_expose_unavailable_tool_action(*, actor_role: str, family: Any, action: Any) -> bool:
        if bool(getattr(family, 'available', True)):
            return False
        if not bool(getattr(family, 'enabled', True)):
            return False
        if not bool(getattr(action, 'agent_visible', True)):
            return False
        allowed_roles = {
            str(role or '').strip()
            for role in list(getattr(action, 'allowed_roles', []) or [])
            if str(role or '').strip()
        }
        return str(actor_role or '').strip() in allowed_roles

    @staticmethod
    def _should_expose_context_lookup_only_tool_action(
        *,
        actor_role: str,
        family: Any,
        action: Any,
        decision: Any,
        executor_visible: bool,
    ) -> bool:
        if not bool(getattr(decision, 'allowed', False)):
            return False
        if executor_visible:
            return False
        if not bool(getattr(family, 'enabled', True)):
            return False
        if not bool(getattr(family, 'available', True)):
            return False
        if not bool(getattr(family, 'callable', True)):
            return False
        if not bool(getattr(action, 'agent_visible', True)):
            return False
        allowed_roles = {
            str(role or '').strip()
            for role in list(getattr(action, 'allowed_roles', []) or [])
            if str(role or '').strip()
        }
        if str(actor_role or '').strip() not in allowed_roles:
            return False
        return True

    def _visible_tool_family_map(self, *, actor_role: str, session_id: str) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for family in self.list_visible_tool_families(actor_role=actor_role, session_id=session_id):
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            if tool_id:
                mapping[tool_id] = family
                external_tool_id = self._external_tool_family_id(tool_id)
                if external_tool_id and external_tool_id not in mapping:
                    mapping[external_tool_id] = family
            for action in list(getattr(family, 'actions', []) or []):
                for executor_name in list(getattr(action, 'executor_names', []) or []):
                    name = str(executor_name or '').strip()
                    if name and name not in mapping:
                        mapping[name] = family
        return mapping

    @staticmethod
    def _tool_context_session_id(*, runtime_context: dict[str, Any], task: Any) -> str:
        return str(
            dict(runtime_context or {}).get('session_key')
            or getattr(task, 'session_id', '')
            or 'web:shared'
        ).strip() or 'web:shared'

    @staticmethod
    def _tool_context_actor_role(*, runtime_context: dict[str, Any], node_kind: str) -> str:
        normalized_node_kind = str(node_kind or '').strip().lower()
        default_role = 'inspection' if normalized_node_kind == 'acceptance' else 'execution'
        return str(dict(runtime_context or {}).get('actor_role') or default_role).strip() or default_role

    @staticmethod
    def _family_executor_names(family: Any) -> list[str]:
        executor_names: list[str] = []
        for action in list(getattr(family, 'actions', []) or []):
            for raw_name in list(getattr(action, 'executor_names', []) or []):
                name = str(raw_name or '').strip()
                if name and name not in executor_names:
                    executor_names.append(name)
        return executor_names

    @staticmethod
    def _normalized_hydrated_executor_names(raw_names: Any) -> list[str]:
        normalized: list[str] = []
        for raw_name in list(raw_names or []):
            name = str(raw_name or '').strip()
            if name and name not in normalized:
                normalized.append(name)
        return normalized

    def _hydrated_tool_limit_value(self) -> int:
        try:
            value = int(getattr(self, '_hydrated_tool_limit', 16) or 16)
        except Exception:
            value = 16
        return max(1, value)

    @staticmethod
    def _fixed_builtin_tool_name_set_for_actor_role(actor_role: str) -> set[str]:
        return fixed_builtin_tool_name_set_for_actor_role(actor_role)

    @staticmethod
    def _provider_tool_exposure_revision(tool_names: list[str] | None) -> str:
        normalized = [
            str(item or '').strip()
            for item in list(tool_names or [])
            if str(item or '').strip()
        ]
        if not normalized:
            return ''
        payload = json.dumps(normalized, ensure_ascii=False, separators=(',', ':'))
        return f"pte:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"

    @classmethod
    def _refresh_provider_tool_bundle(
        cls,
        *,
        prior_provider_tool_names: list[str] | None,
        desired_provider_tool_names: list[str] | None,
        prior_history_shrink_reason: str = '',
    ) -> dict[str, Any]:
        prior = [
            str(item or '').strip()
            for item in list(prior_provider_tool_names or [])
            if str(item or '').strip()
        ]
        desired = [
            str(item or '').strip()
            for item in list(desired_provider_tool_names or [])
            if str(item or '').strip()
        ]
        prior_membership = set(prior)
        desired_membership = set(desired)
        membership_changed = prior_membership != desired_membership
        if not prior:
            active = list(desired)
        elif membership_changed:
            active = list(desired)
        else:
            # Preserve the persisted bundle exactly when membership is unchanged
            # so prompt-cache prefixes do not churn on harmless reorder noise.
            active = list(prior)
        return {
            'provider_tool_names': list(active),
            'pending_provider_tool_names': [],
            'provider_tool_exposure_pending': False,
            'provider_tool_exposure_revision': cls._provider_tool_exposure_revision(active),
            'provider_tool_exposure_commit_reason': '',
            'provider_tool_bundle_seeded': bool((not prior and active) or membership_changed),
            'desired_provider_tool_names': list(desired),
            'prior_history_shrink_reason': str(prior_history_shrink_reason or '').strip(),
            'provider_tool_membership_changed': bool(membership_changed),
        }

    @classmethod
    def _resolve_provider_tool_exposure(
        cls,
        *,
        active_provider_tool_names: list[str] | None,
        pending_provider_tool_names: list[str] | None,
        desired_provider_tool_names: list[str] | None,
        commit_reason: str = '',
    ) -> dict[str, Any]:
        _ = pending_provider_tool_names, commit_reason
        return cls._refresh_provider_tool_bundle(
            prior_provider_tool_names=active_provider_tool_names,
            desired_provider_tool_names=desired_provider_tool_names,
        )

    def _tool_context_hydration_targets(
        self,
        *,
        requested_tool_id: str,
        visible_family: Any,
        actor_role: str = '',
    ) -> list[str]:
        requested_name = str(requested_tool_id or '').strip()
        if not requested_name:
            return []
        family_tool_id = str(getattr(visible_family, 'tool_id', '') or '').strip()
        if family_tool_id == 'filesystem' and requested_name == family_tool_id:
            return []
        family_executors = self._family_executor_names(visible_family)
        if requested_name in family_executors:
            fixed_builtin_names = self._fixed_builtin_tool_name_set_for_actor_role(actor_role)
            if requested_name in fixed_builtin_names:
                return []
            return [requested_name]
        return []

    def _apply_hydrated_executor_lru(
        self,
        *,
        frame: dict[str, Any],
        incoming_executor_names: list[str],
    ) -> tuple[list[str], list[str]]:
        existing = self._normalized_hydrated_executor_names(
            list(frame.get('hydrated_executor_state') or []) or list(frame.get('hydrated_executor_names') or [])
        )
        incoming = self._normalized_hydrated_executor_names(incoming_executor_names)
        if not incoming:
            return existing, []
        next_state = [name for name in existing if name not in incoming]
        next_state.extend(incoming)
        limit = self._hydrated_tool_limit_value()
        evicted: list[str] = []
        if len(next_state) > limit:
            evicted = list(next_state[:-limit])
            next_state = next_state[-limit:]
        return next_state, evicted

    def _tool_context_contract_payload(
        self,
        *,
        actor_role: str,
        session_id: str,
        requested_tool_id: str,
        resolved_tool_id: str,
        callable_value: Any,
        available_value: Any,
    ) -> dict[str, Any]:
        visible_family = self._visible_tool_family_map(actor_role=actor_role, session_id=session_id).get(
            str(requested_tool_id or '').strip() or str(resolved_tool_id or '').strip()
        )
        hydration_targets = (
            self._tool_context_hydration_targets(
                requested_tool_id=str(requested_tool_id or '').strip() or str(resolved_tool_id or '').strip(),
                visible_family=visible_family,
                actor_role=actor_role,
            )
            if visible_family is not None
            else []
        )
        callable_now = bool(callable_value) and bool(available_value)
        requested_or_resolved_tool_id = str(requested_tool_id or '').strip() or str(resolved_tool_id or '').strip()
        return {
            'callable_now': callable_now,
            'will_be_hydrated_next_turn': callable_now and bool(hydration_targets),
            'hydration_targets': list(hydration_targets),
            'exec_runtime_policy': (
                self._current_exec_runtime_policy_payload()
                if requested_or_resolved_tool_id in {EXEC_TOOL_FAMILY_ID, EXEC_TOOL_EXECUTOR_NAME}
                or (
                    visible_family is not None
                    and exec_tool_supports_execution_mode(getattr(visible_family, 'tool_id', ''))
                )
                else None
            ),
        }

    def _current_exec_runtime_policy_payload(self) -> dict[str, Any]:
        family = self._raw_tool_family(EXEC_TOOL_FAMILY_ID)
        descriptor = (
            self._resource_manager.get_tool_descriptor(EXEC_TOOL_EXECUTOR_NAME)
            if self._resource_manager is not None and hasattr(self._resource_manager, 'get_tool_descriptor')
            else None
        )
        return resolve_exec_runtime_policy_payload(
            family=family,
            descriptor=descriptor,
        )

    def _promote_tool_context_hydration(
        self,
        *,
        task_id: str,
        node_id: str,
        tool_call: Any,
        raw_result: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> None:
        tool_name = str(getattr(tool_call, 'name', '') or '').strip()
        if tool_name not in {'load_tool_context', 'load_tool_context_v2'}:
            return
        payload = dict(raw_result or {})
        if not bool(payload.get('ok')):
            return
        normalized_arguments = self._normalize_tool_call_arguments(getattr(tool_call, 'arguments', {}))
        requested_tool_id = str(
            normalized_arguments.get('tool_id')
            or payload.get('tool_id')
            or ''
        ).strip()
        resolved_tool_id = str(payload.get('tool_id') or requested_tool_id or '').strip()
        if not requested_tool_id:
            return
        store = getattr(self, 'store', None)
        get_task = getattr(store, 'get_task', None) if store is not None else None
        get_node = getattr(store, 'get_node', None) if store is not None else None
        task = get_task(str(task_id or '').strip()) if callable(get_task) else None
        node = get_node(str(node_id or '').strip()) if callable(get_node) else None
        session_id = self._tool_context_session_id(runtime_context=dict(runtime_context or {}), task=task)
        actor_role = self._tool_context_actor_role(
            runtime_context=dict(runtime_context or {}),
            node_kind=str(getattr(node, 'node_kind', '') or runtime_context.get('node_kind') or ''),
        )
        log_service = getattr(self, 'log_service', None)
        read_runtime_frame = getattr(log_service, 'read_runtime_frame', None)
        current_frame = (
            read_runtime_frame(str(task_id or '').strip(), str(node_id or '').strip())
            if callable(read_runtime_frame)
            else {}
        )
        candidate_tool_names = self._normalized_tool_name_list(
            list(
                runtime_context.get('candidate_tool_names')
                or dict(current_frame or {}).get('candidate_tool_names')
                or []
            )
        )
        if not any(name in candidate_tool_names for name in [requested_tool_id, resolved_tool_id] if name):
            return
        visible_family_map = self._visible_tool_family_map(actor_role=actor_role, session_id=session_id)
        visible_family = visible_family_map.get(requested_tool_id) or visible_family_map.get(resolved_tool_id)
        if visible_family is None:
            return
        promoted_executor_names = self._normalized_tool_name_list(payload.get('hydration_targets'))
        if not promoted_executor_names:
            promoted_executor_names = self._tool_context_hydration_targets(
                requested_tool_id=requested_tool_id,
                visible_family=visible_family,
                actor_role=actor_role,
            )
        if not promoted_executor_names:
            return
        update_frame = getattr(log_service, 'update_frame', None)
        if not callable(update_frame):
            return

        def _mutate(frame: dict[str, Any]) -> dict[str, Any]:
            next_frame = dict(frame or {})
            hydrated_state, evicted = self._apply_hydrated_executor_lru(
                frame=next_frame,
                incoming_executor_names=promoted_executor_names,
            )
            next_frame['hydrated_executor_state'] = list(hydrated_state)
            next_frame['hydrated_executor_names'] = list(hydrated_state)
            next_frame['hydration_evicted_executor_names'] = list(evicted)
            return next_frame

        update_frame(str(task_id or '').strip(), str(node_id or '').strip(), _mutate, publish_snapshot=True)
        self._clear_node_context_selection(task_id=str(task_id or '').strip(), node_id=str(node_id or '').strip())

    @staticmethod
    def _execution_control_tool_names(*, node_kind: str, can_spawn_children: bool) -> list[str]:
        normalized_node_kind = str(node_kind or '').strip().lower()
        ordered: list[str] = []
        if normalized_node_kind in {'execution', 'acceptance'}:
            ordered.extend(['submit_next_stage', 'submit_final_result'])
        if normalized_node_kind == 'execution' and bool(can_spawn_children):
            ordered.append('spawn_child_nodes')
        return ordered

    def _node_hydrated_executor_names(
        self,
        *,
        task_id: str,
        node_id: str,
        actor_role: str,
        session_id: str,
    ) -> list[str]:
        log_service = getattr(self, 'log_service', None)
        read_runtime_frame = getattr(log_service, 'read_runtime_frame', None)
        if not callable(read_runtime_frame):
            return []
        frame = read_runtime_frame(str(task_id or '').strip(), str(node_id or '').strip()) or {}
        visible_tool_names = {
            str(name or '').strip()
            for name in list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or [])
            if str(name or '').strip()
        }
        raw_hydrated_names = list(frame.get('hydrated_executor_state') or []) or list(frame.get('hydrated_executor_names') or [])
        if not visible_tool_names:
            return self._normalized_hydrated_executor_names(raw_hydrated_names)
        hydrated: list[str] = []
        for raw_name in raw_hydrated_names:
            name = str(raw_name or '').strip()
            if name and name in visible_tool_names and name not in hydrated:
                hydrated.append(name)
        return hydrated

    def _callable_tool_names_for_node(
        self,
        *,
        task,
        node: Any,
        visible_tool_names: list[str] | None = None,
    ) -> list[str]:
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_names = self._normalized_tool_name_list(
            list(visible_tool_names or self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or [])
        )
        visible_name_set = set(visible_names)
        callable_names: list[str] = []
        seen: set[str] = set()
        for name in self._execution_control_tool_names(
            node_kind=str(getattr(node, 'node_kind', '') or ''),
            can_spawn_children=bool(getattr(node, 'can_spawn_children', False)),
        ):
            if name in visible_name_set and name not in seen:
                callable_names.append(name)
                seen.add(name)
        for name in self._execution_fixed_builtin_tool_names(visible_tool_names=visible_names):
            if name not in seen:
                callable_names.append(name)
                seen.add(name)
        for name in self._node_hydrated_executor_names(
            task_id=str(getattr(task, 'task_id', '') or getattr(node, 'task_id', '') or '').strip(),
            node_id=str(getattr(node, 'node_id', '') or '').strip(),
            actor_role=actor_role,
            session_id=session_id,
        ):
            if name not in seen:
                callable_names.append(name)
                seen.add(name)
        return callable_names

    def _model_visible_callable_tool_names_for_node(
        self,
        *,
        task_id: str,
        node_id: str,
        node_kind: str,
        callable_tool_names: list[str] | None,
        stage_payload: dict[str, Any] | None = None,
    ) -> list[str]:
        full_callable_tool_names = self._normalized_tool_name_list(list(callable_tool_names or []))
        if str(node_kind or '').strip().lower() not in {'execution', 'acceptance'}:
            return full_callable_tool_names
        effective_stage_payload = dict(stage_payload or {}) if isinstance(stage_payload, dict) else {}
        if not effective_stage_payload:
            log_service = getattr(self, 'log_service', None)
            stage_payload_supplier = getattr(log_service, 'execution_stage_prompt_payload', None)
            if callable(stage_payload_supplier):
                supplied_stage_payload = stage_payload_supplier(
                    str(task_id or '').strip(),
                    str(node_id or '').strip(),
                )
                if isinstance(supplied_stage_payload, dict):
                    effective_stage_payload = dict(supplied_stage_payload)
        if not effective_stage_payload:
            return full_callable_tool_names
        return callable_tool_names_for_stage_iteration(
            full_callable_tool_names,
            has_active_stage=bool(effective_stage_payload.get('has_active_stage')),
            transition_required=bool(effective_stage_payload.get('transition_required')),
            stage_tool_name=STAGE_TOOL_NAME,
        )

    def _candidate_tool_names_for_node(
        self,
        *,
        task,
        node: Any,
        selection: Any | None,
        visible_tool_names: list[str] | None = None,
    ) -> list[str]:
        visible_names = self._normalized_tool_name_list(
            list(visible_tool_names or self.list_effective_tool_names(
                actor_role=self._actor_role_for_node(node),
                session_id=str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared',
            ) or [])
        )
        visible_name_set = set(visible_names)
        callable_name_set = set(self._callable_tool_names_for_node(task=task, node=node, visible_tool_names=visible_names))
        candidates: list[str] = []
        for raw_name in list(getattr(selection, 'candidate_tool_names', []) or []):
            name = str(raw_name or '').strip()
            if not name or name not in visible_name_set or name in callable_name_set or name in candidates:
                continue
            candidates.append(name)
        return candidates

    @staticmethod
    def _search_limit(limit: int | None, *, default: int = 5, max_limit: int = 20) -> int:
        try:
            value = int(limit if limit is not None else default)
        except Exception:
            value = default
        return max(1, min(max_limit, value))

    @staticmethod
    def _matched_search_fields(query: str, fields: dict[str, Any]) -> list[str]:
        raw_query = ' '.join(str(query or '').lower().split())
        if not raw_query:
            return []
        terms = [term for term in re.split(r'[^\w\u4e00-\u9fff]+', raw_query) if term]
        matched: list[str] = []
        for field_name, value in dict(fields or {}).items():
            haystack = ' '.join(str(value or '').lower().split())
            if not haystack:
                continue
            if raw_query in haystack or any(term in haystack for term in terms):
                matched.append(str(field_name))
        return matched

    def _search_visible_skills(
        self,
        *,
        actor_role: str,
        session_id: str,
        search_query: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        query = str(search_query or '').strip()
        if not query:
            return {'ok': False, 'error': 'search_query_required'}
        visible = list(self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_id) or [])
        scored: list[tuple[float, str, Any, list[str]]] = []
        for record in visible:
            skill_id = str(getattr(record, 'skill_id', '') or '').strip()
            display_name = str(getattr(record, 'display_name', '') or '').strip()
            description = str(getattr(record, 'description', '') or '').strip()
            score = score_query(query, skill_id, display_name, description)
            if score <= 0:
                continue
            matched_fields = self._matched_search_fields(
                query,
                {
                    'skill_id': skill_id,
                    'display_name': display_name,
                    'description': description,
                },
            )
            scored.append((score, skill_id, record, matched_fields))
        scored.sort(key=lambda item: (-item[0], item[1]))
        effective_limit = self._search_limit(limit)
        candidates = [
            {
                'skill_id': str(getattr(record, 'skill_id', '') or '').strip(),
                'display_name': str(getattr(record, 'display_name', '') or '').strip(),
                'description': str(getattr(record, 'description', '') or '').strip(),
                'match_score': score,
                'matched_fields': list(matched_fields),
            }
            for score, _skill_id, record, matched_fields in scored[:effective_limit]
        ]
        return {
            'ok': True,
            'mode': 'search',
            'query': query,
            'limit': effective_limit,
            'total_visible': len(visible),
            'candidates': candidates,
            'message': '' if candidates else 'No visible skills matched the query.',
            'next_action_hint': 'Call load_skill_context(skill_id="<skill_id>") to load details for a candidate.',
        }

    def _search_visible_tools(
        self,
        *,
        actor_role: str,
        session_id: str,
        search_query: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        query = str(search_query or '').strip()
        if not query:
            return {'ok': False, 'error': 'search_query_required'}
        visible = list(self.list_visible_tool_families(actor_role=actor_role, session_id=session_id) or [])
        scored: list[tuple[float, int, int, str, Any, list[str], list[str]]] = []
        for family in visible:
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            display_name = str(getattr(family, 'display_name', '') or '').strip()
            description = str(getattr(family, 'description', '') or '').strip()
            executor_names: list[str] = []
            for action in list(getattr(family, 'actions', []) or []):
                executor_names.extend(
                    str(name or '').strip()
                    for name in list(getattr(action, 'executor_names', []) or [])
                    if str(name or '').strip()
                )
            executor_names = sorted(set(executor_names))
            score = score_query(query, tool_id, display_name, description, ' '.join(executor_names))
            if score <= 0:
                continue
            matched_fields = self._matched_search_fields(
                query,
                {
                    'tool_id': tool_id,
                    'display_name': display_name,
                    'description': description,
                    'executor_names': ' '.join(executor_names),
                },
            )
            scored.append(
                (
                    score,
                    1 if bool(getattr(family, 'available', True)) else 0,
                    1 if bool(getattr(family, 'callable', True)) else 0,
                    tool_id,
                    family,
                    matched_fields,
                    executor_names,
                )
            )
        scored.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        effective_limit = self._search_limit(limit)
        candidates = [
            {
                'tool_id': self._external_tool_family_id(str(getattr(family, 'tool_id', '') or '').strip()),
                'display_name': str(getattr(family, 'display_name', '') or '').strip(),
                'description': str(getattr(family, 'description', '') or '').strip(),
                'executor_names': list(executor_names),
                'available': bool(getattr(family, 'available', True)),
                'callable': bool(getattr(family, 'callable', True)),
                'tool_type': str(getattr(family, 'tool_type', '') or '').strip(),
                'install_dir': str(getattr(family, 'install_dir', '') or '').strip() or None,
                'match_score': score,
                'matched_fields': list(matched_fields),
            }
            for score, _available_rank, _callable_rank, _tool_id, family, matched_fields, executor_names in scored[:effective_limit]
        ]
        return {
            'ok': True,
            'mode': 'search',
            'query': query,
            'limit': effective_limit,
            'total_visible': len(visible),
            'candidates': candidates,
            'message': '' if candidates else 'No visible tools matched the query.',
            'next_action_hint': 'Call load_tool_context(tool_id="<tool_id>") to load details for a candidate.',
        }

    def load_skill_context(
        self,
        *,
        actor_role: str,
        session_id: str,
        skill_id: str = '',
        search_query: str = '',
        limit: int | None = None,
    ) -> dict[str, Any]:
        skill_name = str(skill_id or '').strip()
        if not skill_name:
            if str(search_query or '').strip():
                return self._search_visible_skills(
                    actor_role=actor_role,
                    session_id=session_id,
                    search_query=search_query,
                    limit=limit,
                )
            return {'ok': False, 'error': 'skill_id_or_search_query_required'}
        visible = {
            item.skill_id: item
            for item in self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_id)
        }
        record = visible.get(skill_name)
        if record is None:
            return {'ok': False, 'error': f'Skill not visible: {skill_id}'}
        if self._skill_record_is_repair_required(record):
            return self._repair_required_skill_payload(record)
        path = Path(record.skill_doc_path) if record.skill_doc_path else None
        content = path.read_text(encoding='utf-8') if path and path.exists() else ''
        return {
            'ok': True,
            'skill_id': record.skill_id,
            'content': content,
        }

    def load_skill_context_v2(
        self,
        *,
        actor_role: str,
        session_id: str,
        skill_id: str = '',
        search_query: str = '',
        limit: int | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        skill_name = str(skill_id or '').strip()
        if not skill_name:
            if str(search_query or '').strip():
                return self._search_visible_skills(
                    actor_role=actor_role,
                    session_id=session_id,
                    search_query=search_query,
                    limit=limit,
                )
            return {'ok': False, 'error': 'skill_id_or_search_query_required'}
        visible = {
            item.skill_id: item
            for item in self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_id)
        }
        record = visible.get(skill_name)
        if record is None:
            return {'ok': False, 'error': f'Skill not visible: {skill_id}'}
        if self._skill_record_is_repair_required(record):
            return self._repair_required_skill_payload(record)
        path = Path(record.skill_doc_path) if record.skill_doc_path else None
        content = path.read_text(encoding='utf-8') if path and path.exists() else ''
        payload = layered_body_payload(
            body=content,
            title=str(getattr(record, 'display_name', '') or ''),
            description=str(getattr(record, 'description', '') or ''),
            path=str(path) if path else '',
        )
        return {
            'ok': True,
            'skill_id': record.skill_id,
            'uri': f'g3ku://skill/{record.skill_id}',
            'level': payload['level'],
            'content': payload['content'],
            'l0': payload['l0'],
            'l1': payload['l1'],
            'path': payload['path'],
        }

    def load_tool_context(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str = '',
        search_query: str = '',
        limit: int | None = None,
    ) -> dict[str, Any]:
        tool_name = str(tool_id or '').strip()
        if not tool_name:
            if str(search_query or '').strip():
                return self._search_visible_tools(
                    actor_role=actor_role,
                    session_id=session_id,
                    search_query=search_query,
                    limit=limit,
                )
            return {'ok': False, 'error': 'tool_id_or_search_query_required'}
        visible = self._visible_tool_family_map(actor_role=actor_role, session_id=session_id)
        if tool_name not in visible:
            return {'ok': False, 'error': f'Tool not visible: {tool_id}'}
        if self._resource_manager is None:
            return {'ok': False, 'error': 'Resource manager unavailable'}
        toolskill = self.get_tool_toolskill(tool_name) or {}
        content = str(toolskill.get('content') or '')
        resolved_tool_id = str(toolskill.get('tool_id') or tool_name)
        contract_payload = self._tool_context_contract_payload(
            actor_role=actor_role,
            session_id=session_id,
            requested_tool_id=tool_name,
            resolved_tool_id=resolved_tool_id,
            callable_value=toolskill.get('callable'),
            available_value=toolskill.get('available'),
        )
        payload = {
            'ok': True,
            'tool_id': resolved_tool_id,
            'content': content,
            'tool_type': toolskill.get('tool_type'),
            'install_dir': toolskill.get('install_dir'),
            'callable': toolskill.get('callable'),
            'available': toolskill.get('available'),
            'repair_required': bool(toolskill.get('repair_required')),
            'parameters_schema': toolskill.get('parameters_schema') or {'type': 'object', 'properties': {}, 'required': []},
            'required_parameters': list(toolskill.get('required_parameters') or []),
            'parameter_contract_markdown': str(toolskill.get('parameter_contract_markdown') or ''),
            'example_arguments': toolskill.get('example_arguments') or {},
            'warnings': list(toolskill.get('warnings') or []),
            'errors': list(toolskill.get('errors') or []),
            'exec_runtime_policy': toolskill.get('exec_runtime_policy'),
            **contract_payload,
        }
        fingerprint = build_tool_context_fingerprint(payload)
        if fingerprint:
            payload['tool_context_fingerprint'] = fingerprint
        return payload

    def load_tool_context_v2(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str = '',
        search_query: str = '',
        limit: int | None = None,
        **_unused: Any,
    ) -> dict[str, Any]:
        tool_name = str(tool_id or '').strip()
        if not tool_name:
            if str(search_query or '').strip():
                return self._search_visible_tools(
                    actor_role=actor_role,
                    session_id=session_id,
                    search_query=search_query,
                    limit=limit,
                )
            return {'ok': False, 'error': 'tool_id_or_search_query_required'}
        visible = self._visible_tool_family_map(actor_role=actor_role, session_id=session_id)
        if tool_name not in visible:
            return {'ok': False, 'error': f'Tool not visible: {tool_id}'}
        if self._resource_manager is None:
            return {'ok': False, 'error': 'Resource manager unavailable'}
        toolskill = self.get_tool_toolskill(tool_name) or {}
        content = str(toolskill.get('content') or '')
        resolved_tool_id = str(toolskill.get('tool_id') or tool_name)
        contract_payload = self._tool_context_contract_payload(
            actor_role=actor_role,
            session_id=session_id,
            requested_tool_id=tool_name,
            resolved_tool_id=resolved_tool_id,
            callable_value=toolskill.get('callable'),
            available_value=toolskill.get('available'),
        )
        layered_payload = layered_body_payload(
            body=content,
            title=str(toolskill.get('tool_id') or tool_name),
            description=str(toolskill.get('description') or ''),
            path=str(toolskill.get('path') or ''),
        )
        payload = {
            'ok': True,
            'tool_id': resolved_tool_id,
            'uri': f'g3ku://resource/tool/{resolved_tool_id}',
            'level': layered_payload['level'],
            'content': layered_payload['content'],
            'l0': layered_payload['l0'],
            'l1': layered_payload['l1'],
            'path': layered_payload['path'],
            'tool_type': toolskill.get('tool_type'),
            'install_dir': toolskill.get('install_dir'),
            'callable': toolskill.get('callable'),
            'available': toolskill.get('available'),
            'repair_required': bool(toolskill.get('repair_required')),
            'parameters_schema': toolskill.get('parameters_schema') or {'type': 'object', 'properties': {}, 'required': []},
            'required_parameters': list(toolskill.get('required_parameters') or []),
            'parameter_contract_markdown': str(toolskill.get('parameter_contract_markdown') or ''),
            'example_arguments': toolskill.get('example_arguments') or {},
            'warnings': list(toolskill.get('warnings') or []),
            'errors': list(toolskill.get('errors') or []),
            'exec_runtime_policy': toolskill.get('exec_runtime_policy'),
            **contract_payload,
        }
        fingerprint = build_tool_context_fingerprint(payload)
        if fingerprint:
            payload['tool_context_fingerprint'] = fingerprint
        return payload

    def list_skill_resources(self) -> list[Any]:
        return list(self.resource_registry.list_skill_resources())

    def get_skill_resource(self, skill_id: str):
        return self.resource_registry.get_skill_resource(str(skill_id or '').strip())

    def list_skill_files(self, skill_id: str) -> dict[str, str]:
        return {key: str(path) for key, path in self.resource_registry.skill_file_map(str(skill_id or '').strip()).items()}

    def read_skill_file(self, skill_id: str, file_key: str) -> str:
        path = self.resource_registry.skill_file_map(str(skill_id or '').strip()).get(str(file_key or '').strip())
        if path is None:
            raise ValueError('editable_file_not_allowed')
        return path.read_text(encoding='utf-8')

    def capture_resource_tree_state(self) -> dict[str, dict[str, str]]:
        manager = getattr(self, '_resource_manager', None)
        if manager is None or not hasattr(manager, 'capture_resource_tree_state'):
            return {}
        return manager.capture_resource_tree_state()

    def refresh_resource_paths(
        self,
        paths: list[str | Path],
        *,
        trigger: str = 'path-change',
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        manager = getattr(self, '_resource_manager', None)
        registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        if manager is not None and hasattr(manager, 'refresh_paths'):
            manager.refresh_paths(list(paths or []), trigger=trigger)
            if registry is not None and hasattr(registry, 'refresh_from_current_resources'):
                skills, tools = registry.refresh_from_current_resources()
                if policy_engine is not None and hasattr(policy_engine, 'sync_default_role_policies'):
                    policy_engine.sync_default_role_policies()
                self._record_resource_tree_state()
                return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}
        fallback = getattr(self, 'reload_resources', None)
        if callable(fallback):
            return fallback(session_id=session_id)
        return {'ok': True, 'session_id': session_id, 'skills': 0, 'tools': 0}

    def refresh_changed_resources(
        self,
        before_state: dict[str, dict[str, str]] | None,
        *,
        trigger: str = 'path-change',
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        manager = getattr(self, '_resource_manager', None)
        registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        if manager is not None and hasattr(manager, 'refresh_changed_tree_state'):
            manager.refresh_changed_tree_state(before_state, trigger=trigger)
            if registry is not None and hasattr(registry, 'refresh_from_current_resources'):
                skills, tools = registry.refresh_from_current_resources()
                if policy_engine is not None and hasattr(policy_engine, 'sync_default_role_policies'):
                    policy_engine.sync_default_role_policies()
                self._record_resource_tree_state()
                return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}
        fallback = getattr(self, 'reload_resources', None)
        if callable(fallback):
            return fallback(session_id=session_id)
        return {'ok': True, 'session_id': session_id, 'skills': 0, 'tools': 0}

    def write_skill_file(self, skill_id: str, file_key: str, content: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        path = self.resource_registry.skill_file_map(str(skill_id or '').strip()).get(str(file_key or '').strip())
        if path is None:
            raise ValueError('editable_file_not_allowed')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or ''), encoding='utf-8')
        self.refresh_resource_paths([path], trigger='skill-file-write', session_id=session_id)
        return {'skill_id': str(skill_id or '').strip(), 'file_key': str(file_key or '').strip(), 'path': str(path)}

    async def write_skill_file_async(
        self,
        skill_id: str,
        file_key: str,
        content: str,
        *,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        item = self.write_skill_file(skill_id, file_key, content, session_id=session_id)
        item.update(await self._sync_catalog_targets(skill_ids={str(skill_id or '').strip()}))
        return item

    def _workspace_root(self) -> Path:
        manager = getattr(self, '_resource_manager', None)
        workspace = getattr(manager, 'workspace', None)
        return Path(workspace).resolve(strict=False) if workspace is not None else Path.cwd().resolve()

    @staticmethod
    def _safe_task_dir_name(task_id: str) -> str:
        return str(task_id or '').strip().replace(':', '_').replace('/', '_').replace('\\', '_')

    def _task_temp_root(self, *, create: bool = True) -> Path:
        root = self._workspace_root() / 'temp' / 'tasks'
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    def _task_temp_dir(self, task_id: str, *, create: bool = True) -> Path:
        path = self._task_temp_root(create=create) / self._safe_task_dir_name(task_id)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _task_file_dir_path(self, task_id: str) -> Path:
        return Path(self.file_store.base_dir) / self._safe_task_dir_name(task_id)

    def _task_artifact_dir(self, task_id: str) -> Path:
        return Path(self.artifact_store._task_dir(task_id))

    def _task_event_history_dir(self, task_id: str) -> Path:
        return Path(self.store._event_history_dir) / self._safe_task_dir_name(task_id)

    def _effective_task_temp_dir(self, task_id: str) -> Path:
        runtime_meta = self.log_service.read_task_runtime_meta(task_id) or {}
        configured = str(runtime_meta.get('task_temp_dir') or '').strip()
        legacy_temp_root = (self._workspace_root() / 'temp').resolve(strict=False)
        if configured:
            configured_path = Path(configured).expanduser().resolve(strict=False)
            if configured_path != legacy_temp_root:
                return configured_path
        return self._task_temp_dir(task_id, create=False)

    @classmethod
    def _remove_directory_tree_if_empty(cls, path: Path) -> bool:
        if not path.exists() or not path.is_dir():
            return False
        try:
            children = list(path.iterdir())
        except OSError:
            return False
        for child in children:
            if child.is_symlink() or not child.is_dir():
                return False
            cls._remove_directory_tree_if_empty(child)
        try:
            if any(path.iterdir()):
                return False
        except OSError:
            return False
        try:
            path.rmdir()
        except OSError:
            return False
        return True

    def _cleanup_terminal_task_temp_dir_if_empty(self, task: TaskRecord) -> None:
        task_id = str(getattr(task, 'task_id', '') or '').strip()
        if not task_id:
            return
        try:
            self._remove_directory_tree_if_empty(self._effective_task_temp_dir(task_id))
        except Exception:
            return

    def _resource_base_dir(self, kind: ResourceKind) -> Path:
        manager = getattr(self, '_resource_manager', None)
        registry = getattr(manager, '_registry', None)
        if kind is ResourceKind.SKILL:
            candidate = getattr(registry, 'skills_dir', None)
            fallback = self._workspace_root() / 'skills'
        else:
            candidate = getattr(registry, 'tools_dir', None)
            fallback = self._workspace_root() / 'tools'
        return Path(candidate or fallback).resolve(strict=False)

    @staticmethod
    def _is_relative_to(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
        except ValueError:
            return False
        return True

    def _resolve_workspace_path(self, raw_path: str | Path | None) -> Path:
        path = Path(raw_path or '').expanduser()
        if not path.is_absolute():
            path = self._workspace_root() / path
        return path.resolve(strict=False)

    def _resolve_resource_root(self, raw_path: str | Path | None, *, kind: ResourceKind) -> Path:
        resolved = self._resolve_workspace_path(raw_path)
        base_dir = self._resource_base_dir(kind)
        if not self._is_relative_to(resolved, base_dir):
            raise ValueError(f'{kind.value}_path_outside_workspace')
        if resolved == base_dir:
            raise ValueError(f'{kind.value}_path_invalid')
        return resolved

    def _resource_is_busy(self, kind: ResourceKind, *names: str) -> bool:
        manager = getattr(self, '_resource_manager', None)
        if manager is None or not hasattr(manager, 'busy_state'):
            return False
        for raw_name in names:
            name = str(raw_name or '').strip()
            if not name:
                continue
            try:
                state = manager.busy_state(kind, name)
            except Exception:
                continue
            if bool(getattr(state, 'busy', False)):
                return True
        return False

    @staticmethod
    def _display_role_label(role: str) -> str:
        return {
            'ceo': '主Agent',
            'execution': '执行',
            'inspection': '检验',
        }.get(str(role or '').strip().lower(), str(role or '').strip())

    def _running_task_records(self) -> list[TaskRecord]:
        try:
            tasks = self.store.list_tasks()
        except Exception:
            return []
        return [
            task
            for task in tasks
            if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress' and not bool(getattr(task, 'is_paused', False))
        ]

    def _running_ceo_session_ids(self) -> list[str]:
        loop = getattr(self, '_runtime_loop', None)
        active_tasks = getattr(loop, '_active_tasks', None)
        if not isinstance(active_tasks, dict):
            return []
        session_ids: set[str] = set()
        for raw_session_id, bucket in active_tasks.items():
            session_id = str(raw_session_id or '').strip()
            if not session_id.startswith('web:'):
                continue
            if not bucket:
                continue
            session_ids.add(session_id)
        return sorted(session_ids)

    def _session_title(self, session_id: str) -> str:
        loop = getattr(self, '_runtime_loop', None)
        session_manager = getattr(loop, 'sessions', None)
        if session_manager is None:
            return session_id
        get_path = getattr(session_manager, 'get_path', None)
        if callable(get_path):
            try:
                path = get_path(session_id)
                if path is not None and not Path(path).exists():
                    return session_id
            except Exception:
                return session_id
        get_or_create = getattr(session_manager, 'get_or_create', None)
        if not callable(get_or_create):
            return session_id
        try:
            session = get_or_create(session_id)
        except Exception:
            return session_id
        metadata = getattr(session, 'metadata', None) or {}
        title = str(metadata.get('title') or '').strip() if isinstance(metadata, dict) else ''
        return title or str(getattr(session, 'key', '') or '').strip() or session_id

    def _skill_visible_roles_for_task(self, task: TaskRecord, skill_id: str) -> list[str]:
        roles: list[str] = []
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        for actor_role in ('execution', 'inspection'):
            visible_ids = {
                str(getattr(item, 'skill_id', '') or '').strip()
                for item in self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)
            }
            if skill_id in visible_ids:
                roles.append(actor_role)
        return roles

    def _tool_visible_roles_for_task(self, task: TaskRecord, tool_id: str) -> list[str]:
        roles: list[str] = []
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        for actor_role in ('execution', 'inspection'):
            visible_ids = {
                str(getattr(item, 'tool_id', '') or '').strip()
                for item in self.list_visible_tool_families(actor_role=actor_role, session_id=session_id)
            }
            if tool_id in visible_ids:
                roles.append(actor_role)
        return roles

    @classmethod
    def _format_usage_message(
        cls,
        *,
        resource_label: str,
        display_name: str,
        usage: dict[str, list[dict[str, Any]]],
    ) -> str:
        tasks = list(usage.get('tasks') or [])
        ceo_sessions = list(usage.get('ceo_sessions') or [])
        blockers: list[str] = []
        if tasks:
            blockers.append(f'{len(tasks)} 个进行中的任务')
        if ceo_sessions:
            blockers.append(f'{len(ceo_sessions)} 个正在运行的主Agent会话')
        message = f'无法删除{resource_label}“{display_name}”，当前有{"、".join(blockers)}正在使用。'
        previews: list[str] = []
        if tasks:
            task_text = '；'.join(
                (
                    f"{str(item.get('title') or item.get('task_id') or '未命名任务').strip()} ({str(item.get('task_id') or '').strip()})"
                    + (
                        f" / {'、'.join(cls._display_role_label(role) for role in list(item.get('actor_roles') or []))}"
                        if list(item.get('actor_roles') or [])
                        else ''
                    )
                )
                for item in tasks[:3]
            )
            if len(tasks) > 3:
                task_text += f'；等 {len(tasks)} 个'
            previews.append(f'任务：{task_text}')
        if ceo_sessions:
            ceo_text = '；'.join(
                f"{str(item.get('title') or item.get('session_id') or '未命名会话').strip()} ({str(item.get('session_id') or '').strip()})"
                for item in ceo_sessions[:3]
            )
            if len(ceo_sessions) > 3:
                ceo_text += f'；等 {len(ceo_sessions)} 个'
            previews.append(f'主Agent：{ceo_text}')
        return f"{message} {' '.join(previews)}".strip()

    def _skill_usage_summary(self, skill_id: str) -> dict[str, list[dict[str, Any]]]:
        usage: dict[str, list[dict[str, Any]]] = {'tasks': [], 'ceo_sessions': []}
        for task in self._running_task_records():
            actor_roles = self._skill_visible_roles_for_task(task, skill_id)
            if not actor_roles:
                continue
            usage['tasks'].append(
                {
                    'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                    'title': str(getattr(task, 'title', '') or '').strip(),
                    'session_id': str(getattr(task, 'session_id', '') or '').strip(),
                    'actor_roles': actor_roles,
                }
            )
        for session_id in self._running_ceo_session_ids():
            visible_ids = {
                str(getattr(item, 'skill_id', '') or '').strip()
                for item in self.list_visible_skill_resources(actor_role='ceo', session_id=session_id)
            }
            if skill_id not in visible_ids:
                continue
            usage['ceo_sessions'].append(
                {
                    'session_id': session_id,
                    'title': self._session_title(session_id),
                }
            )
        return usage

    def _tool_usage_summary(self, tool_id: str) -> dict[str, list[dict[str, Any]]]:
        usage: dict[str, list[dict[str, Any]]] = {'tasks': [], 'ceo_sessions': []}
        for task in self._running_task_records():
            actor_roles = self._tool_visible_roles_for_task(task, tool_id)
            if not actor_roles:
                continue
            usage['tasks'].append(
                {
                    'task_id': str(getattr(task, 'task_id', '') or '').strip(),
                    'title': str(getattr(task, 'title', '') or '').strip(),
                    'session_id': str(getattr(task, 'session_id', '') or '').strip(),
                    'actor_roles': actor_roles,
                }
            )
        for session_id in self._running_ceo_session_ids():
            visible_ids = {
                str(getattr(item, 'tool_id', '') or '').strip()
                for item in self.list_visible_tool_families(actor_role='ceo', session_id=session_id)
            }
            if tool_id not in visible_ids:
                continue
            usage['ceo_sessions'].append(
                {
                    'session_id': session_id,
                    'title': self._session_title(session_id),
                }
            )
        return usage

    def _raise_if_skill_in_use(self, skill) -> None:
        target_skill_id = str(getattr(skill, 'skill_id', '') or '').strip()
        display_name = str(getattr(skill, 'display_name', '') or target_skill_id).strip() or target_skill_id
        usage = self._skill_usage_summary(target_skill_id)
        if not usage['tasks'] and not usage['ceo_sessions']:
            return
        raise ResourceDeleteBlockedError(
            code='skill_in_use',
            message=self._format_usage_message(resource_label='Skill', display_name=display_name, usage=usage),
            resource_kind='skill',
            resource_id=target_skill_id,
            usage=usage,
        )

    def _raise_if_tool_in_use(self, family) -> None:
        target_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
        display_name = str(getattr(family, 'display_name', '') or target_tool_id).strip() or target_tool_id
        usage = self._tool_usage_summary(target_tool_id)
        if not usage['tasks'] and not usage['ceo_sessions']:
            return
        raise ResourceDeleteBlockedError(
            code='tool_in_use',
            message=self._format_usage_message(resource_label='工具', display_name=display_name, usage=usage),
            resource_kind='tool',
            resource_id=target_tool_id,
            usage=usage,
        )

    def _delete_path(self, path: Path, *, deleted_paths: list[str]) -> None:
        if not path.exists():
            return
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            return
        except Exception as exc:
            raise ValueError(f'resource_delete_failed:{path}:{exc}') from exc
        deleted_paths.append(str(path))

    def _collect_workspace_delete_path(
        self,
        raw_path: str | Path | None,
        *,
        delete_paths: set[Path],
        skipped_paths: list[str],
    ) -> None:
        text = str(raw_path or '').strip()
        if not text:
            return
        resolved = self._resolve_workspace_path(text)
        workspace_root = self._workspace_root()
        if not self._is_relative_to(resolved, workspace_root):
            skipped_paths.append(str(resolved))
            return
        if resolved == workspace_root:
            skipped_paths.append(str(resolved))
            return
        delete_paths.add(resolved)

    def delete_skill_resource(self, skill_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        skill = self.get_skill_resource(skill_id)
        if skill is None:
            raise ValueError('skill_not_found')
        target_skill_id = str(skill.skill_id or '').strip()
        self._raise_if_skill_in_use(skill)
        if self._resource_is_busy(ResourceKind.SKILL, target_skill_id):
            raise ValueError('skill_busy')

        before_state = self.capture_resource_tree_state()
        skill_root = self._resolve_resource_root(skill.source_path, kind=ResourceKind.SKILL)
        deleted_paths: list[str] = []
        self._delete_path(skill_root, deleted_paths=deleted_paths)
        refresh_result = self.refresh_changed_resources(
            before_state,
            trigger='skill-delete',
            session_id=session_id,
        )
        self.governance_store.delete_role_policies_for_resource(
            resource_kind='skill',
            resource_id=target_skill_id,
        )
        return {
            'skill_id': target_skill_id,
            'path': str(skill_root),
            'deleted_paths': deleted_paths,
            'resources': refresh_result,
        }

    async def delete_skill_resource_async(
        self,
        skill_id: str,
        *,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        target_skill_id = str(skill_id or '').strip()
        item = self.delete_skill_resource(target_skill_id, session_id=session_id)
        item.update(await self._sync_catalog_targets(skill_ids={target_skill_id}))
        return item

    def update_skill_policy(self, skill_id: str, *, session_id: str = 'web:shared', enabled: bool | None = None, allowed_roles: list[str] | None = None):
        skill = self.get_skill_resource(skill_id)
        if skill is None:
            return None
        updated = skill.model_copy(update={
            'enabled': skill.enabled if enabled is None else bool(enabled),
            'allowed_roles': list(skill.allowed_roles if allowed_roles is None else allowed_roles),
        })
        self.governance_store.upsert_skill_resource(updated, updated_at=now_iso())
        self.policy_engine.sync_default_role_policies()
        return updated

    def enable_skill(self, skill_id: str, *, session_id: str = 'web:shared'):
        return self.update_skill_policy(skill_id, session_id=session_id, enabled=True)

    def disable_skill(self, skill_id: str, *, session_id: str = 'web:shared'):
        return self.update_skill_policy(skill_id, session_id=session_id, enabled=False)

    def _raw_tool_family(self, tool_id: str):
        normalized = self._resolve_tool_family_alias(tool_id)
        family = self.resource_registry.get_tool_family(str(normalized or '').strip())
        if family is None and normalized != str(tool_id or '').strip():
            family = self.resource_registry.get_tool_family(str(tool_id or '').strip())
        return family

    def _configured_core_tool_entries(self) -> list[str]:
        if self._resource_manager is not None:
            descriptor = self._resource_manager.get_tool_descriptor('memory_runtime')
            if descriptor is not None:
                try:
                    settings = validate_tool_settings(
                        MemoryRuntimeSettings,
                        raw_tool_settings_from_descriptor(descriptor),
                        tool_name='memory_runtime',
                    )
                    return [str(item).strip() for item in list(settings.assembly.core_tools or []) if str(item).strip()]
                except Exception:
                    pass
        return configured_core_tools(resource_manager=self._resource_manager)

    def _core_tool_resolution(self):
        return resolve_core_tool_targets(
            self._configured_core_tool_entries(),
            list(self.resource_registry.list_tool_families()),
        )

    def _decorate_tool_family(self, family):
        if family is None:
            return None
        resolution = self._core_tool_resolution()
        metadata = dict(getattr(family, 'metadata', {}) or {})
        metadata['repair_required'] = bool(getattr(family, 'callable', True)) and not bool(getattr(family, 'available', True))
        normalized_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
        return family.model_copy(
            update={
                'tool_id': self._external_tool_family_id(str(getattr(family, 'tool_id', '') or '')),
                'is_core': normalized_tool_id in resolution.family_ids,
                'metadata': metadata,
            }
        )

    def list_tool_resources(self) -> list[Any]:
        return [self._decorate_tool_family(item) for item in self.resource_registry.list_tool_families()]

    def get_tool_family(self, tool_id: str):
        return self._decorate_tool_family(self._raw_tool_family(tool_id))

    def get_governance_mode(self) -> dict[str, Any]:
        return {
            'enabled': self.governance_store.get_bool_meta(GOVERNANCE_MODE_META_KEY, default=False),
            'updated_at': str(self.governance_store.get_meta(GOVERNANCE_MODE_UPDATED_AT_META_KEY) or ''),
        }

    def update_governance_mode(self, *, enabled: bool) -> dict[str, Any]:
        self.governance_store.set_bool_meta(GOVERNANCE_MODE_META_KEY, bool(enabled))
        updated_at = now_iso()
        self.governance_store.set_meta(GOVERNANCE_MODE_UPDATED_AT_META_KEY, updated_at)
        return {
            'enabled': bool(enabled),
            'updated_at': updated_at,
        }

    @staticmethod
    def _normalize_permission_risk_level(value: Any, *, default: str = 'medium') -> str:
        normalized = str(value or '').strip().lower()
        if normalized in {'low', 'medium', 'high'}:
            return normalized
        fallback = str(default or 'medium').strip().lower()
        return fallback if fallback in {'low', 'medium', 'high'} else 'medium'

    def frontdoor_reviewable_tool_risk_map(self, *, actor_role: str, session_id: str) -> dict[str, str]:
        if not bool(self.get_governance_mode().get('enabled')):
            return {}
        normalized_actor_role = str(actor_role or 'ceo').strip().lower() or 'ceo'
        normalized_session_id = self._normalize_session_key(session_id)
        visible_names = set(
            self.list_effective_tool_names(
                actor_role=normalized_actor_role,
                session_id=normalized_session_id,
            )
        )
        if not visible_names:
            return {}
        risk_rank = {'low': 0, 'medium': 1, 'high': 2}
        reviewable: dict[str, str] = {}
        families = list(
            self.list_visible_tool_families(
                actor_role=normalized_actor_role,
                session_id=normalized_session_id,
            ) or []
        )
        for family in families:
            family_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            for action in list(getattr(family, 'actions', []) or []):
                risk_level = self._normalize_permission_risk_level(
                    getattr(action, 'risk_level', 'medium'),
                )
                if risk_level not in {'medium', 'high'}:
                    continue
                executor_names = [
                    str(name or '').strip()
                    for name in list(getattr(action, 'executor_names', []) or [])
                    if str(name or '').strip()
                ]
                if not executor_names:
                    fallback_names: list[str] = []
                    primary_executor_name = str(
                        resolve_primary_executor_name(family, resource_manager=self._resource_manager) or ''
                    ).strip()
                    if primary_executor_name:
                        fallback_names.append(primary_executor_name)
                    if family_tool_id:
                        fallback_names.append(family_tool_id)
                    executor_names = fallback_names
                for executor_name in executor_names:
                    if executor_name not in visible_names:
                        continue
                    current = reviewable.get(executor_name, 'low')
                    if risk_rank[risk_level] >= risk_rank.get(current, 0):
                        reviewable[executor_name] = risk_level
        return reviewable

    def reconcile_core_tool_families(self) -> bool:
        resolution = self._core_tool_resolution()
        changed = False
        for family in list(self.resource_registry.list_tool_families()):
            if str(getattr(family, 'tool_id', '') or '').strip() not in resolution.family_ids:
                continue
            family_changed = not bool(getattr(family, 'enabled', True))
            if not family_changed:
                continue
            updated = family.model_copy(update={'enabled': True})
            self.governance_store.upsert_tool_family(updated, updated_at=now_iso())
            changed = True
        return changed

    def _tool_family_executor_name(self, family) -> str:
        return resolve_primary_executor_name(family, resource_manager=self._resource_manager)

    def get_tool_toolskill(self, tool_id: str) -> dict[str, Any] | None:
        payload = build_tool_toolskill_payload(
            self._resolve_tool_family_alias(tool_id),
            raw_tool_family_getter=self._raw_tool_family,
            resource_registry=self.resource_registry,
            resource_manager=self._resource_manager,
        )
        if isinstance(payload, dict):
            payload['family_tool_id'] = self._external_tool_family_id(str(payload.get('family_tool_id') or ''))
        return payload

    def delete_tool_resource(self, tool_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        family = self._raw_tool_family(tool_id)
        if family is None:
            raise ValueError('tool_not_found')

        target_tool_id = str(family.tool_id or '').strip()
        if target_tool_id in self._core_tool_resolution().family_ids:
            raise ResourceMutationBlockedError(
                code='core_tool_delete_forbidden',
                message='Core tool families cannot be deleted.',
                resource_kind='tool_family',
                resource_id=target_tool_id,
            )
        self._raise_if_tool_in_use(family)
        descriptor_names: set[str] = {
            str(getattr(family, 'primary_executor_name', '') or '').strip(),
            target_tool_id,
        }
        for action in list(getattr(family, 'actions', []) or []):
            descriptor_names.update(
                str(name or '').strip()
                for name in list(getattr(action, 'executor_names', []) or [])
                if str(name or '').strip()
            )
        descriptor_names.discard('')

        if self._resource_is_busy(ResourceKind.TOOL, *sorted(descriptor_names)):
            raise ValueError('tool_busy')

        before_state = self.capture_resource_tree_state()
        delete_paths: set[Path] = set()
        skipped_paths: list[str] = []
        delete_paths.add(self._resolve_resource_root(family.source_path, kind=ResourceKind.TOOL))

        manager = getattr(self, '_resource_manager', None)
        for descriptor_name in sorted(descriptor_names):
            descriptor = (
                manager.get_tool_descriptor(descriptor_name)
                if manager is not None and hasattr(manager, 'get_tool_descriptor')
                else None
            )
            if descriptor is None:
                continue
            delete_paths.add(self._resolve_resource_root(descriptor.root, kind=ResourceKind.TOOL))
            self._collect_workspace_delete_path(
                getattr(descriptor, 'install_dir', None),
                delete_paths=delete_paths,
                skipped_paths=skipped_paths,
            )
        self._collect_workspace_delete_path(
            getattr(family, 'install_dir', None),
            delete_paths=delete_paths,
            skipped_paths=skipped_paths,
        )

        deleted_paths: list[str] = []
        for path in sorted(delete_paths, key=lambda item: (len(str(item)), str(item)), reverse=True):
            self._delete_path(path, deleted_paths=deleted_paths)

        refresh_result = self.refresh_changed_resources(
            before_state,
            trigger='tool-delete',
            session_id=session_id,
        )
        self.governance_store.delete_role_policies_for_resource(
            resource_kind='tool_family',
            resource_id=target_tool_id,
        )
        return {
            'tool_id': target_tool_id,
            'path': str(self._resolve_resource_root(family.source_path, kind=ResourceKind.TOOL)),
            'deleted_paths': deleted_paths,
            'skipped_paths': skipped_paths,
            'resources': refresh_result,
        }

    async def delete_tool_resource_async(
        self,
        tool_id: str,
        *,
        session_id: str = 'web:shared',
    ) -> dict[str, Any]:
        target_tool_id = str(tool_id or '').strip()
        item = self.delete_tool_resource(target_tool_id, session_id=session_id)
        item.update(await self._sync_catalog_targets(tool_ids={target_tool_id}))
        return item

    def update_tool_policy(
        self,
        tool_id: str,
        *,
        session_id: str = 'web:shared',
        enabled: bool | None = None,
        allowed_roles_by_action: dict[str, list[str]] | None = None,
        execution_mode: str | None = None,
    ):
        family = self._raw_tool_family(tool_id)
        if family is None:
            return None
        target_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
        is_core = target_tool_id in self._core_tool_resolution().family_ids
        if is_core and enabled is not None and not bool(enabled):
            raise ResourceMutationBlockedError(
                code='core_tool_disable_forbidden',
                message='Core tool families cannot be disabled.',
                resource_kind='tool_family',
                resource_id=target_tool_id,
            )
        allowed_roles_by_action = dict(allowed_roles_by_action or {})
        actions = []
        for action in family.actions:
            roles = allowed_roles_by_action.get(action.action_id)
            if str(getattr(action, 'admin_mode', 'editable') or 'editable') == 'readonly_system' and roles is not None:
                normalized_roles = normalize_public_allowed_roles([str(role) for role in list(roles or [])])
                current_roles = normalize_public_allowed_roles(list(getattr(action, 'allowed_roles', []) or []))
                if normalized_roles != current_roles:
                    raise ResourceMutationBlockedError(
                        code='tool_action_readonly',
                        message='Readonly system actions cannot be edited.',
                        resource_kind='tool_family',
                        resource_id=target_tool_id,
                        details={'action_id': action.action_id},
                    )
            next_roles = (
                list(getattr(action, 'allowed_roles', []) or [])
                if roles is None
                else normalize_public_allowed_roles([str(role) for role in list(roles or [])])
            )
            actions.append(action.model_copy(update={'allowed_roles': next_roles}))
        if execution_mode is not None and not exec_tool_supports_execution_mode(target_tool_id):
            raise ResourceMutationBlockedError(
                code='tool_execution_mode_unsupported',
                message='execution_mode is only supported for exec_runtime.',
                resource_kind='tool_family',
                resource_id=target_tool_id,
            )
        updated = family.model_copy(
            update={
                'enabled': family.enabled if enabled is None else bool(enabled),
                'actions': actions,
                'metadata': merge_exec_execution_mode_metadata(
                    getattr(family, 'metadata', {}) or {},
                    execution_mode=execution_mode,
                ),
            }
        )
        self.governance_store.upsert_tool_family(updated, updated_at=now_iso())
        self.policy_engine.sync_default_role_policies()
        return self.get_tool_family(target_tool_id)

    def enable_tool(self, tool_id: str, *, session_id: str = 'web:shared'):
        return self.update_tool_policy(tool_id, session_id=session_id, enabled=True)

    def disable_tool(self, tool_id: str, *, session_id: str = 'web:shared'):
        return self.update_tool_policy(tool_id, session_id=session_id, enabled=False)

    def is_tool_action_allowed(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str,
        action_id: str,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> bool:
        decision = self.policy_engine.evaluate_tool_action(
            subject=self._subject(actor_role=actor_role, session_id=session_id, task_id=task_id, node_id=node_id),
            tool_id=str(tool_id or '').strip(),
            action_id=str(action_id or '').strip(),
        )
        return bool(decision.allowed)

    def reload_resources(self, *, session_id: str = 'web:shared') -> dict[str, Any]:
        if self._resource_manager is not None:
            self._resource_manager.reload_now(trigger='manual')
        skills, tools = self.resource_registry.refresh_from_current_resources()
        self.reconcile_core_tool_families()
        self.policy_engine.sync_default_role_policies()
        self._record_resource_tree_state()
        return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}

    async def reload_resources_async(self, *, session_id: str = 'web:shared') -> dict[str, Any]:
        result = self.reload_resources(session_id=session_id)
        sync_result = await self._sync_catalog_targets(
            skill_ids={
                str(getattr(item, 'skill_id', '') or '').strip()
                for item in list(self.list_skill_resources() or [])
                if str(getattr(item, 'skill_id', '') or '').strip()
            },
            tool_ids={
                str(getattr(item, 'tool_id', '') or '').strip()
                for item in list(self.list_tool_resources() or [])
                if str(getattr(item, 'tool_id', '') or '').strip()
            },
        )
        if bool(sync_result.get('catalog_synced')):
            result['catalog'] = sync_result.get('catalog', {'created': 0, 'updated': 0, 'removed': 0})
        else:
            result['catalog'] = {'created': 0, 'updated': 0, 'removed': 0}
        return result

    async def get_context_traces(self, *, trace_kind: str, limit: int = 20) -> dict[str, Any]:
        manager = self.memory_manager
        if manager is None or not hasattr(manager, 'read_trace_file'):
            return {'ok': True, 'items': [], 'trace_kind': trace_kind, 'limit': max(1, int(limit))}
        items = await manager.read_trace_file(trace_kind=trace_kind, limit=max(1, int(limit)))
        return {'ok': True, 'items': items, 'trace_kind': trace_kind, 'limit': max(1, int(limit))}

    def get_task_detail_payload(
        self,
        task_id: str,
        *,
        mark_read: bool = False,
    ) -> dict[str, Any] | None:
        task_id = self.normalize_task_id(task_id)
        payload = self.query_service.get_task_snapshot(
            task_id,
            mark_read=mark_read,
        )
        if payload is None:
            return None
        return payload

    def get_task_tree_snapshot_payload(self, task_id: str) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        snapshot = self.query_service.get_tree_snapshot(normalized_task_id)
        if snapshot is None:
            return None
        return {'ok': True, **snapshot.model_dump(mode='json')}

    def get_task_tree_subtree_payload(
        self,
        task_id: str,
        node_id: str,
        *,
        round_id: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        snapshot = self.query_service.get_tree_subtree(
            normalized_task_id,
            node_id,
            round_id=round_id,
        )
        if snapshot is None:
            return None
        return {'ok': True, **snapshot.model_dump(mode='json')}

    def get_node_detail_payload(self, task_id: str, node_id: str, detail_level: str = 'summary') -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        normalized_detail_level = self._normalize_node_detail_level(detail_level)
        detail = self.query_service.get_node_detail(normalized_task_id, node_id, detail_level=normalized_detail_level)
        if detail is None:
            return None
        item = self._repair_legacy_display_payload(detail.model_dump(mode='json'))
        runtime_node = self.store.get_node(node_id)
        runtime_metadata = dict((runtime_node.metadata or {}) if runtime_node is not None else {})
        latest_live_distribution_round_id = ''
        if runtime_node is not None:
            latest_live_round = self.node_runner._latest_incomplete_spawn_round(parent=runtime_node)
            if latest_live_round is not None:
                latest_live_distribution_round_id = str(latest_live_round[0] or '').strip()
            elif self.node_runner.node_is_in_live_distribution_tree(
                task_id=normalized_task_id,
                node_id=node_id,
            ):
                latest_live_distribution_round_id = str(runtime_metadata.get('spawn_owner_round_id') or '').strip()
        item['spawn_owner_parent_node_id'] = str(runtime_metadata.get('spawn_owner_parent_node_id') or '').strip()
        item['spawn_owner_round_id'] = str(runtime_metadata.get('spawn_owner_round_id') or '').strip()
        item['spawn_owner_entry_index'] = int(runtime_metadata.get('spawn_owner_entry_index') or 0)
        item['spawn_owner_kind'] = str(runtime_metadata.get('spawn_owner_kind') or '').strip()
        item['latest_live_distribution_round_id'] = latest_live_distribution_round_id
        latest_context = self.get_node_latest_context_payload(normalized_task_id, node_id)
        if latest_context is not None:
            if not str(item.get('actual_request_ref') or '').strip():
                item['actual_request_ref'] = str(
                    latest_context.get('actual_request_ref') or latest_context.get('ref') or ''
                ).strip()
            if not str(item.get('prompt_cache_key_hash') or '').strip():
                item['prompt_cache_key_hash'] = str(latest_context.get('prompt_cache_key_hash') or '').strip()
            if not str(item.get('actual_request_hash') or '').strip():
                item['actual_request_hash'] = str(latest_context.get('actual_request_hash') or '').strip()
            if not int(item.get('actual_request_message_count') or 0):
                item['actual_request_message_count'] = int(latest_context.get('actual_request_message_count') or 0)
            if not str(item.get('actual_tool_schema_hash') or '').strip():
                item['actual_tool_schema_hash'] = str(latest_context.get('actual_tool_schema_hash') or '').strip()
        if normalized_detail_level == 'summary':
            item.pop('execution_trace', None)
        else:
            item.pop('execution_trace_summary', None)
        return {
            'ok': True,
            'task_id': normalized_task_id,
            'node_id': node_id,
            'item': item,
        }

    def get_node_latest_context_payload(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.get_task(normalized_task_id)
        node = self.store.get_node(node_id)
        if task is None or node is None or str(node.task_id or '').strip() != normalized_task_id:
            return None
        frame = self.store.get_task_runtime_frame(normalized_task_id, node_id)
        actual_request_ref = ''
        messages_ref = ''
        ref = ''
        prompt_cache_key_hash = ''
        actual_request_hash = ''
        actual_request_message_count = 0
        actual_tool_schema_hash = ''
        observed_input_truth: dict[str, Any] = {}
        if frame is not None:
            frame_payload = dict(frame.payload or {})
            actual_request_ref = str(frame_payload.get('actual_request_ref') or '').strip()
            messages_ref = str(frame_payload.get('messages_ref') or '').strip()
            prompt_cache_key_hash = str(frame_payload.get('prompt_cache_key_hash') or '').strip()
            actual_request_hash = str(frame_payload.get('actual_request_hash') or '').strip()
            actual_request_message_count = int(frame_payload.get('actual_request_message_count') or 0)
            actual_tool_schema_hash = str(frame_payload.get('actual_tool_schema_hash') or '').strip()
            if isinstance(frame_payload.get('observed_input_truth'), dict):
                observed_input_truth = dict(frame_payload.get('observed_input_truth') or {})
        metadata = dict(node.metadata or {})
        if not actual_request_ref:
            actual_request_ref = str(metadata.get('latest_runtime_actual_request_ref') or '').strip()
        if not messages_ref:
            messages_ref = str(metadata.get('latest_runtime_messages_ref') or '').strip()
        if not prompt_cache_key_hash:
            prompt_cache_key_hash = str(metadata.get('latest_runtime_prompt_cache_key_hash') or '').strip()
        if not actual_request_hash:
            actual_request_hash = str(metadata.get('latest_runtime_actual_request_hash') or '').strip()
        if not actual_request_message_count:
            actual_request_message_count = int(metadata.get('latest_runtime_actual_request_message_count') or 0)
        if not actual_tool_schema_hash:
            actual_tool_schema_hash = str(metadata.get('latest_runtime_actual_tool_schema_hash') or '').strip()
        if not observed_input_truth and isinstance(metadata.get('latest_runtime_observed_input_truth'), dict):
            observed_input_truth = dict(metadata.get('latest_runtime_observed_input_truth') or {})
        ref = actual_request_ref or messages_ref
        if not ref:
            metadata = dict(node.metadata or {})
            ref = str(metadata.get('latest_runtime_messages_ref') or '').strip()
        resolver = getattr(self.log_service, 'resolve_content_ref', None)
        raw_content = str(resolver(ref) or '') if callable(resolver) and ref else ''
        content = self._render_node_latest_context_content(raw_content)
        return {
            'ok': True,
            'task_id': normalized_task_id,
            'node_id': node_id,
            'title': str(self._repair_legacy_display_text(node.goal or node.node_id)),
            'node_kind': str(node.node_kind or 'execution'),
            'status': str(node.status or 'in_progress'),
            'updated_at': str(node.updated_at or ''),
            'ref': ref,
            'actual_request_ref': actual_request_ref,
            'messages_ref': messages_ref,
            'content': content,
            'prompt_cache_key_hash': prompt_cache_key_hash,
            'actual_request_hash': actual_request_hash,
            'actual_request_message_count': actual_request_message_count,
            'actual_tool_schema_hash': actual_tool_schema_hash,
            'observed_input_truth': observed_input_truth,
        }

    def record_node_file_change(self, task_id: str, node_id: str, *, path: str, change_type: str) -> None:
        normalized_task_id = self.normalize_task_id(task_id)
        self.log_service.record_node_file_change(
            normalized_task_id,
            node_id,
            path=path,
            change_type=change_type,
        )

    def node_detail(self, task_id: str, node_id: str, detail_level: str = 'summary') -> dict[str, Any] | str:
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.get_task(normalized_task_id)
        if task is None:
            return f'Error: Task not found: {normalized_task_id}'

        normalized_detail_level = self._normalize_node_detail_level(detail_level)
        payload = self.get_node_detail_payload(normalized_task_id, node_id, detail_level=normalized_detail_level)
        if payload is None:
            return f'Error: Node not found: {node_id}'

        artifacts = [
            {
                **artifact.model_dump(mode='json'),
                'ref': f'artifact:{artifact.artifact_id}',
            }
            for artifact in self.list_artifacts(normalized_task_id)
            if str(getattr(artifact, 'node_id', '') or '').strip() == str(node_id or '').strip()
            and str(getattr(artifact, 'kind', '') or '').strip() not in {'task_execution_trace', 'task_runtime_messages'}
        ]
        artifacts_preview = artifacts[:3]
        item = payload.get('item') if isinstance(payload, dict) else None
        if isinstance(item, dict):
            payload = {
                **payload,
                'item': {
                    **item,
                    'detail_level': normalized_detail_level,
                    'artifact_count': len(artifacts),
                    'artifacts_preview': artifacts_preview if normalized_detail_level == 'summary' else [],
                },
            }
        if normalized_detail_level == 'full':
            return {
                **payload,
                'artifact_count': len(artifacts),
                'artifacts': artifacts,
            }
        return {
            **payload,
            'artifact_count': len(artifacts),
            'artifacts_preview': artifacts_preview,
        }

    @staticmethod
    def _compact_execution_trace_for_tool(execution_trace: Any) -> dict[str, Any]:
        trace = execution_trace if isinstance(execution_trace, dict) else {}
        stages_payload: list[dict[str, Any]] = []
        for stage in list(trace.get('stages') or []):
            if not isinstance(stage, dict):
                continue
            tool_calls: list[dict[str, str]] = []
            for round_item in list(stage.get('rounds') or []):
                if not isinstance(round_item, dict):
                    continue
                for step in list(round_item.get('tools') or []):
                    compact_step = MainRuntimeService._compact_execution_trace_tool_call(step)
                    if compact_step is not None:
                        tool_calls.append(compact_step)
            stages_payload.append(
                {
                    'stage_goal': str(stage.get('stage_goal') or ''),
                    'tool_calls': tool_calls,
                }
            )

        if stages_payload:
            return {'stages': stages_payload}

        fallback_tool_calls: list[dict[str, str]] = []
        for step in list(trace.get('tool_steps') or []):
            compact_step = MainRuntimeService._compact_execution_trace_tool_call(step)
            if compact_step is not None:
                fallback_tool_calls.append(compact_step)
        if fallback_tool_calls:
            return {
                'stages': [
                    {
                        'stage_goal': '',
                        'tool_calls': fallback_tool_calls,
                    }
                ]
            }
        return {'stages': []}

    @staticmethod
    def _compact_execution_trace_tool_call(step: Any) -> dict[str, Any] | None:
        return compact_tool_step_for_summary(step if isinstance(step, dict) else None)

    @staticmethod
    def _normalize_node_detail_level(detail_level: str | None) -> str:
        normalized = str(detail_level or 'summary').strip().lower()
        return normalized if normalized in {'summary', 'full'} else 'summary'

    def list_artifacts(self, task_id: str) -> list[TaskArtifactRecord]:
        task_id = self.normalize_task_id(task_id)
        return self.store.list_artifacts(task_id)

    def get_artifact(self, artifact_id: str) -> TaskArtifactRecord | None:
        return self.store.get_artifact(artifact_id)

    def describe_content(self, *, ref: str | None = None, path: str | None = None, view: str = 'canonical') -> dict[str, Any]:
        return self.content_store.describe(ref=ref, path=path, view=view)

    def search_content(
        self,
        *,
        query: str,
        ref: str | None = None,
        path: str | None = None,
        view: str = 'canonical',
        limit: int = 10,
        before: int = 2,
        after: int = 2,
    ) -> dict[str, Any]:
        return self.content_store.search(ref=ref, path=path, query=query, view=view, limit=limit, before=before, after=after)

    def open_content(
        self,
        *,
        ref: str | None = None,
        path: str | None = None,
        view: str = 'canonical',
        start_line: int | None = None,
        end_line: int | None = None,
        around_line: int | None = None,
        window: int | None = None,
    ) -> dict[str, Any]:
        return self.content_store.open(
            ref=ref,
            path=path,
            view=view,
            start_line=start_line,
            end_line=end_line,
            around_line=around_line,
            window=window,
        )

    def read_content(
        self,
        *,
        ref: str | None = None,
        path: str | None = None,
        view: str = 'canonical',
    ) -> dict[str, Any]:
        return self.content_store.read(
            ref=ref,
            path=path,
            view=view,
        )

    async def apply_patch_artifact(self, task_id: str, artifact_id: str) -> dict[str, Any] | None:
        task_id = self.normalize_task_id(task_id)
        artifact = self.get_artifact(artifact_id)
        if artifact is None or artifact.task_id != task_id:
            return None
        if artifact.kind != 'patch' or not artifact.path:
            raise ValueError('artifact is not a patch artifact')
        from g3ku.agent.tools.propose_patch import parse_patch_artifact

        patch_path = Path(artifact.path)
        content = patch_path.read_text(encoding='utf-8')
        metadata, _diff_text = parse_patch_artifact(content)
        target_path = Path(str(metadata.get('path') or ''))
        old_text = base64.b64decode(str(metadata.get('old_text_b64') or '')).decode('utf-8')
        new_text = base64.b64decode(str(metadata.get('new_text_b64') or '')).decode('utf-8')
        if not target_path.exists():
            raise ValueError(f'target file not found: {target_path}')
        current = target_path.read_text(encoding='utf-8')
        if old_text not in current:
            raise ValueError('target file no longer matches patch precondition')
        if current.count(old_text) > 1:
            raise ValueError('target file has multiple matches for patch precondition')
        updated = current.replace(old_text, new_text, 1)
        target_path.write_text(updated, encoding='utf-8')
        task = self.get_task(task_id)
        self.refresh_resource_paths([target_path], trigger='artifact-apply', session_id=(task.session_id if task is not None else 'web:shared'))
        if task is not None:
            self.log_service.append_task_event(
                task_id=task.task_id,
                session_id=task.session_id,
                event_type='task.artifact.applied',
                data={'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True, 'task_id': task.task_id},
            )
            self._publish_task_artifact_applied_event(
                task=task,
                artifact_id=artifact.artifact_id,
                path=str(target_path),
            )
        return {'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True}

    def _actor_role_for_node(self, node: NodeRecord) -> str:
        return 'inspection' if node.node_kind == 'acceptance' else 'execution'

    @staticmethod
    def _node_context_selection_cache_key(
        *,
        task: Any | None = None,
        node: Any | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> tuple[str, str] | None:
        normalized_task_id = str(task_id or getattr(task, 'task_id', '') or getattr(node, 'task_id', '') or '').strip()
        normalized_node_id = str(node_id or getattr(node, 'node_id', '') or '').strip()
        if not normalized_task_id or not normalized_node_id:
            return None
        return normalized_task_id, normalized_node_id

    def _cached_node_context_selection_entry(
        self,
        *,
        task: Any | None = None,
        node: Any | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> dict[str, Any] | None:
        cache_key = self._node_context_selection_cache_key(
            task=task,
            node=node,
            task_id=task_id,
            node_id=node_id,
        )
        if cache_key is None:
            return None
        cache = getattr(self, '_node_context_selection_cache', None)
        if not isinstance(cache, dict):
            cache = {}
            self._node_context_selection_cache = cache
        cached = cache.get(cache_key)
        return dict(cached) if isinstance(cached, dict) else None

    def _tool_provider_selection_entry(self, *, task, node: NodeRecord) -> dict[str, Any] | None:
        live_visibility_snapshot: dict[str, Any] | None = None

        def _live_snapshot() -> dict[str, Any]:
            nonlocal live_visibility_snapshot
            if live_visibility_snapshot is None:
                live_visibility_snapshot = self._node_context_selection_live_visibility_snapshot(task=task, node=node)
            return live_visibility_snapshot

        cached = self._cached_node_context_selection_entry(task=task, node=node)
        if isinstance(cached, dict) and isinstance(cached.get('selection'), NodeContextSelectionResult):
            if (
                self._node_context_selection_entry_has_visibility_snapshot(cached)
                and self._node_context_selection_entry_is_stale(entry=cached, live_snapshot=_live_snapshot())
            ):
                self._clear_node_context_selection(task=task, node=node)
            else:
                return cached

        restored = self._restore_node_context_selection_entry(task=task, node=node)
        if restored is None:
            return None
        if (
            self._node_context_selection_entry_has_visibility_snapshot(restored)
            and self._node_context_selection_entry_is_stale(entry=restored, live_snapshot=_live_snapshot())
        ):
            return None

        cache_key = self._node_context_selection_cache_key(task=task, node=node)
        if cache_key is not None:
            cache = getattr(self, '_node_context_selection_cache', None)
            if not isinstance(cache, dict):
                cache = {}
                self._node_context_selection_cache = cache
            cache[cache_key] = restored
        return restored

    @staticmethod
    def _skill_record_value(record: Any, key: str) -> Any:
        if isinstance(record, dict):
            return record.get(key)
        return getattr(record, key, None)

    @classmethod
    def _normalized_skill_id_list(cls, records: list[Any] | None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for record in list(records or []):
            skill_id = str(cls._skill_record_value(record, 'skill_id') or '').strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            ordered.append(skill_id)
        return ordered

    @staticmethod
    def _normalized_string_list(items: list[Any] | None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in list(items or []):
            normalized = str(item or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    @classmethod
    def _node_context_selection_entry_has_visibility_snapshot(cls, entry: dict[str, Any] | None) -> bool:
        if not isinstance(entry, dict):
            return False
        if not isinstance(entry.get('visible_tool_names'), list):
            return False
        if not isinstance(entry.get('contract_visible_skill_ids'), list):
            return False
        if not isinstance(entry.get('skill_visibility_diagnostics'), dict):
            return False
        return bool(str(entry.get('session_key') or '').strip()) and bool(str(entry.get('actor_role') or '').strip())

    @classmethod
    def _node_context_selection_entry_registry_skill_ids(cls, entry: dict[str, Any] | None) -> list[str]:
        if not isinstance(entry, dict):
            return []
        diagnostics = (
            dict(entry.get('skill_visibility_diagnostics') or {})
            if isinstance(entry.get('skill_visibility_diagnostics'), dict)
            else {}
        )
        return cls._normalized_string_list(list(diagnostics.get('registry_skill_ids') or []))

    def _node_context_selection_live_visibility_snapshot(self, *, task, node: NodeRecord) -> dict[str, Any]:
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_skills = list(self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_key) or [])
        visible_tool_names = self._normalized_tool_name_list(
            list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_key) or [])
        )
        resource_registry = getattr(self, 'resource_registry', None)
        registry_skills = (
            list(resource_registry.list_skill_resources() or [])
            if resource_registry is not None and hasattr(resource_registry, 'list_skill_resources')
            else list(visible_skills)
        )
        return {
            'session_key': session_key,
            'actor_role': actor_role,
            'visible_tool_names': visible_tool_names,
            'contract_visible_skill_ids': self._normalized_skill_id_list(visible_skills),
            'registry_skill_ids': self._normalized_skill_id_list(registry_skills),
        }

    def _node_context_selection_entry_is_stale(
        self,
        *,
        entry: dict[str, Any] | None,
        live_snapshot: dict[str, Any],
    ) -> bool:
        if not self._node_context_selection_entry_has_visibility_snapshot(entry):
            return False
        entry = dict(entry or {})
        if str(entry.get('session_key') or '').strip() != str(live_snapshot.get('session_key') or '').strip():
            return True
        if str(entry.get('actor_role') or '').strip() != str(live_snapshot.get('actor_role') or '').strip():
            return True
        entry_visible_tool_names = self._normalized_tool_name_list(list(entry.get('visible_tool_names') or []))
        if entry_visible_tool_names != self._normalized_tool_name_list(list(live_snapshot.get('visible_tool_names') or [])):
            return True
        entry_contract_visible_skill_ids = self._normalized_string_list(list(entry.get('contract_visible_skill_ids') or []))
        if entry_contract_visible_skill_ids != self._normalized_string_list(list(live_snapshot.get('contract_visible_skill_ids') or [])):
            return True
        entry_registry_skill_ids = self._node_context_selection_entry_registry_skill_ids(entry)
        if entry_registry_skill_ids != self._normalized_string_list(list(live_snapshot.get('registry_skill_ids') or [])):
            return True
        return False

    def _skill_visibility_diagnostics(
        self,
        *,
        actor_role: str,
        session_id: str,
        visible_skills: list[Any] | None = None,
    ) -> dict[str, Any]:
        visible_skills = list(visible_skills or [])
        visible_skill_ids = set(self._normalized_skill_id_list(visible_skills))
        resource_registry = getattr(self, 'resource_registry', None)
        policy_engine = getattr(self, 'policy_engine', None)
        registry_skills = (
            list(resource_registry.list_skill_resources() or [])
            if resource_registry is not None and hasattr(resource_registry, 'list_skill_resources')
            else list(visible_skills)
        )
        subject = self._subject(actor_role=actor_role, session_id=session_id)
        find_role_policy = getattr(policy_engine, '_find_role_policy', None)
        registry_skill_ids: list[str] = []
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in list(registry_skills or []):
            skill_id = str(self._skill_record_value(record, 'skill_id') or '').strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            registry_skill_ids.append(skill_id)
            allowed_roles = {
                str(role or '').strip()
                for role in list(self._skill_record_value(record, 'allowed_roles') or [])
                if str(role or '').strip()
            }
            policy_effect = ''
            if callable(find_role_policy):
                policy = find_role_policy(
                    subject=subject,
                    resource_kind='skill',
                    resource_id=skill_id,
                    action_id='load',
                )
                policy_effect = str(getattr(policy, 'effect', '') or '').strip().lower()
            entries.append(
                {
                    'skill_id': skill_id,
                    'enabled': bool(
                        True
                        if self._skill_record_value(record, 'enabled') is None
                        else self._skill_record_value(record, 'enabled')
                    ),
                    'available': bool(
                        True
                        if self._skill_record_value(record, 'available') is None
                        else self._skill_record_value(record, 'available')
                    ),
                    'allowed_for_actor_role': str(actor_role or '').strip() in allowed_roles if allowed_roles else True,
                    'policy_effect': policy_effect,
                    'included_in_contract_visible': skill_id in visible_skill_ids,
                }
            )
        return {
            'registry_skill_ids': list(registry_skill_ids),
            'entries': entries,
        }

    @staticmethod
    def _normalized_reason_values(items: list[Any] | None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in list(items or []):
            normalized = str(item or '').strip()
            if not normalized:
                continue
            marker = normalized.lower()
            if marker in seen:
                continue
            seen.add(marker)
            ordered.append(normalized)
        return ordered

    @classmethod
    def _repair_reason_summary(cls, *, reasons: list[Any] | None, fallback: str) -> str:
        normalized = cls._normalized_reason_values(reasons)
        if normalized:
            return normalized[0]
        return str(fallback or '').strip()

    @classmethod
    def _skill_record_is_repair_required(cls, record: Any) -> bool:
        available = cls._skill_record_value(record, 'available')
        if available is None:
            return False
        return not bool(available)

    @classmethod
    def _skill_repair_reason(cls, record: Any) -> str:
        metadata = cls._skill_record_value(record, 'metadata')
        metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
        reasons = [
            *list(metadata.get('errors') or []),
            *list(metadata.get('warnings') or []),
        ]
        return cls._repair_reason_summary(
            reasons=reasons,
            fallback='Repair is required before this skill can be viewed.',
        )

    def _repair_required_skill_payload(self, record: Any) -> dict[str, Any]:
        skill_id = str(self._skill_record_value(record, 'skill_id') or '').strip()
        return {
            'ok': False,
            'error': 'skill_repair_required',
            'skill_id': skill_id,
            'message': f'Skill "{skill_id}" requires repair before its body can be loaded.',
            'reference_skill': 'writing-skills',
            'warnings': list((dict(self._skill_record_value(record, 'metadata') or {}) if isinstance(self._skill_record_value(record, 'metadata'), dict) else {}).get('warnings') or []),
            'errors': list((dict(self._skill_record_value(record, 'metadata') or {}) if isinstance(self._skill_record_value(record, 'metadata'), dict) else {}).get('errors') or []),
            'next_actions': [
                'Use `exec` and `filesystem_*` tools to repair the skill files or dependencies.',
                'Reference skill `writing-skills` before retrying `load_skill_context`.',
            ],
        }

    @staticmethod
    def _node_message_payload(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
        for message in list(messages or []):
            if str(message.get('role') or '').strip().lower() != 'user':
                continue
            raw_content = message.get('content')
            if not isinstance(raw_content, str):
                continue
            try:
                payload = json.loads(raw_content)
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _normalized_tool_name_list(items: list[Any]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in list(items or []):
            normalized = str(item or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    @staticmethod
    def _normalize_tool_call_arguments(arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return dict(arguments)
        if isinstance(arguments, str):
            text = str(arguments or '').strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        if arguments is None:
            return {}
        try:
            parsed = dict(arguments)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _restore_node_context_selection_entry(self, *, task, node: NodeRecord) -> dict[str, Any] | None:
        log_service = getattr(self, 'log_service', None)
        reader = getattr(log_service, 'read_runtime_frame', None)
        task_id = str(getattr(task, 'task_id', '') or getattr(node, 'task_id', '') or '').strip()
        node_id = str(getattr(node, 'node_id', '') or '').strip()
        if not callable(reader) or not task_id or not node_id:
            return None
        frame = reader(task_id, node_id) or {}
        messages = [item for item in list(frame.get('messages') or []) if isinstance(item, dict)]
        if not messages:
            return None
        payload = self._node_message_payload(messages) or {}
        dynamic_contract_payload = extract_node_dynamic_contract_payload(messages) or {}
        required_list_fields = (
            'callable_tool_names',
            'candidate_tool_names',
            'selected_skill_ids',
            'candidate_skill_ids',
            'rbac_visible_tool_names',
            'rbac_visible_skill_ids',
        )
        missing_fields = [
            field
            for field in required_list_fields
            if field not in frame or not isinstance(frame.get(field), list)
        ]
        if missing_fields:
            raise RuntimeError(
                '运行时工具合同损坏/缺失：节点运行时 frame 缺少 canonical 合同字段 '
                + ', '.join(missing_fields)
            )
        callable_tool_names = self._normalized_tool_name_list(list(frame.get('callable_tool_names') or []))
        if not callable_tool_names:
            raise RuntimeError('运行时工具合同损坏/缺失：节点运行时 frame 的 callable_tool_names 为空')
        candidate_skill_items = [
            dict(item)
            for item in list(dynamic_contract_payload.get('candidate_skills') or [])
            if isinstance(item, dict)
        ]
        if not candidate_skill_items:
            candidate_skill_items = [
                dict(item)
                for item in list(frame.get('candidate_skill_items') or [])
                if isinstance(item, dict)
            ]
        if not candidate_skill_items:
            candidate_skill_items = [
                {
                    'skill_id': str(self._skill_record_value(item, 'skill_id') or '').strip(),
                    'description': str(self._skill_record_value(item, 'description') or '').strip(),
                }
                for item in list(frame.get('visible_skills') or [])
                if str(self._skill_record_value(item, 'skill_id') or '').strip()
            ]
        raw_candidate_skill_ids = list(frame.get('candidate_skill_ids') or [])
        candidate_skill_ids = [
            str(item or '').strip()
            for item in raw_candidate_skill_ids
            if str(item or '').strip()
        ]
        candidate_tool_names = self._normalized_tool_name_list(list(frame.get('candidate_tool_names') or []))
        candidate_tool_items = [
            dict(item)
            for item in list(dynamic_contract_payload.get('candidate_tools') or [])
            if isinstance(item, dict)
        ]
        if not candidate_tool_items:
            candidate_tool_items = [
                dict(item)
                for item in list(frame.get('candidate_tool_items') or [])
                if isinstance(item, dict)
            ]
        visible_tool_names = list(
            self.list_effective_tool_names(
                actor_role=self._actor_role_for_node(node),
                session_id=str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared',
            ) or []
        )
        raw_selected_skill_ids = list(frame.get('selected_skill_ids') or [])
        selected_skill_ids = [
            str(item or '').strip()
            for item in raw_selected_skill_ids
            if str(item or '').strip()
        ]
        if not candidate_skill_items:
            candidate_skill_items = [
                {
                    'skill_id': skill_id,
                    'description': '',
                }
                for skill_id in selected_skill_ids
            ]
        restored_selected_tool_names: list[str] = []
        for raw_name in [*list(callable_tool_names), *list(candidate_tool_names)]:
            name = str(raw_name or '').strip()
            if name and name not in restored_selected_tool_names:
                restored_selected_tool_names.append(name)
        selection = NodeContextSelectionResult(
            mode='persisted_frame_restore',
            selected_skill_ids=selected_skill_ids,
            selected_tool_names=restored_selected_tool_names,
            candidate_skill_ids=candidate_skill_ids,
            candidate_tool_names=candidate_tool_names,
            trace={'mode': 'persisted_frame_restore'},
        )
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_skills = list(self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_key) or [])
        contract_visible_skill_ids = [
            str(item or '').strip()
            for item in list(frame.get('contract_visible_skill_ids') or [])
            if str(item or '').strip()
        ]
        if not contract_visible_skill_ids:
            contract_visible_skill_ids = self._normalized_skill_id_list(visible_skills)
        skill_visibility_diagnostics = (
            dict(frame.get('skill_visibility_diagnostics') or {})
            if isinstance(frame.get('skill_visibility_diagnostics'), dict)
            else self._skill_visibility_diagnostics(
                actor_role=actor_role,
                session_id=session_key,
                visible_skills=visible_skills,
            )
        )
        return {
            'session_key': session_key,
            'actor_role': actor_role,
            'visible_skills': visible_skills,
            'contract_visible_skill_ids': contract_visible_skill_ids,
            'skill_visibility_diagnostics': skill_visibility_diagnostics,
            'candidate_tool_items': candidate_tool_items,
            'candidate_skill_items': candidate_skill_items,
            'visible_tool_families': list(self.list_visible_tool_families(actor_role=actor_role, session_id=session_key) or []),
            'visible_tool_names': visible_tool_names,
            'prompt': str(payload.get('prompt') or getattr(node, 'prompt', '') or '').strip(),
            'goal': str(payload.get('goal') or getattr(node, 'goal', '') or '').strip(),
            'core_requirement': str(payload.get('core_requirement') or '').strip(),
            'selection': selection,
        }

    def _node_context_selection_inputs(self, *, task, node: NodeRecord) -> dict[str, Any]:
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_skills = list(self.list_contract_visible_skill_resources(actor_role=actor_role, session_id=session_key) or [])
        visible_tool_families = list(self.list_visible_tool_families(actor_role=actor_role, session_id=session_key) or [])
        visible_tool_names = list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_key) or [])
        task_metadata = task.metadata if isinstance(getattr(task, 'metadata', None), dict) else {}
        prompt = str(getattr(node, 'prompt', '') or '').strip()
        goal = str(getattr(node, 'goal', '') or '').strip()
        core_requirement = str(task_metadata.get('core_requirement') or prompt or goal).strip()
        return {
            'session_key': session_key,
            'actor_role': actor_role,
            'visible_skills': visible_skills,
            'contract_visible_skill_ids': self._normalized_skill_id_list(visible_skills),
            'skill_visibility_diagnostics': self._skill_visibility_diagnostics(
                actor_role=actor_role,
                session_id=session_key,
                visible_skills=visible_skills,
            ),
            'visible_tool_families': visible_tool_families,
            'visible_tool_names': visible_tool_names,
            'prompt': prompt,
            'goal': goal,
            'core_requirement': core_requirement,
        }

    async def _prepare_node_context_selection(self, *, task, node: NodeRecord) -> NodeContextSelectionResult:
        session_id = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        try:
            await self.maybe_refresh_external_resource_changes(session_id=session_id)
        except Exception:
            pass
        live_visibility_snapshot: dict[str, Any] | None = None

        def _live_snapshot() -> dict[str, Any]:
            nonlocal live_visibility_snapshot
            if live_visibility_snapshot is None:
                live_visibility_snapshot = self._node_context_selection_live_visibility_snapshot(task=task, node=node)
            return live_visibility_snapshot

        cached = self._cached_node_context_selection_entry(task=task, node=node)
        if cached is not None and isinstance(cached.get('selection'), NodeContextSelectionResult):
            if (
                self._node_context_selection_entry_has_visibility_snapshot(cached)
                and self._node_context_selection_entry_is_stale(entry=cached, live_snapshot=_live_snapshot())
            ):
                self._clear_node_context_selection(task=task, node=node)
            else:
                return cached['selection']
        restored = self._restore_node_context_selection_entry(task=task, node=node)
        cache_key = self._node_context_selection_cache_key(task=task, node=node)
        if restored is not None:
            if (
                self._node_context_selection_entry_has_visibility_snapshot(restored)
                and self._node_context_selection_entry_is_stale(entry=restored, live_snapshot=_live_snapshot())
            ):
                restored = None
            elif cache_key is not None:
                cache = getattr(self, '_node_context_selection_cache', None)
                if not isinstance(cache, dict):
                    cache = {}
                    self._node_context_selection_cache = cache
                cache[cache_key] = restored
                return restored['selection']
            else:
                return restored['selection']
        inputs = self._node_context_selection_inputs(task=task, node=node)
        selection = await build_node_context_selection(
            loop=getattr(self, '_react_loop', None),
            memory_manager=getattr(self, 'memory_manager', None),
            prompt=str(inputs.get('prompt') or ''),
            goal=str(inputs.get('goal') or ''),
            core_requirement=str(inputs.get('core_requirement') or ''),
            visible_skills=list(inputs.get('visible_skills') or []),
            visible_tool_families=list(inputs.get('visible_tool_families') or []),
            visible_tool_names=list(inputs.get('visible_tool_names') or []),
        )
        if cache_key is not None:
            cache = getattr(self, '_node_context_selection_cache', None)
            if not isinstance(cache, dict):
                cache = {}
                self._node_context_selection_cache = cache
            cache[cache_key] = {**inputs, 'selection': selection}
        return selection

    def _clear_node_context_selection(
        self,
        *,
        task: Any | None = None,
        node: Any | None = None,
        task_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        cache_key = self._node_context_selection_cache_key(
            task=task,
            node=node,
            task_id=task_id,
            node_id=node_id,
        )
        if cache_key is None:
            return
        cache = getattr(self, '_node_context_selection_cache', None)
        if isinstance(cache, dict):
            cache.pop(cache_key, None)

    def _tool_provider(self, node: NodeRecord) -> dict[str, Tool]:
        task = self.store.get_task(node.task_id)
        session_id = task.session_id if task is not None else 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_tool_names = list(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id) or [])
        selected_visible = set(
            self._callable_tool_names_for_node(
                task=(task or SimpleNamespace(task_id=node.task_id, session_id=session_id)),
                node=node,
                visible_tool_names=visible_tool_names,
            )
        )
        task_for_selection = task or SimpleNamespace(task_id=node.task_id, session_id=session_id)
        selection_entry = self._tool_provider_selection_entry(task=task_for_selection, node=node)
        selection = selection_entry.get('selection') if isinstance(selection_entry, dict) else None
        for raw_name in list(getattr(selection, 'selected_tool_names', []) or []):
            name = str(raw_name or '').strip()
            if name and name in visible_tool_names:
                selected_visible.add(name)
        for raw_name in list(getattr(selection, 'candidate_tool_names', []) or []):
            name = str(raw_name or '').strip()
            if name and name in visible_tool_names:
                selected_visible.add(name)
        provided = dict(self._external_tool_provider(node) or {})
        provided.update(self._builtin_tool_instances(actor_role=actor_role))
        resource_manager = getattr(self, '_resource_manager', None)
        if resource_manager is not None:
            for name, tool in resource_manager.tool_instances().items():
                if name in selected_visible:
                    provided[name] = tool
        return provided

    def _builtin_tool_instances(self, *, actor_role: str) -> dict[str, Tool]:
        if str(actor_role or '').strip().lower() != 'ceo':
            return {}
        if self._builtin_tool_cache is None:
            def manager_getter():
                return self.tool_execution_manager

            def task_service_getter():
                return self

            def inline_registry_getter():
                runtime_loop = getattr(self, "_runtime_loop", None)
                return getattr(runtime_loop, "inline_tool_execution_registry", None) if runtime_loop is not None else None

            self._builtin_tool_cache = {
                'wait_tool_execution': WaitToolExecutionTool(manager_getter),
                'stop_tool_execution': StopToolExecutionTool(
                    manager_getter,
                    task_service_getter,
                    inline_registry_getter,
                ),
                'task_append_notice': TaskAppendNoticeTool(self),
            }
        return dict(self._builtin_tool_cache)

    @staticmethod
    def _visible_skill_prompt_items(visible_skills: list[Any]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for record in list(visible_skills or []):
            skill_id = str(MainRuntimeService._skill_record_value(record, 'skill_id') or '').strip()
            if not skill_id:
                continue
            items.append(
                {
                    'skill_id': skill_id,
                    'display_name': str(MainRuntimeService._skill_record_value(record, 'display_name') or skill_id).strip() or skill_id,
                    'description': str(MainRuntimeService._skill_record_value(record, 'description') or '').strip(),
                }
            )
        return items

    @staticmethod
    def _tool_family_by_executor_name(visible_tool_families: list[Any]) -> dict[str, Any]:
        family_by_executor: dict[str, Any] = {}
        for family in list(visible_tool_families or []):
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            executor_names: list[str] = []
            for action in list(getattr(family, 'actions', []) or []):
                for raw_name in list(getattr(action, 'executor_names', []) or []):
                    executor_name = str(raw_name or '').strip()
                    if executor_name and executor_name not in executor_names:
                        executor_names.append(executor_name)
            if not executor_names and tool_id:
                executor_names.append(tool_id)
            for executor_name in executor_names:
                family_by_executor.setdefault(executor_name, family)
        return family_by_executor

    def _repair_required_tool_reason(
        self,
        *,
        tool_id: str,
        visible_tool_families: list[Any],
    ) -> str:
        try:
            toolskill_payload = dict(self.get_tool_toolskill(tool_id) or {})
        except Exception:
            toolskill_payload = {}
        reasons = [
            *list(toolskill_payload.get('errors') or []),
            *list(toolskill_payload.get('warnings') or []),
        ]
        family_by_executor = self._tool_family_by_executor_name(visible_tool_families)
        family = family_by_executor.get(tool_id)
        if family is None:
            family = next(
                (
                    item
                    for item in list(visible_tool_families or [])
                    if str(getattr(item, 'tool_id', '') or '').strip() == tool_id
                ),
                None,
            )
        metadata = dict(getattr(family, 'metadata', {}) or {}) if family is not None else {}
        reasons.extend(list(metadata.get('errors') or []))
        reasons.extend(list(metadata.get('warnings') or []))
        return self._repair_reason_summary(
            reasons=reasons,
            fallback='Repair is required before this tool can be used.',
        )

    def _split_repair_required_tool_prompt_items(
        self,
        *,
        candidate_tool_names: list[str],
        visible_tool_families: list[Any],
        preferred_items: list[Any] | None = None,
    ) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
        candidate_tool_items = self._candidate_tool_prompt_items(
            candidate_tool_names=candidate_tool_names,
            visible_tool_families=visible_tool_families,
            preferred_items=preferred_items,
        )
        family_by_executor = self._tool_family_by_executor_name(visible_tool_families)
        repair_required_items: list[dict[str, str]] = []
        remaining_names: list[str] = []
        remaining_items: list[dict[str, str]] = []
        for item in list(candidate_tool_items or []):
            tool_id = str(item.get('tool_id') or '').strip()
            if not tool_id:
                continue
            try:
                toolskill_payload = dict(self.get_tool_toolskill(tool_id) or {})
            except Exception:
                toolskill_payload = {}
            family = family_by_executor.get(tool_id)
            repair_required = bool(toolskill_payload.get('repair_required'))
            if not repair_required and family is not None:
                repair_required = bool(getattr(family, 'callable', True)) and not bool(getattr(family, 'available', True))
            if repair_required:
                repair_required_items.append(
                    {
                        'tool_id': tool_id,
                        'description': str(item.get('description') or '').strip(),
                        'reason': self._repair_required_tool_reason(
                            tool_id=tool_id,
                            visible_tool_families=visible_tool_families,
                        ),
                    }
                )
                continue
            remaining_names.append(tool_id)
            remaining_items.append(dict(item))
        return remaining_names, remaining_items, repair_required_items

    def _split_repair_required_skill_prompt_items(
        self,
        *,
        candidate_skill_ids: list[str],
        visible_skills: list[Any],
        preferred_items: list[Any] | None = None,
    ) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
        candidate_skill_items = self._candidate_skill_prompt_items(
            candidate_skill_ids=candidate_skill_ids,
            visible_skills=visible_skills,
            preferred_items=preferred_items,
        )
        visible_by_skill_id = {
            str(self._skill_record_value(record, 'skill_id') or '').strip(): record
            for record in list(visible_skills or [])
            if str(self._skill_record_value(record, 'skill_id') or '').strip()
        }
        repair_required_items: list[dict[str, str]] = []
        remaining_ids: list[str] = []
        remaining_items: list[dict[str, str]] = []
        for item in list(candidate_skill_items or []):
            skill_id = str(item.get('skill_id') or '').strip()
            if not skill_id:
                continue
            record = visible_by_skill_id.get(skill_id)
            if record is not None and self._skill_record_is_repair_required(record):
                repair_required_items.append(
                    {
                        'skill_id': skill_id,
                        'description': str(item.get('description') or '').strip(),
                        'reason': self._skill_repair_reason(record),
                    }
                )
                continue
            remaining_ids.append(skill_id)
            remaining_items.append(dict(item))
        return remaining_ids, remaining_items, repair_required_items

    def _all_repair_required_tool_prompt_items(
        self,
        *,
        visible_tool_families: list[Any],
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for family in list(visible_tool_families or []):
            if not bool(getattr(family, 'callable', True)) or bool(getattr(family, 'available', True)):
                continue
            executor_names: list[str] = []
            for action in list(getattr(family, 'actions', []) or []):
                for raw_name in list(getattr(action, 'executor_names', []) or []):
                    executor_name = str(raw_name or '').strip()
                    if executor_name and executor_name not in executor_names:
                        executor_names.append(executor_name)
            if not executor_names:
                normalized_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
                if normalized_tool_id:
                    executor_names.append(normalized_tool_id)
            for tool_id in executor_names:
                if tool_id in seen:
                    continue
                seen.add(tool_id)
                description = str(getattr(family, 'description', '') or '').strip()
                items.append(
                    {
                        'tool_id': tool_id,
                        'description': description,
                        'reason': self._repair_required_tool_reason(
                            tool_id=tool_id,
                            visible_tool_families=visible_tool_families,
                        ),
                    }
                )
        return items

    def _all_repair_required_skill_prompt_items(
        self,
        *,
        visible_skills: list[Any],
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for record in list(visible_skills or []):
            if not self._skill_record_is_repair_required(record):
                continue
            skill_id = str(self._skill_record_value(record, 'skill_id') or '').strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            items.append(
                {
                    'skill_id': skill_id,
                    'description': str(self._skill_record_value(record, 'description') or '').strip(),
                    'reason': self._skill_repair_reason(record),
                }
            )
        return items

    @staticmethod
    def _merge_repair_prompt_items(
        existing: list[dict[str, str]] | None,
        incoming: list[dict[str, str]] | None,
        *,
        key: str,
    ) -> list[dict[str, str]]:
        ordered: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in [*list(existing or []), *list(incoming or [])]:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get(key) or '').strip()
            if not item_key or item_key in seen:
                continue
            seen.add(item_key)
            ordered.append(dict(item))
        return ordered

    def _candidate_tool_prompt_items(
        self,
        *,
        candidate_tool_names: list[str],
        visible_tool_families: list[Any],
        preferred_items: list[Any] | None = None,
    ) -> list[dict[str, str]]:
        family_by_executor: dict[str, Any] = {}
        for family in list(visible_tool_families or []):
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            executor_names: list[str] = []
            for action in list(getattr(family, 'actions', []) or []):
                for raw_name in list(getattr(action, 'executor_names', []) or []):
                    executor_name = str(raw_name or '').strip()
                    if executor_name and executor_name not in executor_names:
                        executor_names.append(executor_name)
            if not executor_names and tool_id:
                executor_names.append(tool_id)
            for executor_name in executor_names:
                family_by_executor.setdefault(executor_name, family)

        preferred_by_tool_id: dict[str, dict[str, str]] = {}
        for item in list(preferred_items or []):
            if not isinstance(item, dict):
                continue
            tool_id = str(item.get('tool_id') or '').strip()
            if not tool_id or tool_id in preferred_by_tool_id:
                continue
            preferred_by_tool_id[tool_id] = {
                'tool_id': tool_id,
                'description': str(item.get('description') or '').strip(),
            }

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_name in list(candidate_tool_names or []):
            tool_id = str(raw_name or '').strip()
            if not tool_id or tool_id in seen:
                continue
            seen.add(tool_id)
            description = str((preferred_by_tool_id.get(tool_id) or {}).get('description') or '').strip()
            if not description:
                try:
                    toolskill_payload = dict(self.get_tool_toolskill(tool_id) or {})
                except Exception:
                    toolskill_payload = {}
                description = str(toolskill_payload.get('description') or toolskill_payload.get('l0') or '').strip()
            if not description:
                family = family_by_executor.get(tool_id)
                if family is None:
                    family = next(
                        (
                            item
                            for item in list(visible_tool_families or [])
                            if str(getattr(item, 'tool_id', '') or '').strip() == tool_id
                        ),
                        None,
                    )
                description = str(getattr(family, 'description', '') or '').strip() if family is not None else ''
            items.append(
                {
                    'tool_id': tool_id,
                    'description': description,
                }
            )
        return items

    def _candidate_skill_prompt_items(
        self,
        *,
        candidate_skill_ids: list[str],
        visible_skills: list[Any],
        preferred_items: list[Any] | None = None,
    ) -> list[dict[str, str]]:
        preferred_by_skill_id: dict[str, dict[str, str]] = {}
        for item in list(preferred_items or []):
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get('skill_id') or '').strip()
            if not skill_id or skill_id in preferred_by_skill_id:
                continue
            preferred_by_skill_id[skill_id] = {
                'skill_id': skill_id,
                'description': str(item.get('description') or '').strip(),
            }

        visible_by_skill_id: dict[str, dict[str, str]] = {}
        for item in self._visible_skill_prompt_items(visible_skills):
            skill_id = str(item.get('skill_id') or '').strip()
            if not skill_id or skill_id in visible_by_skill_id:
                continue
            visible_by_skill_id[skill_id] = {
                'skill_id': skill_id,
                'description': str(item.get('description') or '').strip(),
            }

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_skill_id in list(candidate_skill_ids or []):
            skill_id = str(raw_skill_id or '').strip()
            if not skill_id or skill_id in seen:
                continue
            seen.add(skill_id)
            preferred_item = preferred_by_skill_id.get(skill_id) or {}
            visible_item = visible_by_skill_id.get(skill_id) or {}
            items.append(
                {
                    'skill_id': skill_id,
                    'description': (
                        str(preferred_item.get('description') or '').strip()
                        or str(visible_item.get('description') or '').strip()
                    ),
                }
            )
        return items

    def _node_exec_runtime_policy_payload(
        self,
        *,
        callable_tool_names: list[str],
        candidate_tool_names: list[str],
    ) -> dict[str, Any] | None:
        exposed_tool_names = {
            str(item or '').strip()
            for item in [*list(callable_tool_names or []), *list(candidate_tool_names or [])]
            if str(item or '').strip()
        }
        if EXEC_TOOL_EXECUTOR_NAME not in exposed_tool_names and EXEC_TOOL_FAMILY_ID not in exposed_tool_names:
            return None
        return self._current_exec_runtime_policy_payload()

    async def _enrich_node_messages(self, *, task, node: NodeRecord, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selection = await self._prepare_node_context_selection(task=task, node=node)
        cached = self._cached_node_context_selection_entry(task=task, node=node)
        inputs = cached or self._node_context_selection_inputs(task=task, node=node)
        session_key = str(inputs.get('session_key') or getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        visible_skills = list(inputs.get('visible_skills') or [])
        contract_visible_skill_ids = [
            str(item or '').strip()
            for item in list(inputs.get('contract_visible_skill_ids') or [])
            if str(item or '').strip()
        ]
        skill_visibility_diagnostics = (
            dict(inputs.get('skill_visibility_diagnostics') or {})
            if isinstance(inputs.get('skill_visibility_diagnostics'), dict)
            else {}
        )
        full_callable_tool_names = self._callable_tool_names_for_node(
            task=task,
            node=node,
            visible_tool_names=list(inputs.get('visible_tool_names') or []),
        )
        used_selector_selected_as_callable = not bool(full_callable_tool_names)
        if used_selector_selected_as_callable:
            full_callable_tool_names = self._normalized_tool_name_list(list(getattr(selection, 'selected_tool_names', []) or []))
        stage_payload = {}
        log_service = getattr(self, 'log_service', None)
        stage_payload_supplier = getattr(log_service, 'execution_stage_prompt_payload', None)
        if callable(stage_payload_supplier):
            supplied_stage_payload = stage_payload_supplier(
                str(getattr(task, 'task_id', '') or '').strip(),
                str(getattr(node, 'node_id', '') or '').strip(),
            )
            if isinstance(supplied_stage_payload, dict):
                stage_payload = dict(supplied_stage_payload)
        callable_tool_names = self._model_visible_callable_tool_names_for_node(
            task_id=str(getattr(task, 'task_id', '') or '').strip(),
            node_id=str(getattr(node, 'node_id', '') or '').strip(),
            node_kind=str(getattr(node, 'node_kind', '') or '').strip(),
            callable_tool_names=full_callable_tool_names,
            stage_payload=stage_payload,
        )
        candidate_tool_names = self._candidate_tool_names_for_node(
            task=task,
            node=node,
            selection=selection,
            visible_tool_names=list(inputs.get('visible_tool_names') or []),
        )
        raw_candidate_tool_names = self._normalized_tool_name_list(
            list(getattr(selection, 'candidate_tool_names', []) or [])
        )
        if raw_candidate_tool_names:
            family_by_executor = self._tool_family_by_executor_name(list(inputs.get('visible_tool_families') or []))
            callable_name_set = set(callable_tool_names)
            for tool_name in raw_candidate_tool_names:
                family = family_by_executor.get(tool_name)
                if family is None:
                    continue
                if not bool(getattr(family, 'callable', True)) or bool(getattr(family, 'available', True)):
                    continue
                if tool_name in callable_name_set or tool_name in candidate_tool_names:
                    continue
                candidate_tool_names.append(tool_name)
        candidate_tool_names, candidate_tool_items, repair_required_tool_items = self._split_repair_required_tool_prompt_items(
            candidate_tool_names=list(candidate_tool_names),
            visible_tool_families=list(inputs.get('visible_tool_families') or []),
            preferred_items=list(inputs.get('candidate_tool_items') or []),
        )
        repair_required_tool_items = self._merge_repair_prompt_items(
            repair_required_tool_items,
            self._all_repair_required_tool_prompt_items(
                visible_tool_families=list(inputs.get('visible_tool_families') or []),
            ),
            key='tool_id',
        )
        repair_required_tool_ids = {
            str(item.get('tool_id') or '').strip()
            for item in list(repair_required_tool_items or [])
            if str(item.get('tool_id') or '').strip()
        }
        candidate_tool_names = [name for name in list(candidate_tool_names or []) if name not in repair_required_tool_ids]
        candidate_tool_items = [
            dict(item)
            for item in list(candidate_tool_items or [])
            if str(item.get('tool_id') or '').strip() not in repair_required_tool_ids
        ]
        candidate_skill_ids = list(
            getattr(selection, 'candidate_skill_ids', []) or getattr(selection, 'selected_skill_ids', []) or []
        )
        candidate_skill_ids, candidate_skill_items, repair_required_skill_items = self._split_repair_required_skill_prompt_items(
            candidate_skill_ids=candidate_skill_ids,
            visible_skills=visible_skills,
            preferred_items=list(inputs.get('candidate_skill_items') or []),
        )
        repair_required_skill_items = self._merge_repair_prompt_items(
            repair_required_skill_items,
            self._all_repair_required_skill_prompt_items(visible_skills=visible_skills),
            key='skill_id',
        )
        repair_required_skill_ids = {
            str(item.get('skill_id') or '').strip()
            for item in list(repair_required_skill_items or [])
            if str(item.get('skill_id') or '').strip()
        }
        candidate_skill_ids = [skill_id for skill_id in list(candidate_skill_ids or []) if skill_id not in repair_required_skill_ids]
        candidate_skill_items = [
            dict(item)
            for item in list(candidate_skill_items or [])
            if str(item.get('skill_id') or '').strip() not in repair_required_skill_ids
        ]
        enriched = inject_node_dynamic_contract_message(
            list(messages or []),
            NodeRuntimeToolContract(
                node_id=str(getattr(node, 'node_id', '') or '').strip(),
                node_kind=str(getattr(node, 'node_kind', '') or '').strip(),
                callable_tool_names=callable_tool_names,
                candidate_tool_names=candidate_tool_names,
                candidate_tool_items=candidate_tool_items,
                visible_skills=[],
                candidate_skill_ids=candidate_skill_ids,
                candidate_skill_items=candidate_skill_items,
                contract_visible_skill_ids=contract_visible_skill_ids,
                skill_visibility_diagnostics=skill_visibility_diagnostics,
                repair_required_tool_items=repair_required_tool_items,
                repair_required_skill_items=repair_required_skill_items,
                stage_payload=stage_payload,
                hydrated_executor_names=self._node_hydrated_executor_names(
                    task_id=str(getattr(task, 'task_id', '') or '').strip(),
                    node_id=str(getattr(node, 'node_id', '') or '').strip(),
                    actor_role=self._actor_role_for_node(node),
                    session_id=session_key,
                ),
                lightweight_tool_ids=[],
                selection_trace={
                    'mode': 'node_context_enricher',
                    'full_callable_tool_names': list(full_callable_tool_names),
                    'stage_locked_to_submit_next_stage': (
                        list(callable_tool_names) == [STAGE_TOOL_NAME]
                        and list(full_callable_tool_names) != [STAGE_TOOL_NAME]
                    ),
                },
                exec_runtime_policy=self._node_exec_runtime_policy_payload(
                    callable_tool_names=callable_tool_names,
                    candidate_tool_names=candidate_tool_names,
                ),
            ),
        )
        # Node catalog narrowing stays selector-driven through the memory catalog bridge.
        # Long-term MEMORY.md snapshot injection is CEO/frontdoor-only, so nodes do not
        # receive an extra retrieved-memory block here.
        return enriched

    def summary(self, session_id: str) -> str:
        return self.query_service.summary(str(session_id or 'web:shared').strip() or 'web:shared').text

    def get_tasks(self, session_id: str, task_type: int) -> str:
        items = self.query_service.get_tasks(str(session_id or 'web:shared').strip() or 'web:shared', task_type)
        if not items:
            return '无匹配任务。'
        return '\n'.join(f'- {item.task_id}：{item.brief}' for item in items)

    def failed_node_ids(self, task_id: str) -> str:
        task_id = self.normalize_task_id(task_id)
        failed_node_ids = self.query_service.failed_node_ids(task_id)
        if failed_node_ids is None:
            return f'Error: Task not found: {task_id}'
        if not failed_node_ids:
            return '无失败节点。'
        return '\n'.join(f'- {node_id}' for node_id in failed_node_ids)

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> str:
        task_id = self.normalize_task_id(task_id)
        payload = self.query_service.view_progress(task_id, mark_read=mark_read)
        if payload is None:
            return f'Error: Task not found: {task_id}'
        return payload.text

    async def close(self) -> None:
        for task in [self._command_poller_task, self._worker_heartbeat_task]:
            if task is not None and not task.done():
                task.cancel()
        if self._command_poller_task is not None or self._worker_heartbeat_task is not None:
            await asyncio.gather(
                *(task for task in [self._command_poller_task, self._worker_heartbeat_task] if task is not None),
                return_exceptions=True,
            )
        delivery_tasks = [task for task in self._task_terminal_delivery_tasks.values() if task is not None and not task.done()]
        for task in delivery_tasks:
            task.cancel()
        if delivery_tasks:
            await asyncio.gather(*delivery_tasks, return_exceptions=True)
        self._task_terminal_delivery_tasks.clear()
        stall_delivery_tasks = [task for task in self._task_stall_delivery_tasks.values() if task is not None and not task.done()]
        for task in stall_delivery_tasks:
            task.cancel()
        if stall_delivery_tasks:
            await asyncio.gather(*stall_delivery_tasks, return_exceptions=True)
        self._task_stall_delivery_tasks.clear()
        worker_status_delivery_tasks = [task for task in self._task_worker_status_delivery_tasks.values() if task is not None and not task.done()]
        for task in worker_status_delivery_tasks:
            task.cancel()
        if worker_status_delivery_tasks:
            await asyncio.gather(*worker_status_delivery_tasks, return_exceptions=True)
        self._task_worker_status_delivery_tasks.clear()
        task_summary_flush_tasks = [task for task in self._task_summary_flush_tasks.values() if task is not None and not task.done()]
        for task in task_summary_flush_tasks:
            task.cancel()
        if task_summary_flush_tasks:
            await asyncio.gather(*task_summary_flush_tasks, return_exceptions=True)
        self._task_summary_flush_tasks.clear()
        if self._pending_task_summaries:
            for task_id in list(self._pending_task_summaries.keys()):
                self._flush_task_summary_to_outbox(task_id)
        task_summary_delivery_task = self._task_summary_delivery_task
        if task_summary_delivery_task is not None and not task_summary_delivery_task.done():
            task_summary_delivery_task.cancel()
            await asyncio.gather(task_summary_delivery_task, return_exceptions=True)
        self._task_summary_delivery_task = None
        callback_tasks = [task for task in list(self._task_event_dispatch_tasks) if task is not None and not task.done()]
        for task in callback_tasks:
            task.cancel()
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        self._task_event_dispatch_tasks.clear()
        governance_review_tasks = [task for task in self._governance_review_tasks.values() if task is not None and not task.done()]
        for task in governance_review_tasks:
            task.cancel()
        if governance_review_tasks:
            await asyncio.gather(*governance_review_tasks, return_exceptions=True)
        self._governance_review_tasks.clear()
        callback_client = self._callback_client
        self._callback_client = None
        if callback_client is not None:
            await callback_client.aclose()
        if self.tool_pressure_monitor is not None:
            await self.tool_pressure_monitor.close()
        if self.node_turn_controller is not None:
            await self.node_turn_controller.close()
        await self.worker_heartbeat_service.close()
        await self.task_stall_notifier.close()
        if self.execution_mode == 'worker' and self.worker_id and self._worker_lease_acquired:
            stopped_item = {
                'worker_id': self.worker_id,
                'role': 'task_worker',
                'status': 'stopped',
                'updated_at': now_iso(),
                'payload': {'execution_mode': self.execution_mode, 'worker_heartbeat_at': now_iso(), 'debug': {'recent_long_blocks': self.runtime_debug_recorder.snapshot()}, **self._tool_pressure_snapshot()},
            }
            self.store.upsert_worker_status(
                worker_id=str(stopped_item['worker_id']),
                role=str(stopped_item['role']),
                status=str(stopped_item['status']),
                updated_at=str(stopped_item['updated_at']),
                payload=dict(stopped_item['payload']),
            )
            self.publish_worker_status_event(item=stopped_item)
            self.store.release_worker_lease(role=_WORKER_LEASE_ROLE, worker_id=str(self.worker_id))
            self._worker_lease_acquired = False
        await self.global_scheduler.close()
        self.log_service.close()
        await self.registry.close()
        self.governance_store.close()
        self.store.close()

    def _tool_pressure_snapshot(self) -> dict[str, Any]:
        summary_stats = self._summary_stats_snapshot()
        node_turn_snapshot = self._node_turn_snapshot()
        if self.tool_pressure_monitor is not None:
            return {**dict(self.tool_pressure_monitor.snapshot() or {}), **node_turn_snapshot, **summary_stats}
        if self.adaptive_tool_budget_controller is not None:
            return {**dict(self.adaptive_tool_budget_controller.snapshot() or {}), **node_turn_snapshot, **summary_stats}
        return {**node_turn_snapshot, **summary_stats}

    def _node_turn_snapshot(self) -> dict[str, Any]:
        controller = getattr(self, 'node_turn_controller', None)
        if controller is None:
            return {
                'node_queue_running_count': 0,
                'node_queue_waiting_count': 0,
                'node_queue_oldest_wait_ms': 0.0,
            }
        try:
            payload = dict(controller.snapshot() or {})
        except Exception:
            payload = {}
        return {
            'node_queue_running_count': int(payload.get('node_queue_running_count') or 0),
            'node_queue_waiting_count': int(payload.get('node_queue_waiting_count') or 0),
            'node_queue_frozen_count': int(payload.get('node_queue_frozen_count') or 0),
            'node_queue_oldest_wait_ms': float(payload.get('node_queue_oldest_wait_ms') or 0.0),
        }

    def _node_turn_gate_allowed(self) -> bool:
        monitor = getattr(self, 'tool_pressure_monitor', None)
        if monitor is None:
            return True
        try:
            snapshot = dict(monitor.snapshot() or {})
        except Exception:
            return True
        budget_state = str(snapshot.get('budget_state') or snapshot.get('tool_pressure_state') or 'normal').strip().lower()
        machine_state = str(snapshot.get('machine_pressure_state') or '').strip().lower()
        local_state = str(snapshot.get('local_pressure_state') or '').strip().lower()
        return budget_state != 'critical' and machine_state != 'critical' and local_state != 'critical'

    def _node_turn_task_frozen(self, task_id: str) -> bool:
        meta = self.log_service.read_task_runtime_meta(str(task_id or '').strip()) or {}
        governance = dict(meta.get('governance') or {}) if isinstance(meta.get('governance'), dict) else {}
        return bool(governance.get('frozen'))

    def _task_governance_state(self, task_id: str) -> dict[str, Any]:
        total_nodes = max(1, len(list(self.store.list_task_nodes(task_id) or [])))
        meta = self.log_service.read_task_runtime_meta(task_id) or {}
        governance = dict(meta.get('governance') or {}) if isinstance(meta.get('governance'), dict) else {}
        governance.setdefault('enabled', True)
        governance.setdefault('frozen', False)
        governance.setdefault('review_inflight', False)
        governance['depth_baseline'] = max(1, int(governance.get('depth_baseline') or 1))
        governance['node_count_baseline'] = max(1, int(governance.get('node_count_baseline') or total_nodes))
        governance.setdefault('hard_limited_depth', None)
        governance['latest_limit_reason'] = str(governance.get('latest_limit_reason') or '').strip()
        governance.setdefault('supervision_disabled_after_limit', False)
        governance['history'] = [
            dict(item)
            for item in list(governance.get('history') or [])
            if isinstance(item, dict)
        ]
        return governance

    def _task_tree_stats(self, task_id: str) -> dict[str, int]:
        nodes = list(self.store.list_task_nodes(task_id) or [])
        max_depth = 0
        for node in nodes:
            try:
                max_depth = max(max_depth, int(getattr(node, 'depth', 0) or 0))
            except Exception:
                continue
        return {
            'max_depth': max_depth,
            'total_nodes': len(nodes),
        }

    def _governance_trigger_reason(self, task_id: str) -> tuple[str, dict[str, int]] | tuple[str, None]:
        governance = self._task_governance_state(task_id)
        if not bool(governance.get('enabled', True)):
            return '', None
        if bool(governance.get('frozen')) or bool(governance.get('review_inflight')) or bool(governance.get('supervision_disabled_after_limit')):
            return '', None
        stats = self._task_tree_stats(task_id)
        reasons: list[str] = []
        if int(stats['max_depth']) >= int(governance.get('depth_baseline') or 1) + 1:
            reasons.append('depth_plus_one')
        if int(stats['total_nodes']) >= 8 and int(stats['total_nodes']) >= int(governance.get('node_count_baseline') or 1) * 2:
            reasons.append('node_count_doubled')
        return ('+'.join(reasons), stats) if reasons else ('', None)

    def _governance_frontier_summary(self, task_id: str) -> list[str]:
        payload = self.get_task_detail_payload(task_id, mark_read=False) or {}
        frontier = [dict(item) for item in list(payload.get('frontier') or []) if isinstance(item, dict)]
        lines: list[str] = []
        for item in frontier[:8]:
            node_id = str(item.get('node_id') or '').strip()
            stage_goal = str(item.get('stage_goal') or '').strip()
            phase = str(item.get('phase') or '').strip()
            if not any([node_id, stage_goal, phase]):
                continue
            lines.append(' | '.join(part for part in [node_id, stage_goal, phase] if part))
        return lines

    def _on_governance_child_created(self, *, task_id: str, child_node: NodeRecord) -> None:
        if str(getattr(child_node, 'node_kind', '') or '').strip().lower() != 'execution':
            return
        trigger_reason, stats = self._governance_trigger_reason(task_id)
        if not trigger_reason or stats is None:
            return
        governance = self._task_governance_state(task_id)
        governance['frozen'] = True
        governance['review_inflight'] = True
        self.log_service.update_task_governance(task_id, governance)
        if self.node_turn_controller is not None:
            self.node_turn_controller.poke()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        current = self._governance_review_tasks.get(task_id)
        if current is not None and not current.done():
            return
        review_task = loop.create_task(
            self._run_task_governance_review(
                task_id=task_id,
                trigger_reason=trigger_reason,
                trigger_snapshot=dict(stats),
            ),
            name=f'task-governance-review:{task_id}',
        )
        self._governance_review_tasks[task_id] = review_task
        review_task.add_done_callback(lambda done_task, target_task_id=task_id: self._clear_governance_review_task(target_task_id, done_task))

    def _clear_governance_review_task(self, task_id: str, done_task: asyncio.Task[Any]) -> None:
        current = self._governance_review_tasks.get(task_id)
        if current is done_task:
            self._governance_review_tasks.pop(task_id, None)

    def _governance_spawn_refusal_message(self, *, task_id: str, parent_node_id: str, specs: list[Any]) -> str:
        _ = specs
        governance = self._task_governance_state(task_id)
        hard_limit = governance.get('hard_limited_depth')
        if hard_limit in {None, ''}:
            return ''
        parent = self.get_node(parent_node_id)
        if parent is None:
            return ''
        next_depth = int(getattr(parent, 'depth', 0) or 0) + 1
        if next_depth <= int(hard_limit or 0):
            return ''
        reason = str(governance.get('latest_limit_reason') or '').strip()
        if not reason:
            for item in reversed(list(governance.get('history') or [])):
                if str(item.get('decision') or '').strip() == 'cap_current_depth':
                    reason = str(item.get('decision_reason') or '').strip()
                    if reason:
                        break
        if not reason:
            reason = '监管已限制当前任务树继续扩深。'
        return f'派生被拦截，接下来不允许再派生任何子节点，请自行执行!拦截原因：{reason}'

    async def _run_task_governance_review(self, *, task_id: str, trigger_reason: str, trigger_snapshot: dict[str, int]) -> None:
        task = self.get_task(task_id)
        if task is None:
            return
        governance = self._task_governance_state(task_id)
        try:
            decision = await self._execute_task_governance_review(
                task=task,
                trigger_reason=trigger_reason,
                trigger_snapshot=trigger_snapshot,
            )
        except Exception as exc:
            decision = {
                'decision': 'cap_current_depth',
                'reason': f'{type(exc).__name__}: {exc}',
                'evidence': [],
                'error_text': f'{type(exc).__name__}: {exc}',
            }
        live_stats = self._task_tree_stats(task_id)
        history_item = {
            'triggered_at': now_iso(),
            'trigger_reason': str(trigger_reason or '').strip(),
            'trigger_snapshot': dict(trigger_snapshot or {}),
            'decision': str(decision.get('decision') or 'cap_current_depth').strip(),
            'decision_reason': str(decision.get('reason') or '').strip(),
            'decision_evidence': [str(item).strip() for item in list(decision.get('evidence') or []) if str(item).strip()],
            'limited_depth': None,
            'error_text': str(decision.get('error_text') or '').strip(),
        }
        governance = self._task_governance_state(task_id)
        governance['frozen'] = False
        governance['review_inflight'] = False
        if history_item['decision'] == 'allow':
            governance['depth_baseline'] = max(1, int(live_stats.get('max_depth') or trigger_snapshot.get('max_depth') or 1))
            governance['node_count_baseline'] = max(1, int(live_stats.get('total_nodes') or trigger_snapshot.get('total_nodes') or 1))
        else:
            limited_depth = max(0, int(live_stats.get('max_depth') or trigger_snapshot.get('max_depth') or 0))
            self.log_service.update_task_max_depth(task_id, limited_depth)
            governance['hard_limited_depth'] = limited_depth
            governance['latest_limit_reason'] = history_item['decision_reason']
            governance['supervision_disabled_after_limit'] = True
            history_item['limited_depth'] = limited_depth
        governance['history'] = [*list(governance.get('history') or []), history_item]
        self.log_service.update_task_governance(task_id, governance)
        if self.node_turn_controller is not None:
            self.node_turn_controller.poke()

    async def _execute_task_governance_review(self, *, task: TaskRecord, trigger_reason: str, trigger_snapshot: dict[str, int]) -> dict[str, Any]:
        model_refs = list(self.node_runner._acceptance_model_refs or self.node_runner._execution_model_refs)
        if not model_refs:
            return {
                'decision': 'cap_current_depth',
                'reason': 'RuntimeError: task governance inspection model chain is empty',
                'evidence': [],
                'error_text': '[RuntimeError: task governance inspection model chain is empty + 默认限深]',
            }
        backend = self._chat_backend
        if backend is None or not callable(getattr(backend, 'chat', None)):
            return {
                'decision': 'cap_current_depth',
                'reason': 'RuntimeError: task governance model chain is unavailable',
                'evidence': [],
                'error_text': '[RuntimeError: task governance model chain is unavailable + 默认限深]',
            }
        root = self.get_node(task.root_node_id)
        while True:
            try:
                response = await backend.chat(
                    messages=[
                        {'role': 'system', 'content': load_prompt('task_governance_review.md').strip()},
                        {'role': 'user', 'content': json.dumps({
                            'task_id': task.task_id,
                            'user_request': str(task.user_request or ''),
                            'core_requirement': str((task.metadata or {}).get('core_requirement') or ''),
                            'root_prompt': str(getattr(root, 'prompt', '') or ''),
                            'trigger_reason': str(trigger_reason or ''),
                            'max_depth': int(trigger_snapshot.get('max_depth') or 0),
                            'total_nodes': int(trigger_snapshot.get('total_nodes') or 0),
                            'frontier_stage_goals': self._governance_frontier_summary(task.task_id),
                            'task_progress_text': self.view_progress(task.task_id, mark_read=False),
                        }, ensure_ascii=False, indent=2)},
                    ],
                    tools=[{
                        'type': 'function',
                        'function': {
                            'name': 'review_task_governance',
                            'description': 'Review whether the task tree should be allowed to continue expanding depth.',
                            'parameters': {
                                'type': 'object',
                                'properties': {
                                    'decision': {'type': 'string', 'enum': ['allow', 'cap_current_depth']},
                                    'reason': {'type': 'string'},
                                },
                                'required': ['decision', 'reason'],
                                'additionalProperties': False,
                            },
                        },
                    }],
                    model_refs=model_refs,
                )
            except Exception as exc:
                return {
                    'decision': 'cap_current_depth',
                    'reason': f'{type(exc).__name__}: {exc}',
                    'evidence': [],
                    'error_text': f'[{type(exc).__name__}: {exc} + 默认限深]',
                }
            parsed = self._parse_task_governance_review_response(response)
            if parsed is not None:
                return parsed
            await asyncio.sleep(0.1)

    @staticmethod
    def _parse_task_governance_review_response(response: Any) -> dict[str, Any] | None:
        tool_calls = list(getattr(response, 'tool_calls', []) or [])
        for call in tool_calls:
            name = ''
            arguments: Any = None
            if isinstance(call, dict):
                name = str(call.get('name') or '').strip()
                arguments = call.get('arguments')
            else:
                name = str(getattr(call, 'name', '') or '').strip()
                arguments = getattr(call, 'arguments', None)
            if name != 'review_task_governance':
                continue
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    return None
            if not isinstance(arguments, dict):
                return None
            decision = str(arguments.get('decision') or '').strip()
            if decision not in {'allow', 'cap_current_depth'}:
                return None
            return {
                'decision': decision,
                'reason': str(arguments.get('reason') or '').strip(),
                'evidence': [],
                'error_text': '',
            }
        content = str(getattr(response, 'content', '') or '').strip()
        if not content:
            return None
        try:
            payload = json.loads(content)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        decision = str(payload.get('decision') or '').strip()
        if decision not in {'allow', 'cap_current_depth'}:
            return None
        return {
            'decision': decision,
            'reason': str(payload.get('reason') or '').strip(),
            'evidence': [],
            'error_text': '',
        }

    def _resolve_model_limit_payload(self, model_ref: str) -> dict[str, Any]:
        config = self._app_config
        normalized_model_ref = str(model_ref or '').strip()
        if config is None or not normalized_model_ref:
            return {'key_count': 1, 'key_indexes': [0], 'per_key_limits': {0: None}}
        try:
            target = resolve_chat_target(config, normalized_model_ref, workspace=config.workspace_path)
        except Exception:
            return {'key_count': 1, 'key_indexes': [0], 'per_key_limits': {0: None}}
        raw_api_key = str((dict(getattr(target, 'secret_payload', {}) or {})).get('api_key', '') or '')
        key_count = max(1, len(parse_api_keys(raw_api_key)))
        layout = resolve_api_key_concurrency_layout(
            raw_api_key,
            getattr(target, 'single_api_key_max_concurrency', None),
            include_empty_slot=True,
            reject_all_zero=False,
        )
        key_indexes = list(layout.key_indexes) or [0]
        per_key_limits = {index: None for index in key_indexes}
        for index, limit in zip(layout.key_indexes, layout.key_limits):
            per_key_limits[int(index)] = limit
        return {
            'key_count': key_count,
            'key_indexes': key_indexes,
            'per_key_limits': per_key_limits,
        }

    def _tool_pressure_status_payload(self, item: dict[str, Any] | None) -> dict[str, Any]:
        current_payload = dict(item.get('payload') or {}) if isinstance(item, dict) else {}
        live_snapshot = self._tool_pressure_snapshot() if self.execution_mode == 'worker' else {}
        merged = {**current_payload, **live_snapshot}
        summary_stats = self._summary_stats_snapshot()
        for key in list(summary_stats.keys()):
            if key in merged:
                try:
                    summary_stats[key] = float(merged.get(key) or 0.0)
                except Exception:
                    summary_stats[key] = 0.0
        sample_at = str(merged.get('pressure_sample_at') or merged.get('tool_pressure_sample_at') or '')
        sample_age_ms: float | None
        sample_dt = self._parse_worker_timestamp(sample_at)
        if sample_dt is None:
            sample_age_ms = None
        else:
            sample_age_ms = max(0.0, (datetime.now(timezone.utc) - sample_dt).total_seconds() * 1000.0)
        stale_after_seconds = float(
            self._adaptive_tool_budget_settings(self._app_config).get('pressure_snapshot_stale_after_seconds', 3.0)
        )
        pressure_snapshot_fresh = bool(
            sample_age_ms is not None
            and bool(merged.get('machine_pressure_available'))
            and sample_age_ms <= (stale_after_seconds * 1000.0)
        )
        heartbeat_at = str(merged.get('worker_heartbeat_at') or (item.get('updated_at') if isinstance(item, dict) else '') or '')
        heartbeat_dt = self._parse_worker_timestamp(heartbeat_at)
        if heartbeat_dt is None:
            heartbeat_age_ms = None
        else:
            heartbeat_age_ms = max(0.0, (datetime.now(timezone.utc) - heartbeat_dt).total_seconds() * 1000.0)
        heartbeat_fresh = bool(
            heartbeat_age_ms is not None
            and heartbeat_age_ms <= (self._worker_status_stale_after_seconds(item if isinstance(item, dict) else None) * 1000.0)
        )
        return {
            'tool_pressure_state': str(merged.get('tool_pressure_state') or 'normal'),
            'tool_pressure_target_limit': int(merged.get('tool_pressure_target_limit') or 0),
            'tool_pressure_running_count': int(merged.get('tool_pressure_running_count') or 0),
            'tool_pressure_waiting_count': int(merged.get('tool_pressure_waiting_count') or 0),
            'tool_queue_running_count': int(merged.get('tool_queue_running_count') or merged.get('tool_pressure_running_count') or 0),
            'tool_queue_waiting_count': int(merged.get('tool_queue_waiting_count') or merged.get('tool_pressure_waiting_count') or 0),
            'tool_pressure_event_loop_lag_ms': float(merged.get('tool_pressure_event_loop_lag_ms') or 0.0),
            'tool_pressure_writer_queue_depth': int(merged.get('tool_pressure_writer_queue_depth') or 0),
            'tool_pressure_process_cpu_ratio': float(merged.get('tool_pressure_process_cpu_ratio') or 0.0),
            'tool_pressure_last_transition_at': str(merged.get('tool_pressure_last_transition_at') or ''),
            'tool_pressure_throttled_since': str(merged.get('tool_pressure_throttled_since') or ''),
            'tool_pressure_critical_since': str(merged.get('tool_pressure_critical_since') or ''),
            'worker_execution_state': str(merged.get('worker_execution_state') or merged.get('tool_pressure_state') or 'normal'),
            'worker_execution_target_limit': int(merged.get('worker_execution_target_limit') or merged.get('tool_pressure_target_limit') or 0),
            'worker_execution_running_count': int(merged.get('worker_execution_running_count') or merged.get('tool_pressure_running_count') or 0),
            'worker_execution_waiting_count': int(merged.get('worker_execution_waiting_count') or merged.get('tool_pressure_waiting_count') or 0),
            'worker_execution_oldest_wait_ms': float(merged.get('worker_execution_oldest_wait_ms') or 0.0),
            'node_queue_running_count': int(merged.get('node_queue_running_count') or 0),
            'node_queue_waiting_count': int(merged.get('node_queue_waiting_count') or 0),
            'node_queue_oldest_wait_ms': float(merged.get('node_queue_oldest_wait_ms') or 0.0),
            'machine_pressure_state': str(merged.get('machine_pressure_state') or 'unknown'),
            'local_pressure_state': str(merged.get('local_pressure_state') or 'unknown'),
            'budget_state': str(merged.get('budget_state') or merged.get('tool_pressure_state') or 'normal'),
            'machine_pressure_available': bool(merged.get('machine_pressure_available')),
            'machine_pressure_cpu_percent': float(merged.get('machine_pressure_cpu_percent') or 0.0),
            'machine_pressure_memory_percent': float(merged.get('machine_pressure_memory_percent') or 0.0),
            'machine_pressure_disk_busy_percent': float(merged.get('machine_pressure_disk_busy_percent') or 0.0),
            'machine_pressure_disk_busy_available': bool(merged.get('machine_pressure_disk_busy_available')),
            'machine_pressure_disk_read_bytes_per_sec': float(merged.get('machine_pressure_disk_read_bytes_per_sec') or 0.0),
            'machine_pressure_disk_write_bytes_per_sec': float(merged.get('machine_pressure_disk_write_bytes_per_sec') or 0.0),
            'sqlite_write_wait_ms': float(merged.get('sqlite_write_wait_ms') or 0.0),
            'sqlite_query_latency_ms': float(merged.get('sqlite_query_latency_ms') or 0.0),
            'pressure_sample_at': sample_at,
            'pressure_sample_age_ms': round(sample_age_ms, 3) if sample_age_ms is not None else None,
            'pressure_snapshot_fresh': pressure_snapshot_fresh,
            'worker_heartbeat_at': heartbeat_at,
            'worker_heartbeat_age_ms': round(heartbeat_age_ms, 3) if heartbeat_age_ms is not None else None,
            'worker_heartbeat_fresh': heartbeat_fresh,
            **summary_stats,
            'debug': dict(merged.get('debug') or {}) if isinstance(merged.get('debug'), dict) else {},
        }

    def _clamp_depth(self, requested: int | None) -> int:
        if requested is None:
            return self._default_max_depth
        return max(0, min(int(requested), self._hard_max_depth))


def _runtime_task_default_max_depth(runtime: dict[str, Any] | None) -> int | None:
    payload = runtime if isinstance(runtime, dict) else {}
    task_defaults = payload.get('task_defaults')
    if not isinstance(task_defaults, dict):
        return None
    raw_depth = task_defaults.get('max_depth', task_defaults.get('maxDepth'))
    if raw_depth in (None, ''):
        return None
    try:
        return int(raw_depth)
    except (TypeError, ValueError):
        return None


def _tool_runtime_payload(runtime: dict[str, Any] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    if isinstance(runtime, dict):
        return runtime
    fallback = kwargs.get('__g3ku_runtime')
    return fallback if isinstance(fallback, dict) else {}


_TASK_KEYWORDS_PARAM = '\u4efb\u52a1\u5173\u952e\u8bcd'
_TASK_ID_LIST_PARAM = '\u4efb\u52a1id\u5217\u8868'


class TaskSummaryTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_summary'

    @property
    def description(self) -> str:
        return '返回总任务、进行中任务、失败任务数量。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {}}

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        await self._service.startup()
        return self._service.summary(str(runtime.get('session_key') or 'web:shared'))


class GetTasksTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_list'

    @property
    def description(self) -> str:
        return '按任务类型返回任务 id 列表和简要描述。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务类型': {'type': 'integer', 'enum': [1, 2, 3, 4], 'description': '1=所有任务，2=进行中任务，3=失败任务，4=未读任务。'},
            },
            'required': ['任务类型'],
        }

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        await self._service.startup()
        task_type = int(kwargs.get('任务类型'))
        return self._service.get_tasks(str(runtime.get('session_key') or 'web:shared'), task_type)


class TaskStatsTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_stats'

    @property
    def description(self) -> str:
        return 'List task statistics globally or by task id, including prompt preview and task disk usage.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'mode': {
                    'type': 'string',
                    'enum': ['list', 'id'],
                    'description': 'list lists tasks by date range; id returns specific task ids.',
                },
                _TASK_KEYWORDS_PARAM: {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Optional keyword list for list mode. Matches the initial user_request with OR semantics.',
                },
                'from': {
                    'type': 'string',
                    'description': 'Required in list mode. Date format: YYYY/M/D.',
                },
                'to': {
                    'type': 'string',
                    'description': 'Required in list mode. Date format: YYYY/M/D.',
                },
                _TASK_ID_LIST_PARAM: {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Required in id mode. Task ids to inspect in input order.',
                },
            },
            'required': ['mode'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        normalized_mode = str((params or {}).get('mode') or '').strip().lower()
        if normalized_mode == 'list':
            if not str((params or {}).get('from') or '').strip():
                errors.append('from is required when mode=list')
            if not str((params or {}).get('to') or '').strip():
                errors.append('to is required when mode=list')
        if normalized_mode == 'id' and not list((params or {}).get(_TASK_ID_LIST_PARAM) or []):
            errors.append(f'{_TASK_ID_LIST_PARAM} is required when mode=id')
        return errors

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        result = self._service.task_stats(
            mode=str(kwargs.get('mode') or '').strip(),
            task_keywords=kwargs.get(_TASK_KEYWORDS_PARAM),
            date_from=str(kwargs.get('from') or '').strip(),
            date_to=str(kwargs.get('to') or '').strip(),
            task_ids=kwargs.get(_TASK_ID_LIST_PARAM),
        )
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class TaskDeleteTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_delete'

    @property
    def description(self) -> str:
        return 'Preview or confirm deletion of one or more saved tasks.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'mode': {
                    'type': 'string',
                    'enum': ['preview', 'confirm'],
                    'description': 'preview returns candidate tasks and a confirmation token; confirm performs deletion.',
                },
                _TASK_ID_LIST_PARAM: {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Task ids to delete. Supports batch operations.',
                },
                'confirmation_token': {
                    'type': 'string',
                    'description': 'Required in confirm mode. Must come from a prior preview of the same task id batch.',
                },
            },
            'required': ['mode', _TASK_ID_LIST_PARAM],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        normalized_mode = str((params or {}).get('mode') or '').strip().lower()
        if normalized_mode == 'confirm' and not str((params or {}).get('confirmation_token') or '').strip():
            errors.append('confirmation_token is required when mode=confirm')
        return errors

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        await self._service.startup()
        normalized_mode = str(kwargs.get('mode') or '').strip().lower()
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        if normalized_mode == 'preview':
            result = self._service.task_delete_preview(
                task_ids=kwargs.get(_TASK_ID_LIST_PARAM),
                session_id=session_id,
            )
        else:
            result = self._service.task_delete_confirm(
                task_ids=kwargs.get(_TASK_ID_LIST_PARAM),
                confirmation_token=str(kwargs.get('confirmation_token') or '').strip(),
                session_id=session_id,
            )
            if inspect.isawaitable(result):
                result = await result
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class ViewTaskProgressTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_progress'

    @property
    def description(self) -> str:
        return '按任务 id 返回任务状态和带阶段目标的树状图文本，并将任务标记为已读。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {'任务id': {'type': 'string', 'description': '目标任务 id。'}}, 'required': ['任务id']}

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        return self._service.view_progress(task_id, mark_read=True)


class TaskFailedNodesTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_failed_nodes'

    @property
    def description(self) -> str:
        return '按任务 id 返回当前任务树中的失败节点 id 列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {'任务id': {'type': 'string', 'description': '目标任务 id。'}},
            'required': ['任务id'],
        }

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        return self._service.failed_node_ids(task_id)


class _LegacyTaskNodeDetailToolMojibakeB(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '_legacy_task_node_detail_removed_b0'

    @property
    def description(self) -> str:
        return '按任务 id 和节点 id 返回节点详情及关联工件列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务id': {'type': 'string', 'description': '目标任务 id。'},
                '节点id': {'type': 'string', 'description': '目标节点 id。'},
            },
            'required': ['任务id', '节点id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        node_id = str(kwargs.get('节点id') or '').strip()
        return self._service.node_detail(task_id, node_id)


class _LegacyTaskNodeDetailToolMojibakeA(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '_legacy_task_node_detail_removed_b'

    @property
    def description(self) -> str:
        return '按任务 id 和节点 id 返回节点详情及关联工件列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '浠诲姟id': {'type': 'string', 'description': '目标任务 id。'},
                '鑺傜偣id': {'type': 'string', 'description': '目标节点 id。'},
                'detail_level': build_detail_level_schema(
                    description='summary 返回轻量节点详情与 refs；full 返回完整执行轨迹和完整工件列表。',
                ),
            },
            'required': ['浠诲姟id', '鑺傜偣id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('浠诲姟id') or '').strip()
        node_id = str(kwargs.get('鑺傜偣id') or '').strip()
        detail_level = str(kwargs.get('detail_level') or 'summary').strip()
        try:
            result = self._service.node_detail(task_id, node_id, detail_level=detail_level)
        except TypeError as exc:
            if "unexpected keyword argument 'detail_level'" not in str(exc):
                raise
            result = self._service.node_detail(task_id, node_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class _TaskNodeDetailToolMojibakeCurrent(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return '_legacy_task_node_detail_removed_a'

    @property
    def description(self) -> str:
        return '按任务 id 和节点 id 返回节点详情及关联工件列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务id': {'type': 'string', 'description': '目标任务 id。'},
                '节点id': {'type': 'string', 'description': '目标节点 id。'},
                'detail_level': build_detail_level_schema(
                    description='summary 返回轻量节点详情与 refs/工件预览；full 返回完整执行轨迹和完整工件列表。',
                ),
            },
            'required': ['任务id', '节点id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        node_id = str(kwargs.get('节点id') or '').strip()
        detail_level = str(kwargs.get('detail_level') or 'summary').strip()
        try:
            return self._service.node_detail(task_id, node_id, detail_level=detail_level)
        except TypeError as exc:
            if "unexpected keyword argument 'detail_level'" not in str(exc):
                raise
            return self._service.node_detail(task_id, node_id)


class TaskNodeDetailTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_node_detail'

    @property
    def description(self) -> str:
        return '按任务 id 和节点 id 返回节点详情及关联工件列表。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                '任务id': {'type': 'string', 'description': '目标任务 id。'},
                '节点id': {'type': 'string', 'description': '目标节点 id。'},
                'detail_level': build_detail_level_schema(
                    description='summary 返回轻量节点详情与 refs/工件预览；full 返回完整执行轨迹和完整工件列表。',
                ),
            },
            'required': ['任务id', '节点id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        node_id = str(kwargs.get('节点id') or '').strip()
        detail_level = str(kwargs.get('detail_level') or 'summary').strip()
        try:
            result = self._service.node_detail(task_id, node_id, detail_level=detail_level)
        except TypeError as exc:
            if "unexpected keyword argument 'detail_level'" not in str(exc):
                raise
            result = self._service.node_detail(task_id, node_id)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class TaskAppendNoticeTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'task_append_notice'

    @property
    def description(self) -> str:
        return TASK_APPEND_NOTICE_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return build_task_append_notice_parameters()

    async def execute(
        self,
        task_ids: list[str] | None = None,
        node_ids: list[str] | None = None,
        message: str = '',
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        result = await self._service.task_append_notice(
            task_ids=list(task_ids or []),
            node_ids=list(node_ids or []),
            message=str(message or '').strip(),
            session_id=str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared',
        )
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


class CreateAsyncTaskTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'create_async_task'

    @property
    def description(self) -> str:
        return CREATE_ASYNC_TASK_DESCRIPTION

    @property
    def parameters(self) -> dict[str, Any]:
        return build_create_async_task_parameters()

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if 'core_requirement' in (params or {}):
            core_requirement = str((params or {}).get('core_requirement') or '').strip()
            if not core_requirement:
                errors.append('core_requirement must not be empty')
        if 'continuation_of_task_id' in (params or {}) or 'reuse_existing' in (params or {}):
            errors.append('create_async_task_no_longer_supports_continuation')
        requires_final_acceptance = (params or {}).get('requires_final_acceptance')
        final_acceptance_prompt = str((params or {}).get('final_acceptance_prompt') or '').strip()
        if requires_final_acceptance is True and not final_acceptance_prompt:
            errors.append('final_acceptance_prompt is required when requires_final_acceptance=true')
        return errors

    async def execute(
        self,
        task: str,
        core_requirement: str = '',
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        if 'continuation_of_task_id' in kwargs or 'reuse_existing' in kwargs:
            raise ValueError('create_async_task_no_longer_supports_continuation')
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        explicit_max_depth = kwargs.get('max_depth', kwargs.get('maxDepth'))
        if explicit_max_depth in (None, ''):
            explicit_max_depth = _runtime_task_default_max_depth(runtime)
        normalized_core_requirement = str(core_requirement or kwargs.get('core_requirement') or '').strip() or str(task or '').strip()
        normalized_execution_policy = normalize_execution_policy_metadata(kwargs.get('execution_policy'))
        final_acceptance_prompt = str(kwargs.get('final_acceptance_prompt') or '').strip()
        raw_requires_final_acceptance = kwargs.get('requires_final_acceptance')
        requires_final_acceptance = bool(raw_requires_final_acceptance) or (raw_requires_final_acceptance in (None, '') and bool(final_acceptance_prompt))
        precheck = await self._service.precheck_async_task_creation(
            session_id=session_id,
            task_text=str(task or '').strip(),
            core_requirement=normalized_core_requirement,
            execution_policy=normalized_execution_policy.model_dump(mode='json'),
            requires_final_acceptance=requires_final_acceptance,
            final_acceptance_prompt=final_acceptance_prompt,
        )
        decision = str(precheck.get('decision') or '').strip()
        matched_task_id = str(precheck.get('matched_task_id') or '').strip()
        reason = str(precheck.get('reason') or '').strip()
        if decision == 'reject_duplicate':
            return f'任务未创建：与进行中任务 {matched_task_id} 高度重复。原因：{reason}'
        if decision == 'reject_use_append_notice':
            return (
                f'任务未创建：现有任务 {matched_task_id} 需要追加通知而不是新建。'
                f'请改用 task_append_notice。原因：{reason}'
            )
        record = await self._service.create_task(
            str(task or ''),
            session_id=session_id,
            max_depth=explicit_max_depth,
            metadata={
                'core_requirement': normalized_core_requirement,
                'execution_policy': normalized_execution_policy.model_dump(mode='json'),
                'final_acceptance': {
                    'required': requires_final_acceptance,
                    'prompt': final_acceptance_prompt,
                    'node_id': '',
                    'status': 'pending',
                }
            },
        )
        return f'创建任务成功{record.task_id}'
    @staticmethod
    def _external_tool_family_id(tool_id: str) -> str:
        normalized = str(tool_id or '').strip()
        if normalized == 'content_navigation':
            return 'content'
        return normalized

    @staticmethod
    def _resolve_tool_family_alias(tool_id: str) -> str:
        normalized = str(tool_id or '').strip()
        if normalized == 'content':
            return 'content_navigation'
        return normalized
