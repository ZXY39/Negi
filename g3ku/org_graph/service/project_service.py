from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from g3ku.org_graph.config import ResolvedOrgGraphConfig
from g3ku.org_graph.errors import PermissionDeniedError
from g3ku.org_graph.governance.approval_service import GovernanceApprovalService
from g3ku.org_graph.governance.capability_filter import list_effective_skill_ids, list_effective_tool_names
from g3ku.org_graph.governance.policy_engine import GovernancePolicyEngine
from g3ku.org_graph.governance.resource_registry import OrgGraphResourceRegistry
from g3ku.org_graph.governance.store import GovernanceStore
from g3ku.org_graph.ids import new_project_id, new_unit_id
from g3ku.org_graph.execution.propose_patch_tool import parse_patch_artifact
from g3ku.org_graph.llm.client import OrgGraphLLM
from g3ku.org_graph.models import ProjectCreateRequest, ProjectRecord, UnitAgentRecord
from g3ku.org_graph.planning.depth_policy import can_delegate, clamp_max_depth
from g3ku.org_graph.protocol import build_envelope, now_iso
from g3ku.org_graph.prompt_loader import load_prompt_preview
from g3ku.org_graph.service.notice_service import NoticeService
from g3ku.org_graph.service.project_registry import ProjectRegistry
from g3ku.agent.rag_memory import MemoryManager
from g3ku.org_graph.public_roles import to_public_actor_role, to_public_allowed_roles, to_public_model_defaults
from g3ku.org_graph.storage.artifact_store import ArtifactStore
from g3ku.org_graph.storage.checkpoint_store import CheckpointStore
from g3ku.org_graph.storage.event_store import EventStore
from g3ku.org_graph.storage.project_store import ProjectStore
from g3ku.org_graph.tracing.emitter import TraceEmitter
from g3ku.org_graph.tracing.tree_builder import TreeBuilder


class ProjectService:
    PROJECT_NODE_MODEL_TYPES = ('ceo', 'execution', 'inspection')

    def __init__(self, config: ResolvedOrgGraphConfig):
        self.config = config
        self.store = ProjectStore(config.project_store_path)
        self.event_store = EventStore(config.event_store_path)
        self.checkpoint_store = CheckpointStore(config.checkpoint_store_path)
        self.governance_store = GovernanceStore(config.governance_store_path)
        self.registry = ProjectRegistry()
        self.notice_service = NoticeService(self.store)
        self.llm = OrgGraphLLM.from_config(config.raw, default_model=config.execution_model)
        self.memory_manager = self._build_memory_manager()
        self.artifact_store = ArtifactStore(artifact_dir=config.artifact_dir, project_store=self.store)
        self.tree_builder = TreeBuilder()
        self.emitter = TraceEmitter(event_store=self.event_store, registry=self.registry)
        self.resource_registry = OrgGraphResourceRegistry(config, self.governance_store)
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
        from g3ku.org_graph.execution.ceo_runner import CEORunner
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
        self.ceo_runner = CEORunner(self)
        self._startup_lock = asyncio.Lock()
        self._started = False

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

    def default_node_provider_models(self) -> dict[str, str]:
        fallback = self._first_ready_provider_model()
        return to_public_model_defaults(
            {
                'ceo': self._effective_provider_model(str(self.config.ceo_model or '').strip(), fallback=fallback),
                'execution': self._effective_provider_model(str(self.config.execution_model or '').strip(), fallback=fallback),
                'inspection': self._effective_provider_model(str(self.config.inspection_model or '').strip(), fallback=fallback),
            }
        )

    def _provider_model_candidates(self) -> list[str]:
        return [
            self.config.raw.agents.defaults.model,
            self.config.raw.agents.multi_agent.orchestrator_model,
            self.config.raw.org_graph.ceo_model,
            self.config.raw.org_graph.execution_model,
            self.config.raw.org_graph.inspection_model,
        ]

    def _provider_model_is_ready(self, provider_model: str) -> bool:
        candidate = str(provider_model or '').strip()
        if not candidate:
            return False
        try:
            from g3ku.org_graph.llm.provider_factory import build_provider_from_model

            build_provider_from_model(self.config.raw, candidate)
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

    def list_available_provider_models(self) -> list[str]:
        seen: set[str] = set()
        models: list[str] = []
        for candidate in self._provider_model_candidates():
            provider_model = str(candidate or '').strip()
            if not provider_model or provider_model in seen:
                continue
            try:
                self.config.raw.parse_provider_model(provider_model)
            except Exception:
                continue
            if not self._provider_model_is_ready(provider_model):
                continue
            seen.add(provider_model)
            models.append(provider_model)
        return models

    def validate_default_provider_models(self, payload: dict[str, Any] | None) -> dict[str, str]:
        payload = payload if isinstance(payload, dict) else {}
        allowed = set(self.list_available_provider_models())
        normalized: dict[str, str] = {}
        for node_type in self.PROJECT_NODE_MODEL_TYPES:
            provider_model = str(payload.get(node_type) or '').strip()
            if not provider_model:
                continue
            try:
                self.config.raw.parse_provider_model(provider_model)
            except Exception as exc:
                raise ValueError(f'Invalid provider model for {node_type}: {provider_model}') from exc
            if allowed and provider_model not in allowed:
                raise ValueError(f'Provider model for {node_type} must come from the system model list')
            normalized[node_type] = provider_model
        return normalized

    def resolve_project_provider_model(self, *, project: ProjectRecord | None, node_type: str) -> str:
        normalized_type = 'inspection' if node_type in {'inspection', 'checker'} else 'execution' if node_type in {'execution', 'execution', 'execution'} else 'ceo'
        raw_defaults = {
            'ceo': str(self.config.ceo_model or '').strip(),
            'execution': str(self.config.execution_model or '').strip(),
            'inspection': str(self.config.inspection_model or '').strip(),
        }
        defaults = self.default_node_provider_models()
        resolved = str(defaults.get(normalized_type) or '').strip()
        if resolved:
            return resolved
        return raw_defaults[normalized_type]

    async def update_default_provider_models(self, payload: dict[str, Any]) -> dict[str, str]:
        normalized = self.validate_default_provider_models(payload)
        org_graph = self.config.raw.org_graph.model_copy(
            update={
                'ceo_model': normalized.get('ceo') or self.config.raw.org_graph.ceo_model,
                'execution_model': normalized.get('execution') or self.config.raw.org_graph.execution_model,
                'inspection_model': normalized.get('inspection') or self.config.raw.org_graph.inspection_model,
            }
        )
        self.config.raw = self.config.raw.model_copy(update={'org_graph': org_graph})
        from g3ku.config.loader import save_config

        save_config(self.config.raw)
        self.config.ceo_model = org_graph.ceo_model or self.config.ceo_model
        self.config.execution_model = org_graph.execution_model or self.config.execution_model
        self.config.inspection_model = org_graph.inspection_model or self.config.inspection_model
        self.llm = OrgGraphLLM.from_config(self.config.raw, default_model=self.config.execution_model)
        return self.default_node_provider_models()

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
            if self.config.governance_enabled:
                self.resource_registry.refresh()
                self.policy_engine.sync_default_role_policies()
            await self._recover_active_projects()
            self._started = True

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
                    role_title='执行单元',
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
                    provider_model=self.resolve_project_provider_model(project=project, node_type='execution'),
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
            role_title='执行单元',
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
            provider_model=self.resolve_project_provider_model(project=project, node_type='execution'),
            mutation_allowed=False,
            metadata={'request_output_target': request.output_target},
        )
        self.store.upsert_project(project)
        self.store.upsert_unit(root_unit)
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
        provider_model: str | None = None,
        mutation_allowed: bool = False,
    ) -> UnitAgentRecord:
        return UnitAgentRecord(
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
            provider_model=str(provider_model or self.resolve_project_provider_model(project=project, node_type='execution') or parent.provider_model or self.config.execution_model),
            mutation_allowed=bool(mutation_allowed),
            metadata={},
        )

    async def emit_event(self, *, project: ProjectRecord, scope: str, event_name: str, text: str, unit_id: str | None = None, stage_id: str | None = None, level: str = 'info', data: dict[str, Any] | None = None):
        return await self.emitter.emit_event(session_id=project.session_id, project_id=project.project_id, scope=scope, event_name=event_name, text=text, unit_id=unit_id, stage_id=stage_id, level=level, data=data)

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
        payload = {'unit': unit.model_dump(mode='json')}
        if extra_data:
            payload.update(extra_data)
        return await self.emit_event(
            project=project,
            scope='unit',
            event_name=event_name,
            text=text,
            unit_id=unit.unit_id,
            stage_id=stage_id,
            level=level,
            data=payload,
        )

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
            build_envelope(channel='project', session_id=project.session_id, project_id=project.project_id, seq=self.event_store.latest_seq(project.project_id), type='project.summary.changed', data=summary_payload),
        )

    async def publish_artifact(self, project: ProjectRecord, artifact) -> None:
        await self.registry.publish_project(
            project.session_id,
            project.project_id,
            build_envelope(
                channel='project',
                session_id=project.session_id,
                project_id=project.project_id,
                seq=self.event_store.latest_seq(project.project_id),
                type='artifact.created',
                data=artifact.model_dump(mode='json'),
            ),
        )

    def refresh_project_governance_summary(self, project_id: str) -> ProjectRecord | None:
        return self.get_project(project_id)

    async def handle_ceo_message(self, session_id: str, text: str) -> str:
        return await self.ceo_runner.handle_message(session_id, text)

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

    def get_skill_resource(self, skill_id: str):
        return self.resource_registry.get_skill_resource(skill_id)

    def list_tool_resources(self):
        return self.resource_registry.list_tool_families()

    def get_tool_resource(self, tool_id: str):
        return self.resource_registry.get_tool_family(tool_id)

    def list_events(self, project_id: str, after_seq: int = 0, limit: int = 200):
        return self.event_store.list_after(project_id, after_seq=after_seq, limit=min(limit, self.config.event_replay_limit))

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
        project = self.get_project(project_id)
        if project is None:
            return None
        return self.tree_builder.build(units=self.list_units(project_id), root_unit_id=project.root_unit_id)

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
        await self.emitter.emit_terminal(session_id=updated.session_id, project_id=updated.project_id, payload=updated.model_dump(mode='json'))
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

    async def close(self) -> None:
        self._started = False
        await self.registry.close()
        if self.memory_manager is not None:
            try:
                self.memory_manager.close()
            except Exception:
                pass
        self.checkpoint_store.close()
        self.event_store.close()
        self.governance_store.close()
        self.store.close()












