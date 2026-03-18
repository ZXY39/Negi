from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime.memory_scope import normalize_memory_scope
from main.errors import TaskPausedError
from main.ids import new_node_id
from main.models import NodeFinalResult, NodeRecord, SpawnChildResult, SpawnChildSpec, TokenUsageSummary
from main.prompts import load_prompt
from main.runtime.internal_tools import SpawnChildNodesTool
from main.types import KIND_ACCEPTANCE, KIND_EXECUTION, STATUS_FAILED, STATUS_SUCCESS

SKIPPED_CHECK_RESULT = '未检验'
ACCEPTANCE_REF_GUIDANCE = (
    'If more detail is needed, use content.search first and then content.open for targeted reads. '
    'Do not request the full document body.'
)
EXECUTION_SPAWN_GUIDANCE = """
This execution node may spawn child nodes.

Before doing the next step, check whether the work should be split into child pipelines first.
- Prefer `spawn_child_nodes` when the workload is broad, slow, or naturally decomposes into independent scopes.
- Only call `spawn_child_nodes` when the tool is present in the current tool list.
- Split by directory, module, file group, result batch, or another clear ownership boundary.
- Pass paths, refs, search clues, and acceptance requirements to children. Do not inline large source excerpts.
- Decide per child whether acceptance is actually required instead of defaulting every child to acceptance.
""".strip()


class NodeRunner:
    def __init__(
        self,
        *,
        store,
        log_service,
        react_loop,
        tool_provider,
        execution_model_refs: list[str],
        acceptance_model_refs: list[str],
        execution_max_iterations: int = 16,
        acceptance_max_iterations: int | None = None,
        max_parallel_child_pipelines: int = 10,
        context_enricher=None,
    ) -> None:
        self._store = store
        self._log_service = log_service
        self._react_loop = react_loop
        self._tool_provider = tool_provider
        self._execution_model_refs = list(execution_model_refs or [])
        self._acceptance_model_refs = list(acceptance_model_refs or []) or list(execution_model_refs or [])
        self._execution_max_iterations = max(2, int(execution_max_iterations or 16))
        self._acceptance_max_iterations = max(2, int(acceptance_max_iterations or self._execution_max_iterations))
        self._max_parallel_child_pipelines = max(1, int(max_parallel_child_pipelines or 1))
        self._context_enricher = context_enricher

    async def run_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        task = self._store.get_task(task_id)
        node = self._store.get_node(node_id)
        if task is None or node is None:
            raise ValueError(f'missing task or node: {task_id} / {node_id}')
        if node.status in {STATUS_SUCCESS, STATUS_FAILED}:
            return NodeFinalResult(status=node.status, output=node.final_output or node.failure_reason)
        if task.cancel_requested:
            return self._mark_failed(task_id, node.node_id, reason='canceled')
        try:
            tools = self._build_tools(task=task, node=node)
            messages = await self._resume_messages(task=task, node=node)
            result = await self._react_loop.run(
                task=task,
                node=node,
                messages=messages,
                tools=tools,
                model_refs=self._model_refs_for(node),
                runtime_context=self._runtime_context(task=task, node=node),
                max_iterations=self._max_iterations_for(node),
            )
            if (self._store.get_task(task_id) or task).cancel_requested:
                return self._mark_failed(task_id, node.node_id, reason='canceled')
            return self._mark_finished(task_id, node.node_id, result)
        except TaskPausedError:
            raise
        except asyncio.CancelledError:
            return self._mark_failed(task_id, node.node_id, reason='canceled')
        except Exception as exc:
            return self._mark_failed(task_id, node.node_id, reason=str(exc))

    async def _resume_messages(self, *, task, node: NodeRecord) -> list[dict[str, Any]]:
        state = self._log_service.read_runtime_state(task.task_id) or {}
        for frame in list(state.get('frames') or []):
            if str(frame.get('node_id') or '') == node.node_id and isinstance(frame.get('messages'), list) and frame.get('messages'):
                return list(frame['messages'])
        return await self._build_messages(task=task, node=node)

    def _build_tools(self, *, task, node: NodeRecord) -> dict[str, Tool]:
        tools = dict(self._tool_provider(node) or {})
        if node.node_kind == KIND_EXECUTION and node.can_spawn_children:
            tools['spawn_child_nodes'] = SpawnChildNodesTool(
                lambda children, call_id=None: self._spawn_children(
                    task_id=task.task_id,
                    parent_node_id=node.node_id,
                    specs=children,
                    call_id=call_id,
                )
            )
        else:
            tools.pop('spawn_child_nodes', None)
        return tools

    async def _build_messages(self, *, task, node: NodeRecord) -> list[dict[str, Any]]:
        system_prompt = self._build_system_prompt(node=node)
        messages = [
            {'role': 'system', 'content': system_prompt},
            {
                'role': 'user',
                'content': json.dumps(
                    {
                        'task_id': task.task_id,
                        'node_id': node.node_id,
                        'node_kind': node.node_kind,
                        'depth': node.depth,
                        'can_spawn_children': bool(node.can_spawn_children),
                        'goal': node.goal,
                        'prompt': node.prompt,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ]
        if self._context_enricher is not None:
            enriched = await self._context_enricher(task=task, node=node, messages=list(messages))
            if isinstance(enriched, list) and enriched:
                return enriched
        return messages

    def _build_system_prompt(self, *, node: NodeRecord) -> str:
        system_name = 'acceptance_execution.md' if node.node_kind == KIND_ACCEPTANCE else 'node_execution.md'
        prompt = load_prompt(system_name).strip()
        if node.node_kind == KIND_EXECUTION and node.can_spawn_children:
            return f'{prompt}\n\n{EXECUTION_SPAWN_GUIDANCE}'
        return prompt

    def _model_refs_for(self, node: NodeRecord) -> list[str]:
        return list(self._acceptance_model_refs if node.node_kind == KIND_ACCEPTANCE else self._execution_model_refs)

    def _max_iterations_for(self, node: NodeRecord) -> int:
        return int(self._acceptance_max_iterations if node.node_kind == KIND_ACCEPTANCE else self._execution_max_iterations)

    def _runtime_context(self, *, task, node: NodeRecord) -> dict[str, Any]:
        memory_scope = normalize_memory_scope(
            (task.metadata or {}).get('memory_scope') if isinstance(task.metadata, dict) else None,
            fallback_session_key=task.session_id,
        )
        return {
            'session_key': task.session_id,
            'task_id': task.task_id,
            'node_id': node.node_id,
            'depth': node.depth,
            'node_kind': node.node_kind,
            'actor_role': self._actor_role_for_node(node),
            'memory_channel': str(memory_scope.get('channel') or 'unknown'),
            'memory_chat_id': str(memory_scope.get('chat_id') or 'unknown'),
        }

    @staticmethod
    def _actor_role_for_node(node: NodeRecord) -> str:
        return 'inspection' if node.node_kind == KIND_ACCEPTANCE else 'execution'

    async def _spawn_children(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        specs: list[SpawnChildSpec],
        call_id: str | None,
    ) -> list[SpawnChildResult]:
        task = self._store.get_task(task_id)
        parent = self._store.get_node(parent_node_id)
        if task is None or parent is None:
            raise ValueError('parent task or node missing')
        if not parent.can_spawn_children:
            raise ValueError('spawn_child_nodes is not available for this node')
        cache_key = str(call_id or f'call:{len(specs)}')
        cached = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
        if isinstance(cached, dict) and cached.get('completed'):
            return [SpawnChildResult.model_validate(item) for item in list(cached.get('results') or [])]

        cached_payload = copy.deepcopy(cached) if isinstance(cached, dict) else {
            'specs': [item.model_dump(mode='json') for item in specs],
            'entries': [],
            'results': [],
            'completed': False,
        }
        cached_payload['specs'] = [item.model_dump(mode='json') for item in specs]
        entries = list(cached_payload.get('entries') or [])
        while len(entries) < len(specs):
            entries.append({})
        cached_payload['entries'] = [
            self._normalize_spawn_entry(index=index, spec=spec, entry=entries[index])
            for index, spec in enumerate(specs)
        ]
        cached_payload['completed'] = False
        self._save_spawn_cache(task.task_id, parent.node_id, cache_key, cached_payload)

        semaphore = asyncio.Semaphore(self._max_parallel_child_pipelines)

        async def _run_spec(index: int, spec: SpawnChildSpec) -> SpawnChildResult:
            async with semaphore:
                return await self._run_child_pipeline(
                    task=task,
                    parent=parent,
                    spec=spec,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                )

        results = await asyncio.gather(*[_run_spec(index, spec) for index, spec in enumerate(specs)])
        cached_payload['results'] = [item.model_dump(mode='json') for item in results]
        cached_payload['completed'] = True
        self._save_spawn_cache(task.task_id, parent.node_id, cache_key, cached_payload)
        return list(results)

    async def _run_child_pipeline(
        self,
        *,
        task,
        parent: NodeRecord,
        spec: SpawnChildSpec,
        cache_key: str,
        cached_payload: dict[str, Any],
        index: int,
    ) -> SpawnChildResult:
        entries = list(cached_payload.get('entries') or [])
        entry = dict(entries[index] or {})
        existing_result = entry.get('result')
        if isinstance(existing_result, dict) and str(entry.get('status') or '').strip().lower() in {'success', 'error'}:
            return SpawnChildResult.model_validate(existing_result)

        requires_acceptance = bool(entry.get('requires_acceptance'))
        started_at = str(entry.get('started_at') or _now())
        self._update_spawn_entry(
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            cache_key=cache_key,
            cached_payload=cached_payload,
            index=index,
            status='running',
            started_at=started_at,
            finished_at='',
            check_status='pending' if requires_acceptance else 'skipped',
        )

        try:
            child_id = str(entry.get('child_node_id') or '').strip()
            child = self._store.get_node(child_id) if child_id else None
            if child is None:
                child = self._create_execution_child(task=task, parent=parent, spec=spec)
                self._update_spawn_entry(
                    task_id=task.task_id,
                    parent_node_id=parent.node_id,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                    child_node_id=child.node_id,
                )

            child_result = await self.run_node(task.task_id, child.node_id)
            child = self._store.get_node(child.node_id) or child
            child_summary, child_ref = self._child_handoff_payload(
                task_id=task.task_id,
                node=child,
                fallback_output=child_result.output,
            )

            if child_result.status != STATUS_SUCCESS:
                self._log_service.update_node_check_result(task.task_id, child.node_id, SKIPPED_CHECK_RESULT)
                result = SpawnChildResult(
                    goal=spec.goal,
                    check_result=SKIPPED_CHECK_RESULT,
                    node_output=child_summary,
                    node_output_summary=child_summary,
                    node_output_ref=child_ref,
                )
                self._update_spawn_entry(
                    task_id=task.task_id,
                    parent_node_id=parent.node_id,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                    status='error',
                    finished_at=_now(),
                    check_status='skipped',
                    result=result.model_dump(mode='json'),
                )
                return result

            if not requires_acceptance:
                self._log_service.update_node_check_result(task.task_id, child.node_id, SKIPPED_CHECK_RESULT)
                result = SpawnChildResult(
                    goal=spec.goal,
                    check_result=SKIPPED_CHECK_RESULT,
                    node_output=child_summary,
                    node_output_summary=child_summary,
                    node_output_ref=child_ref,
                )
                self._update_spawn_entry(
                    task_id=task.task_id,
                    parent_node_id=parent.node_id,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                    status='success',
                    finished_at=_now(),
                    check_status='skipped',
                    result=result.model_dump(mode='json'),
                )
                return result

            acceptance_id = str(entry.get('acceptance_node_id') or '').strip()
            acceptance = self._store.get_node(acceptance_id) if acceptance_id else None
            if acceptance is None:
                acceptance = self.create_acceptance_node(
                    task=task,
                    accepted_node=child,
                    goal=f'accept:{spec.goal}',
                    acceptance_prompt=str(spec.acceptance_prompt or ''),
                    parent_node_id=child.node_id,
                )
                self._update_spawn_entry(
                    task_id=task.task_id,
                    parent_node_id=parent.node_id,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                    acceptance_node_id=acceptance.node_id,
                    check_status='running',
                )

            acceptance_result = await self.run_node(task.task_id, acceptance.node_id)
            acceptance = self._store.get_node(acceptance.node_id) or acceptance
            check_result = str(acceptance_result.output or acceptance.failure_reason or '').strip() or SKIPPED_CHECK_RESULT
            self._log_service.update_node_check_result(task.task_id, child.node_id, check_result)
            result = SpawnChildResult(
                goal=spec.goal,
                check_result=check_result,
                node_output=child_summary,
                node_output_summary=child_summary,
                node_output_ref=child_ref,
            )
            self._update_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                status='success' if acceptance_result.status == STATUS_SUCCESS else 'error',
                finished_at=_now(),
                check_status='passed' if acceptance_result.status == STATUS_SUCCESS else 'failed',
                result=result.model_dump(mode='json'),
            )
            return result
        except TaskPausedError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = SpawnChildResult(
                goal=spec.goal,
                check_result=f'Error: {exc}',
                node_output='',
                node_output_summary='',
                node_output_ref='',
            )
            self._update_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                status='error',
                finished_at=_now(),
                check_status='failed',
                result=result.model_dump(mode='json'),
            )
            return result

    def _normalize_spawn_entry(self, *, index: int, spec: SpawnChildSpec, entry: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(entry or {})
        status = str(payload.get('status') or '').strip().lower()
        if status not in {'queued', 'running', 'success', 'error'}:
            status = 'queued'
        check_status = str(payload.get('check_status') or '').strip().lower()
        if check_status not in {'', 'pending', 'running', 'skipped', 'passed', 'failed'}:
            check_status = ''
        result = payload.get('result')
        return {
            'index': index,
            'goal': spec.goal,
            'prompt': spec.prompt,
            'requires_acceptance': bool(payload.get('requires_acceptance')) if 'requires_acceptance' in payload else self._requires_acceptance(spec),
            'acceptance_prompt': str(spec.acceptance_prompt or ''),
            'status': status,
            'started_at': str(payload.get('started_at') or ''),
            'finished_at': str(payload.get('finished_at') or ''),
            'child_node_id': str(payload.get('child_node_id') or ''),
            'acceptance_node_id': str(payload.get('acceptance_node_id') or ''),
            'check_status': check_status,
            'result': copy.deepcopy(result) if isinstance(result, dict) else {},
        }

    def _update_spawn_entry(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        cached_payload: dict[str, Any],
        index: int,
        **changes: Any,
    ) -> dict[str, Any]:
        entries = list(cached_payload.get('entries') or [])
        while len(entries) <= index:
            entries.append({})
        entry = dict(entries[index] or {})
        for key, value in changes.items():
            if value is None:
                continue
            entry[key] = copy.deepcopy(value)
        entries[index] = entry
        cached_payload['entries'] = entries
        cached_payload['results'] = [
            copy.deepcopy(item.get('result'))
            for item in entries
            if isinstance(item, dict) and isinstance(item.get('result'), dict) and item.get('result')
        ]
        self._save_spawn_cache(task_id, parent_node_id, cache_key, cached_payload)
        return entry

    def _save_spawn_cache(self, task_id: str, parent_node_id: str, cache_key: str, payload: dict[str, Any]) -> None:
        payload_copy = copy.deepcopy(payload)

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            operations = dict(metadata.get('spawn_operations') or {})
            operations[cache_key] = payload_copy
            metadata['spawn_operations'] = operations
            return metadata

        self._log_service.update_node_metadata(parent_node_id, _mutate)
        self._sync_parent_child_pipelines_frame(task_id, parent_node_id)
        self._log_service.refresh_task_view(task_id, mark_unread=True)

    def _sync_parent_child_pipelines_frame(self, task_id: str, parent_node_id: str) -> None:
        parent = self._store.get_node(parent_node_id)
        if parent is None:
            return
        child_pipelines, pending_specs, partial_results, has_active = self._parent_spawn_frame_state(parent)
        state = self._log_service.read_runtime_state(task_id) or {}
        frame_exists = any(str(item.get('node_id') or '') == parent_node_id for item in list(state.get('frames') or []))
        if not frame_exists and not child_pipelines:
            return

        def _mutate(frame: dict[str, Any]) -> dict[str, Any]:
            next_frame = dict(frame or {})
            next_frame['node_id'] = parent.node_id
            next_frame['depth'] = int(parent.depth or 0)
            next_frame['node_kind'] = parent.node_kind
            next_frame['child_pipelines'] = child_pipelines
            next_frame['pending_child_specs'] = pending_specs
            next_frame['partial_child_results'] = partial_results
            if has_active:
                next_frame['phase'] = 'waiting_children'
            elif str(next_frame.get('phase') or '').strip() == 'waiting_children':
                next_frame['phase'] = 'waiting_tool_results' if list(next_frame.get('tool_calls') or []) else 'after_model'
            return next_frame

        self._log_service.update_frame(task_id, parent.node_id, _mutate, publish_snapshot=False)

    @staticmethod
    def _parent_spawn_frame_state(parent: NodeRecord) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
        spawn_operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(spawn_operations, dict):
            return [], [], [], False

        child_pipelines: list[dict[str, Any]] = []
        pending_specs: list[dict[str, Any]] = []
        partial_results: list[dict[str, Any]] = []

        for operation_index, (_operation_id, payload) in enumerate(spawn_operations.items()):
            if not isinstance(payload, dict):
                continue
            specs = [copy.deepcopy(item) for item in list(payload.get('specs') or []) if isinstance(item, dict)]
            if not bool(payload.get('completed')):
                pending_specs.extend(specs)
            for entry_index, entry in enumerate(list(payload.get('entries') or [])):
                if not isinstance(entry, dict):
                    continue
                status = str(entry.get('status') or '').strip().lower()
                if status not in {'queued', 'running', 'success', 'error'}:
                    status = 'queued'
                child_pipelines.append(
                    {
                        'index': int(entry.get('index') or entry_index),
                        'goal': str(entry.get('goal') or ''),
                        'status': status,
                        'child_node_id': str(entry.get('child_node_id') or ''),
                        'acceptance_node_id': str(entry.get('acceptance_node_id') or ''),
                        'check_status': str(entry.get('check_status') or ''),
                        'started_at': str(entry.get('started_at') or ''),
                        'finished_at': str(entry.get('finished_at') or ''),
                        '_sort_key': (
                            str(entry.get('started_at') or ''),
                            operation_index,
                            int(entry.get('index') or entry_index),
                        ),
                    }
                )
                result = entry.get('result')
                if isinstance(result, dict) and result:
                    partial_results.append(copy.deepcopy(result))

        child_pipelines.sort(key=lambda item: item.get('_sort_key') or ('', 0, 0))
        for item in child_pipelines:
            item.pop('_sort_key', None)
        has_active = any(str(item.get('status') or '').strip().lower() in {'queued', 'running'} for item in child_pipelines)
        return child_pipelines, pending_specs, partial_results, has_active

    def _requires_acceptance(self, spec: SpawnChildSpec) -> bool:
        if spec.requires_acceptance is True:
            return True
        if spec.requires_acceptance is False:
            return False
        return bool(str(spec.acceptance_prompt or '').strip())

    def _create_execution_child(self, *, task, parent: NodeRecord, spec: SpawnChildSpec) -> NodeRecord:
        child = NodeRecord(
            node_id=new_node_id(),
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            root_node_id=parent.root_node_id,
            depth=parent.depth + 1,
            node_kind=KIND_EXECUTION,
            status='in_progress',
            goal=spec.goal,
            prompt=spec.prompt,
            input=spec.prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=(parent.depth + 1) < int(task.max_depth),
            created_at=_now(),
            updated_at=_now(),
            token_usage=TokenUsageSummary(tracked=bool(getattr(task.token_usage, 'tracked', False))),
            token_usage_by_model=[],
            metadata={},
        )
        return self._log_service.create_node(task.task_id, child)

    def create_acceptance_node(
        self,
        *,
        task,
        accepted_node: NodeRecord,
        goal: str,
        acceptance_prompt: str,
        parent_node_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> NodeRecord:
        child_summary, child_ref = self._child_handoff_payload(
            task_id=task.task_id,
            node=accepted_node,
            fallback_output='',
        )
        prompt = self._compose_acceptance_prompt(
            acceptance_prompt=str(acceptance_prompt or ''),
            node_output=child_summary,
            node_output_ref=child_ref,
        )
        acceptance = NodeRecord(
            node_id=new_node_id(),
            task_id=task.task_id,
            parent_node_id=parent_node_id or accepted_node.node_id,
            root_node_id=accepted_node.root_node_id,
            depth=accepted_node.depth + 1,
            node_kind=KIND_ACCEPTANCE,
            status='in_progress',
            goal=goal,
            prompt=prompt,
            input=prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=False,
            created_at=_now(),
            updated_at=_now(),
            token_usage=TokenUsageSummary(tracked=bool(getattr(task.token_usage, 'tracked', False))),
            token_usage_by_model=[],
            metadata={'accepted_node_id': accepted_node.node_id, **dict(metadata or {})},
        )
        return self._log_service.create_node(task.task_id, acceptance)

    def _compose_acceptance_prompt(self, *, acceptance_prompt: str, node_output: str, node_output_ref: str) -> str:
        prompt = f'{acceptance_prompt}\n\nChild node output summary:\n{node_output}\n'
        if node_output_ref:
            prompt = f'{prompt}\nChild node output ref: {node_output_ref}\n{ACCEPTANCE_REF_GUIDANCE}\n'
        return prompt

    def _child_handoff_payload(self, *, task_id: str, node: NodeRecord, fallback_output: str) -> tuple[str, str]:
        latest = self._log_service.ensure_node_output_externalized(task_id, node.node_id) or self._store.get_node(node.node_id) or node
        summary = str(getattr(latest, 'final_output', '') or fallback_output or getattr(latest, 'failure_reason', '') or '').strip()
        ref = str(getattr(latest, 'final_output_ref', '') or '').strip()
        return summary, ref

    def _mark_finished(self, task_id: str, node_id: str, result: NodeFinalResult) -> NodeFinalResult:
        status = STATUS_SUCCESS if result.status == STATUS_SUCCESS else STATUS_FAILED
        self._log_service.remove_frame(task_id, node_id, publish_snapshot=False)
        self._log_service.update_node_status(
            task_id,
            node_id,
            status=status,
            final_output=result.output,
            failure_reason='' if status == STATUS_SUCCESS else result.output,
        )
        return NodeFinalResult(status=status, output=result.output)

    def _mark_failed(self, task_id: str, node_id: str, *, reason: str) -> NodeFinalResult:
        text = str(reason or 'node failed').strip() or 'node failed'
        self._log_service.remove_frame(task_id, node_id, publish_snapshot=False)
        self._log_service.update_node_status(task_id, node_id, status=STATUS_FAILED, final_output=text, failure_reason=text)
        return NodeFinalResult(status=STATUS_FAILED, output=text)


def _now() -> str:
    from main.protocol import now_iso

    return now_iso()
