from __future__ import annotations

import asyncio
import base64
import shutil
from pathlib import Path
from typing import Any, Callable

from g3ku.config.live_runtime import get_runtime_config
from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.tool_execution_control import StopToolExecutionTool, WaitToolExecutionTool
from g3ku.content import ContentNavigationService
from g3ku.resources.models import ResourceKind
from g3ku.runtime.memory_scope import DEFAULT_WEB_MEMORY_SCOPE, normalize_memory_scope
from g3ku.runtime.tool_watchdog import ToolExecutionManager
from g3ku.runtime.context.summarizer import layered_body_payload
from main.governance import (
    GovernanceStore,
    MainRuntimePolicyEngine,
    MainRuntimeResourceRegistry,
    PermissionSubject,
    list_effective_skill_ids,
    list_effective_tool_names,
)
from main.ids import new_node_id, new_task_id
from main.models import NodeRecord, TaskArtifactRecord, TaskRecord, TokenUsageSummary, normalize_final_acceptance_metadata
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
from main.storage.artifact_store import TaskArtifactStore
from main.storage.sqlite_store import SQLiteTaskStore


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
        max_iterations: int = 16,
        execution_max_iterations: int | None = None,
        acceptance_max_iterations: int | None = None,
    ) -> None:
        self._chat_backend = chat_backend
        self._app_config = app_config
        self.store = SQLiteTaskStore(store_path or (Path.cwd() / '.g3ku' / 'main-runtime' / 'runtime.sqlite3'))
        self.file_store = TaskFileStore(files_base_dir or (Path.cwd() / '.g3ku' / 'main-runtime' / 'tasks'))
        self.artifact_store = TaskArtifactStore(artifact_dir=artifact_dir or (Path.cwd() / '.g3ku' / 'main-runtime' / 'artifacts'), store=self.store)
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
        react_loop = ReActToolLoop(
            chat_backend=chat_backend,
            log_service=self.log_service,
            max_iterations=max_iterations,
            parallel_tool_calls_enabled=parallel_enabled,
            max_parallel_tool_calls=max_parallel_tool_calls,
        )
        react_loop._tool_execution_manager = self.tool_execution_manager
        self._react_loop = react_loop
        self.node_runner = NodeRunner(
            store=self.store,
            log_service=self.log_service,
            react_loop=react_loop,
            tool_provider=self._tool_provider,
            execution_model_refs=list(execution_model_refs or ['execution']),
            acceptance_model_refs=list(acceptance_model_refs or execution_model_refs or ['inspection']),
            execution_max_iterations=execution_max_iterations if execution_max_iterations is not None else max_iterations,
            acceptance_max_iterations=acceptance_max_iterations if acceptance_max_iterations is not None else (execution_max_iterations if execution_max_iterations is not None else max_iterations),
            max_parallel_child_pipelines=max_parallel_child_pipelines,
            context_enricher=self._enrich_node_messages,
        )
        self.task_runner = TaskRunner(store=self.store, log_service=self.log_service, node_runner=self.node_runner)
        self._started = False
        self._runtime_loop = None

    async def startup(self) -> None:
        if self._started:
            return
        self._started = True
        self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        if self.memory_manager is not None and hasattr(self.memory_manager, 'sync_catalog'):
            try:
                await self.memory_manager.sync_catalog(self)
            except Exception:
                pass
        for task in self.store.list_tasks():
            self.log_service.bootstrap_missing_files(task.task_id)
            if task.status != 'in_progress':
                continue
            runtime_state = self.log_service.read_runtime_state(task.task_id)
            if runtime_state is None:
                self.log_service.mark_task_failed(task.task_id, reason='runtime_state_corrupt')
                continue
            if bool(task.is_paused) or bool(runtime_state.get('paused')):
                continue
            self.task_runner.start_background(task.task_id)

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
        prompt = str(task or '').strip()
        if not prompt:
            raise ValueError('task must not be empty')
        effective_max_depth = self._clamp_depth(max_depth)
        task_id = new_task_id()
        root_node_id = new_node_id()
        now = now_iso()
        task_metadata = self._normalize_task_metadata(session_id=session_id, metadata=metadata)
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
            metadata={},
        )
        record, root = self.log_service.initialize_task(record, root)
        self.task_runner.start_background(task_id)
        return self.store.get_task(record.task_id) or record

    async def cancel_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        await self.task_runner.cancel(task_id)
        return self.get_task(task_id)

    async def pause_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        await self.task_runner.pause(task_id)
        return self.get_task(task_id)

    async def resume_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        await self.task_runner.resume(task_id)
        return self.get_task(task_id)

    async def retry_task(self, task_id: str) -> TaskRecord | None:
        await self.startup()
        task_id = self.normalize_task_id(task_id)
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
        await self.registry.forget_task(task.session_id, task_id)
        return task

    async def wait_for_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        await self.task_runner.wait(task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord | None:
        task_id = self.normalize_task_id(task_id)
        return self.store.get_task(task_id)

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

    def get_session_task_counts(self, session_id: str) -> dict[str, int]:
        tasks = self.list_tasks_for_session(session_id)
        unfinished = sum(1 for task in tasks if str(getattr(task, 'status', '') or '').strip().lower() == 'in_progress')
        return {
            'total': len(tasks),
            'unfinished': unfinished,
            'terminal': max(0, len(tasks) - unfinished),
        }

    async def delete_task_records_for_session(self, session_id: str) -> int:
        unfinished = self.list_unfinished_tasks_for_session(session_id)
        if unfinished:
            raise ValueError('session_has_unfinished_tasks')
        deleted = 0
        for task in list(self.list_tasks_for_session(session_id)):
            await self.delete_task(task.task_id)
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
            setattr(self._react_loop, '_tool_execution_manager', manager)

    @staticmethod
    def _node_parallelism_settings(config: Any | None) -> tuple[bool, int, int]:
        agents = getattr(config, 'agents', None) if config is not None else None
        parallelism = getattr(agents, 'node_parallelism', None) if agents is not None else None
        enabled = bool(getattr(parallelism, 'enabled', True)) if parallelism is not None else True
        max_parallel_tool_calls = max(1, int(getattr(parallelism, 'max_parallel_tool_calls_per_node', 10) or 1))
        max_parallel_child_pipelines = max(1, int(getattr(parallelism, 'max_parallel_child_pipelines_per_node', 10) or 1))
        if not enabled:
            return False, 1, 1
        return True, max_parallel_tool_calls, max_parallel_child_pipelines

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
        parallel_enabled, max_parallel_tool_calls, max_parallel_child_pipelines = self._node_parallelism_settings(config)
        self._react_loop._parallel_tool_calls_enabled = parallel_enabled
        self._react_loop._max_parallel_tool_calls = max_parallel_tool_calls
        self.node_runner._max_parallel_child_pipelines = max_parallel_child_pipelines
        if self._resource_manager is not None and hasattr(self._resource_manager, 'bind_app_config'):
            self._resource_manager.bind_app_config(config)
        self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        self._runtime_model_revision = int(revision or 0)
        return True

    def _normalize_task_metadata(self, *, session_id: str, metadata: dict[str, Any] | None) -> dict[str, Any]:
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
            actions = [
                action
                for action in family.actions
                if (
                    self.policy_engine.evaluate_tool_action(
                        subject=subject,
                        tool_id=family.tool_id,
                        action_id=action.action_id,
                    ).allowed
                    and (
                        not callable_family
                        or bool(set(action.executor_names) & visible_names)
                    )
                )
            ]
            if actions:
                families.append(family.model_copy(update={'actions': actions}))
        return families

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

    def load_skill_context(self, *, actor_role: str, session_id: str, skill_id: str) -> dict[str, Any]:
        visible = {item.skill_id: item for item in self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)}
        record = visible.get(str(skill_id or '').strip())
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
        skill_id: str,
        level: str = 'l1',
        query: str = '',
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        visible = {item.skill_id: item for item in self.list_visible_skill_resources(actor_role=actor_role, session_id=session_id)}
        record = visible.get(str(skill_id or '').strip())
        if record is None:
            return {'ok': False, 'error': f'Skill not visible: {skill_id}'}
        path = Path(record.skill_doc_path) if record.skill_doc_path else None
        content = path.read_text(encoding='utf-8') if path and path.exists() else ''
        payload = layered_body_payload(
            body=content,
            level=level,
            query=query,
            max_tokens=max_tokens,
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

    def load_tool_context(self, *, actor_role: str, session_id: str, tool_id: str) -> dict[str, Any]:
        tool_name = str(tool_id or '').strip()
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
        }

    def load_tool_context_v2(
        self,
        *,
        actor_role: str,
        session_id: str,
        tool_id: str,
        level: str = 'l1',
        query: str = '',
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        tool_name = str(tool_id or '').strip()
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
            level=level,
            query=query,
            max_tokens=max_tokens,
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

    def list_tool_resources(self) -> list[Any]:
        return list(self.resource_registry.list_tool_families())

    def get_tool_family(self, tool_id: str):
        return self.resource_registry.get_tool_family(str(tool_id or '').strip())

    def _tool_family_executor_name(self, family) -> str:
        primary = str(getattr(family, 'primary_executor_name', '') or '').strip()
        if primary:
            return primary
        for action in list(getattr(family, 'actions', []) or []):
            for executor_name in list(getattr(action, 'executor_names', []) or []):
                name = str(executor_name or '').strip()
                if name:
                    return name
        fallback = str(getattr(family, 'tool_id', '') or '').strip()
        if self._resource_manager is not None and fallback:
            descriptor = self._resource_manager.get_tool_descriptor(fallback)
            if descriptor is not None:
                return fallback
        return ''

    def get_tool_toolskill(self, tool_id: str) -> dict[str, Any] | None:
        family = self.get_tool_family(tool_id)
        if family is None:
            needle = str(tool_id or '').strip()
            for item in self.list_tool_resources():
                action_names = {
                    str(executor_name or '').strip()
                    for action in list(getattr(item, 'actions', []) or [])
                    for executor_name in list(getattr(action, 'executor_names', []) or [])
                    if str(executor_name or '').strip()
                }
                if needle and needle in action_names:
                    family = item
                    break
        if family is None:
            return None
        executor_name = self._tool_family_executor_name(family)
        content = ''
        path = ''
        descriptor = None
        if executor_name and self._resource_manager is not None:
            try:
                content = self._resource_manager.load_toolskill_body(executor_name)
            except FileNotFoundError:
                content = ''
            descriptor = self._resource_manager.get_tool_descriptor(executor_name)
            if descriptor is not None and getattr(descriptor, 'toolskills_main_path', None) is not None:
                path = str(descriptor.toolskills_main_path)
        if descriptor is None and self._resource_manager is not None:
            descriptor = self._resource_manager.get_tool_descriptor(str(getattr(family, 'tool_id', '') or '').strip())
            if descriptor is not None and getattr(descriptor, 'toolskills_main_path', None) is not None:
                path = str(descriptor.toolskills_main_path)
        tool_type = str(getattr(family, 'tool_type', getattr(descriptor, 'tool_type', 'internal')) or 'internal')
        install_dir = str(
            getattr(family, 'install_dir', None)
            or getattr(descriptor, 'install_dir', '')
            or ''
        ).strip() or None
        callable_flag = bool(getattr(family, 'callable', getattr(descriptor, 'callable', True)))
        return {
            'tool_id': family.tool_id,
            'primary_executor_name': executor_name,
            'content': content,
            'path': path,
            'description': family.description,
            'tool_type': tool_type,
            'install_dir': install_dir,
            'callable': callable_flag,
        }

    def delete_tool_resource(self, tool_id: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        family = self.get_tool_family(tool_id)
        if family is None:
            raise ValueError('tool_not_found')

        target_tool_id = str(family.tool_id or '').strip()
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
        family = self.get_tool_family(tool_id)
        if family is None:
            return None
        allowed_roles_by_action = dict(allowed_roles_by_action or {})
        actions = []
        for action in family.actions:
            roles = allowed_roles_by_action.get(action.action_id)
            actions.append(action.model_copy(update={'allowed_roles': list(action.allowed_roles if roles is None else roles)}))
        updated = family.model_copy(update={'enabled': family.enabled if enabled is None else bool(enabled), 'actions': actions})
        self.governance_store.upsert_tool_family(updated, updated_at=now_iso())
        self.policy_engine.sync_default_role_policies()
        return updated

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
        task = self.get_task(task_id)
        if task is None:
            return None
        progress = self.query_service.view_progress(task_id, mark_read=mark_read)
        if progress is None:
            return None
        latest = self.get_task(task_id) or task
        return {
            'task': latest.model_dump(mode='json'),
            'progress': progress.model_dump(mode='json'),
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
            payload = build_envelope(
                channel='task',
                session_id=task.session_id,
                task_id=task.task_id,
                seq=self.registry.next_global_task_seq(task.task_id),
                type='artifact.applied',
                data={'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True},
            )
            self.registry.publish_global_task(task.task_id, payload)
            ceo_payload = build_envelope(
                channel='ceo',
                session_id=task.session_id,
                task_id=task.task_id,
                seq=self.registry.next_ceo_seq(task.session_id),
                type='task.artifact.applied',
                data={'artifact_id': artifact.artifact_id, 'path': str(target_path), 'task_id': task.task_id},
            )
            self.registry.publish_global_ceo(ceo_payload)
        return {'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True}

    def _actor_role_for_node(self, node: NodeRecord) -> str:
        return 'inspection' if node.node_kind == 'acceptance' else 'execution'

    def _tool_provider(self, node: NodeRecord) -> dict[str, Tool]:
        task = self.store.get_task(node.task_id)
        session_id = task.session_id if task is not None else 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible = set(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id))
        provided = dict(self._external_tool_provider(node) or {})
        provided.update(self._builtin_tool_instances())
        if self._resource_manager is not None:
            for name, tool in self._resource_manager.tool_instances().items():
                if name in visible:
                    provided[name] = tool
        return provided

    def _builtin_tool_instances(self) -> dict[str, Tool]:
        if self._builtin_tool_cache is None:
            manager_getter = lambda: self.tool_execution_manager
            self._builtin_tool_cache = {
                'wait_tool_execution': WaitToolExecutionTool(manager_getter),
                'stop_tool_execution': StopToolExecutionTool(manager_getter),
            }
        return dict(self._builtin_tool_cache)

    async def _enrich_node_messages(self, *, task, node: NodeRecord, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        manager = getattr(self, 'memory_manager', None)
        if manager is None or not getattr(manager, '_feature_enabled', lambda _key: False)('unified_context'):
            return messages
        query_text = str(getattr(node, 'prompt', '') or getattr(node, 'goal', '') or '').strip()
        if not query_text:
            return messages
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        memory_scope = self._task_memory_scope(task)
        channel = str(memory_scope.get('channel') or 'unknown')
        chat_id = str(memory_scope.get('chat_id') or 'unknown')
        try:
            block = await manager.retrieve_block(
                query=query_text,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
            )
        except Exception:
            return messages
        if not block:
            return messages
        enriched = list(messages)
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

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> str:
        task_id = self.normalize_task_id(task_id)
        payload = self.query_service.view_progress(task_id, mark_read=mark_read)
        if payload is None:
            return f'Error: Task not found: {task_id}'
        return payload.text

    async def close(self) -> None:
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


class _LegacyCreateAsyncTaskTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'create_async_task'

    @property
    def description(self) -> str:
        return '把用户需求转交为后台异步任务；主 agent 不可直接使用派生子节点。对于工作量大的任务，应在 task 说明中显式要求执行节点优先评估拆解并派生子节点。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'task': {
                    'type': 'string',
                    'description': '用户的原始需求。若任务工作量大，应在说明中写明拆分维度，并显式建议执行节点优先评估拆解/派生子节点。',
                }
            },
            'required': ['task'],
        }

    async def execute(self, task: str, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        explicit_max_depth = kwargs.get('max_depth', kwargs.get('maxDepth'))
        if explicit_max_depth in (None, ''):
            explicit_max_depth = _runtime_task_default_max_depth(runtime)
        record = await self._service.create_task(
            str(task or ''),
            session_id=session_id,
            max_depth=explicit_max_depth,
        )
        return f'创建任务成功{record.task_id}'


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
        return '按任务 id 返回任务状态、树状图文本和最新节点输出内容，并将任务标记为已读。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {'任务id': {'type': 'string', 'description': '目标任务 id。'}}, 'required': ['任务id']}

    async def execute(self, **kwargs: Any) -> str:
        await self._service.startup()
        task_id = str(kwargs.get('任务id') or '').strip()
        return self._service.view_progress(task_id, mark_read=True)
class CreateAsyncTaskTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'create_async_task'

    @property
    def description(self) -> str:
        return '把用户需求转交为后台异步任务；主 agent 不可直接使用派生子节点。对于工作量大的任务，应在 task 说明中显式要求执行节点优先评估拆解并派生子节点。必要时可同时声明最终结果是否需要验收。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'task': {
                    'type': 'string',
                    'description': '用户的原始需求。若任务工作量大，应在说明中写明拆分维度，并显式建议执行节点优先评估拆解/派生子节点。',
                },
                'requires_final_acceptance': {
                    'type': 'boolean',
                    'description': '是否需要在 root execution 完成后再做最终验收。',
                },
                'final_acceptance_prompt': {
                    'type': 'string',
                    'description': '最终验收提示词。仅当 requires_final_acceptance=true 时必填。',
                },
            },
            'required': ['task'],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        requires_final_acceptance = (params or {}).get('requires_final_acceptance')
        final_acceptance_prompt = str((params or {}).get('final_acceptance_prompt') or '').strip()
        if requires_final_acceptance is True and not final_acceptance_prompt:
            errors.append('final_acceptance_prompt is required when requires_final_acceptance=true')
        return errors

    async def execute(self, task: str, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = _tool_runtime_payload(__g3ku_runtime, kwargs)
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        explicit_max_depth = kwargs.get('max_depth', kwargs.get('maxDepth'))
        if explicit_max_depth in (None, ''):
            explicit_max_depth = _runtime_task_default_max_depth(runtime)
        final_acceptance_prompt = str(kwargs.get('final_acceptance_prompt') or '').strip()
        raw_requires_final_acceptance = kwargs.get('requires_final_acceptance')
        requires_final_acceptance = bool(raw_requires_final_acceptance) or (raw_requires_final_acceptance in (None, '') and bool(final_acceptance_prompt))
        record = await self._service.create_task(
            str(task or ''),
            session_id=session_id,
            max_depth=explicit_max_depth,
            metadata={
                'final_acceptance': {
                    'required': requires_final_acceptance,
                    'prompt': final_acceptance_prompt,
                    'node_id': '',
                    'status': 'pending',
                }
            },
        )
        return f'创建任务成功{record.task_id}'
