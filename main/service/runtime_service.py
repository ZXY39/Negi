from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Callable

from g3ku.config.live_runtime import get_runtime_config
from g3ku.agent.tools.base import Tool
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
from main.models import NodeRecord, TaskArtifactRecord, TaskRecord
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
    ) -> None:
        self._chat_backend = chat_backend
        self._app_config = app_config
        self.store = SQLiteTaskStore(store_path or (Path.cwd() / '.g3ku' / 'main-runtime' / 'runtime.sqlite3'))
        self.file_store = TaskFileStore(files_base_dir or (Path.cwd() / '.g3ku' / 'main-runtime' / 'tasks'))
        self.artifact_store = TaskArtifactStore(artifact_dir=artifact_dir or (Path.cwd() / '.g3ku' / 'main-runtime' / 'artifacts'), store=self.store)
        self.registry = TaskEventRegistry()
        self.tree_builder = TaskTreeBuilder()
        self.log_service = TaskLogService(store=self.store, file_store=self.file_store, tree_builder=self.tree_builder, registry=self.registry)
        self.query_service = TaskQueryService(store=self.store, file_store=self.file_store, log_service=self.log_service)
        self.governance_store = GovernanceStore(governance_store_path or (Path.cwd() / '.g3ku' / 'main-runtime' / 'governance.sqlite3'))
        self.resource_registry = MainRuntimeResourceRegistry(workspace_root=Path.cwd(), store=self.governance_store, resource_manager=resource_manager)
        self.policy_engine = MainRuntimePolicyEngine(store=self.governance_store, resource_registry=self.resource_registry)
        self._external_tool_provider = tool_provider or (lambda _node: {})
        self._resource_manager = resource_manager
        self.memory_manager = None
        self._default_max_depth = max(0, int(default_max_depth or 0))
        self._hard_max_depth = max(self._default_max_depth, int(hard_max_depth or self._default_max_depth))
        react_loop = ReActToolLoop(chat_backend=chat_backend, log_service=self.log_service, max_iterations=max_iterations)
        self.node_runner = NodeRunner(
            store=self.store,
            log_service=self.log_service,
            react_loop=react_loop,
            tool_provider=self._tool_provider,
            execution_model_refs=list(execution_model_refs or ['execution']),
            acceptance_model_refs=list(acceptance_model_refs or execution_model_refs or ['inspection']),
            context_enricher=self._enrich_node_messages,
        )
        self.task_runner = TaskRunner(store=self.store, log_service=self.log_service, node_runner=self.node_runner)
        self._started = False

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
            metadata=dict(metadata or {}),
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
            metadata={},
        )
        record, root = self.log_service.initialize_task(record, root)
        self.task_runner.start_background(task_id)
        return self.store.get_task(record.task_id) or record

    async def cancel_task(self, task_id: str) -> TaskRecord | None:
        await self.task_runner.cancel(task_id)
        return self.get_task(task_id)

    async def pause_task(self, task_id: str) -> TaskRecord | None:
        await self.task_runner.pause(task_id)
        return self.get_task(task_id)

    async def resume_task(self, task_id: str) -> TaskRecord | None:
        await self.task_runner.resume(task_id)
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord | None:
        return self.store.get_task(task_id)

    def bind_resource_manager(self, resource_manager) -> None:
        self._resource_manager = resource_manager
        self.resource_registry.bind_resource_manager(resource_manager)

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
        if self._resource_manager is not None and hasattr(self._resource_manager, 'bind_app_config'):
            self._resource_manager.bind_app_config(config)
        self.resource_registry.refresh_from_current_resources()
        self.policy_engine.sync_default_role_policies()
        self._runtime_model_revision = int(revision or 0)
        return True

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
            actions = [
                action
                for action in family.actions
                if set(action.executor_names) & visible_names
                and self.policy_engine.evaluate_tool_action(subject=subject, tool_id=family.tool_id, action_id=action.action_id).allowed
            ]
            if actions:
                families.append(family.model_copy(update={'actions': actions}))
        return families

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
        visible = set(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id))
        if tool_name not in visible:
            return {'ok': False, 'error': f'Tool not visible: {tool_id}'}
        if self._resource_manager is None:
            return {'ok': False, 'error': 'Resource manager unavailable'}
        toolskill = self.get_tool_toolskill(tool_name) or {}
        content = str(toolskill.get('content') or '')
        return {
            'ok': True,
            'tool_id': tool_name,
            'content': content,
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
        visible = set(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id))
        if tool_name not in visible:
            return {'ok': False, 'error': f'Tool not visible: {tool_id}'}
        if self._resource_manager is None:
            return {'ok': False, 'error': 'Resource manager unavailable'}
        toolskill = self.get_tool_toolskill(tool_name) or {}
        content = str(toolskill.get('content') or '')
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
            'tool_id': tool_name,
            'uri': f'g3ku://resource/tool/{tool_name}',
            'level': payload['level'],
            'content': payload['content'],
            'l0': payload['l0'],
            'l1': payload['l1'],
            'path': payload['path'],
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

    def write_skill_file(self, skill_id: str, file_key: str, content: str, *, session_id: str = 'web:shared') -> dict[str, Any]:
        path = self.resource_registry.skill_file_map(str(skill_id or '').strip()).get(str(file_key or '').strip())
        if path is None:
            raise ValueError('editable_file_not_allowed')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content or ''), encoding='utf-8')
        self.reload_resources(session_id=session_id)
        return {'skill_id': str(skill_id or '').strip(), 'file_key': str(file_key or '').strip(), 'path': str(path)}

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
        if executor_name and self._resource_manager is not None:
            try:
                content = self._resource_manager.load_toolskill_body(executor_name)
            except FileNotFoundError:
                content = ''
            descriptor = self._resource_manager.get_tool_descriptor(executor_name)
            if descriptor is not None and getattr(descriptor, 'toolskills_main_path', None) is not None:
                path = str(descriptor.toolskills_main_path)
        return {
            'tool_id': family.tool_id,
            'primary_executor_name': executor_name,
            'content': content,
            'path': path,
            'description': family.description,
        }

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
        return self.store.list_artifacts(task_id)

    def get_artifact(self, artifact_id: str) -> TaskArtifactRecord | None:
        return self.store.get_artifact(artifact_id)

    async def apply_patch_artifact(self, task_id: str, artifact_id: str) -> dict[str, Any] | None:
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
        if task is not None:
            payload = build_envelope(channel='task', session_id=task.session_id, task_id=task.task_id, seq=self.registry.next_task_seq(task.session_id, task.task_id), type='artifact.applied', data={'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True})
            self.registry.publish_task(task.session_id, task.task_id, payload)
            ceo_payload = build_envelope(channel='ceo', session_id=task.session_id, task_id=task.task_id, seq=self.registry.next_ceo_seq(task.session_id), type='task.artifact.applied', data={'artifact_id': artifact.artifact_id, 'path': str(target_path), 'task_id': task.task_id})
            self.registry.publish_ceo(task.session_id, ceo_payload)
        return {'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True}

    def _actor_role_for_node(self, node: NodeRecord) -> str:
        return 'inspection' if node.node_kind == 'acceptance' else 'execution'

    def _tool_provider(self, node: NodeRecord) -> dict[str, Tool]:
        task = self.store.get_task(node.task_id)
        session_id = task.session_id if task is not None else 'web:shared'
        actor_role = self._actor_role_for_node(node)
        visible = set(self.list_effective_tool_names(actor_role=actor_role, session_id=session_id))
        provided = dict(self._external_tool_provider(node) or {})
        if self._resource_manager is not None:
            for name, tool in self._resource_manager.tool_instances().items():
                if name in visible:
                    provided[name] = tool
        return provided

    async def _enrich_node_messages(self, *, task, node: NodeRecord, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        manager = getattr(self, 'memory_manager', None)
        if manager is None or not getattr(manager, '_feature_enabled', lambda _key: False)('unified_context'):
            return messages
        query_text = str(getattr(node, 'prompt', '') or getattr(node, 'goal', '') or '').strip()
        if not query_text:
            return messages
        session_key = str(getattr(task, 'session_id', '') or 'web:shared').strip() or 'web:shared'
        if ':' in session_key:
            channel, chat_id = session_key.split(':', 1)
        else:
            channel, chat_id = 'task', session_key
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
        return self.query_service.summary(session_id).text

    def get_tasks(self, session_id: str, task_type: int) -> str:
        items = self.query_service.get_tasks(session_id, task_type)
        if not items:
            return '无匹配任务。'
        return '\n'.join(f'- {item.task_id}：{item.brief}' for item in items)

    def view_progress(self, task_id: str, *, mark_read: bool = True) -> str:
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


class CreateAsyncTaskTool(Tool):
    def __init__(self, service: MainRuntimeService):
        self._service = service

    @property
    def name(self) -> str:
        return 'create_async_task'

    @property
    def description(self) -> str:
        return '把用户需求转交为后台异步任务；主 agent 不可直接使用派生子节点。'

    @property
    def parameters(self) -> dict[str, Any]:
        return {'type': 'object', 'properties': {'task': {'type': 'string', 'description': '用户的原始需求。'}}, 'required': ['task']}

    async def execute(self, task: str, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        record = await self._service.create_task(str(task or ''), session_id=session_id)
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
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
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
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
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
