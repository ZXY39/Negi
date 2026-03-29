from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from loguru import logger

from g3ku.config.live_runtime import get_runtime_config
from g3ku.web.worker_control import managed_worker_snapshot
from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.tool_execution_control import StopToolExecutionTool, WaitToolExecutionTool
from g3ku.content import ContentNavigationService
from g3ku.resources.tool_settings import (
    MemoryRuntimeSettings,
    raw_tool_settings_from_descriptor,
    validate_tool_settings,
)
from g3ku.resources.models import ResourceKind
from g3ku.runtime.core_tools import configured_core_tools, resolve_core_tool_targets
from g3ku.runtime.memory_scope import DEFAULT_WEB_MEMORY_SCOPE, normalize_memory_scope
from g3ku.runtime.tool_watchdog import ToolExecutionManager
from g3ku.runtime.context.semantic_scope import plan_retrieval_scope, semantic_catalog_rankings
from g3ku.runtime.context.summarizer import layered_body_payload, score_query
from main.governance import (
    GovernanceStore,
    MainRuntimePolicyEngine,
    MainRuntimeResourceRegistry,
    PermissionSubject,
    list_effective_skill_ids,
    list_effective_tool_names,
)
from main.governance.tool_context import build_tool_toolskill_payload, resolve_primary_executor_name
from main.governance.roles import to_public_allowed_roles
from main.ids import new_command_id, new_node_id, new_task_id, new_worker_id
from main.models import (
    NodeRecord,
    TaskArtifactRecord,
    TaskRecord,
    TokenUsageSummary,
    normalize_execution_policy_metadata,
    normalize_final_acceptance_metadata,
)
from main.monitoring.file_store import TaskFileStore
from main.monitoring.log_service import TaskLogService
from main.monitoring.query_service import TaskQueryService
from main.protocol import build_envelope, now_iso
from main.monitoring.tree_builder import TaskTreeBuilder
from main.runtime.chat_backend import ChatBackend
from main.runtime.node_runner import NodeRunner
from main.runtime.react_loop import ReActToolLoop
from main.runtime.task_runner import TaskRunner
from main.service.event_registry import TaskEventRegistry
from main.service.create_async_task_contract import (
    CREATE_ASYNC_TASK_DESCRIPTION,
    build_create_async_task_parameters,
)
from main.service.task_terminal_callback import (
    build_task_terminal_payload,
    enrich_task_terminal_payload,
    normalize_task_terminal_payload,
    resolve_task_terminal_callback_token,
    resolve_task_terminal_callback_url,
)
from main.service.task_stall_callback import (
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
from main.service.task_event_callback import (
    normalize_task_event_payload,
    resolve_task_event_callback_token,
    resolve_task_event_callback_url,
)
from main.service.task_stall_notifier import (
    TaskStallNotifier,
    stalled_minutes_since,
    stall_bucket_minutes,
)
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore

_UNSET = object()
_WORKER_STATUS_STALE_AFTER_SECONDS = 15.0
_WORKER_STATUS_ACTIVE_TASK_STALE_AFTER_SECONDS = 60.0
_WORKER_STATUS_STARTING_GRACE_SECONDS = 10.0
_WORKER_STATUS_CALLBACK_RETRY_DELAYS = [0.0, 0.5, 2.0, 5.0]
_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS = 2.0
_WORKER_STATE_STARTING = 'starting'
_WORKER_STATE_ONLINE = 'online'
_WORKER_STATE_STALE = 'stale'
_WORKER_STATE_STOPPED = 'stopped'
_WORKER_STATE_OFFLINE = 'offline'
_WORKER_STATUS_TERMINAL_STATES = frozenset({'stopped', 'offline', 'dead'})
_TASK_RECOVERY_NOTICE_KEY = 'recovery_notice'
_TASK_RECOVERY_NOTICE_TEXT = '本任务遇到异常停止，已回退到稳定步骤继续。'
_CONTINUATION_TASK_CREATED_BY_SOURCES = frozenset({'heartbeat_auto_continue', 'ceo_user_rebuild'})


_TASK_RUNTIME_V2_MARKER = '.task-runtime-v2'


def _prepare_task_runtime_v2_root(
    *,
    store_path: Path,
    files_base_dir: Path,
    artifact_dir: Path,
) -> None:
    runtime_root = store_path.parent
    marker_path = runtime_root / _TASK_RUNTIME_V2_MARKER
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
    for directory in (files_base_dir, artifact_dir):
        try:
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
        except Exception:
            logger.debug('task runtime v2 cleanup skipped for {}', directory)
    marker_path.write_text('task-runtime-v2\n', encoding='utf-8')


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
        _prepare_task_runtime_v2_root(
            store_path=resolved_store_path,
            files_base_dir=resolved_files_base_dir,
            artifact_dir=resolved_artifact_dir,
        )
        self.store = SQLiteTaskStore(resolved_store_path)
        self.file_store = TaskFileStore(resolved_files_base_dir)
        self.artifact_store = TaskArtifactStore(artifact_dir=resolved_artifact_dir, store=self.store)
        self.content_store = ContentNavigationService(
            workspace=Path.cwd(),
            artifact_store=self.artifact_store,
            artifact_lookup=self.store,
        )
        self.registry = TaskEventRegistry()
        self.tree_builder = TaskTreeBuilder()
        self.log_service = TaskLogService(
            store=self.store,
            file_store=self.file_store,
            tree_builder=self.tree_builder,
            registry=self.registry,
            content_store=self.content_store,
        )
        self.log_service.add_live_snapshot_publisher(self._publish_live_snapshot)
        self.task_stall_notifier = TaskStallNotifier(service=self)
        self.log_service.add_task_visible_output_listener(self.task_stall_notifier.reset_visible_output)
        self.log_service.add_task_terminal_listener(self.task_stall_notifier.terminal_task)
        self.query_service = TaskQueryService(store=self.store, file_store=self.file_store, log_service=self.log_service)
        self.log_service.set_snapshot_payload_builder(lambda task_id: self.get_task_detail_payload(task_id, mark_read=False))
        self.governance_store = GovernanceStore(governance_store_path or (Path.cwd() / '.g3ku' / 'main-runtime' / 'governance.sqlite3'))
        self.resource_registry = MainRuntimeResourceRegistry(workspace_root=Path.cwd(), store=self.governance_store, resource_manager=resource_manager)
        self.policy_engine = MainRuntimePolicyEngine(store=self.governance_store, resource_registry=self.resource_registry)
        self._external_tool_provider = tool_provider or (lambda _node: {})
        self._resource_manager = resource_manager
        self.memory_manager = None
        self._default_max_depth = max(0, int(default_max_depth or 0))
        self._hard_max_depth = max(self._default_max_depth, int(hard_max_depth or self._default_max_depth))
        self.tool_execution_manager = ToolExecutionManager()
        self._builtin_tool_cache: dict[str, Tool] | None = None
        parallel_enabled, max_parallel_tool_calls, max_parallel_child_pipelines = self._node_parallelism_settings(app_config)
        execution_max_concurrency = app_config.get_role_max_concurrency('execution') if app_config is not None and hasattr(app_config, 'get_role_max_concurrency') else None
        acceptance_max_concurrency = app_config.get_role_max_concurrency('inspection') if app_config is not None and hasattr(app_config, 'get_role_max_concurrency') else None
        react_loop = ReActToolLoop(
            chat_backend=chat_backend,
            log_service=self.log_service,
            max_iterations=resolved_max_iterations,
            parallel_tool_calls_enabled=parallel_enabled,
            max_parallel_tool_calls=max_parallel_tool_calls,
        )
        react_loop._tool_execution_manager = None
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
        )
        self.task_runner = TaskRunner(
            store=self.store,
            log_service=self.log_service,
            node_runner=self.node_runner,
            stall_notifier=self.task_stall_notifier,
        )
        self._started = False
        self._runtime_loop = None
        self._command_poller_task: asyncio.Task[Any] | None = None
        self._worker_heartbeat_task: asyncio.Task[Any] | None = None
        self._task_terminal_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_stall_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_worker_status_delivery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._task_event_dispatch_tasks: set[asyncio.Task[Any]] = set()
        if self.execution_mode == 'worker':
            self.log_service.add_task_terminal_listener(self._enqueue_task_terminal_callback)

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        self.resource_registry.refresh_from_current_resources()
        self.reconcile_core_tool_families()
        self.policy_engine.sync_default_role_policies()
        if self.memory_manager is not None and hasattr(self.memory_manager, 'sync_catalog'):
            try:
                await self.memory_manager.sync_catalog(self)
            except Exception:
                pass
        if self.execution_mode in {'embedded', 'worker'}:
            for task in self.store.list_tasks():
                self.log_service.bootstrap_missing_files(task.task_id)
                self.log_service.ensure_task_projection(task.task_id)
                if task.status != 'in_progress':
                    continue
                runtime_state = self.log_service.read_runtime_state(task.task_id)
                if runtime_state is None:
                    self.log_service.mark_task_failed(task.task_id, reason='runtime_state_corrupt')
                    continue
                if bool(task.is_paused) or bool(runtime_state.get('paused')):
                    continue
                self._recover_interrupted_task(task.task_id)
                self.task_runner.start_background(task.task_id)
            self.task_stall_notifier.bootstrap_running_tasks()
        if self.execution_mode == 'worker':
            self._start_worker_loops()
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

        nodes = list(self.store.list_nodes(task.task_id) or [])
        discard_ids = self._recovery_discard_node_ids(task.root_node_id, nodes)
        if discard_ids:
            self.store.delete_nodes(sorted(discard_ids))

        for node in list(self.store.list_nodes(task.task_id) or []):
            if node.node_id == task.root_node_id:
                continue
            self._sanitize_recovered_node(node)

        root = self.store.get_node(task.root_node_id) or root
        root_status = str(root.status or '').strip().lower()
        if root_status != 'success':
            root = self._reset_root_for_recovery(root)
            self.store.upsert_node(root)

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

        self.log_service.update_runtime_state(
            task.task_id,
            root_node_id=task.root_node_id,
            paused=False,
            pause_requested=False,
            cancel_requested=bool(task.cancel_requested),
            active_node_ids=[root.node_id],
            runnable_node_ids=[root.node_id],
            waiting_node_ids=[],
            frames=[self.log_service._default_frame(node_id=root.node_id, depth=root.depth, node_kind=root.node_kind, phase='before_model')],
            publish_snapshot=False,
        )
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
        while True:
            try:
                active_task_count = sum(
                    1
                    for task in self.store.list_tasks()
                    if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress'
                    and not bool(getattr(task, 'is_paused', False))
                )
                updated_at = now_iso()
                payload = {
                    'execution_mode': self.execution_mode,
                    'active_task_count': active_task_count,
                }
                self.store.upsert_worker_status(
                    worker_id=self.worker_id or 'worker',
                    role='task_worker',
                    status='running',
                    updated_at=updated_at,
                    payload=payload,
                )
                self.publish_worker_status_event(
                    item={
                        'worker_id': self.worker_id or 'worker',
                        'role': 'task_worker',
                        'status': 'running',
                        'updated_at': updated_at,
                        'payload': payload,
                    }
                )
                await asyncio.sleep(1.0 if active_task_count > 0 else 5.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)

    async def _process_worker_command(self, command: dict[str, Any]) -> None:
        command_id = str(command.get('command_id') or '').strip()
        command_type = str(command.get('command_type') or '').strip()
        task_id = self.normalize_task_id(str(command.get('task_id') or '').strip())
        success = False
        error_text = ''
        try:
            if command_type == 'create_task':
                task = self.get_task(task_id)
                if task is not None and not bool(task.is_paused):
                    self.task_runner.start_background(task.task_id)
                success = True
            elif command_type == 'resume_task':
                if task_id:
                    await self.task_runner.resume(task_id)
                success = True
            elif command_type == 'pause_task':
                if task_id:
                    await self.task_runner.pause(task_id)
                success = True
            elif command_type == 'cancel_task':
                if task_id:
                    await self.task_runner.cancel(task_id)
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
        root = NodeRecord(
            node_id=root_node_id,
            task_id=task_id,
            parent_node_id=None,
            root_node_id=root_node_id,
            depth=0,
            node_kind='execution',
            status='in_progress',
            goal=prompt,
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
        if self.execution_mode in {'embedded', 'worker'}:
            self.task_runner.start_background(record.task_id)
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
            await self.task_runner.cancel(task_id)
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
            await self.task_runner.pause(task_id)
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
            await self.task_runner.resume(task_id)
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

    async def retry_task(self, task_id: str) -> TaskRecord | None:
        await self.startup()
        task_id = self.normalize_task_id(task_id)
        if self.execution_mode == 'web':
            self._assert_worker_available()
        task = self.get_task(task_id)
        if task is None:
            return None
        if task.status != 'failed':
            raise ValueError('task_not_failed')
        metadata = dict(task.metadata or {})
        metadata['retry_of_task_id'] = task.task_id
        return await self.create_task(
            task.user_request,
            session_id=task.session_id,
            max_depth=task.max_depth,
            title=task.title,
            metadata=metadata,
        )

    async def delete_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        task = self.get_task(task_id)
        if task is None:
            return None
        if not (bool(task.is_paused) or task.status in {'success', 'failed'}):
            raise ValueError('task_not_paused')
        if self.task_runner.is_active(task_id):
            try:
                await asyncio.wait_for(self.task_runner.wait(task_id), timeout=2.0)
            except asyncio.TimeoutError as exc:
                raise ValueError('task_still_stopping') from exc
        if self.task_runner.is_active(task_id):
            raise ValueError('task_still_stopping')
        artifacts = self.list_artifacts(task_id)
        self.artifact_store.delete_artifacts_for_task(task_id, artifacts=artifacts)
        self.file_store.delete_task_files(task_id)
        self.store.delete_task(task_id)
        self.store.append_task_event(
            task_id=task.task_id,
            session_id=task.session_id,
            event_type='task.deleted',
            created_at=now_iso(),
            payload={'task_id': task.task_id},
        )
        self._publish_task_deleted_event(session_id=task.session_id, task_id=task.task_id)
        await self.registry.forget_task(task.session_id, task_id)
        return task

    async def wait_for_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        await self.task_runner.wait(task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        return self.store.get_task(task_id)

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

    def _schedule_task_event_callback(self, payload: dict[str, Any] | None) -> None:
        if self.execution_mode != 'worker':
            return
        normalized = normalize_task_event_payload(payload)
        if not normalized:
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
        callback_url = resolve_task_event_callback_url(workspace=Path.cwd())
        if not callback_url:
            return
        headers: dict[str, str] = {}
        callback_token = resolve_task_event_callback_token(workspace=Path.cwd())
        if callback_token:
            headers['x-g3ku-internal-token'] = callback_token
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                await client.post(callback_url, json=payload, headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception:
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

    def _publish_task_snapshot_event(
        self,
        *,
        session_id: str,
        task_id: str,
        data: dict[str, Any],
        bridge: bool = False,
    ) -> dict[str, Any]:
        payload = build_envelope(
            channel='task',
            session_id=session_id,
            task_id=task_id,
            seq=self.registry.next_global_task_seq(task_id),
            type='task.snapshot',
            data=data,
        )
        self.registry.publish_global_task(task_id, payload)
        if self.execution_mode == 'worker' and not bridge:
            self._schedule_task_event_callback(
                {
                    'event_type': 'task.snapshot',
                    'session_id': session_id,
                    'task_id': task_id,
                    'data': data,
                }
            )
        return payload

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
        event_type = str(payload.get('event_type') or '').strip()
        data = dict(payload.get('data') or {})
        if event_type == 'task.summary.patch':
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
            if self.execution_mode == 'worker':
                self._schedule_task_event_callback(
                    {
                        'event_type': event_type,
                        'session_id': task.session_id,
                        'task_id': task.task_id,
                        'data': data,
                    }
                )
            return
        if event_type in {'task.node.patch', 'task.live.patch', 'task.terminal'}:
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
            return
        self._publish_task_snapshot_event(
            session_id=task.session_id,
            task_id=task.task_id,
            data=payload,
        )
        if publish_summary:
            self._publish_task_list_patch_event(
                session_id=task.session_id,
                task_payload=self.log_service._task_summary_payload(task),
            )

    def forward_live_task_event(self, payload: dict[str, Any] | None) -> bool:
        normalized = normalize_task_event_payload(payload)
        if not normalized:
            return False
        event_type = str(normalized.get('event_type') or '').strip()
        session_id = str(normalized.get('session_id') or 'web:shared').strip() or 'web:shared'
        task_id = self.normalize_task_id(str(normalized.get('task_id') or '').strip()) if normalized.get('task_id') else ''
        data = dict(normalized.get('data') or {})
        if event_type == 'task.snapshot' and task_id:
            self._publish_task_snapshot_event(
                session_id=session_id,
                task_id=task_id,
                data=data,
                bridge=True,
            )
            return True
        if event_type in {'task.node.patch', 'task.live.patch', 'task.terminal'} and task_id:
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

    @staticmethod
    def _normalize_continuation_task_id(value: Any) -> str:
        task_id = str(value or '').strip()
        return task_id if task_id.startswith('task:') else ''

    @staticmethod
    def _normalize_continuation_created_by_source(value: Any) -> str:
        source = str(value or '').strip()
        return source if source in _CONTINUATION_TASK_CREATED_BY_SOURCES else ''

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

    def find_reusable_continuation_task(self, session_id: str, continuation_of_task_id: str) -> TaskRecord | None:
        key = self._normalize_session_key(session_id)
        target_task_id = self._normalize_continuation_task_id(continuation_of_task_id)
        if not target_task_id:
            return None
        matches: list[TaskRecord] = []
        for task in self.list_unfinished_tasks_for_session(key):
            metadata = task.metadata if isinstance(task.metadata, dict) else {}
            if self._normalize_session_key(self._task_origin_session_id(task)) != key:
                continue
            if self._normalize_continuation_task_id(metadata.get('continuation_of_task_id')) != target_task_id:
                continue
            matches.append(task)
        if not matches:
            return None
        matches.sort(
            key=lambda item: (
                str(getattr(item, 'updated_at', '') or ''),
                str(getattr(item, 'created_at', '') or ''),
                str(getattr(item, 'task_id', '') or ''),
            ),
            reverse=True,
        )
        return matches[0]

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
                'continuation_of_task_id': self._normalize_continuation_task_id(metadata.get('continuation_of_task_id')),
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
            status = str(getattr(task, 'status', '') or '').strip().lower()
            if status == 'in_progress' and not bool(getattr(task, 'is_paused', False)):
                continue
            try:
                await self.delete_task(task.task_id)
            except ValueError as exc:
                if str(exc) in {'task_not_paused', 'task_still_stopping'}:
                    continue
                raise
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

    @staticmethod
    def _task_stall_latest_node_summary(detail: dict[str, Any]) -> str:
        progress = detail.get('progress') if isinstance(detail.get('progress'), dict) else {}
        latest_node = progress.get('latest_node') if isinstance(progress.get('latest_node'), dict) else {}
        if not latest_node:
            return ''
        title = str(latest_node.get('title') or latest_node.get('node_id') or 'node').strip() or 'node'
        status = str(latest_node.get('status') or 'in_progress').strip() or 'in_progress'
        output = str(latest_node.get('output') or latest_node.get('output_excerpt') or '').strip()
        text = f'{title} [{status}]'
        if output:
            text = f'{text}: {output}'
        return text[:240]

    @staticmethod
    def _task_stall_runtime_summary(detail: dict[str, Any]) -> str:
        progress = detail.get('progress') if isinstance(detail.get('progress'), dict) else {}
        live_state = progress.get('live_state') if isinstance(progress.get('live_state'), dict) else {}
        frames = [item for item in list(live_state.get('frames') or []) if isinstance(item, dict)]
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

    def _schedule_pending_task_worker_status_callbacks(self) -> None:
        if self.execution_mode != 'worker':
            return
        for entry in self.store.list_pending_task_worker_status_outbox(limit=500):
            worker_id = str(entry.get('worker_id') or '').strip()
            if worker_id:
                self._schedule_task_worker_status_delivery(worker_id)

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
            callback_url = resolve_task_event_callback_url(workspace=Path.cwd())
            if not callback_url:
                self.store.mark_task_worker_status_outbox_attempt(
                    worker_id,
                    attempted_at=now_iso(),
                    error_text='task_event_callback_url_unavailable',
                )
                return
            headers: dict[str, str] = {}
            callback_token = resolve_task_event_callback_token(workspace=Path.cwd())
            if callback_token:
                headers['x-g3ku-internal-token'] = callback_token
            try:
                async with httpx.AsyncClient(timeout=_WORKER_STATUS_CALLBACK_TIMEOUT_SECONDS) as client:
                    response = await client.post(callback_url, json=payload, headers=headers)
                if 200 <= int(response.status_code or 0) < 300:
                    self.store.mark_task_worker_status_outbox_delivered(worker_id, delivered_at=now_iso())
                    return
                error_text = f'task_event_callback_http_{int(response.status_code or 0)}'
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_text = str(exc or 'task_event_callback_failed').strip() or 'task_event_callback_failed'
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
            callback_url = resolve_task_stall_callback_url(workspace=Path.cwd())
            if not callback_url:
                self.store.mark_task_stall_outbox_attempt(
                    dedupe_key,
                    attempted_at=now_iso(),
                    error_text='task_stall_callback_url_unavailable',
                )
                return
            headers: dict[str, str] = {}
            callback_token = resolve_task_stall_callback_token(workspace=Path.cwd())
            if callback_token:
                headers['x-g3ku-internal-token'] = callback_token
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.post(callback_url, json=payload, headers=headers)
                if 200 <= int(response.status_code or 0) < 300:
                    self.store.mark_task_stall_outbox_delivered(dedupe_key, delivered_at=now_iso())
                    return
                error_text = f'task_stall_callback_http_{int(response.status_code or 0)}'
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_text = str(exc or 'task_stall_callback_failed').strip() or 'task_stall_callback_failed'
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
            callback_url = resolve_task_terminal_callback_url(workspace=Path.cwd())
            if not callback_url:
                self.store.mark_task_terminal_outbox_attempt(
                    dedupe_key,
                    attempted_at=now_iso(),
                    error_text='task_terminal_callback_url_unavailable',
                )
                return
            headers: dict[str, str] = {}
            callback_token = resolve_task_terminal_callback_token(workspace=Path.cwd())
            if callback_token:
                headers['x-g3ku-internal-token'] = callback_token
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    response = await client.post(callback_url, json=payload, headers=headers)
                if 200 <= int(response.status_code or 0) < 300:
                    self.store.mark_task_terminal_outbox_delivered(dedupe_key, delivered_at=now_iso())
                    return
                error_text = f'task_terminal_callback_http_{int(response.status_code or 0)}'
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_text = str(exc or 'task_terminal_callback_failed').strip() or 'task_terminal_callback_failed'
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
        parallel_enabled, max_parallel_tool_calls, max_parallel_child_pipelines = self._node_parallelism_settings(config)
        self._react_loop._parallel_tool_calls_enabled = parallel_enabled
        self._react_loop._max_parallel_tool_calls = max_parallel_tool_calls
        self.node_runner._max_parallel_child_pipelines = max_parallel_child_pipelines
        self.node_runner._parallel_child_pipelines_enabled = parallel_enabled
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
        continuation_of_task_id = self._normalize_continuation_task_id(payload.get('continuation_of_task_id'))
        if continuation_of_task_id:
            payload['continuation_of_task_id'] = continuation_of_task_id
        else:
            payload.pop('continuation_of_task_id', None)
        created_by_source = self._normalize_continuation_created_by_source(payload.get('created_by_source'))
        if continuation_of_task_id and created_by_source:
            payload['created_by_source'] = created_by_source
        else:
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
        supported = sorted((self._resource_manager.tool_instances().keys() if self._resource_manager is not None else []))
        return list_effective_tool_names(subject=self._subject(actor_role=actor_role, session_id=session_id), supported_tool_names=supported, resource_registry=self.resource_registry, policy_engine=self.policy_engine, mutation_allowed=True)

    def list_visible_skill_resources(self, *, actor_role: str, session_id: str):
        visible_ids = set(list_effective_skill_ids(subject=self._subject(actor_role=actor_role, session_id=session_id), available_skill_ids=[item.skill_id for item in self.resource_registry.list_skill_resources()], policy_engine=self.policy_engine))
        return [item for item in self.resource_registry.list_skill_resources() if item.skill_id in visible_ids]

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
            for action in list(getattr(family, 'actions', []) or []):
                for executor_name in list(getattr(action, 'executor_names', []) or []):
                    name = str(executor_name or '').strip()
                    if name and name not in mapping:
                        mapping[name] = family
        return mapping

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
        visible = list(self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id) or [])
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
                'tool_id': str(getattr(family, 'tool_id', '') or '').strip(),
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
        visible = {item.skill_id: item for item in self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)}
        record = visible.get(skill_name)
        if record is None:
            return {'ok': False, 'error': f'Skill not visible: {skill_id}'}
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
        visible = {item.skill_id: item for item in self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)}
        record = visible.get(skill_name)
        if record is None:
            return {'ok': False, 'error': f'Skill not visible: {skill_id}'}
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
        return {
            'ok': True,
            'tool_id': resolved_tool_id,
            'content': content,
            'tool_type': toolskill.get('tool_type'),
            'install_dir': toolskill.get('install_dir'),
            'callable': toolskill.get('callable'),
            'available': toolskill.get('available'),
            'warnings': list(toolskill.get('warnings') or []),
            'errors': list(toolskill.get('errors') or []),
        }

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
        payload = layered_body_payload(
            body=content,
            title=str(toolskill.get('tool_id') or tool_name),
            description=str(toolskill.get('description') or ''),
            path=str(toolskill.get('path') or ''),
        )
        return {
            'ok': True,
            'tool_id': resolved_tool_id,
            'uri': f'g3ku://resource/tool/{resolved_tool_id}',
            'level': payload['level'],
            'content': payload['content'],
            'l0': payload['l0'],
            'l1': payload['l1'],
            'path': payload['path'],
            'tool_type': toolskill.get('tool_type'),
            'install_dir': toolskill.get('install_dir'),
            'callable': toolskill.get('callable'),
            'available': toolskill.get('available'),
            'warnings': list(toolskill.get('warnings') or []),
            'errors': list(toolskill.get('errors') or []),
        }

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
        if self.memory_manager is not None and hasattr(self.memory_manager, 'sync_catalog'):
            try:
                sync_result = await self.memory_manager.sync_catalog(
                    self,
                    skill_ids={str(skill_id or '').strip()},
                )
                item['catalog_synced'] = True
                item['catalog'] = sync_result
            except Exception:
                item['catalog_synced'] = False
        else:
            item['catalog_synced'] = False
        return item

    def _workspace_root(self) -> Path:
        manager = getattr(self, '_resource_manager', None)
        workspace = getattr(manager, 'workspace', None)
        return Path(workspace).resolve(strict=False) if workspace is not None else Path.cwd().resolve()

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
        if self.memory_manager is not None and hasattr(self.memory_manager, 'sync_catalog'):
            try:
                sync_result = await self.memory_manager.sync_catalog(
                    self,
                    skill_ids={target_skill_id},
                )
                item['catalog_synced'] = True
                item['catalog'] = sync_result
            except Exception:
                item['catalog_synced'] = False
        else:
            item['catalog_synced'] = False
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
        return self.resource_registry.get_tool_family(str(tool_id or '').strip())

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
        return family.model_copy(update={'is_core': family.tool_id in resolution.family_ids, 'metadata': metadata})

    def list_tool_resources(self) -> list[Any]:
        return [self._decorate_tool_family(item) for item in self.resource_registry.list_tool_families()]

    def get_tool_family(self, tool_id: str):
        return self._decorate_tool_family(self._raw_tool_family(tool_id))

    def reconcile_core_tool_families(self) -> bool:
        resolution = self._core_tool_resolution()
        changed = False
        for family in list(self.resource_registry.list_tool_families()):
            if str(getattr(family, 'tool_id', '') or '').strip() not in resolution.family_ids:
                continue
            family_changed = not bool(getattr(family, 'enabled', True))
            actions = []
            for action in list(getattr(family, 'actions', []) or []):
                roles = list(getattr(action, 'allowed_roles', []) or [])
                if bool(getattr(action, 'agent_visible', True)) and 'ceo' not in roles:
                    roles = to_public_allowed_roles([*roles, 'ceo'])
                    family_changed = True
                actions.append(action.model_copy(update={'allowed_roles': roles}))
            if not family_changed:
                continue
            updated = family.model_copy(update={'enabled': True, 'actions': actions})
            self.governance_store.upsert_tool_family(updated, updated_at=now_iso())
            changed = True
        return changed

    def _tool_family_executor_name(self, family) -> str:
        return resolve_primary_executor_name(family, resource_manager=self._resource_manager)

    def get_tool_toolskill(self, tool_id: str) -> dict[str, Any] | None:
        return build_tool_toolskill_payload(
            tool_id,
            raw_tool_family_getter=self._raw_tool_family,
            resource_registry=self.resource_registry,
            resource_manager=self._resource_manager,
        )

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
        if self.memory_manager is not None and hasattr(self.memory_manager, 'sync_catalog'):
            try:
                sync_result = await self.memory_manager.sync_catalog(
                    self,
                    tool_ids={target_tool_id},
                )
                item['catalog_synced'] = True
                item['catalog'] = sync_result
            except Exception:
                item['catalog_synced'] = False
        else:
            item['catalog_synced'] = False
        return item

    def update_tool_policy(self, tool_id: str, *, session_id: str = 'web:shared', enabled: bool | None = None, allowed_roles_by_action: dict[str, list[str]] | None = None):
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
                normalized_roles = to_public_allowed_roles([str(role) for role in (roles or [])])
                current_roles = to_public_allowed_roles(list(getattr(action, 'allowed_roles', []) or []))
                if normalized_roles != current_roles:
                    raise ResourceMutationBlockedError(
                        code='tool_action_readonly',
                        message='Readonly system actions cannot be edited.',
                        resource_kind='tool_family',
                        resource_id=target_tool_id,
                        details={'action_id': action.action_id},
                    )
            next_roles = to_public_allowed_roles(
                [str(role) for role in (action.allowed_roles if roles is None else roles)]
            )
            if is_core and bool(getattr(action, 'agent_visible', True)) and 'ceo' not in next_roles:
                raise ResourceMutationBlockedError(
                    code='core_tool_ceo_visibility_required',
                    message='Core tool families must remain visible to the CEO for agent-visible actions.',
                    resource_kind='tool_family',
                    resource_id=target_tool_id,
                    details={'action_id': action.action_id},
                )
            actions.append(action.model_copy(update={'allowed_roles': next_roles}))
        updated = family.model_copy(update={'enabled': family.enabled if enabled is None else bool(enabled), 'actions': actions})
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
        return {'ok': True, 'session_id': session_id, 'skills': len(skills), 'tools': len(tools)}

    async def reload_resources_async(self, *, session_id: str = 'web:shared') -> dict[str, Any]:
        result = self.reload_resources(session_id=session_id)
        if self.memory_manager is not None and hasattr(self.memory_manager, 'sync_catalog'):
            try:
                sync_result = await self.memory_manager.sync_catalog(self)
                result['catalog'] = sync_result
            except Exception:
                result['catalog'] = {'created': 0, 'updated': 0, 'removed': 0}
        return result

    async def get_context_traces(self, *, trace_kind: str, limit: int = 20) -> dict[str, Any]:
        manager = self.memory_manager
        if manager is None or not hasattr(manager, 'read_trace_file'):
            return {'ok': True, 'items': [], 'trace_kind': trace_kind, 'limit': max(1, int(limit))}
        items = await manager.read_trace_file(trace_kind=trace_kind, limit=max(1, int(limit)))
        return {'ok': True, 'items': items, 'trace_kind': trace_kind, 'limit': max(1, int(limit))}

    def get_task_detail_payload(self, task_id: str, *, mark_read: bool = False) -> dict[str, Any] | None:
        task_id = self.normalize_task_id(task_id)
        self.log_service.ensure_task_projection(task_id)
        payload = self.query_service.get_task_snapshot(task_id, mark_read=mark_read)
        if payload is None:
            return None
        return payload

    def get_node_detail_payload(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        self.log_service.ensure_task_projection(normalized_task_id)
        detail = self.query_service.get_node_detail(normalized_task_id, node_id)
        if detail is None:
            return None
        return {
            'ok': True,
            'task_id': normalized_task_id,
            'node_id': node_id,
            'item': detail.model_dump(mode='json'),
        }

    def get_node_children_payload(
        self,
        task_id: str,
        node_id: str,
        *,
        round_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any] | None:
        normalized_task_id = self.normalize_task_id(task_id)
        payload = self.query_service.get_node_children(
            normalized_task_id,
            node_id,
            round_id=round_id,
            offset=offset,
            limit=limit,
        )
        if payload is None:
            return None
        return {'ok': True, **payload}

    def node_detail(self, task_id: str, node_id: str) -> dict[str, Any] | str:
        normalized_task_id = self.normalize_task_id(task_id)
        task = self.get_task(normalized_task_id)
        if task is None:
            return f'Error: Task not found: {normalized_task_id}'

        payload = self.get_node_detail_payload(normalized_task_id, node_id)
        if payload is None:
            return f'Error: Node not found: {node_id}'

        item = payload.get('item') if isinstance(payload, dict) else None
        if isinstance(item, dict):
            payload = {
                **payload,
                'item': {
                    **item,
                    'execution_trace': self._compact_execution_trace_for_tool(item.get('execution_trace')),
                },
            }

        artifacts = [
            {
                **artifact.model_dump(mode='json'),
                'ref': f'artifact:{artifact.artifact_id}',
            }
            for artifact in self.list_artifacts(normalized_task_id)
            if str(getattr(artifact, 'node_id', '') or '').strip() == str(node_id or '').strip()
        ]
        return {
            **payload,
            'artifact_count': len(artifacts),
            'artifacts': artifacts,
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
    def _compact_execution_trace_tool_call(step: Any) -> dict[str, str] | None:
        if not isinstance(step, dict):
            return None
        tool_name = str(step.get('tool_name') or '').strip() or 'tool'
        arguments_text = str(step.get('arguments_text') or '')
        output_text = str(step.get('output_text') or '')
        output_ref = str(step.get('output_ref') or '')
        return {
            'tool_name': tool_name,
            'arguments_text': arguments_text,
            'output_text': output_text,
            'output_ref': output_ref,
        }

    def list_artifacts(self, task_id: str) -> list[TaskArtifactRecord]:
        task_id = self.normalize_task_id(task_id)
        return self.store.list_artifacts(task_id)

    def get_artifact(self, artifact_id: str) -> TaskArtifactRecord | None:
        return self.store.get_artifact(artifact_id)

    def describe_content(self, *, ref: str | None = None, path: str | None = None) -> dict[str, Any]:
        return self.content_store.describe(ref=ref, path=path)

    def search_content(
        self,
        *,
        query: str,
        ref: str | None = None,
        path: str | None = None,
        limit: int = 10,
        before: int = 2,
        after: int = 2,
    ) -> dict[str, Any]:
        return self.content_store.search(ref=ref, path=path, query=query, limit=limit, before=before, after=after)

    def open_content(
        self,
        *,
        ref: str | None = None,
        path: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        around_line: int | None = None,
        window: int | None = None,
    ) -> dict[str, Any]:
        return self.content_store.open(
            ref=ref,
            path=path,
            start_line=start_line,
            end_line=end_line,
            around_line=around_line,
            window=window,
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
            self.store.append_task_event(
                task_id=task.task_id,
                session_id=task.session_id,
                event_type='task.artifact.applied',
                created_at=now_iso(),
                payload={'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True, 'task_id': task.task_id},
            )
            self._publish_task_artifact_applied_event(
                task=task,
                artifact_id=artifact.artifact_id,
                path=str(target_path),
            )
        return {'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True}

    def _actor_role_for_node(self, node: NodeRecord) -> str:
        return 'inspection' if node.node_kind == 'acceptance' else 'execution'

    def _tool_provider(self, node: NodeRecord) -> dict[str, Tool]:
        task = self.store.get_task(node.task_id)
        session_id = task.session_id if task is not None else 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible = set(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id))
        provided = dict(self._external_tool_provider(node) or {})
        provided.update(self._builtin_tool_instances(actor_role=actor_role))
        if self._resource_manager is not None:
            for name, tool in self._resource_manager.tool_instances().items():
                if name in visible:
                    provided[name] = tool
        return provided

    def _builtin_tool_instances(self, *, actor_role: str) -> dict[str, Tool]:
        if str(actor_role or '').strip().lower() != 'ceo':
            return {}
        if self._builtin_tool_cache is None:
            manager_getter = lambda: self.tool_execution_manager
            task_service_getter = lambda: self
            self._builtin_tool_cache = {
                'wait_tool_execution': WaitToolExecutionTool(manager_getter),
                'stop_tool_execution': StopToolExecutionTool(manager_getter, task_service_getter),
            }
        return dict(self._builtin_tool_cache)

    @staticmethod
    def _visible_skill_prompt_items(visible_skills: list[Any]) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for record in list(visible_skills or []):
            skill_id = str(getattr(record, 'skill_id', '') or '').strip()
            if not skill_id:
                continue
            items.append(
                {
                    'skill_id': skill_id,
                    'display_name': str(getattr(record, 'display_name', '') or skill_id).strip() or skill_id,
                    'description': str(getattr(record, 'description', '') or '').strip(),
                }
            )
        return items

    def _inject_visible_skills_into_node_messages(
        self,
        *,
        messages: list[dict[str, Any]],
        visible_skills: list[Any],
    ) -> list[dict[str, Any]]:
        enriched = list(messages or [])
        skill_items = self._visible_skill_prompt_items(visible_skills)
        for index, message in enumerate(enriched):
            if str(message.get('role') or '').strip().lower() != 'user':
                continue
            raw_content = message.get('content')
            if not isinstance(raw_content, str):
                continue
            try:
                payload = json.loads(raw_content)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            payload['visible_skills'] = skill_items
            enriched[index] = {
                **message,
                'content': json.dumps(payload, ensure_ascii=False, indent=2),
            }
            break
        return enriched

    async def _enrich_node_messages(self, *, task, node: NodeRecord, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible_skills = list(self.list_visible_skill_resources(actor_role=actor_role, session_id=session_key) or [])
        enriched = self._inject_visible_skills_into_node_messages(messages=messages, visible_skills=visible_skills)
        manager = getattr(self, 'memory_manager', None)
        if manager is None or not getattr(manager, '_feature_enabled', lambda _key: False)('unified_context'):
            return enriched
        query_text = str(getattr(node, 'prompt', '') or getattr(node, 'goal', '') or '').strip()
        if not query_text:
            return enriched
        memory_scope = self._task_memory_scope(task)
        channel = str(memory_scope.get('channel') or 'unknown')
        chat_id = str(memory_scope.get('chat_id') or 'unknown')
        visible_families = list(self.list_visible_tool_families(actor_role=actor_role, session_id=session_key) or [])
        semantic_frontdoor = await semantic_catalog_rankings(
            memory_manager=manager,
            query_text=query_text,
            visible_skills=visible_skills,
            visible_families=visible_families,
            skill_limit=max(8, len(visible_skills)),
            tool_limit=max(8, len(visible_families)),
        )
        retrieval_scope = plan_retrieval_scope(
            visible_skills=visible_skills,
            visible_families=visible_families,
            semantic_frontdoor=semantic_frontdoor,
        )
        try:
            block = await manager.retrieve_block(
                query=query_text,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
                search_context_types=retrieval_scope['search_context_types'],
                allowed_context_types=retrieval_scope['allowed_context_types'],
                allowed_resource_record_ids=retrieval_scope['allowed_resource_record_ids'],
                allowed_skill_record_ids=retrieval_scope['allowed_skill_record_ids'],
            )
        except Exception:
            return enriched
        if not block:
            return enriched
        if enriched and enriched[0].get('role') == 'system':
            base = str(enriched[0].get('content') or '')
            enriched[0] = {**enriched[0], 'content': f"{base}\n\n{block}".strip()}
        else:
            enriched.insert(0, {'role': 'system', 'content': block})
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
        self.log_service.ensure_task_projection(task_id)
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
        callback_tasks = [task for task in list(self._task_event_dispatch_tasks) if task is not None and not task.done()]
        for task in callback_tasks:
            task.cancel()
        if callback_tasks:
            await asyncio.gather(*callback_tasks, return_exceptions=True)
        self._task_event_dispatch_tasks.clear()
        await self.task_stall_notifier.close()
        if self.execution_mode == 'worker' and self.worker_id:
            stopped_item = {
                'worker_id': self.worker_id,
                'role': 'task_worker',
                'status': 'stopped',
                'updated_at': now_iso(),
                'payload': {'execution_mode': self.execution_mode},
            }
            self.store.upsert_worker_status(
                worker_id=str(stopped_item['worker_id']),
                role=str(stopped_item['role']),
                status=str(stopped_item['status']),
                updated_at=str(stopped_item['updated_at']),
                payload=dict(stopped_item['payload']),
            )
            self.publish_worker_status_event(item=stopped_item)
        await self.task_runner.close()
        await self.registry.close()
        self.governance_store.close()
        self.store.close()

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
            },
            'required': ['任务id', '节点id'],
        }

    async def execute(self, **kwargs: Any) -> dict[str, Any] | str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        node_id = str(kwargs.get('节点id') or '').strip()
        return self._service.node_detail(task_id, node_id)


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
        if 'continuation_of_task_id' in (params or {}):
            continuation_of_task_id = str((params or {}).get('continuation_of_task_id') or '').strip()
            if not continuation_of_task_id:
                errors.append('continuation_of_task_id must not be empty when provided')
            elif not continuation_of_task_id.startswith('task:'):
                errors.append('continuation_of_task_id must start with task:')
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
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        explicit_max_depth = kwargs.get('max_depth', kwargs.get('maxDepth'))
        if explicit_max_depth in (None, ''):
            explicit_max_depth = _runtime_task_default_max_depth(runtime)
        normalized_core_requirement = str(core_requirement or kwargs.get('core_requirement') or '').strip() or str(task or '').strip()
        normalized_execution_policy = normalize_execution_policy_metadata(kwargs.get('execution_policy'))
        final_acceptance_prompt = str(kwargs.get('final_acceptance_prompt') or '').strip()
        raw_requires_final_acceptance = kwargs.get('requires_final_acceptance')
        requires_final_acceptance = bool(raw_requires_final_acceptance) or (raw_requires_final_acceptance in (None, '') and bool(final_acceptance_prompt))
        continuation_of_task_id = MainRuntimeService._normalize_continuation_task_id(kwargs.get('continuation_of_task_id'))
        raw_reuse_existing = kwargs.get('reuse_existing')
        reuse_existing = True if raw_reuse_existing in (None, '') else bool(raw_reuse_existing)
        created_by_source = ''
        if continuation_of_task_id:
            created_by_source = 'heartbeat_auto_continue' if bool(runtime.get('heartbeat_internal')) else 'ceo_user_rebuild'
            if reuse_existing:
                finder = getattr(self._service, 'find_reusable_continuation_task', None)
                existing = (
                    finder(
                        session_id=session_id,
                        continuation_of_task_id=continuation_of_task_id,
                    )
                    if callable(finder)
                    else None
                )
                if existing is not None:
                    return f'复用进行中任务{existing.task_id}'
        record = await self._service.create_task(
            str(task or ''),
            session_id=session_id,
            max_depth=explicit_max_depth,
            metadata={
                'core_requirement': normalized_core_requirement,
                'execution_policy': normalized_execution_policy.model_dump(mode='json'),
                'continuation_of_task_id': continuation_of_task_id,
                'created_by_source': created_by_source,
                'final_acceptance': {
                    'required': requires_final_acceptance,
                    'prompt': final_acceptance_prompt,
                    'node_id': '',
                    'status': 'pending',
                }
            },
        )
        return f'创建任务成功{record.task_id}'
