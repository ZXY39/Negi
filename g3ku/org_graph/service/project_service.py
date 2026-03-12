from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from g3ku.config.live_runtime import get_runtime_config, peek_runtime_revision
from g3ku.config.model_manager import ModelManager
from g3ku.org_graph.config import ResolvedOrgGraphConfig
from g3ku.org_graph.errors import PermissionDeniedError
from g3ku.org_graph.governance.approval_service import GovernanceApprovalService
from g3ku.org_graph.governance.resource_filter import list_effective_skill_ids, list_effective_tool_names
from g3ku.org_graph.governance.policy_engine import GovernancePolicyEngine
from g3ku.org_graph.governance.resource_registry import OrgGraphResourceRegistry
from g3ku.org_graph.governance.store import GovernanceStore
from g3ku.org_graph.ids import new_project_id, new_unit_id
from g3ku.agent.tools.propose_patch import parse_patch_artifact
from g3ku.org_graph.llm.client import OrgGraphLLM
from g3ku.org_graph.models import ProjectCreateRequest, ProjectRecord, UnitAgentRecord
from g3ku.org_graph.planning.depth_policy import can_delegate, clamp_max_depth
from g3ku.org_graph.protocol import build_envelope, now_iso
from g3ku.org_graph.prompt_loader import load_prompt_preview
from g3ku.org_graph.service.notice_service import NoticeService
from g3ku.org_graph.service.project_registry import ProjectRegistry
from g3ku.agent.rag_memory import MemoryManager
from g3ku.org_graph.public_roles import MAIN_ACTOR_ROLE, to_public_actor_role, to_public_allowed_roles, to_public_model_defaults
from g3ku.org_graph.storage.artifact_store import ArtifactStore
from g3ku.org_graph.storage.checkpoint_store import CheckpointStore
from g3ku.org_graph.storage.project_store import ProjectStore
from g3ku.org_graph.storage.task_monitor_store import TaskMonitorStore
from g3ku.org_graph.service.task_monitor_service import TaskMonitorService


ROOT_EXECUTION_ROLE_TITLE = '项目主管'


def _normalize_model_role(role: str) -> str:
    raw = str(role or '').strip().lower()
    if raw in {'inspection', 'checker'}:
        return 'inspection'
    if raw == 'execution':
        return 'execution'
    if raw == MAIN_ACTOR_ROLE:
        return MAIN_ACTOR_ROLE
    raise ValueError(f"Unsupported model role: {role}")


class ProjectService:
    def __init__(self, config: ResolvedOrgGraphConfig, resource_manager=None):
        self.config = config
        self.store = ProjectStore(config.project_store_path)
        self.checkpoint_store = CheckpointStore(config.checkpoint_store_path)
        self.task_monitor_store = TaskMonitorStore(config.task_monitor_store_path)
        self.governance_store = GovernanceStore(config.governance_store_path)
        self.registry = ProjectRegistry()
        self.notice_service = NoticeService(self.store)
        self.llm = OrgGraphLLM.from_config(config.raw, default_model=config.execution_model)
        self.memory_manager = self._build_memory_manager()
        self.artifact_store = ArtifactStore(artifact_dir=config.artifact_dir, project_store=self.store)
        self.monitor_service = TaskMonitorService(self, self.task_monitor_store)
        self.resource_manager = resource_manager
        self.resource_registry = OrgGraphResourceRegistry(config, self.governance_store, resource_manager=resource_manager)
        self.policy_engine = GovernancePolicyEngine(store=self.governance_store, resource_registry=self.resource_registry)
        self.approval_service = GovernanceApprovalService(
            self,
            store=self.governance_store,
            resource_registry=self.resource_registry,
            policy_engine=self.policy_engine,
        )
        if config.governance_enabled:
            self.resource_registry.refresh()
            self.policy_engine.sync_default_role_policies()
        from g3ku.org_graph.execution.hybrid_scheduler import HybridScheduler
        from g3ku.org_graph.execution.project_runner import ProjectRunner
        from g3ku.org_graph.execution.checker_runner import CheckerRunner
        from g3ku.org_graph.execution.tool_runtime import OrgGraphToolRuntime
        from g3ku.org_graph.execution.unit_runner import UnitRunner
        self.scheduler = HybridScheduler(config.max_parallel_units_total)
        self.tool_runtime = OrgGraphToolRuntime(self)
        self.checker_runner = CheckerRunner(self)
        self.unit_runner = UnitRunner(self)
        self.project_runner = ProjectRunner(self)
        self._runtime_model_revision = max(int(peek_runtime_revision() or 0), 1)
        self._startup_lock = asyncio.Lock()
        self._started = False
        self._closed = False

    def bind_resource_manager(self, resource_manager) -> None:
        self.resource_manager = resource_manager
        self.resource_registry.bind_resource_manager(resource_manager)

    def _build_memory_manager(self):
        if not bool(getattr(self.config.raw.tools.memory, 'enabled', False)):
            return None
        try:
            return MemoryManager(self.config.raw.workspace_path, self.config.raw.tools.memory)
        except Exception:
            return None

    def list_runtime_tool_names(self) -> list[str]:
        runtime = getattr(self, 'tool_runtime', None)
        if runtime is None:
            return []
        return list(runtime.supported_tool_names())

    def list_available_skill_ids(self) -> list[str]:
        records = self.resource_registry.list_skill_resources()
        seen: set[str] = set()
        skill_ids: list[str] = []
        for record in records:
            skill_id = str(record.skill_id or '').strip()
            if not skill_id or skill_id in seen:
                continue
            if hasattr(record, 'enabled') and not bool(record.enabled):
                continue
            if hasattr(record, 'available') and not bool(record.available):
                continue
            seen.add(skill_id)
            skill_ids.append(skill_id)
        return skill_ids

    def build_policy_subject(
        self,
        *,
        session_id: str,
        actor_role: str,
        project_id: str | None = None,
        unit_id: str | None = None,
    ):
        return self.approval_service.build_subject(
            session_id=session_id,
            actor_role=to_public_actor_role(actor_role),
            project_id=project_id,
            unit_id=unit_id,
        )

    def _apply_runtime_config(self, config, revision: int) -> None:
        from g3ku.org_graph.config import resolve_org_graph_config

        self.config = resolve_org_graph_config(config)
        self.llm = OrgGraphLLM.from_config(self.config.raw, default_model=self.config.execution_model)
        self._runtime_model_revision = int(revision or self._runtime_model_revision or 1)
        if self.resource_manager is not None and hasattr(self.resource_manager, "bind_app_config"):
            self.resource_manager.bind_app_config(self.config.raw)

    def ensure_runtime_config_current(self, force: bool = False, reason: str = "runtime") -> bool:
        _ = reason
        runtime_config, revision, changed = get_runtime_config(force=force)
        if not changed and int(revision or 0) == int(self._runtime_model_revision or 0):
            return False
        self._apply_runtime_config(runtime_config, revision)
        return True

    def resolve_role_model_key(self, role: str) -> str:
        self.ensure_runtime_config_current(force=False, reason="resolve_role_model_key")
        normalized = _normalize_model_role(role)
        return self.config.raw.resolve_role_model_key(normalized)

    def resolve_role_model_chain(self, role: str) -> list[str]:
        self.ensure_runtime_config_current(force=False, reason="resolve_role_model_chain")
        normalized = _normalize_model_role(role)
        return self.config.raw.get_role_model_keys(normalized)

    def resolve_bound_model_key(self, role: str, model_binding: str, model_key: str | None) -> str:
        binding = str(model_binding or "live_role").strip() or "live_role"
        if binding == "fixed_key":
            resolved = str(model_key or "").strip()
            if not resolved:
                raise ValueError(f"Fixed model binding for role '{role}' requires model_key")
            item = self.config.raw.get_managed_model(resolved)
            if item is None:
                raise ValueError(f"Unknown model key: {resolved}")
            if not item.enabled:
                raise ValueError(f"Disabled model key cannot be used: {resolved}")
            return resolved
        return self.resolve_role_model_key(role)

    def resolve_bound_model_chain(self, role: str, model_binding: str, model_key: str | None) -> list[str]:
        binding = str(model_binding or "live_role").strip() or "live_role"
        if binding == "fixed_key":
            return [self.resolve_bound_model_key(role, binding, model_key)]
        return self.resolve_role_model_chain(role)

    def _model_manager(self) -> ModelManager:
        return ModelManager(self.config.raw.model_copy(deep=True))

    def list_model_catalog(self) -> dict[str, Any]:
        self.ensure_runtime_config_current(force=False, reason="list_model_catalog")
        manager = self._model_manager()
        catalog = manager.list_models()
        return {
            "items": [str(item.get('key') or '').strip() for item in catalog if str(item.get('key') or '').strip()],
            "catalog": catalog,
            "roles": {
                "ceo": list(manager.config.models.roles.ceo),
                "execution": list(manager.config.models.roles.execution),
                "inspection": list(manager.config.models.roles.inspection),
            },
            "scopes": ["ceo", "execution", "inspection"],
            "defaults": self.default_node_provider_models(),
        }

    async def add_model_catalog_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        manager = self._model_manager()
        result = manager.add_model(
            key=str(payload.get("key") or "").strip(),
            provider_model=str(payload.get("provider_model") or "").strip(),
            api_key=str(payload.get("api_key") or "").strip(),
            api_base=str(payload.get("api_base") or "").strip(),
            scopes=[str(item) for item in (payload.get("scopes") or [])],
            extra_headers=payload.get("extra_headers") if isinstance(payload.get("extra_headers"), dict) else None,
            enabled=bool(payload.get("enabled", True)),
            max_tokens=payload.get("max_tokens"),
            temperature=payload.get("temperature"),
            reasoning_effort=payload.get("reasoning_effort"),
            retry_on=[str(item) for item in (payload.get("retry_on") or [])] if payload.get("retry_on") is not None else None,
            description=str(payload.get("description") or ""),
        )
        self.ensure_runtime_config_current(force=True, reason="add_model_catalog_entry")
        return result

    async def update_model_catalog_entry(self, model_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        manager = self._model_manager()
        result = manager.update_model(
            key=model_key,
            provider_model=payload.get("provider_model"),
            api_key=payload.get("api_key"),
            api_base=payload.get("api_base"),
            extra_headers=payload.get("extra_headers") if isinstance(payload.get("extra_headers"), dict) else None,
            max_tokens=payload.get("max_tokens"),
            temperature=payload.get("temperature"),
            reasoning_effort=payload.get("reasoning_effort"),
            retry_on=[str(item) for item in (payload.get("retry_on") or [])] if payload.get("retry_on") is not None else None,
            description=payload.get("description"),
        )
        self.ensure_runtime_config_current(force=True, reason="update_model_catalog_entry")
        return result

    async def set_model_catalog_entry_enabled(self, model_key: str, enabled: bool) -> dict[str, Any]:
        manager = self._model_manager()
        result = manager.set_model_enabled(model_key, enabled)
        self.ensure_runtime_config_current(force=True, reason="set_model_catalog_entry_enabled")
        return result

    async def update_model_role_chain(self, scope: str, model_keys: list[str]) -> dict[str, Any]:
        manager = self._model_manager()
        result = manager.set_scope_chain(scope, model_keys)
        self.ensure_runtime_config_current(force=True, reason="update_model_role_chain")
        return result

    async def delete_model_catalog_entry(self, model_key: str) -> dict[str, Any]:
        self.ensure_runtime_config_current(force=False, reason="delete_model_catalog_entry")
        if self._model_key_in_use(model_key):
            raise ValueError("model_in_use")
        manager = self._model_manager()
        result = manager.delete_model(model_key)
        self.ensure_runtime_config_current(force=True, reason="delete_model_catalog_entry")
        return result

    def default_node_provider_models(self) -> dict[str, str]:
        self.ensure_runtime_config_current(force=False, reason="default_node_provider_models")
        fallback = self._first_ready_provider_model()
        return to_public_model_defaults(
            {
                'ceo': self._effective_provider_model(self.resolve_role_model_key('ceo'), fallback=fallback),
                'execution': self._effective_provider_model(self.resolve_role_model_key('execution'), fallback=fallback),
                'inspection': self._effective_provider_model(self.resolve_role_model_key('inspection'), fallback=fallback),
            }
        )

    def _provider_model_candidates(self) -> list[str]:
        role_refs = [
            *self.config.raw.models.roles.ceo,
            *self.config.raw.models.roles.execution,
            *self.config.raw.models.roles.inspection,
        ]
        catalog_refs = [item.key for item in self.config.raw.models.catalog]
        return [
            *catalog_refs,
            *role_refs,
        ]

    def _model_key_in_use(self, target_model_key: str) -> bool:
        target = str(target_model_key or '').strip()
        if not target:
            return False
        active_project_statuses = {'queued', 'planning', 'running', 'checking', 'blocked'}
        active_unit_statuses = {'pending', 'planning', 'ready', 'running', 'checking', 'blocked'}
        for project in self.list_projects():
            if project.status not in active_project_statuses:
                continue
            for unit in self.list_units(project.project_id):
                if unit.status in active_unit_statuses and unit.model_binding == 'fixed_key' and str(unit.model_key or '').strip() == target:
                    return True
                checkpoint = self.checkpoint_store.get(unit.unit_id)
                if not isinstance(checkpoint, dict):
                    continue
                for stage_state in checkpoint.get('stages', []):
                    if str(stage_state.get('status') or '') == 'completed':
                        continue
                    for work_state in stage_state.get('work_items', []):
                        legacy_model_key = str(work_state.get('provider_model') or '').strip() or None
                        model_key = str(work_state.get('model_key') or legacy_model_key or '').strip()
                        model_binding = str(work_state.get('model_binding') or ('fixed_key' if model_key else 'live_role')).strip()
                        if model_binding != 'fixed_key' or model_key != target:
                            continue
                        if str(work_state.get('status') or '') not in {'completed', 'canceled'}:
                            return True
        return False

    def _provider_model_is_ready(self, provider_model: str) -> bool:
        candidate = str(provider_model or '').strip()
        if not candidate:
            return False
        try:
            from g3ku.org_graph.llm.provider_factory import build_provider_from_model_key

            build_provider_from_model_key(self.config.raw, candidate)
        except Exception:
            return False
        return True

    def _first_ready_provider_model(self) -> str:
        for candidate in self._provider_model_candidates():
            provider_model = str(candidate or '').strip()
            if provider_model and self._provider_model_is_ready(provider_model):
                return provider_model
        return ''

    def _effective_provider_model(self, provider_model: str, *, fallback: str = '') -> str:
        candidate = str(provider_model or '').strip()
        if candidate and self._provider_model_is_ready(candidate):
            return candidate
        return str(fallback or '').strip()

    def resolve_project_model_chain(self, *, project: ProjectRecord | None, node_type: str) -> list[str]:
        _ = project
        normalized_type = 'inspection' if node_type in {'inspection', 'checker'} else 'execution' if node_type in {'execution'} else 'ceo'
        chain = self.resolve_role_model_chain(normalized_type)
        if chain:
            return chain
        fallback = self.resolve_project_provider_model(project=project, node_type=node_type)
        return [fallback] if fallback else []

    def resolve_project_provider_model(self, *, project: ProjectRecord | None, node_type: str) -> str:
        _ = project
        normalized_type = 'inspection' if node_type in {'inspection', 'checker'} else 'execution' if node_type in {'execution'} else 'ceo'
        return self.resolve_role_model_key(normalized_type)

    def list_effective_tool_names(
        self,
        *,
        session_id: str,
        actor_role: str,
        project_id: str | None = None,
        unit_id: str | None = None,
        mutation_allowed: bool = True,
    ) -> list[str]:
        subject = self.build_policy_subject(
            session_id=session_id,
            actor_role=actor_role,
            project_id=project_id,
            unit_id=unit_id,
        )
        return list_effective_tool_names(
            subject=subject,
            supported_tool_names=self.list_runtime_tool_names(),
            resource_registry=self.resource_registry,
            policy_engine=self.policy_engine,
            mutation_allowed=mutation_allowed,
        )

    def list_effective_skill_ids(
        self,
        *,
        session_id: str,
        actor_role: str,
        project_id: str | None = None,
        unit_id: str | None = None,
    ) -> list[str]:
        subject = self.build_policy_subject(
            session_id=session_id,
            actor_role=actor_role,
            project_id=project_id,
            unit_id=unit_id,
        )
        return list_effective_skill_ids(
            subject=subject,
            available_skill_ids=self.list_available_skill_ids(),
            policy_engine=self.policy_engine,
        )

    async def startup(self) -> None:
        async with self._startup_lock:
            if self._started:
                return
            self.ensure_runtime_config_current(force=False, reason="startup")
            if self.config.governance_enabled:
                self.resource_registry.refresh()
                self.seed_ceo_governance_defaults()
                self.policy_engine.sync_default_role_policies()
            await self._recover_active_projects()
            self.monitor_service.startup_backfill()
            self._started = True

    def seed_ceo_governance_defaults(self) -> None:
        marker = self.governance_store.get_meta('ceo_role_seed_v1')
        if marker == 'done':
            return
        stamp = self.now()
        for record in self.governance_store.list_skill_resources():
            if 'ceo' in set(record.allowed_roles or []):
                continue
            updated = record.model_copy(update={'allowed_roles': to_public_allowed_roles([*list(record.allowed_roles or []), 'ceo'])})
            self.governance_store.upsert_skill_resource(updated, updated_at=stamp)
        for family in self.governance_store.list_tool_families():
            changed = False
            actions = []
            for action in family.actions:
                roles = to_public_allowed_roles([*list(action.allowed_roles or []), 'ceo'])
                changed = changed or roles != list(action.allowed_roles or [])
                actions.append(action.model_copy(update={'allowed_roles': roles}))
            if changed:
                self.governance_store.upsert_tool_family(family.model_copy(update={'actions': actions}), updated_at=stamp)
        self.governance_store.set_meta('ceo_role_seed_v1', 'done')

    async def _recover_active_projects(self) -> None:
        for project in self.list_projects():
            if project.status in {'completed', 'failed', 'canceled', 'archived'}:
                continue
            root_unit = self.get_unit(project.root_unit_id)
            if root_unit is None:
                root_unit = UnitAgentRecord(
                    unit_id=project.root_unit_id,
                    project_id=project.project_id,
                    parent_unit_id=None,
                    root_unit_id=project.root_unit_id,
                    level=0,
                    role_kind='execution',
                    role_title=ROOT_EXECUTION_ROLE_TITLE,
                    objective_summary=project.user_request,
                    prompt_preview='围绕用户目标规划阶段并协调执行',
                    status='pending',
                    current_stage_id=None,
                    current_action='Recovered and waiting for scheduler',
                    result_summary='',
                    error_summary='',
                    can_delegate=can_delegate(0, project.effective_max_depth),
                    child_count=0,
                    created_at=self.now(),
                    updated_at=self.now(),
                    started_at=None,
                    finished_at=None,
                    model_key=None,
                    model_binding='live_role',
                    mutation_allowed=False,
                    metadata={},
                )
                self.store.upsert_unit(root_unit)
            if project.status == 'blocked':
                await self.registry.pause(project.project_id)
                recovered = project.model_copy(update={'updated_at': self.now(), 'summary': 'Project recovered in paused state'})
                self.store.upsert_project(recovered)
                await self.emit_event(project=recovered, scope='project', event_name='project.recovered', text='Project recovered in paused state', level='warn')
                notice = self.notice_service.create(session_id=recovered.session_id, project_id=recovered.project_id, kind='project_blocked', title='Project recovered paused', text=f'Project {recovered.title} was restored in paused state after restart.')
                await self.publish_notice(recovered.session_id, notice)
                continue
            recovered = project.model_copy(
                update={
                    'status': 'running' if project.status not in {'queued', 'planning', 'running', 'checking'} else project.status,
                    'updated_at': self.now(),
                    'finished_at': None,
                    'summary': 'Project recovered after service restart and will resume from checkpoint',
                    'error_summary': '',
                }
            )
            self.store.upsert_project(recovered)
            await self.emit_event(project=recovered, scope='project', event_name='project.recovered', text='Project recovered after service restart and resumed from checkpoint', level='warn')
            notice = self.notice_service.create(session_id=recovered.session_id, project_id=recovered.project_id, kind='project_running', title='Project recovered', text=f'Project {recovered.title} was recovered after service restart and will resume from its last checkpoint.')
            await self.publish_notice(recovered.session_id, notice)
            await self.publish_summary(recovered)
            task = await self.registry.task_for(recovered.project_id)
            if task is None or task.done():
                task = asyncio.create_task(self.project_runner.run(recovered.project_id))
                await self.registry.register_task(recovered.project_id, task)


    def now(self) -> str:
        return now_iso()

    async def create_project(self, request: ProjectCreateRequest) -> ProjectRecord:
        self.ensure_runtime_config_current(force=False, reason="create_project")
        session_id = str(request.session_id or 'web:shared').strip() or 'web:shared'
        active = [project for project in self.list_projects(session_id) if project.status not in {'completed', 'failed', 'canceled', 'archived'}]
        if len(active) >= self.config.max_active_projects_per_session:
            raise ValueError('Too many active projects for this session')
        effective_depth = clamp_max_depth(request.max_depth, default_depth=self.config.default_max_depth, hard_max_depth=self.config.hard_max_depth)
        project_id = new_project_id()
        root_unit_id = new_unit_id('execution')
        title = (request.preferred_title or request.prompt[:60]).strip() or project_id
        project = ProjectRecord(
            project_id=project_id,
            session_id=session_id,
            title=title,
            user_request=request.prompt,
            status='queued',
            root_unit_id=root_unit_id,
            max_depth=request.max_depth or effective_depth,
            effective_max_depth=effective_depth,
            created_at=self.now(),
            updated_at=self.now(),
            started_at=None,
            finished_at=None,
            summary='Project queued',
            final_result='',
            error_summary='',
            active_unit_count=0,
            completed_unit_count=0,
            failed_unit_count=0,
            metadata=dict(request.metadata or {}),
        )
        root_unit = UnitAgentRecord(
            unit_id=root_unit_id,
            project_id=project_id,
            parent_unit_id=None,
            root_unit_id=root_unit_id,
            level=0,
            role_kind='execution',
            role_title=ROOT_EXECUTION_ROLE_TITLE,
            objective_summary=request.prompt,
            prompt_preview='围绕用户目标规划阶段并协调执行',
            status='pending',
            current_stage_id=None,
            current_action='Waiting for scheduler',
            result_summary='',
            error_summary='',
            can_delegate=can_delegate(0, effective_depth),
            child_count=0,
            created_at=self.now(),
            updated_at=self.now(),
            started_at=None,
            finished_at=None,
            model_key=None,
            model_binding='live_role',
            mutation_allowed=False,
            metadata={'request_output_target': request.output_target},
        )
        self.store.upsert_project(project)
        self.store.upsert_unit(root_unit)
        self.monitor_service.ensure_project(project)
        self.monitor_service.ensure_node(project=project, unit=root_unit)
        await self.emit_event(project=project, scope='project', event_name='project.created', text='Project created')
        notice = self.notice_service.create(session_id=session_id, project_id=project.project_id, kind='project_created', title='Project created', text=f'Project {project.title} has been created.')
        await self.publish_notice(session_id, notice)
        await self.publish_summary(project)
        task = asyncio.create_task(self.project_runner.run(project.project_id))
        await self.registry.register_task(project.project_id, task)
        return project

    def create_child_unit(
        self,
        *,
        project: ProjectRecord,
        parent: UnitAgentRecord,
        role_title: str,
        objective: str,
        prompt_preview: str,
        model_key: str | None = None,
        mutation_allowed: bool = False,
    ) -> UnitAgentRecord:
        explicit_model_key = str(model_key or "").strip() or None
        unit = UnitAgentRecord(
            unit_id=new_unit_id('execution'),
            project_id=project.project_id,
            parent_unit_id=parent.unit_id,
            root_unit_id=parent.root_unit_id,
            level=parent.level + 1,
            role_kind='execution',
            role_title=str(role_title or '').strip() or '执行单元',
            objective_summary=objective,
            prompt_preview=prompt_preview,
            status='pending',
            current_stage_id=None,
            current_action='Queued by parent unit',
            result_summary='',
            error_summary='',
            can_delegate=can_delegate(parent.level + 1, project.effective_max_depth),
            child_count=0,
            created_at=self.now(),
            updated_at=self.now(),
            started_at=None,
            finished_at=None,
            model_key=explicit_model_key,
            model_binding='fixed_key' if explicit_model_key else 'live_role',
            mutation_allowed=bool(mutation_allowed),
            metadata={},
        )
        self.monitor_service.ensure_node(project=project, unit=unit)
        return unit

    async def emit_event(self, *, project: ProjectRecord, scope: str, event_name: str, text: str, unit_id: str | None = None, stage_id: str | None = None, level: str = 'info', data: dict[str, Any] | None = None):
        unit = self.get_unit(unit_id) if unit_id else None
        if unit is not None:
            self.monitor_service.ensure_node(project=project, unit=unit)
            self.monitor_service.append_log(
                project_id=project.project_id,
                node_id=unit.node_id if hasattr(unit, 'node_id') else unit.unit_id,
                kind='lifecycle',
                content=f'{event_name}: {text}',
                stage_id=stage_id,
                meta={'scope': scope, 'level': level, 'data': dict(data or {})},
            )
        if event_name == 'notice.raised' or (level == 'error' and 'engineering' in str((data or {}).get('failure_kind') or '').lower()):
            self.monitor_service.record_engineering_exception(project=project, text=text, node_id=unit_id)
        elif level == 'warn' and str((data or {}).get('failure_kind') or '').lower() == 'model_chain_unavailable':
            self.monitor_service.record_engineering_exception(project=project, text=text, node_id=unit_id, wait_reason='model_chain_unavailable')
        else:
            self.monitor_service.record_progress(project=project, text=text, node_id=unit_id)
        if event_name in {'project.running', 'project.completed', 'project.canceled', 'project.archived'}:
            self.monitor_service.clear_engineering_exception(project.project_id)
        await self.publish_tree_snapshot(project.project_id)
        return {'seq': self._project_seq(project.project_id)}

    async def emit_unit_event(
        self,
        *,
        project: ProjectRecord,
        unit: UnitAgentRecord,
        event_name: str,
        text: str,
        stage_id: str | None = None,
        level: str = 'info',
        extra_data: dict[str, Any] | None = None,
    ):
        self.monitor_service.ensure_node(project=project, unit=unit)
        payload = {'unit': unit.model_dump(mode='json')}
        if extra_data:
            payload.update(extra_data)
        await self.emit_event(
            project=project,
            scope='unit',
            event_name=event_name,
            text=text,
            unit_id=unit.unit_id,
            stage_id=stage_id,
            level=level,
            data=payload,
        )
        self.monitor_service.recompute_project(project.project_id)
        return {'ok': True}

    async def publish_notice(self, session_id: str, notice) -> None:
        await self.registry.publish_ceo(
            session_id,
            build_envelope(channel='ceo', session_id=session_id, project_id=notice.project_id, type='project.notice', data=notice.model_dump(mode='json')),
        )

    async def publish_summary(self, project: ProjectRecord) -> None:
        summary_payload = project.model_dump(mode='json')
        await self.registry.publish_ceo(
            project.session_id,
            build_envelope(channel='ceo', session_id=project.session_id, project_id=project.project_id, type='project.summary.changed', data=summary_payload),
        )
        await self.registry.publish_project(
            project.session_id,
            project.project_id,
            build_envelope(channel='project', session_id=project.session_id, project_id=project.project_id, seq=self._project_seq(project.project_id), type='snapshot.project', data=summary_payload),
        )

    async def publish_tree_snapshot(self, project_id: str) -> None:
        project = self.get_project(project_id)
        if project is None:
            return
        tree = self.get_tree(project_id)
        await self.registry.publish_project(
            project.session_id,
            project.project_id,
            build_envelope(
                channel='project',
                session_id=project.session_id,
                project_id=project.project_id,
                seq=self._project_seq(project.project_id),
                type='snapshot.tree',
                data=(tree.model_dump(mode='json') if tree is not None else {}),
            ),
        )

    async def publish_artifact(self, project: ProjectRecord, artifact) -> None:
        _ = project, artifact
        return None

    def _project_seq(self, project_id: str) -> int:
        record = self.task_monitor_store.get_project(project_id)
        if record is None:
            return 0
        return max(int(record.latest_progress_rev or 0), int(record.latest_engineering_rev or 0))

    def refresh_project_governance_summary(self, project_id: str) -> ProjectRecord | None:
        return self.get_project(project_id)

    @staticmethod
    def _legacy_project_status(status: str) -> str:
        return {
            'queued': 'pending',
            'planning': 'injecting',
            'running': 'running',
            'checking': 'running',
            'blocked': 'paused',
            'completed': 'completed',
            'failed': 'failed',
            'canceled': 'canceled',
            'archived': 'destroyed',
        }.get(str(status or '').lower(), str(status or 'pending'))

    @staticmethod
    def _legacy_unit_status(status: str) -> str:
        return {
            'pending': 'pending',
            'planning': 'injecting',
            'ready': 'pending',
            'running': 'active',
            'checking': 'active',
            'blocked': 'paused',
            'completed': 'completed',
            'failed': 'failed',
            'canceled': 'canceled',
        }.get(str(status or '').lower(), str(status or 'pending'))

    def list_legacy_tasks(self, session_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for project in self.list_projects(session_id):
            root_unit = self.get_unit(project.root_unit_id)
            items.append(
                {
                    'task_id': project.project_id,
                    'session_id': root_unit.unit_id if root_unit is not None else project.root_unit_id,
                    'parent_session_id': project.session_id,
                    'category': (root_unit.role_kind if root_unit is not None else 'org_graph_project'),
                    'status': self._legacy_project_status(project.status),
                    'created_at': project.created_at,
                    'started_at': project.started_at,
                    'updated_at': project.updated_at,
                    'result_summary': project.final_result or project.summary,
                    'error': project.error_summary or None,
                    'metadata': {
                        'legacy_source': 'org_graph',
                        'project_status': project.status,
                        'root_unit_id': project.root_unit_id,
                        'archived': project.status == 'archived',
                    },
                }
            )
        items.sort(key=lambda item: str(item.get('updated_at') or ''), reverse=True)
        return items

    def get_legacy_task(self, task_id: str) -> dict[str, Any] | None:
        project = self.get_project(task_id)
        if project is None:
            return None
        return next((item for item in self.list_legacy_tasks(project.session_id) if item['task_id'] == task_id), None)

    def list_legacy_subagents(self, session_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for project in self.list_projects(session_id):
            for unit in self.list_units(project.project_id):
                items.append(
                    {
                        'session_id': unit.unit_id,
                        'parent_session_id': unit.parent_unit_id or project.session_id,
                        'task_id': project.project_id,
                        'category': unit.role_kind,
                        'status': self._legacy_unit_status(unit.status),
                        'run_mode': 'background',
                        'created_at': unit.created_at,
                        'updated_at': unit.updated_at,
                        'last_result_summary': unit.result_summary,
                        'role_label': unit.role_title,
                        'delegated_prompt_preview': unit.objective_summary,
                        'system_prompt_preview': unit.prompt_preview,
                        'current_action': unit.current_action,
                        'result_summary': unit.result_summary,
                        'metadata': {
                            'legacy_source': 'org_graph',
                            'project_id': project.project_id,
                            'role_kind': unit.role_kind,
                            'unit_status': unit.status,
                            'level': unit.level,
                        },
                    }
                )
        items.sort(key=lambda item: str(item.get('updated_at') or ''), reverse=True)
        return items

    async def cancel_session_projects(self, session_id: str) -> int:
        count = 0
        for project in self.list_projects(session_id):
            if project.status in {'completed', 'failed', 'canceled', 'archived'}:
                continue
            result = await self.cancel_project(project.project_id)
            if result is not None:
                count += 1
        return count

    def list_projects(self, session_id: str | None = None) -> list[ProjectRecord]:
        return self.store.list_projects(session_id)

    def get_project(self, project_id: str) -> ProjectRecord | None:
        return self.store.get_project(project_id)

    def list_units(self, project_id: str):
        return self.store.list_units(project_id)

    def get_unit(self, unit_id: str):
        return self.store.get_unit(unit_id)

    def list_stages(self, project_id: str):
        return self.store.list_stages(project_id)

    def get_stage(self, stage_id: str):
        return self.store.get_stage(stage_id)

    def list_skill_resources(self):
        return self.resource_registry.list_skill_resources()

    def list_visible_skill_resources(self, *, actor_role: str, session_id: str) -> list:
        subject = self.build_policy_subject(session_id=session_id, actor_role=actor_role)
        visible = []
        for item in self.resource_registry.list_skill_resources():
            decision = self.policy_engine.evaluate_skill_access(subject=subject, skill_id=item.skill_id)
            if decision.allowed:
                visible.append(item)
        return visible

    def get_skill_resource(self, skill_id: str):
        return self.resource_registry.get_skill_resource(skill_id)

    def list_tool_resources(self):
        return self.resource_registry.list_tool_families()

    def list_visible_tool_families(self, *, actor_role: str, session_id: str) -> list:
        subject = self.build_policy_subject(session_id=session_id, actor_role=actor_role)
        visible = []
        for family in self.resource_registry.list_tool_families():
            actions = []
            for action in family.actions:
                decision = self.policy_engine.evaluate_tool_action(subject=subject, tool_id=family.tool_id, action_id=action.action_id)
                if decision.allowed:
                    actions.append(action)
            if actions:
                visible.append(family.model_copy(update={'actions': actions}))
        return visible

    def get_tool_resource(self, tool_id: str):
        return self.resource_registry.get_tool_family(tool_id)

    def list_events(self, project_id: str, after_seq: int = 0, limit: int = 200):
        _ = project_id, after_seq, limit
        return []

    def list_artifacts(self, project_id: str):
        return self.store.list_artifacts(project_id)

    def get_artifact(self, artifact_id: str):
        return self.store.get_artifact(artifact_id)

    async def apply_patch_artifact(self, project_id: str, artifact_id: str):
        project = self.get_project(project_id)
        artifact = self.get_artifact(artifact_id)
        if project is None or artifact is None or artifact.project_id != project_id:
            return None
        if artifact.kind != 'patch' or not artifact.path:
            raise ValueError('artifact is not a patch artifact')
        patch_path = Path(artifact.path)
        content = patch_path.read_text(encoding='utf-8')
        metadata, _diff_text = parse_patch_artifact(content)
        target_path = Path(str(metadata.get('path') or ''))
        old_text = __import__('base64').b64decode(str(metadata.get('old_text_b64') or '')).decode('utf-8')
        new_text = __import__('base64').b64decode(str(metadata.get('new_text_b64') or '')).decode('utf-8')
        if not target_path.exists():
            raise ValueError(f'target file not found: {target_path}')
        current = target_path.read_text(encoding='utf-8')
        if old_text not in current:
            raise ValueError('target file no longer matches patch precondition')
        if current.count(old_text) > 1:
            raise ValueError('target file has multiple matches for patch precondition')
        updated = current.replace(old_text, new_text, 1)
        target_path.write_text(updated, encoding='utf-8')
        await self.emit_event(project=project, scope='project', event_name='artifact.applied', text=f'Applied patch artifact: {artifact.title}', data={'artifact_id': artifact.artifact_id, 'path': str(target_path)})
        return {'artifact_id': artifact.artifact_id, 'path': str(target_path), 'applied': True}

    def list_notices(self, session_id: str, *, include_acknowledged: bool = False):
        return self.notice_service.list(session_id, include_acknowledged=include_acknowledged)

    def ack_notice(self, notice_id: str):
        return self.notice_service.ack(notice_id)

    def get_tree(self, project_id: str):
        return self.monitor_service.get_tree(project_id)

    def delete_execution_subtree(self, *, project_id: str, root_unit_id: str) -> list[str]:
        project = self.get_project(project_id)
        if project is None:
            return []
        units = self.list_units(project_id)
        if not units:
            return []
        by_parent: dict[str, list[str]] = {}
        by_id = {unit.unit_id: unit for unit in units}
        for unit in units:
            parent_id = str(unit.parent_unit_id or '').strip()
            if not parent_id:
                continue
            by_parent.setdefault(parent_id, []).append(unit.unit_id)
        root_id = str(root_unit_id or '').strip()
        if not root_id or root_id not in by_id:
            return []
        deleted: list[str] = []
        stack = [root_id]
        while stack:
            current = stack.pop()
            if current in deleted:
                continue
            deleted.append(current)
            stack.extend(reversed(by_parent.get(current, [])))
        self.checkpoint_store.delete_many(deleted)
        self.store.delete_stages_for_units(deleted)
        self.artifact_store.delete_artifacts_for_units(project_id, deleted)
        self.store.delete_units(deleted)
        self.monitor_service.delete_nodes(project_id=project_id, node_ids=deleted)
        self.store.recount_project_units(project_id)
        parent_id = by_id[root_id].parent_unit_id
        if parent_id:
            parent = self.get_unit(parent_id)
            if parent is not None:
                remaining = [unit for unit in self.list_units(project_id) if unit.parent_unit_id == parent.unit_id]
                self.store.upsert_unit(parent.model_copy(update={'child_count': len(remaining), 'updated_at': self.now()}))
        return deleted

    def list_skill_files(self, skill_id: str) -> dict[str, str]:
        return {key: str(path) for key, path in self.resource_registry.skill_file_map(skill_id).items()}

    def read_skill_file(self, skill_id: str, file_key: str) -> str:
        path = self.resource_registry.skill_file_map(skill_id).get(file_key)
        if path is None:
            raise ValueError('editable_file_not_allowed')
        return path.read_text(encoding='utf-8')

    async def write_skill_file(self, skill_id: str, file_key: str, content: str, *, session_id: str = 'web:shared'):
        path = self.resource_registry.skill_file_map(skill_id).get(file_key)
        if path is None:
            raise ValueError('editable_file_not_allowed')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        if self.config.auto_reload_on_write:
            await self.reload_resources(session_id=session_id, decided_by='manual_policy')
        return {'skill_id': skill_id, 'file_key': file_key, 'path': str(path)}

    async def update_skill_policy(self, skill_id: str, *, session_id: str = 'web:shared', enabled: bool | None = None, allowed_roles: list[str] | None = None):
        skill = self.get_skill_resource(skill_id)
        if skill is None:
            return None
        updated = skill.model_copy(update={
            'enabled': skill.enabled if enabled is None else bool(enabled),
            'allowed_roles': to_public_allowed_roles(list(skill.allowed_roles if allowed_roles is None else allowed_roles)),
        })
        self.governance_store.upsert_skill_resource(updated, updated_at=self.now())
        self.policy_engine.sync_default_role_policies()
        return updated

    async def enable_skill(self, skill_id: str, *, session_id: str = 'web:shared'):
        return await self.update_skill_policy(skill_id, session_id=session_id, enabled=True)

    async def disable_skill(self, skill_id: str, *, session_id: str = 'web:shared'):
        return await self.update_skill_policy(skill_id, session_id=session_id, enabled=False)

    async def update_tool_policy(self, tool_id: str, *, session_id: str = 'web:shared', enabled: bool | None = None, actions: list[dict[str, Any]] | None = None):
        tool = self.get_tool_resource(tool_id)
        if tool is None:
            return None
        action_map = {action.action_id: action for action in tool.actions}
        for item in actions or []:
            action_id = str(item.get('action_id') or '').strip()
            if not action_id or action_id not in action_map:
                continue
            action = action_map[action_id]
            action_map[action_id] = action.model_copy(
                update={'allowed_roles': to_public_allowed_roles(list(item.get('allowed_roles') or action.allowed_roles))}
            )
        updated = tool.model_copy(update={'enabled': tool.enabled if enabled is None else bool(enabled), 'actions': list(action_map.values())})
        self.governance_store.upsert_tool_family(updated, updated_at=self.now())
        self.policy_engine.sync_default_role_policies()
        return updated

    async def enable_tool(self, tool_id: str, *, session_id: str = 'web:shared'):
        return await self.update_tool_policy(tool_id, session_id=session_id, enabled=True)

    async def disable_tool(self, tool_id: str, *, session_id: str = 'web:shared'):
        return await self.update_tool_policy(tool_id, session_id=session_id, enabled=False)

    async def reload_resources(self, *, session_id: str = 'web:shared', decided_by: str = 'manual_policy'):
        skills, tools = self.resource_registry.refresh()
        self.seed_ceo_governance_defaults()
        self.policy_engine.sync_default_role_policies()
        return {'skills': len(skills), 'tools': len(tools)}

    async def pause_project(self, project_id: str):
        project = self.get_project(project_id)
        if project is None:
            return None
        await self.registry.pause(project_id)
        updated = project.model_copy(update={'status': 'blocked', 'updated_at': self.now(), 'summary': 'Project paused'})
        self.store.upsert_project(updated)
        await self.emit_event(project=updated, scope='project', event_name='project.blocked', text='Project paused', level='warn')
        notice = self.notice_service.create(session_id=updated.session_id, project_id=updated.project_id, kind='project_blocked', title='Project paused', text=f'Project {updated.title} has been paused.')
        await self.publish_notice(updated.session_id, notice)
        await self.publish_summary(updated)
        return updated

    async def resume_project(self, project_id: str):
        project = self.get_project(project_id)
        if project is None:
            return None
        await self.registry.resume(project_id)
        updated = project.model_copy(update={'status': 'running', 'updated_at': self.now(), 'summary': 'Project resumed'})
        self.store.upsert_project(updated)
        task = await self.registry.task_for(project_id)
        if task is None or task.done():
            task = asyncio.create_task(self.project_runner.run(project_id))
            await self.registry.register_task(project_id, task)
        await self.emit_event(project=updated, scope='project', event_name='project.running', text='Project resumed')
        await self.publish_summary(updated)
        return updated

    async def cancel_project(self, project_id: str):
        project = self.get_project(project_id)
        if project is None:
            return None
        await self.registry.cancel(project_id)
        updated = project.model_copy(update={'status': 'canceled', 'updated_at': self.now(), 'finished_at': self.now(), 'summary': 'Project canceled'})
        self.store.upsert_project(updated)
        await self.emit_event(project=updated, scope='project', event_name='project.canceled', text='Project canceled by user', level='warn')
        notice = self.notice_service.create(session_id=updated.session_id, project_id=updated.project_id, kind='project_canceled', title='Project canceled', text=f'Project {updated.title} has been canceled.')
        await self.publish_notice(updated.session_id, notice)
        await self.publish_summary(updated)
        return updated

    async def archive_project(self, project_id: str):
        project = self.get_project(project_id)
        if project is None:
            return None
        updated = project.model_copy(update={'status': 'archived', 'updated_at': self.now(), 'summary': 'Project archived'})
        self.store.upsert_project(updated)
        await self.emit_event(project=updated, scope='project', event_name='project.archived', text='Project archived')
        await self.publish_summary(updated)
        return updated

    async def delete_project(self, project_id: str):
        project = self.get_project(project_id)
        if project is None:
            return None

        unit_ids = [unit.unit_id for unit in self.list_units(project_id)]
        artifacts = self.list_artifacts(project_id)
        task = await self.registry.task_for(project_id)
        if task is not None and not task.done():
            await self.registry.cancel(project_id)
            await asyncio.gather(task, return_exceptions=True)
            await self.registry.clear_task(project_id)

        self.checkpoint_store.delete_many(unit_ids)
        self.task_monitor_store.delete_project(project_id)
        self.artifact_store.delete_project_artifacts(project_id, artifacts)
        self.store.delete_project(project_id)
        await self.registry.purge_project(
            project_id,
            payload=build_envelope(
                channel='project',
                session_id=project.session_id,
                project_id=project_id,
                type='project.deleted',
                data={'project_id': project_id},
            ),
        )
        return {'project_id': project_id}

    async def close(self) -> None:
        if self._closed:
            return
        self._started = False
        self._closed = True
        await self.registry.close()
        if self.memory_manager is not None:
            try:
                self.memory_manager.close()
            except Exception:
                pass
        self.task_monitor_store.close()
        self.checkpoint_store.close()
        self.governance_store.close()
        self.store.close()












