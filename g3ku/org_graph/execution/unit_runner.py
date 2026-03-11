from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import json_repair

from g3ku.org_graph.errors import (
    EngineeringFailureError,
    OrdinaryTaskFailureError,
    PermissionBlockedError,
    PermissionDeniedError,
)
from g3ku.org_graph.ids import new_stage_id
from g3ku.org_graph.models import UnitAgentRecord, UnitStageRecord
from g3ku.org_graph.planning.decomposition import build_execution_plan
from g3ku.org_graph.planning.depth_policy import can_delegate
from g3ku.org_graph.protocol import now_iso
from g3ku.org_graph.prompt_loader import load_prompt

LOCAL_EXECUTION_SYSTEM_PROMPT = load_prompt('execution_unit.md')
REWORK_REWRITER_SYSTEM_PROMPT = load_prompt('rework_rewriter.md')
MAX_ORDINARY_RETRY_COUNT = 3
STAGE_SYNTHESIS_SYSTEM_PROMPT = 'You are the stage synthesis agent. Produce a concise stage summary from verified child results only.'


def _offline_fallback_enabled() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or os.getenv("G3KU_ORG_GRAPH_OFFLINE") == "1"


class UnitRunner:
    CHECKPOINT_VERSION = 3

    def __init__(self, service):
        self._service = service

    async def run(self, project, unit: UnitAgentRecord) -> str:
        latest_unit = self._service.get_unit(unit.unit_id)
        if latest_unit is not None:
            unit = latest_unit
        if bool((unit.metadata or {}).get('force_fail')):
            raise EngineeringFailureError('Unit metadata requested forced failure')
        checkpoint = self._reconcile_checkpoint(project, unit, self._load_checkpoint(unit.unit_id))
        try:
            if checkpoint.get('status') == 'completed':
                return str(checkpoint.get('final_summary') or unit.result_summary or '').strip() or f'{unit.role_title} completed.'
            if unit.status == 'completed' and not checkpoint:
                return unit.result_summary or f'{unit.role_title} completed.'
            await self._ensure_skill_access(project, unit, unit.current_stage_id)
            if not checkpoint:
                unit = unit.model_copy(
                    update={
                        'status': 'planning',
                        'current_action': 'Building stage plan',
                        'updated_at': now_iso(),
                        'started_at': unit.started_at or now_iso(),
                    }
                )
                self._service.store.upsert_unit(unit)
                await self._service.emit_unit_event(
                    project=project,
                    unit=unit,
                    event_name='unit.updated',
                    text=f'{unit.role_title} is building a stage plan',
                )
                provider_model = str(unit.provider_model or self._service.config.execution_model)
                provider_model_chain = [provider_model] if unit.provider_model else self._service.resolve_project_model_chain(project=project, node_type='execution')
                plan = await build_execution_plan(
                    unit.objective_summary,
                    level=unit.level,
                    effective_max_depth=project.effective_max_depth,
                    llm=self._service.llm,
                    provider_model=provider_model,
                    provider_model_chain=provider_model_chain,
                    local_available_tools=self._service.list_effective_tool_names(
                        session_id=project.session_id,
                        actor_role=unit.role_kind,
                        project_id=project.project_id,
                        unit_id=unit.unit_id,
                        mutation_allowed=True,
                    ),
                    local_available_skills=self._service.list_effective_skill_ids(
                        session_id=project.session_id,
                        actor_role=unit.role_kind,
                        project_id=project.project_id,
                        unit_id=unit.unit_id,
                    ),
                    delegate_available_tools=self._service.list_effective_tool_names(
                        session_id=project.session_id,
                        actor_role='execution',
                        project_id=project.project_id,
                        unit_id=unit.unit_id,
                        mutation_allowed=True,
                    ),
                    delegate_available_skills=self._service.list_effective_skill_ids(
                        session_id=project.session_id,
                        actor_role='execution',
                        project_id=project.project_id,
                        unit_id=unit.unit_id,
                    ),
                )
                checkpoint = self._new_checkpoint(plan)
                self._persist_checkpoint(unit.unit_id, checkpoint)
            elif unit.status not in {'completed', 'failed', 'canceled'}:
                unit = unit.model_copy(
                    update={
                        'status': 'running',
                        'current_action': self._resume_action(checkpoint),
                        'updated_at': now_iso(),
                        'started_at': unit.started_at or now_iso(),
                    }
                )
                self._service.store.upsert_unit(unit)
                await self._service.emit_unit_event(
                    project=project,
                    unit=unit,
                    event_name='unit.updated',
                    text=f'{unit.role_title} resumed from checkpoint',
                )
            stage_summaries = self._completed_stage_summaries(checkpoint)
            for stage_state in checkpoint.get('stages', []):
                if str(stage_state.get('status') or '') == 'completed':
                    continue
                await self._service.registry.wait_until_resumed(project.project_id)
                stage = await self._ensure_stage_record(project, unit, stage_state)
                while True:
                    await self._service.registry.wait_until_resumed(project.project_id)
                    stage = self._service.get_stage(stage.stage_id) or stage
                    outcome = await self._run_stage(project, unit, stage, stage_state, checkpoint)
                    stage = outcome['stage']
                    ordinary_failures = list(outcome['ordinary_failures'])
                    if ordinary_failures:
                        await self._rewrite_failed_work_items(
                            project=project,
                            unit=unit,
                            stage=stage,
                            stage_state=stage_state,
                            failures=ordinary_failures,
                            checkpoint=checkpoint,
                        )
                        continue

                    if self._all_work_items_passed(stage_state):
                        verified_child_results = self._collect_verified_child_results(stage_state)
                        stage_result = await self._synthesize_stage_result(project, stage, verified_child_results)
                        stage = (self._service.get_stage(stage.stage_id) or stage).model_copy(
                            update={
                                'status': 'completed',
                                'result_summary': stage_result,
                                'error_summary': '',
                                'finished_at': now_iso(),
                            }
                        )
                        self._service.store.upsert_stage(stage)
                        stage_state['status'] = 'completed'
                        stage_state['result_summary'] = stage_result
                        stage_state['error_summary'] = ''
                        checkpoint['status'] = 'running'
                        checkpoint['current_stage_index'] = int(stage_state.get('index', 1))
                        checkpoint['current_stage_id'] = None
                        self._persist_checkpoint(unit.unit_id, checkpoint)
                        stage_summaries = self._completed_stage_summaries(checkpoint)
                        await self._service.emit_event(
                            project=project,
                            scope='stage',
                            event_name='stage.completed',
                            text=f'Stage completed: {stage.title}',
                            unit_id=unit.unit_id,
                            stage_id=stage.stage_id,
                        )
                        break
                    failed_items = self._failed_items_for_rework(stage_state)
                    if failed_items:
                        await self._rewrite_failed_work_items(
                            project=project,
                            unit=unit,
                            stage=stage,
                            stage_state=stage_state,
                            failures=failed_items,
                            checkpoint=checkpoint,
                        )
                        continue
                    raise EngineeringFailureError(f'Stage {stage.title} reached a non-progress state')
            final_summary = ' | '.join(summary for summary in stage_summaries if summary).strip() or f'{unit.role_title} completed.'
            checkpoint['status'] = 'completed'
            checkpoint['current_stage_index'] = len(checkpoint.get('stages', []))
            checkpoint['current_stage_id'] = None
            checkpoint['final_summary'] = final_summary
            checkpoint['error_summary'] = ''
            self._persist_checkpoint(unit.unit_id, checkpoint)
            unit = (self._service.get_unit(unit.unit_id) or unit).model_copy(
                update={
                    'status': 'completed',
                    'current_action': '',
                    'result_summary': final_summary,
                    'error_summary': '',
                    'updated_at': now_iso(),
                    'finished_at': now_iso(),
                }
            )
            self._service.store.upsert_unit(unit)
            await self._service.emit_unit_event(project=project, unit=unit, event_name='unit.completed', text=f'{unit.role_title} completed')
            return final_summary
        except asyncio.CancelledError:
            raise
        except PermissionBlockedError:
            latest_checkpoint = self._load_checkpoint(unit.unit_id)
            if latest_checkpoint:
                latest_checkpoint['status'] = 'blocked'
                self._persist_checkpoint(unit.unit_id, latest_checkpoint)
            raise
        except PermissionDeniedError as exc:
            latest_checkpoint = self._load_checkpoint(unit.unit_id)
            if latest_checkpoint:
                latest_checkpoint['status'] = 'failed'
                latest_checkpoint['error_summary'] = str(exc)
                self._persist_checkpoint(unit.unit_id, latest_checkpoint)
            await self._service.project_runner._failure_reporter.record_unit_failure(
                project=project,
                unit=unit,
                error=exc,
                failure_kind='engineering',
            )
            raise
        except OrdinaryTaskFailureError as exc:
            latest_checkpoint = self._load_checkpoint(unit.unit_id)
            if latest_checkpoint:
                latest_checkpoint['status'] = 'failed'
                latest_checkpoint['error_summary'] = str(exc)
                self._persist_checkpoint(unit.unit_id, latest_checkpoint)
            await self._service.project_runner._failure_reporter.record_unit_failure(
                project=project,
                unit=unit,
                error=exc,
                failure_kind='ordinary',
            )
            raise
        except EngineeringFailureError as exc:
            latest_checkpoint = self._load_checkpoint(unit.unit_id)
            if latest_checkpoint:
                latest_checkpoint['status'] = 'failed'
                latest_checkpoint['error_summary'] = str(exc)
                self._persist_checkpoint(unit.unit_id, latest_checkpoint)
            await self._service.project_runner._failure_reporter.record_unit_failure(
                project=project,
                unit=unit,
                error=exc,
                failure_kind='engineering',
            )
            raise
        except Exception as exc:
            wrapped = EngineeringFailureError(str(exc))
            latest_checkpoint = self._load_checkpoint(unit.unit_id)
            if latest_checkpoint:
                latest_checkpoint['status'] = 'failed'
                latest_checkpoint['error_summary'] = str(wrapped)
                self._persist_checkpoint(unit.unit_id, latest_checkpoint)
            await self._service.project_runner._failure_reporter.record_unit_failure(
                project=project,
                unit=unit,
                error=wrapped,
                failure_kind='engineering',
            )
            raise wrapped

    async def _ensure_stage_record(self, project, unit: UnitAgentRecord, stage_state: dict[str, Any]) -> UnitStageRecord:
        existing = self._service.get_stage(str(stage_state.get('stage_id') or ''))
        if existing is not None:
            return existing
        stage = UnitStageRecord(
            stage_id=str(stage_state.get('stage_id') or new_stage_id()),
            project_id=project.project_id,
            unit_id=unit.unit_id,
            index=int(stage_state.get('index') or 1),
            title=str(stage_state.get('title') or 'Execution'),
            objective_summary=str(stage_state.get('objective_summary') or stage_state.get('title') or 'Execution'),
            dispatch_shape=str(stage_state.get('dispatch_shape') or 'single'),
            planned_work_count=int(stage_state.get('planned_work_count') or len(stage_state.get('work_items') or [])),
            status='pending',
            result_summary='',
            error_summary='',
            started_at=None,
            finished_at=None,
        )
        self._service.store.upsert_stage(stage)
        await self._service.emit_event(
            project=project,
            scope='stage',
            event_name='stage.created',
            text=f'Stage created: {stage.title}',
            unit_id=unit.unit_id,
            stage_id=stage.stage_id,
        )
        return stage

    async def _run_stage(
        self,
        project,
        unit: UnitAgentRecord,
        stage: UnitStageRecord,
        stage_state: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> dict[str, Any]:
        previous_status = str(stage_state.get('status') or 'pending')
        stage = stage.model_copy(
            update={
                'status': 'running',
                'started_at': stage.started_at or now_iso(),
                'error_summary': '',
                'finished_at': None,
            }
        )
        self._service.store.upsert_stage(stage)
        latest_unit = self._service.get_unit(unit.unit_id) or unit
        latest_unit = latest_unit.model_copy(
            update={
                'status': 'running',
                'current_stage_id': stage.stage_id,
                'current_action': f'Running stage: {stage.title}',
                'updated_at': now_iso(),
                'started_at': latest_unit.started_at or now_iso(),
            }
        )
        self._service.store.upsert_unit(latest_unit)
        stage_state['status'] = 'running'
        stage_state['error_summary'] = ''
        checkpoint['status'] = 'running'
        checkpoint['current_stage_index'] = int(stage_state.get('index', 1)) - 1
        checkpoint['current_stage_id'] = stage.stage_id
        self._persist_checkpoint(unit.unit_id, checkpoint)
        if previous_status in {'pending', 'rework', 'checking'}:
            await self._service.emit_event(
                project=project,
                scope='stage',
                event_name='stage.started',
                text=f'Stage started: {stage.title}',
                unit_id=unit.unit_id,
                stage_id=stage.stage_id,
            )
        ordinary_failures: list[dict[str, Any]] = []
        work_items = list(stage_state.get('work_items') or [])
        runnable_items = [
            item
            for item in work_items
            if str(item.get('check_status') or 'pending') != 'passed' and not bool(item.get('raw_result_ready', False))
        ]
        if str(stage_state.get('dispatch_shape') or 'single') == 'parallel' and len(runnable_items) > 1:
            outcomes = await self._service.scheduler.run_parallel(
                [self._run_work_item(project, latest_unit, stage, item, checkpoint) for item in runnable_items],
                return_exceptions=True,
            )
            for item, outcome in zip(runnable_items, outcomes):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if isinstance(outcome, (PermissionBlockedError, PermissionDeniedError)):
                    raise outcome
                if isinstance(outcome, OrdinaryTaskFailureError):
                    ordinary_failures.append({'index': int(item.get('index') or 0), 'reason': str(outcome), 'work_state': item})
                    continue
                if isinstance(outcome, EngineeringFailureError):
                    raise outcome
                if isinstance(outcome, Exception):
                    raise EngineeringFailureError(str(outcome))
        else:
            for item in runnable_items:
                try:
                    await self._run_work_item(project, latest_unit, stage, item, checkpoint)
                except asyncio.CancelledError:
                    raise
                except (PermissionBlockedError, PermissionDeniedError):
                    raise
                except OrdinaryTaskFailureError as exc:
                    ordinary_failures.append({'index': int(item.get('index') or 0), 'reason': str(exc), 'work_state': item})
                except EngineeringFailureError:
                    raise
                except Exception as exc:
                    raise EngineeringFailureError(str(exc)) from exc
        if ordinary_failures:
            stage_error = ' | '.join(str(item['reason']) for item in ordinary_failures if item.get('reason'))
            stage = stage.model_copy(update={'error_summary': stage_error})
            self._service.store.upsert_stage(stage)
            stage_state['error_summary'] = stage_error
        else:
            stage_state['error_summary'] = ''
        self._persist_checkpoint(unit.unit_id, checkpoint)
        self._persist_checkpoint(unit.unit_id, checkpoint)
        return {
            'stage': stage,
            'ordinary_failures': ordinary_failures,
        }


    async def _run_work_item(
        self,
        project,
        unit: UnitAgentRecord,
        stage: UnitStageRecord,
        work_state: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> None:
        await self._service.registry.wait_until_resumed(project.project_id)
        work_state['status'] = 'running'
        work_state['error_summary'] = ''
        work_state['failure_kind'] = None
        self._persist_checkpoint(unit.unit_id, checkpoint)
        try:
            if can_delegate(unit.level, project.effective_max_depth) and str(work_state.get('mode') or 'local') == 'delegate' and unit.can_delegate:
                child = None
                child_unit_id = str(work_state.get('child_unit_id') or '').strip()
                if child_unit_id:
                    child = self._service.get_unit(child_unit_id)
                if child is None:
                    child = self._service.create_child_unit(
                        project=project,
                        parent=unit,
                        role_title=str(work_state.get('role_title') or 'Execution Unit'),
                        objective=str(work_state.get('objective_summary') or ''),
                        prompt_preview=str(work_state.get('prompt_preview') or work_state.get('objective_summary') or ''),
                        provider_model=str(work_state.get('provider_model') or '') or None,
                        mutation_allowed=bool(work_state.get('mutation_allowed', False)),
                    )
                    parent_record = self._service.get_unit(unit.unit_id) or unit
                    self._service.store.upsert_unit(
                        parent_record.model_copy(update={'child_count': parent_record.child_count + 1, 'updated_at': now_iso()})
                    )
                    self._service.store.upsert_unit(child)
                    work_state['child_unit_id'] = child.unit_id
                    self._persist_checkpoint(unit.unit_id, checkpoint)
                    await self._service.emit_unit_event(
                        project=project,
                        unit=child,
                        event_name='unit.created',
                        text=f'{child.role_title} created',
                        stage_id=stage.stage_id,
                    )
                if child.status in {'failed', 'canceled'}:
                    failure_kind = str((child.metadata or {}).get('failure_kind') or 'engineering')
                    if failure_kind == 'ordinary':
                        raise OrdinaryTaskFailureError(child.error_summary or f'{child.role_title} failed')
                    raise EngineeringFailureError(child.error_summary or f'{child.role_title} failed')
                if child.status != 'completed':
                    await self.run(project, child)
                    child = self._service.get_unit(child.unit_id) or child
                # Immediate Checker verification for child
                candidate_item = {
                    'index': int(work_state.get('index') or 0),
                    'acceptance_criteria': str(work_state.get('acceptance_criteria') or ''),
                    'validation_tools': list(work_state.get('validation_tools') or []),
                    'candidate_content': str(child.result_summary or ''),
                }
                passed, reason = await self._service.checker_runner.run_checker_for_item(
                    project=project,
                    parent_unit=unit,
                    stage=stage,
                    candidate_item=candidate_item,
                )
                if not passed:
                    raise OrdinaryTaskFailureError(str(reason or f'{work_state.get("role_title")} verification failed'))
                    
                work_state['status'] = 'completed'
                work_state['result_summary'] = ''
                work_state['error_summary'] = ''
                work_state['raw_result_ready'] = True
                work_state['check_status'] = 'passed'
                self._persist_checkpoint(unit.unit_id, checkpoint)
                return
            if str(work_state.get('mode') or 'local') == 'delegate' and not unit.can_delegate:
                await self._service.emit_event(
                    project=project,
                    scope='project',
                    event_name='notice.raised',
                    text=f'Depth limit reached for {unit.role_title}; falling back to local execution.',
                    unit_id=unit.unit_id,
                    stage_id=stage.stage_id,
                    level='warn',
                )
            local_unit = self._overlay_local_capabilities(unit, work_state)
            await self._ensure_skill_access(project, local_unit, stage.stage_id)
            result = await self._execute_local(
                project,
                local_unit,
                stage,
                str(work_state.get('role_title') or 'Execution Unit'),
                str(work_state.get('objective_summary') or ''),
                str(work_state.get('prompt_preview') or work_state.get('objective_summary') or ''),
            )
            if result['status'] == 'ordinary_failure':
                raise OrdinaryTaskFailureError(str(result['blocking_reason'] or 'Worker reported ordinary failure'))
                
            # Immediate Checker verification
            candidate_item = {
                'index': int(work_state.get('index') or 0),
                'acceptance_criteria': str(work_state.get('acceptance_criteria') or ''),
                'validation_tools': list(work_state.get('validation_tools') or []),
                'candidate_content': str(result.get('deliverable') or ''),
            }
            passed, reason = await self._service.checker_runner.run_checker_for_item(
                project=project,
                parent_unit=unit,
                stage=stage,
                candidate_item=candidate_item,
            )
            if not passed:
                raise OrdinaryTaskFailureError(str(reason or f'{work_state.get("role_title")} verification failed'))
                
            work_state['status'] = 'completed'
            work_state['result_summary'] = str(result['deliverable'] or '')
            work_state['error_summary'] = ''
            work_state['raw_result_ready'] = True
            work_state['check_status'] = 'passed'
            self._persist_checkpoint(unit.unit_id, checkpoint)
            return
        except asyncio.CancelledError:
            raise
        except (PermissionBlockedError, PermissionDeniedError):
            work_state['failure_kind'] = 'permission'
            self._persist_checkpoint(unit.unit_id, checkpoint)
            raise
        except OrdinaryTaskFailureError as exc:
            work_state['status'] = 'failed'
            work_state['failure_kind'] = 'ordinary'
            work_state['error_summary'] = str(exc)
            self._persist_checkpoint(unit.unit_id, checkpoint)
            raise
        except EngineeringFailureError as exc:
            work_state['status'] = 'failed'
            work_state['failure_kind'] = 'engineering'
            work_state['error_summary'] = str(exc)
            self._persist_checkpoint(unit.unit_id, checkpoint)
            raise
        except Exception as exc:
            wrapped = EngineeringFailureError(str(exc))
            work_state['status'] = 'failed'
            work_state['failure_kind'] = 'engineering'
            work_state['error_summary'] = str(wrapped)
            self._persist_checkpoint(unit.unit_id, checkpoint)
            raise wrapped

    async def _execute_local(self, project, unit, stage: UnitStageRecord, role_title: str, objective: str, prompt_preview: str) -> dict[str, Any]:
        await self._service.emit_event(
            project=project,
            scope='tool',
            event_name='tool.started',
            text=f'{role_title} started local execution',
            unit_id=unit.unit_id,
            stage_id=stage.stage_id,
            data={'objective': objective},
        )
        for action in [f'Scoping {role_title}', f'Executing {role_title}', f'Summarizing {role_title}']:
            await self._service.registry.wait_until_resumed(project.project_id)
            unit = unit.model_copy(update={'status': 'running', 'current_action': action, 'updated_at': now_iso()})
            self._service.store.upsert_unit(unit)
            await self._service.emit_unit_event(project=project, unit=unit, event_name='unit.updated', text=f'{unit.role_title}: {action}', stage_id=stage.stage_id)
            await self._service.emit_event(project=project, scope='tool', event_name='tool.updated', text=action, unit_id=unit.unit_id, stage_id=stage.stage_id)
            await asyncio.sleep(0.05)
        provider_model = str(unit.provider_model or self._service.config.execution_model)
        provider_model_chain = [provider_model] if unit.provider_model else self._service.resolve_project_model_chain(project=project, node_type='execution')
        result_text = None
        try:
            result_text = await self._service.tool_runtime.run(
                unit=unit,
                project=project,
                stage=stage,
                prompt_preview=prompt_preview,
                objective=objective,
            )
        except (PermissionBlockedError, PermissionDeniedError):
            raise
        except Exception as exc:
            raise EngineeringFailureError(f'Tool runtime failed: {exc}') from exc
        if not result_text:
            try:
                result_text = await self._service.llm.chat_text(
                    provider_model=provider_model,
                    provider_model_chain=provider_model_chain,
                    system_prompt=LOCAL_EXECUTION_SYSTEM_PROMPT,
                    user_prompt=(
                        f'Project: {project.title}\n'
                        f'Unit role: {unit.role_title}\n'
                        f'Stage: {stage.title}\n'
                        f'Local role: {role_title}\n'
                        f'Prompt preview: {prompt_preview}\n'
                        f'Objective: {objective}\n'
                    ),
                    max_tokens=1000,
                    temperature=0.2,
                )
            except Exception as exc:
                if _offline_fallback_enabled():
                    result_text = json.dumps(
                        {
                            'status': 'success',
                            'summary': f'{role_title} completed (offline)',
                            'deliverable': objective or role_title,
                            'blocking_reason': '',
                            'evidence': [],
                        },
                        ensure_ascii=False,
                    )
                else:
                    message = str(exc)
                    if 'not configured with an API key' in message:
                        raise EngineeringFailureError(
                            f'Local execution requires a configured provider model. Current model: {provider_model}. '
                            'Configure the provider API key or pick a ready model in the sidebar.'
                        ) from exc
                    raise EngineeringFailureError(f'Local execution LLM failed: {exc}') from exc
        payload = self._parse_worker_payload(result_text)
        await self._service.emit_event(
            project=project,
            scope='tool',
            event_name='tool.completed',
            text=f'{role_title} completed local execution',
            unit_id=unit.unit_id,
            stage_id=stage.stage_id,
            data={
                'status': payload['status'],
                'result_summary': payload.get('summary') or payload.get('blocking_reason') or payload.get('deliverable') or '',
            },
        )
        return payload
    async def _rewrite_failed_work_items(
        self,
        *,
        project,
        unit: UnitAgentRecord,
        stage: UnitStageRecord,
        stage_state: dict[str, Any],
        failures: list[dict[str, Any]],
        checkpoint: dict[str, Any],
    ) -> None:
        if not failures:
            return
        failed_indexes: list[int] = []
        for failure in failures:
            work_state = failure.get('work_state') if isinstance(failure, dict) else None
            if not isinstance(work_state, dict):
                continue
            retry_count = int(work_state.get('retry_count') or 0)
            if retry_count >= MAX_ORDINARY_RETRY_COUNT:
                raise OrdinaryTaskFailureError(str(failure.get('reason') or f"{work_state.get('role_title') or 'Work item'} exceeded retry limit"))
            rewritten = await self._rewrite_single_work_item(work_state, reason=str(failure.get('reason') or ''))
            profile_id = self._next_validation_profile_id(stage_state)
            stage_state.setdefault('validation_profiles', []).append(
                {
                    'profile_id': profile_id,
                    'acceptance_criteria': rewritten['acceptance_criteria'],
                    'validation_tools': rewritten['validation_tools'],
                }
            )
            work_state['objective_summary'] = rewritten['objective_summary']
            work_state['prompt_preview'] = rewritten['prompt_preview']
            work_state['validation_profile_id'] = profile_id
            work_state['acceptance_criteria'] = rewritten['acceptance_criteria']
            work_state['validation_tools'] = rewritten['validation_tools']
            work_state['retry_count'] = retry_count + 1
            work_state['status'] = 'pending'
            work_state['check_status'] = 'pending'
            work_state['check_reason'] = ''
            work_state['error_summary'] = ''
            work_state['result_summary'] = ''
            work_state['failure_kind'] = None
            work_state['last_checker_id'] = None
            work_state['raw_result_ready'] = False
            work_state['child_unit_id'] = None
            failed_indexes.append(int(work_state.get('index') or 0))
        stage_error = ' | '.join(str(failure.get('reason') or '') for failure in failures if failure.get('reason'))
        checker_state = stage_state.setdefault('checker', self._new_checker_state())
        checker_state['status'] = 'pending'
        checker_state['summary'] = ''
        checker_state['rework_instructions'] = ''
        checker_state['checker_id'] = None
        checker_state['checked_work_item_indexes'] = []
        checker_state['passed_work_item_indexes'] = [
            int(item.get('index') or 0)
            for item in stage_state.get('work_items', [])
            if str(item.get('check_status') or '') == 'passed'
        ]
        checker_state['failed_work_item_indexes'] = failed_indexes
        stage_state['status'] = 'rework'
        stage_state['result_summary'] = ''
        stage_state['error_summary'] = stage_error
        stage = stage.model_copy(
            update={
                'status': 'rework',
                'result_summary': '',
                'error_summary': stage_error,
                'finished_at': None,
            }
        )
        self._service.store.upsert_stage(stage)
        self._persist_checkpoint(unit.unit_id, checkpoint)
        await self._service.emit_event(
            project=project,
            scope='stage',
            event_name='stage.rework_requested',
            text=f'Stage {stage.title} will retry failed work items only.',
            unit_id=unit.unit_id,
            stage_id=stage.stage_id,
            level='warn',
            data={'failed_work_item_indexes': failed_indexes},
        )

    async def _rewrite_single_work_item(self, work_state: dict[str, Any], *, reason: str) -> dict[str, Any]:
        payload = None
        try:
            payload = await self._service.llm.chat_json(
                provider_model=str(work_state.get('provider_model') or self._service.config.execution_model),
                provider_model_chain=[str(work_state.get('provider_model') or self._service.config.execution_model)],
                system_prompt=REWORK_REWRITER_SYSTEM_PROMPT,
                user_prompt=json.dumps(
                    {
                        'objective_summary': str(work_state.get('objective_summary') or ''),
                        'prompt_preview': str(work_state.get('prompt_preview') or ''),
                        'acceptance_criteria': str(work_state.get('acceptance_criteria') or ''),
                        'validation_tools': list(work_state.get('validation_tools') or []),
                        'failure_reason': reason,
                        'retry_count': int(work_state.get('retry_count') or 0),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                max_tokens=1200,
                temperature=0.2,
            )
        except Exception as exc:
            raise EngineeringFailureError(f'Rework rewriter failed: {exc}') from exc
        if not isinstance(payload, dict):
            raise EngineeringFailureError('Rework rewriter did not return a JSON object')
        objective_summary = str(payload.get('objective_summary') or '').strip()
        prompt_preview = str(payload.get('prompt_preview') or '').strip()
        acceptance_criteria = str(payload.get('acceptance_criteria') or '').strip()
        validation_tools = self._normalize_validation_tools(payload.get('validation_tools') or [])
        if not objective_summary or not prompt_preview or not acceptance_criteria:
            raise EngineeringFailureError('Rework rewriter returned incomplete fields')
        return {
            'objective_summary': objective_summary,
            'prompt_preview': prompt_preview,
            'acceptance_criteria': acceptance_criteria,
            'validation_tools': validation_tools,
        }

    def _collect_candidate_items_for_check(self, stage_state: dict[str, Any]) -> list[dict[str, Any]]:
        candidate_items: list[dict[str, Any]] = []
        for item in stage_state.get('work_items', []):
            if str(item.get('check_status') or 'pending') != 'pending':
                continue
            if not bool(item.get('raw_result_ready', False)):
                continue
            candidate_items.append(
                {
                    'index': int(item.get('index') or 0),
                    'acceptance_criteria': str(item.get('acceptance_criteria') or ''),
                    'candidate_content': self._load_work_candidate_content(item),
                    'validation_tools': self._normalize_validation_tools(item.get('validation_tools') or []),
                    'source_unit_id': str(item.get('child_unit_id') or '') or None,
                }
            )
        return candidate_items

    def _collect_verified_child_results(self, stage_state: dict[str, Any]) -> list[str]:
        if not self._all_work_items_passed(stage_state):
            raise EngineeringFailureError('Cannot collect child results before all work items pass checker validation')
        results: list[str] = []
        for item in stage_state.get('work_items', []):
            result = self._load_work_candidate_content(item)
            if not result:
                raise EngineeringFailureError(f"Missing verified result for work item {item.get('index')}")
            results.append(result)
        return results

    def _load_work_candidate_content(self, work_state: dict[str, Any]) -> str:
        child_unit_id = str(work_state.get('child_unit_id') or '').strip()
        if child_unit_id:
            child = self._service.get_unit(child_unit_id)
            if child is None:
                raise EngineeringFailureError(f'Missing child unit {child_unit_id}')
            content = str(child.result_summary or '').strip()
            if not content:
                raise EngineeringFailureError(f'Missing child result for {child_unit_id}')
            return content
        content = str(work_state.get('result_summary') or '').strip()
        if not content:
            raise EngineeringFailureError(f"Missing local result for work item {work_state.get('index')}")
        return content

    async def _synthesize_stage_result(self, project, stage: UnitStageRecord, verified_child_results: list[str]) -> str:
        try:
            result = await self._service.llm.chat_text(
                provider_model=self._service.resolve_project_provider_model(project=project, node_type='inspection'),
                provider_model_chain=self._service.resolve_project_model_chain(project=project, node_type='inspection'),
                system_prompt=STAGE_SYNTHESIS_SYSTEM_PROMPT,
                user_prompt=json.dumps(
                    {
                        'stage_title': stage.title,
                        'stage_objective_summary': stage.objective_summary,
                        'verified_child_results': verified_child_results,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                max_tokens=1000,
                temperature=0.2,
            )
            result = str(result or '').strip()
            if result:
                return result
        except Exception:
            pass
        return ' | '.join(result for result in verified_child_results if result).strip() or stage.objective_summary
    def _load_checkpoint(self, unit_id: str) -> dict[str, Any]:
        payload = self._service.checkpoint_store.get(unit_id)
        if not isinstance(payload, dict):
            return {}
        version = int(payload.get('version') or 0)
        if version <= 0:
            return {}
        if not isinstance(payload.get('stages'), list):
            return {}
        if version != self.CHECKPOINT_VERSION:
            payload = {**payload, 'version': self.CHECKPOINT_VERSION}
        return payload

    def _persist_checkpoint(self, unit_id: str, checkpoint: dict[str, Any]) -> None:
        checkpoint['version'] = self.CHECKPOINT_VERSION
        checkpoint['updated_at'] = now_iso()
        self._service.checkpoint_store.touch(unit_id, checkpoint)

    def _new_checkpoint(self, plan) -> dict[str, Any]:
        stages = []
        for index, blueprint in enumerate(plan.stages, start=1):
            profile_map = {
                str(profile.profile_id): {
                    'profile_id': str(profile.profile_id),
                    'acceptance_criteria': str(profile.acceptance_criteria),
                    'validation_tools': self._normalize_validation_tools(profile.validation_tools or []),
                }
                for profile in blueprint.validation_profiles
            }
            stage_profiles = list(profile_map.values())
            stages.append(
                {
                    'index': index,
                    'stage_id': new_stage_id(),
                    'title': blueprint.title,
                    'objective_summary': blueprint.objective_summary,
                    'dispatch_shape': blueprint.dispatch_shape,
                    'planned_work_count': len(blueprint.work_units),
                    'status': 'pending',
                    'result_summary': '',
                    'error_summary': '',
                    'checker': self._new_checker_state(),
                    'validation_profiles': stage_profiles,
                    'work_items': [
                        self._new_work_state(work, work_index, profile_map)
                        for work_index, work in enumerate(blueprint.work_units, start=1)
                    ],
                }
            )
        return {
            'version': self.CHECKPOINT_VERSION,
            'status': 'planned',
            'current_stage_index': 0,
            'current_stage_id': None,
            'final_summary': '',
            'error_summary': '',
            'stages': stages,
        }

    def _new_work_state(self, work, index: int, profile_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
        profile_id = str(work.validation_profile_id or f'vp_{index}')
        profile = profile_map.get(profile_id) or {
            'profile_id': profile_id,
            'acceptance_criteria': f'Work item output must directly satisfy this objective: {work.objective_summary}',
            'validation_tools': [],
        }
        return {
            'index': index,
            'role_title': work.role_title,
            'objective_summary': work.objective_summary,
            'prompt_preview': work.prompt_preview,
            'mode': work.mode,
            'provider_model': work.provider_model,
            'mutation_allowed': bool(work.mutation_allowed),
            'validation_profile_id': profile_id,
            'acceptance_criteria': str(profile.get('acceptance_criteria') or ''),
            'validation_tools': self._normalize_validation_tools(profile.get('validation_tools') or []),
            'status': 'pending',
            'result_summary': '',
            'error_summary': '',
            'child_unit_id': None,
            'check_status': 'pending',
            'check_reason': '',
            'failure_kind': None,
            'retry_count': 0,
            'last_checker_id': None,
            'raw_result_ready': False,
        }

    async def _ensure_skill_access(self, project, unit: UnitAgentRecord, stage_id: str | None) -> None:
        return None

    def _overlay_local_capabilities(self, unit: UnitAgentRecord, work_state: dict[str, Any]) -> UnitAgentRecord:
        return unit.model_copy(
            update={
                'provider_model': str(work_state.get('provider_model') or unit.provider_model or self._service.config.execution_model),
                'mutation_allowed': bool(work_state.get('mutation_allowed', unit.mutation_allowed)),
            }
        )

    @staticmethod
    def _parse_worker_payload(raw_output: str | None) -> dict[str, Any]:
        text = str(raw_output or '').strip()
        if not text:
            raise EngineeringFailureError('Worker returned empty output')
        try:
            payload = json_repair.loads(text)
        except Exception as exc:
            raise EngineeringFailureError('Worker output is not valid JSON') from exc
        if not isinstance(payload, dict):
            raise EngineeringFailureError('Worker output must be a JSON object')
        status = str(payload.get('status') or '').strip().lower()
        summary = str(payload.get('summary') or '').strip()
        deliverable = str(payload.get('deliverable') or '').strip()
        blocking_reason = str(payload.get('blocking_reason') or '').strip()
        evidence = payload.get('evidence')
        if not isinstance(evidence, list):
            evidence = []
        if status == 'success':
            if not deliverable:
                raise EngineeringFailureError('Worker success payload is missing deliverable')
            return {
                'status': 'success',
                'summary': summary,
                'deliverable': deliverable,
                'blocking_reason': '',
                'evidence': evidence,
            }
        if status == 'ordinary_failure':
            if not blocking_reason:
                raise EngineeringFailureError('Worker ordinary_failure payload is missing blocking_reason')
            return {
                'status': 'ordinary_failure',
                'summary': summary,
                'deliverable': '',
                'blocking_reason': blocking_reason,
                'evidence': evidence,
            }
        raise EngineeringFailureError('Worker status must be success or ordinary_failure')
    def _normalize_validation_tools(self, raw: Any) -> list[str]:
        values = raw if isinstance(raw, list) else [raw] if raw else []
        allowed = set(self._service.list_runtime_tool_names())
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            tool = str(value or '').strip()
            if not tool or tool in seen or (allowed and tool not in allowed):
                continue
            seen.add(tool)
            normalized.append(tool)
        return normalized

    @staticmethod
    def _new_checker_state() -> dict[str, Any]:
        return {
            'status': 'pending',
            'checker_id': None,
            'summary': '',
            'rework_instructions': '',
            'checked_work_item_indexes': [],
            'passed_work_item_indexes': [],
            'failed_work_item_indexes': [],
        }

    def _reconcile_checkpoint(self, project, unit: UnitAgentRecord, checkpoint: dict[str, Any]) -> dict[str, Any]:
        if not checkpoint:
            return {}
        stage_records = {
            stage.stage_id: stage
            for stage in self._service.list_stages(project.project_id)
            if stage.unit_id == unit.unit_id
        }
        for stage_state in checkpoint.get('stages', []):
            self._ensure_stage_state_defaults(stage_state)
            checker_state = stage_state.setdefault('checker', self._new_checker_state())
            work_items = list(stage_state.get('work_items') or [])
            record = stage_records.get(str(stage_state.get('stage_id') or ''))
            if record is not None:
                record_checker_id = getattr(record, 'checker_id', None)
                checker_state['checker_id'] = record_checker_id or checker_state.get('checker_id')
                if record.status == 'completed':
                    stage_state['status'] = 'completed'
                    stage_state['result_summary'] = record.result_summary or stage_state.get('result_summary', '')
                    stage_state['error_summary'] = ''
                    checker_state['status'] = 'passed'
                    for item in work_items:
                        item['status'] = 'completed'
                        item['check_status'] = 'passed'
                        item['error_summary'] = ''
                        item['raw_result_ready'] = True
                elif record.status in {'checking', 'rework', 'running', 'pending', 'failed'}:
                    stage_state['status'] = record.status
                    stage_state['error_summary'] = record.error_summary or stage_state.get('error_summary', '')
            for item in work_items:
                self._ensure_work_state_defaults(item)
        if unit.status == 'completed':
            checkpoint['status'] = 'completed'
            checkpoint['final_summary'] = unit.result_summary or checkpoint.get('final_summary', '')
            checkpoint['current_stage_index'] = len(checkpoint.get('stages', []))
            checkpoint['current_stage_id'] = None
            return checkpoint
        if unit.status == 'failed':
            checkpoint['status'] = 'failed'
            checkpoint['error_summary'] = unit.error_summary or checkpoint.get('error_summary', '')
            return checkpoint
        checkpoint['current_stage_index'] = next(
            (index for index, stage_state in enumerate(checkpoint.get('stages', [])) if str(stage_state.get('status') or '') != 'completed'),
            len(checkpoint.get('stages', [])),
        )
        checkpoint['current_stage_id'] = self._current_stage_id(checkpoint)
        checkpoint['status'] = 'running'
        return checkpoint

    def _ensure_stage_state_defaults(self, stage_state: dict[str, Any]) -> None:
        stage_state.setdefault('status', 'pending')
        stage_state.setdefault('result_summary', '')
        stage_state.setdefault('error_summary', '')
        stage_state.setdefault('validation_profiles', [])
        stage_state.setdefault('checker', self._new_checker_state())
        work_items = list(stage_state.get('work_items') or [])
        stage_state['work_items'] = work_items
        for item in work_items:
            self._ensure_work_state_defaults(item)

    def _ensure_work_state_defaults(self, item: dict[str, Any]) -> None:
        item.setdefault('validation_profile_id', str(item.get('validation_profile_id') or f"vp_{item.get('index') or 1}"))
        item.setdefault('acceptance_criteria', f"Work item output must directly satisfy this objective: {item.get('objective_summary') or ''}")
        item['validation_tools'] = self._normalize_validation_tools(item.get('validation_tools') or [])
        item.setdefault('status', 'pending')
        item.setdefault('result_summary', '')
        item.setdefault('error_summary', '')
        item.setdefault('child_unit_id', None)
        item.setdefault('check_status', 'pending')
        item.setdefault('check_reason', '')
        item.setdefault('failure_kind', None)
        item.setdefault('retry_count', 0)
        item.setdefault('last_checker_id', None)
        item.setdefault('raw_result_ready', False)

    @staticmethod
    def _current_stage_id(checkpoint: dict[str, Any]) -> str | None:
        current_index = int(checkpoint.get('current_stage_index') or 0)
        stages = list(checkpoint.get('stages') or [])
        if 0 <= current_index < len(stages):
            return str(stages[current_index].get('stage_id') or '') or None
        return None

    @staticmethod
    def _completed_stage_summaries(checkpoint: dict[str, Any]) -> list[str]:
        summaries: list[str] = []
        for stage_state in checkpoint.get('stages', []):
            if str(stage_state.get('status') or '') != 'completed':
                continue
            summary = str(stage_state.get('result_summary') or '').strip() or str(stage_state.get('title') or '').strip()
            if summary:
                summaries.append(summary)
        return summaries

    def _resume_action(self, checkpoint: dict[str, Any]) -> str:
        current_index = int(checkpoint.get('current_stage_index') or 0)
        stages = list(checkpoint.get('stages') or [])
        if 0 <= current_index < len(stages):
            return f"Resuming stage: {stages[current_index].get('title') or 'Execution'}"
        return 'Resuming from checkpoint'

    @staticmethod
    def _all_work_items_passed(stage_state: dict[str, Any]) -> bool:
        work_items = list(stage_state.get('work_items') or [])
        return bool(work_items) and all(str(item.get('check_status') or 'pending') == 'passed' for item in work_items)

    @staticmethod
    def _failed_items_for_rework(stage_state: dict[str, Any]) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        for item in stage_state.get('work_items', []):
            if str(item.get('check_status') or '') == 'failed':
                failures.append({'index': int(item.get('index') or 0), 'reason': str(item.get('check_reason') or ''), 'work_state': item})
        return failures

    def _next_validation_profile_id(self, stage_state: dict[str, Any]) -> str:
        max_index = 0
        for profile in stage_state.get('validation_profiles', []):
            profile_id = str(profile.get('profile_id') or '').strip()
            if not profile_id.startswith('vp_'):
                continue
            try:
                max_index = max(max_index, int(profile_id.split('_', 1)[1]))
            except Exception:
                continue
        return f'vp_{max_index + 1}'


