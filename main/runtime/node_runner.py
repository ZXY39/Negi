from __future__ import annotations

import asyncio
import json
from typing import Any

from g3ku.agent.tools.base import Tool
from main.errors import TaskPausedError
from main.ids import new_node_id
from main.models import NodeFinalResult, NodeRecord, SpawnChildResult, SpawnChildSpec
from main.prompts import load_prompt
from main.runtime.internal_tools import SpawnChildNodesTool
from main.types import KIND_ACCEPTANCE, KIND_EXECUTION, STATUS_FAILED, STATUS_SUCCESS


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
                lambda children, call_id=None: self._spawn_children(task_id=task.task_id, parent_node_id=node.node_id, specs=children, call_id=call_id)
            )
        else:
            tools.pop('spawn_child_nodes', None)
        return tools

    async def _build_messages(self, *, task, node: NodeRecord) -> list[dict[str, Any]]:
        system_name = 'acceptance_execution.md' if node.node_kind == KIND_ACCEPTANCE else 'node_execution.md'
        messages = [
            {'role': 'system', 'content': load_prompt(system_name)},
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

    def _model_refs_for(self, node: NodeRecord) -> list[str]:
        return list(self._acceptance_model_refs if node.node_kind == KIND_ACCEPTANCE else self._execution_model_refs)

    def _max_iterations_for(self, node: NodeRecord) -> int:
        return int(self._acceptance_max_iterations if node.node_kind == KIND_ACCEPTANCE else self._execution_max_iterations)

    def _runtime_context(self, *, task, node: NodeRecord) -> dict[str, Any]:
        return {
            'session_key': task.session_id,
            'task_id': task.task_id,
            'node_id': node.node_id,
            'depth': node.depth,
            'node_kind': node.node_kind,
            'actor_role': self._actor_role_for_node(node),
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
            raise ValueError('当前节点不允许继续派生子节点')
        cache_key = str(call_id or f'call:{len(specs)}')
        cached = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
        if isinstance(cached, dict) and cached.get('completed'):
            return [SpawnChildResult.model_validate(item) for item in list(cached.get('results') or [])]

        async def _run_spec(index: int, spec: SpawnChildSpec, cached_entry: dict[str, Any]) -> SpawnChildResult:
            child_id = str(cached_entry.get('child_node_id') or '').strip()
            child = self._store.get_node(child_id) if child_id else None
            if child is None:
                child = self._create_execution_child(task=task, parent=parent, spec=spec)
                cached_entry['child_node_id'] = child.node_id
            child_result = await self.run_node(task.task_id, child.node_id)
            child = self._store.get_node(child.node_id) or child
            child_summary = str(getattr(child, 'final_output', '') or child_result.output or '').strip()
            child_ref = str(getattr(child, 'final_output_ref', '') or '').strip()
            if child_result.status != STATUS_SUCCESS:
                result = SpawnChildResult(
                    goal=spec.goal,
                    check_result='无',
                    node_output=child_summary,
                    node_output_summary=child_summary,
                    node_output_ref=child_ref,
                )
                cached_entry['result'] = result.model_dump(mode='json')
                self._save_spawn_cache(parent.node_id, cache_key, cached_payload)
                return result
            acceptance_id = str(cached_entry.get('acceptance_node_id') or '').strip()
            acceptance = self._store.get_node(acceptance_id) if acceptance_id else None
            if acceptance is None:
                acceptance = self._create_acceptance_child(
                    task=task,
                    child=child,
                    spec=spec,
                    child_output=child_summary,
                    child_output_ref=child_ref,
                )
                cached_entry['acceptance_node_id'] = acceptance.node_id
            acceptance_result = await self.run_node(task.task_id, acceptance.node_id)
            check_result = acceptance_result.output if acceptance_result.status == STATUS_SUCCESS else '无'
            self._log_service.update_node_check_result(task.task_id, child.node_id, check_result)
            result = SpawnChildResult(
                goal=spec.goal,
                check_result=check_result,
                node_output=child_summary,
                node_output_summary=child_summary,
                node_output_ref=child_ref,
            )
            cached_entry['result'] = result.model_dump(mode='json')
            self._save_spawn_cache(parent.node_id, cache_key, cached_payload)
            return result

        cached_payload = cached if isinstance(cached, dict) else {'specs': [item.model_dump(mode='json') for item in specs], 'entries': [], 'results': [], 'completed': False}
        entries = list(cached_payload.get('entries') or [])
        while len(entries) < len(specs):
            entries.append({})
        cached_payload['entries'] = entries
        self._save_spawn_cache(parent.node_id, cache_key, cached_payload)
        results = [await asyncio.create_task(_run_spec(index, spec, entries[index])) for index, spec in enumerate(specs)]
        cached_payload['results'] = [item.model_dump(mode='json') for item in results]
        cached_payload['completed'] = True
        self._save_spawn_cache(parent.node_id, cache_key, cached_payload)
        return results

    def _save_spawn_cache(self, parent_node_id: str, cache_key: str, payload: dict[str, Any]) -> None:
        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            operations = dict(metadata.get('spawn_operations') or {})
            operations[cache_key] = payload
            metadata['spawn_operations'] = operations
            return metadata

        self._log_service.update_node_metadata(parent_node_id, _mutate)

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
            metadata={},
        )
        return self._log_service.create_node(task.task_id, child)

    def _create_acceptance_child(
        self,
        *,
        task,
        child: NodeRecord,
        spec: SpawnChildSpec,
        child_output: str,
        child_output_ref: str = '',
    ) -> NodeRecord:
        prompt = (
            f'{spec.acceptance_prompt}\n\n'
            f'子节点输出摘要：\n{child_output}\n'
        )
        if child_output_ref:
            prompt = (
                f'{prompt}\n'
                f'子节点输出引用：{child_output_ref}\n'
                '如需查看更多细节，只能先使用 content.search，再使用 content.open 读取局部片段，不要请求全文。\n'
            )
        acceptance = NodeRecord(
            node_id=new_node_id(),
            task_id=task.task_id,
            parent_node_id=child.node_id,
            root_node_id=child.root_node_id,
            depth=child.depth + 1,
            node_kind=KIND_ACCEPTANCE,
            status='in_progress',
            goal=f'验收:{spec.goal}',
            prompt=prompt,
            input=prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=False,
            created_at=_now(),
            updated_at=_now(),
            metadata={'accepted_node_id': child.node_id},
        )
        return self._log_service.create_node(task.task_id, acceptance)

    def _mark_finished(self, task_id: str, node_id: str, result: NodeFinalResult) -> NodeFinalResult:
        status = STATUS_SUCCESS if result.status == STATUS_SUCCESS else STATUS_FAILED
        self._log_service.update_node_status(task_id, node_id, status=status, final_output=result.output, failure_reason='' if status == STATUS_SUCCESS else result.output)
        return NodeFinalResult(status=status, output=result.output)

    def _mark_failed(self, task_id: str, node_id: str, *, reason: str) -> NodeFinalResult:
        text = str(reason or 'node failed').strip() or 'node failed'
        self._log_service.update_node_status(task_id, node_id, status=STATUS_FAILED, final_output=text, failure_reason=text)
        return NodeFinalResult(status=STATUS_FAILED, output=text)


def _now() -> str:
    from main.protocol import now_iso

    return now_iso()
