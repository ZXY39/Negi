from __future__ import annotations

import asyncio
import copy
import json
import os
import platform
from datetime import datetime
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime.memory_scope import normalize_memory_scope
from g3ku.runtime.project_environment import current_project_environment
from main.errors import TaskPausedError, describe_exception
from main.ids import new_node_id
from main.models import (
    NodeFinalResult,
    NodeRecord,
    RESULT_SCHEMA_VERSION,
    SpawnChildFailureInfo,
    SpawnChildResult,
    SpawnChildSpec,
    TokenUsageSummary,
    normalize_execution_policy_metadata,
    normalize_final_acceptance_metadata,
    normalize_result_payload,
)
from main.prompts import load_prompt
from main.runtime.internal_tools import SpawnChildNodesTool, SubmitFinalResultTool, SubmitNextStageTool
from main.types import KIND_ACCEPTANCE, KIND_EXECUTION, STATUS_FAILED, STATUS_SUCCESS

SKIPPED_CHECK_RESULT = '未检验'
_RECOVERY_FINGERPRINT_KEY = 'recovery_fingerprint'
_SUPERSEDED_SPAWN_REASON_PREFIX = 'superseded by newer spawn round'
_SPAWN_REVIEW_TOOL_NAME = 'review_spawn_candidates'
_SPAWN_REVIEW_BLOCKED_CHECK_RESULT = '派生已被拦截'
_SPAWN_REVIEW_DEFAULT_BLOCK_REASON = '检验派生未批准该候选派生。'
_SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION = '请在当前父节点内自行执行，或收缩为更聚焦的单一派生。'
_SPAWN_REVIEW_RETRY_DELAY_SECONDS = 0.1
_SPAWN_REVIEW_REPAIR_PREFIX = '上一轮检验派生回复无效。'


_UNSET = object()


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
        execution_max_iterations: int | None | object = _UNSET,
        acceptance_max_iterations: int | None | object = _UNSET,
        max_parallel_child_pipelines: int | None | object = _UNSET,
        execution_max_concurrency: int | None = None,
        acceptance_max_concurrency: int | None = None,
        context_enricher=None,
        context_preparer=None,
        context_finalizer=None,
        workspace_root_getter=None,
    ) -> None:
        self._store = store
        self._log_service = log_service
        self._react_loop = react_loop
        self._tool_provider = tool_provider
        self._execution_model_refs = list(execution_model_refs or [])
        self._acceptance_model_refs = list(acceptance_model_refs or []) or list(execution_model_refs or [])
        self._execution_max_iterations = self._normalize_optional_limit(execution_max_iterations, default=16)
        self._acceptance_max_iterations = self._normalize_optional_limit(
            acceptance_max_iterations,
            default=self._execution_max_iterations,
        )
        self._max_parallel_child_pipelines = self._normalize_optional_limit(max_parallel_child_pipelines, default=10)
        self._execution_max_concurrency = self._normalize_optional_limit(execution_max_concurrency, default=None)
        self._acceptance_max_concurrency = self._normalize_optional_limit(
            acceptance_max_concurrency,
            default=self._execution_max_concurrency,
        )
        self._parallel_child_pipelines_enabled = True
        self._adaptive_tool_budget_controller = getattr(react_loop, '_adaptive_tool_budget_controller', None)
        self._context_enricher = context_enricher
        self._context_preparer = context_preparer
        self._context_finalizer = context_finalizer
        self._workspace_root_getter = workspace_root_getter
        self.nested_node_executor = None
        self.cancel_node_subtree_executor = None
        self.governance_child_created_observer = None
        self.governance_spawn_refusal_supplier = None
        self._spawn_operation_locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _normalized_status(value: Any) -> str:
        return str(value or '').strip().lower()

    def _task_terminal_reason(self, task_id: str, *, task=None) -> str:
        current = self._store.get_task(task_id) or task
        if current is None:
            return ''
        status = self._normalized_status(getattr(current, 'status', ''))
        if status == STATUS_FAILED:
            return str(getattr(current, 'failure_reason', '') or getattr(current, 'brief_text', '') or 'task failed').strip() or 'task failed'
        if status == STATUS_SUCCESS:
            return 'task already completed'
        return ''

    def _node_terminal_reason(self, node: NodeRecord | None, *, default_failed: str, default_success: str) -> str:
        if node is None:
            return ''
        status = self._normalized_status(getattr(node, 'status', ''))
        if status == STATUS_FAILED:
            return str(getattr(node, 'failure_reason', '') or getattr(node, 'final_output', '') or default_failed).strip() or default_failed
        if status == STATUS_SUCCESS:
            return default_success
        return ''

    def _spawn_abort_result(self, goal: str, reason: str) -> SpawnChildResult:
        text = str(reason or 'task terminated').strip() or 'task terminated'
        error_text = f'Error: {text}'
        return SpawnChildResult(
            goal=goal,
            check_result=error_text,
            node_output='',
            node_output_summary='',
            node_output_ref='',
            failure_info=self._runtime_spawn_failure_info(error_text),
        )

    @staticmethod
    def _governance_refusal_result(goal: str, reason_text: str, *, brief: bool = False) -> SpawnChildResult:
        text = (
            '同上：本轮监管统一拦截，请自行执行。'
            if brief
            else str(reason_text or '派生已被拦截').strip() or '派生已被拦截'
        )
        return SpawnChildResult(
            goal=goal,
            check_result=_SPAWN_REVIEW_BLOCKED_CHECK_RESULT,
            node_output=text,
            node_output_summary=text,
            node_output_ref='',
            failure_info=None,
        )

    @staticmethod
    def _spawn_operation_lock_key(parent_node_id: str) -> str:
        return str(parent_node_id or '').strip()

    def _spawn_operation_lock(self, parent_node_id: str) -> asyncio.Lock:
        key = self._spawn_operation_lock_key(parent_node_id)
        lock = self._spawn_operation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._spawn_operation_locks[key] = lock
        return lock

    @staticmethod
    def _spawn_round_superseded_reason(replacement_round_id: str) -> str:
        round_id = str(replacement_round_id or '').strip()
        if round_id:
            return f'{_SUPERSEDED_SPAWN_REASON_PREFIX}: {round_id}'
        return _SUPERSEDED_SPAWN_REASON_PREFIX

    @staticmethod
    def _spawn_exception_text(exc: BaseException | None) -> str:
        return f'Error: {describe_exception(exc)}'

    def _spawn_runtime_result(self, goal: str, *, error_text: str) -> SpawnChildResult:
        normalized_error_text = str(error_text or 'Error: runtime failure').strip() or 'Error: runtime failure'
        return SpawnChildResult(
            goal=goal,
            check_result=normalized_error_text,
            node_output='',
            node_output_summary='',
            node_output_ref='',
            failure_info=self._runtime_spawn_failure_info(normalized_error_text),
        )

    def _should_propagate_child_pipeline_cancellation(self, *, task_id: str, parent_node_id: str) -> bool:
        task = self._store.get_task(task_id)
        if task is None:
            return False
        if bool(task.pause_requested) or bool(task.cancel_requested):
            return True
        if self._task_terminal_reason(task_id, task=task):
            return True
        parent = self._store.get_node(parent_node_id)
        return bool(
            self._node_terminal_reason(
                parent,
                default_failed='parent node failed',
                default_success='parent node already completed',
            )
        )

    def _spawn_spec_payload(self, spec: SpawnChildSpec | dict[str, Any] | Any) -> dict[str, Any] | None:
        try:
            normalized_spec = spec if isinstance(spec, SpawnChildSpec) else SpawnChildSpec.model_validate(spec)
        except Exception:
            return None
        normalized_policy = normalize_execution_policy_metadata(
            normalized_spec.execution_policy.model_dump(mode='json')
        ).model_dump(mode='json')
        return {
            'goal': str(normalized_spec.goal or ''),
            'prompt': str(normalized_spec.prompt or ''),
            'execution_policy': normalized_policy,
            'acceptance_prompt': str(normalized_spec.acceptance_prompt or ''),
            'requires_acceptance': bool(self._requires_acceptance(normalized_spec)),
        }

    def _spawn_specs_fingerprint(self, specs: list[SpawnChildSpec] | list[dict[str, Any]] | Any) -> str:
        normalized_specs: list[dict[str, Any]] = []
        for item in list(specs or []):
            payload = self._spawn_spec_payload(item)
            if payload is None:
                return ''
            normalized_specs.append(payload)
        if not normalized_specs:
            return ''
        return json.dumps(normalized_specs, ensure_ascii=False, sort_keys=True)

    def _completed_successful_spawn_results(
        self,
        *,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        exclude_cache_key: str = '',
    ) -> list[SpawnChildResult] | None:
        operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(operations, dict):
            return None
        target_fingerprint = self._spawn_specs_fingerprint(specs)
        if not target_fingerprint:
            return None
        for cache_key, payload in reversed(list(operations.items())):
            if str(cache_key or '').strip() == str(exclude_cache_key or '').strip():
                continue
            if not isinstance(payload, dict) or not bool(payload.get('completed')):
                continue
            if self._spawn_specs_fingerprint(payload.get('specs') or []) != target_fingerprint:
                continue
            entries = [item for item in list(payload.get('entries') or []) if isinstance(item, dict)]
            if len(entries) != len(specs):
                continue
            if any(self._normalized_status(item.get('status')) != 'success' for item in entries):
                continue
            results = self._spawn_round_results_from_entries(
                task_id=parent.task_id,
                entries=entries,
                specs=specs,
            )
            if len(results) != len(specs):
                continue
            if any(result.failure_info is not None for result in results):
                continue
            return results
        return None

    async def run_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        task = self._store.get_task(task_id)
        node = self._store.get_node(node_id)
        if task is None or node is None:
            raise ValueError(f'missing task or node: {task_id} / {node_id}')
        if node.status in {STATUS_SUCCESS, STATUS_FAILED}:
            return self._result_from_record(node)
        if self._pause_requested(task_id):
            self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            raise TaskPausedError(task_id)
        terminal_reason = self._task_terminal_reason(task_id, task=task)
        if terminal_reason:
            return self._mark_failed(task_id, node.node_id, reason=terminal_reason)
        if task.cancel_requested:
            return self._mark_failed(task_id, node.node_id, reason='canceled')
        try:
            if self._context_preparer is not None:
                await self._await_with_runtime_marker(
                    task_id=task_id,
                    node_id=node.node_id,
                    marker='context_preparer',
                    awaitable=self._context_preparer(task=task, node=node),
                )
            tools = self._build_tools(task=task, node=node)
            react_state = await self._await_with_runtime_marker(
                task_id=task_id,
                node_id=node.node_id,
                marker='resume_react_state',
                awaitable=self._resume_react_state(task=task, node=node),
            )
            result = await self._await_with_runtime_marker(
                task_id=task_id,
                node_id=node.node_id,
                marker='react_loop.run',
                awaitable=self._react_loop.run(
                    task=task,
                    node=node,
                    messages=list(react_state.get('messages') or []),
                    tools=tools,
                    tools_supplier=lambda current_task=task, current_node=node: self._build_tools(
                        task=current_task,
                        node=current_node,
                    ),
                    model_refs=self._model_refs_for(node),
                    model_refs_supplier=lambda current_node=node: self._model_refs_for(current_node),
                    runtime_context=self._runtime_context(task=task, node=node),
                    max_iterations=self._max_iterations_for(node),
                    max_parallel_tool_calls=self._max_parallel_tool_calls_for(node),
                ),
            )
            if self._pause_requested(task_id):
                self._mark_finished(task_id, node.node_id, result)
                self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
                raise TaskPausedError(task_id)
            terminal_reason = self._task_terminal_reason(task_id, task=task)
            if terminal_reason:
                return self._mark_failed(task_id, node.node_id, reason=terminal_reason)
            if (self._store.get_task(task_id) or task).cancel_requested:
                return self._mark_failed(task_id, node.node_id, reason='canceled')
            return self._mark_finished(task_id, node.node_id, result)
        except TaskPausedError:
            self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
            raise
        except asyncio.CancelledError:
            if self._pause_requested(task_id):
                self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
                self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
                raise TaskPausedError(task_id)
            return self._mark_failed(task_id, node.node_id, reason='canceled')
        except Exception as exc:
            return self._mark_failed(task_id, node.node_id, reason=describe_exception(exc))
        finally:
            if self._context_finalizer is not None:
                self._context_finalizer(task=task, node=node)

    def _set_runtime_await_marker(self, *, task_id: str, node_id: str, marker: str, started_at: str = '') -> None:
        normalized_marker = str(marker or '').strip()
        normalized_started_at = str(started_at or '').strip()

        def _mutate(frame: dict[str, Any]) -> dict[str, Any]:
            next_frame = dict(frame or {})
            next_frame['await_marker'] = normalized_marker
            next_frame['await_started_at'] = normalized_started_at if normalized_marker else ''
            return next_frame

        self._log_service.update_frame(task_id, node_id, _mutate, publish_snapshot=True)

    async def _await_with_runtime_marker(self, *, task_id: str, node_id: str, marker: str, awaitable: Any) -> Any:
        started_at = datetime.now().isoformat()
        self._set_runtime_await_marker(
            task_id=task_id,
            node_id=node_id,
            marker=marker,
            started_at=started_at,
        )
        try:
            return await awaitable
        finally:
            self._set_runtime_await_marker(task_id=task_id, node_id=node_id, marker='')

    async def _run_nested_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        executor = self.nested_node_executor
        if callable(executor):
            return await executor(task_id, node_id)
        return await self.run_node(task_id, node_id)

    async def _resume_react_state(self, *, task, node: NodeRecord) -> dict[str, Any]:
        frame = self._log_service.read_runtime_frame(task.task_id, node.node_id) or {}
        if isinstance(frame.get('messages'), list) and frame.get('messages'):
            return {
                'messages': list(frame.get('messages') or []),
            }
        return {
            'messages': await self._build_messages(task=task, node=node),
        }

    def _flush_latest_valid_result_if_paused(self, *, task_id: str, node_id: str) -> NodeFinalResult | None:
        latest = self._store.get_node(node_id)
        if latest is None or latest.status in {STATUS_SUCCESS, STATUS_FAILED}:
            return None
        _ = task_id
        return None

    def _runtime_frame_messages(self, *, task_id: str, node_id: str) -> list[dict[str, Any]]:
        frame = self._log_service.read_runtime_frame(task_id, node_id) or {}
        messages = frame.get('messages')
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
        return []

    def _build_tools(self, *, task, node: NodeRecord) -> dict[str, Tool]:
        tools = dict(self._tool_provider(node) or {})
        if node.node_kind in {KIND_EXECUTION, KIND_ACCEPTANCE}:
            tools['submit_next_stage'] = SubmitNextStageTool(
                lambda stage_goal, tool_round_budget, completed_stage_summary, key_refs, final: self._submit_next_stage(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    stage_goal=stage_goal,
                    tool_round_budget=tool_round_budget,
                    completed_stage_summary=completed_stage_summary,
                    key_refs=key_refs,
                    final=final,
                )
            )
            tools['submit_final_result'] = SubmitFinalResultTool(
                lambda payload: self._submit_final_result(payload),
                node_kind=node.node_kind,
            )
        else:
            tools.pop('submit_next_stage', None)
            tools.pop('submit_final_result', None)
        if node.node_kind == KIND_EXECUTION:
            if node.can_spawn_children:
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
        else:
            tools.pop('spawn_child_nodes', None)
        return tools

    async def _build_messages(self, *, task, node: NodeRecord) -> list[dict[str, Any]]:
        system_prompt = self._build_system_prompt(node=node)
        core_requirement = self._resolve_core_requirement(task)
        execution_policy = self._resolve_execution_policy(task, node=node)
        payload: dict[str, Any] = {
            'task_id': task.task_id,
            'node_id': node.node_id,
            'node_kind': node.node_kind,
            'depth': node.depth,
            'can_spawn_children': bool(node.can_spawn_children),
            'goal': node.goal,
            'prompt': str(node.prompt or ''),
            'core_requirement': core_requirement,
            'execution_policy': execution_policy.model_dump(mode='json'),
            'runtime_environment': self._runtime_environment_payload(task=task),
        }
        completion_contract = self._completion_contract_payload(task=task, node=node)
        if completion_contract is not None:
            payload['completion_contract'] = completion_contract
        messages = [
            {'role': 'system', 'content': system_prompt},
            {
                'role': 'user',
                'content': json.dumps(payload, ensure_ascii=False, indent=2),
            },
        ]
        if self._context_enricher is not None:
            enriched = await self._context_enricher(task=task, node=node, messages=list(messages))
            if isinstance(enriched, list) and enriched:
                return enriched
        return messages

    def _build_system_prompt(self, *, node: NodeRecord) -> str:
        system_name = 'acceptance_execution.md' if node.node_kind == KIND_ACCEPTANCE else 'node_execution.md'
        return load_prompt(system_name).strip()

    def _execution_stage_payload(self, *, task, node: NodeRecord) -> dict[str, Any]:
        return self._log_service.execution_stage_prompt_payload(task.task_id, node.node_id)

    def _completion_contract_payload(self, *, task, node: NodeRecord) -> dict[str, Any] | None:
        if node.node_kind != KIND_EXECUTION or node.parent_node_id is not None:
            return None
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        return {
            'result_schema_version': RESULT_SCHEMA_VERSION,
            'final_acceptance_required': bool(final_acceptance.required),
            'final_acceptance_prompt': str(final_acceptance.prompt or ''),
        }

    def _model_refs_for(self, node: NodeRecord) -> list[str]:
        return list(self._acceptance_model_refs if node.node_kind == KIND_ACCEPTANCE else self._execution_model_refs)

    def _max_iterations_for(self, node: NodeRecord) -> int | None:
        return self._acceptance_max_iterations if node.node_kind == KIND_ACCEPTANCE else self._execution_max_iterations

    def _max_parallel_tool_calls_for(self, node: NodeRecord) -> int | None:
        role_limit = self._acceptance_max_concurrency if node.node_kind == KIND_ACCEPTANCE else self._execution_max_concurrency
        global_limit = getattr(self._react_loop, '_max_parallel_tool_calls', None)
        if role_limit is None:
            return global_limit
        if global_limit is None:
            return role_limit
        return min(int(role_limit), int(global_limit))

    def _max_parallel_child_pipelines_for(self, node: NodeRecord) -> int | None:
        _ = node
        return self._max_parallel_child_pipelines

    def _runtime_context(self, *, task, node: NodeRecord) -> dict[str, Any]:
        memory_scope = normalize_memory_scope(
            (task.metadata or {}).get('memory_scope') if isinstance(task.metadata, dict) else None,
            fallback_session_key=task.session_id,
        )
        project_environment = current_project_environment(
            shell_family=self._shell_family(),
            workspace_root=self._workspace_root(),
            process_cwd=self._process_cwd(),
        )
        task_temp_dir = str(self._task_temp_dir(getattr(task, 'task_id', None)))
        return {
            'session_key': task.session_id,
            'task_id': task.task_id,
            'node_id': node.node_id,
            'depth': node.depth,
            'node_kind': node.node_kind,
            'actor_role': self._actor_role_for_node(node),
            'can_spawn_children': bool(node.can_spawn_children),
            'memory_channel': str(memory_scope.get('channel') or 'unknown'),
            'memory_chat_id': str(memory_scope.get('chat_id') or 'unknown'),
            'project_python': str(project_environment.get('project_python') or ''),
            'project_python_dir': str(project_environment.get('project_python_dir') or ''),
            'project_scripts_dir': str(project_environment.get('project_scripts_dir') or ''),
            'project_path_entries': list(project_environment.get('project_path_entries') or []),
            'project_virtual_env': str(project_environment.get('project_virtual_env') or ''),
            'project_python_hint': str(project_environment.get('project_python_hint') or ''),
            'task_temp_dir': task_temp_dir,
            'temp_dir': task_temp_dir,
            'tool_snapshot_supplier': (
                (lambda current_task_id=task.task_id: self._tool_snapshot_supplier(current_task_id))
                if callable(getattr(self, '_tool_snapshot_supplier', None))
                else None
            ),
        }

    def _workspace_root(self) -> Path:
        getter = self._workspace_root_getter
        if callable(getter):
            value = getter()
            if value is not None:
                return Path(value).expanduser().resolve()
        return Path.cwd().expanduser().resolve()

    @staticmethod
    def _process_cwd() -> Path:
        return Path(os.getcwd()).expanduser().resolve()

    @staticmethod
    def _os_family() -> str:
        system = platform.system().strip().lower()
        if system:
            return system
        return 'windows' if os.name == 'nt' else os.name or 'unknown'

    @staticmethod
    def _shell_family() -> str:
        if os.name == 'nt':
            return 'powershell'
        shell = str(os.environ.get('SHELL') or '').strip().lower()
        if 'powershell' in shell or 'pwsh' in shell:
            return 'powershell'
        if 'bash' in shell:
            return 'bash'
        if 'zsh' in shell:
            return 'zsh'
        if shell:
            return Path(shell).name
        return 'sh'

    def _task_temp_dir(self, task_id: str | None) -> Path:
        fallback = (self._workspace_root() / 'temp').resolve()
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return fallback
        getter = getattr(self._log_service, 'read_task_runtime_meta', None)
        if not callable(getter):
            return fallback
        try:
            runtime_meta = dict(getter(normalized_task_id) or {})
        except Exception:
            return fallback
        raw = str(runtime_meta.get('task_temp_dir') or '').strip()
        if not raw:
            return fallback
        try:
            candidate = Path(raw).expanduser().resolve(strict=False)
        except Exception:
            return fallback
        return candidate if candidate.is_absolute() else fallback

    def _runtime_environment_payload(self, *, task=None) -> dict[str, Any]:
        project_environment = current_project_environment(
            shell_family=self._shell_family(),
            workspace_root=self._workspace_root(),
            process_cwd=self._process_cwd(),
        )
        task_temp_dir = str(self._task_temp_dir(getattr(task, 'task_id', None)))
        return {
            'os_family': self._os_family(),
            'shell_family': str(project_environment.get('shell_family') or self._shell_family()),
            'process_cwd': str(project_environment.get('process_cwd') or self._process_cwd()),
            'workspace_root': str(project_environment.get('workspace_root') or self._workspace_root()),
            'project_python': str(project_environment.get('project_python') or ''),
            'project_python_dir': str(project_environment.get('project_python_dir') or ''),
            'project_scripts_dir': str(project_environment.get('project_scripts_dir') or ''),
            'project_virtual_env': str(project_environment.get('project_virtual_env') or ''),
            'project_python_hint': str(project_environment.get('project_python_hint') or ''),
            'task_temp_dir': task_temp_dir,
            'path_policy': {
                'relative_paths_bind_to_workspace': False,
                'filesystem_requires_absolute_path': True,
                'content_requires_absolute_path': True,
                'exec_default_working_dir': 'task_temp_dir',
                'exec_requires_explicit_working_dir_for_target_dir': True,
            },
            'tool_guidance': {
                'filesystem': (
                    '使用绝对路径。默认把新建脚本、抓取结果、缓存、调试输出和其他中间文件写到 '
                    'runtime_environment.task_temp_dir；只有为了满足任务要求且只能写到其他目录时才允许例外。'
                ),
                'content': '单个内容体优先用 ref 导航或绝对文件路径；不要把它当成目录搜索工具。',
                'exec': (
                    'Windows 上的 exec 运行在 PowerShell 中，其他系统运行在宿主 shell 中。'
                    '未传 working_dir 时，默认在 runtime_environment.task_temp_dir 执行，并优先把所有中间文件写到该目录。'
                    '它会继承当前 G3KU 进程使用的同一套 Python 环境，并把该解释器注入 PATH。'
                    '不要假设 bash heredoc、rg 或 `true` 这类 Unix shell 内建一定可用。'
                    '需要特定目录时，显式传入 working_dir。'
                    f"当解释器选择必须精确一致时，优先使用 `{project_environment.get('project_python_hint') or 'python'}`，"
                    '不要假设裸 `python` 一定会解析到正确解释器。'
                ),
            },
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
        async with self._spawn_operation_lock(parent_node_id):
            return await self._spawn_children_locked(
                task_id=task_id,
                parent_node_id=parent_node_id,
                specs=specs,
                call_id=call_id,
            )

    async def _spawn_children_locked(
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
        refusal_supplier = self.governance_spawn_refusal_supplier
        if callable(refusal_supplier):
            try:
                refusal_text = str(
                    refusal_supplier(task_id=task_id, parent_node_id=parent_node_id, specs=list(specs or [])) or ''
                ).strip()
            except Exception:
                refusal_text = ''
            if refusal_text:
                results: list[SpawnChildResult] = []
                for index, spec in enumerate(list(specs or [])):
                    results.append(
                        self._governance_refusal_result(
                            spec.goal,
                            refusal_text,
                            brief=index > 0,
                        )
                    )
                return results
        if not parent.can_spawn_children:
            raise ValueError('spawn_child_nodes is not available for this node')
        self._log_service.mark_execution_stage_contains_spawn(task.task_id, parent.node_id)
        cache_key = str(call_id or f'call:{len(specs)}')
        await self._settle_superseded_spawn_operations(
            task=task,
            parent=parent,
            exclude_cache_key=cache_key,
            replacement_round_id=cache_key,
        )
        parent = self._store.get_node(parent_node_id) or parent
        cached = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
        if isinstance(cached, dict) and cached.get('completed'):
            return self._spawn_round_results_from_entries(
                task_id=task.task_id,
                entries=[dict(item) for item in list(cached.get('entries') or []) if isinstance(item, dict)],
                specs=specs,
            )
        reused_results = self._completed_successful_spawn_results(
            parent=parent,
            specs=specs,
            exclude_cache_key=cache_key,
        )
        if reused_results is not None:
            return reused_results

        cached_payload = copy.deepcopy(cached) if isinstance(cached, dict) else {
            'specs': [item.model_dump(mode='json') for item in specs],
            'entries': [],
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

        spawn_review = await self._review_spawn_batch(
            task=task,
            parent=parent,
            specs=specs,
            cache_key=cache_key,
        )
        allowed_indexes = self._apply_spawn_review_results(
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            cache_key=cache_key,
            cached_payload=cached_payload,
            specs=specs,
            spawn_review=spawn_review,
        )

        if allowed_indexes:
            await self._admit_spawn_batch(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                spec_count=len(allowed_indexes),
            )
        self._materialize_spawn_batch_children(
            task=task,
            parent=parent,
            specs=specs,
            allowed_indexes=allowed_indexes,
            cache_key=cache_key,
            cached_payload=cached_payload,
        )

        semaphore = asyncio.Semaphore(
            self._parallel_slot_count(
                self._max_parallel_child_pipelines_for(parent),
                len(specs),
                enabled=self._parallel_child_pipelines_enabled,
            )
        )

        async def _run_spec(index: int, spec: SpawnChildSpec) -> SpawnChildResult:
            async with semaphore:
                try:
                    return await self._run_child_pipeline(
                        task=task,
                        parent=parent,
                        spec=spec,
                        cache_key=cache_key,
                        cached_payload=cached_payload,
                        index=index,
                    )
                except TaskPausedError:
                    raise
                except asyncio.CancelledError as exc:
                    if self._should_propagate_child_pipeline_cancellation(
                        task_id=task.task_id,
                        parent_node_id=parent.node_id,
                    ):
                        raise
                    error_text = self._spawn_exception_text(exc)
                    result = self._spawn_runtime_result(spec.goal, error_text=error_text)
                    self._update_spawn_entry(
                        task_id=task.task_id,
                        parent_node_id=parent.node_id,
                        cache_key=cache_key,
                        cached_payload=cached_payload,
                        index=index,
                        status='error',
                        finished_at=_now(),
                        check_status='failed',
                        result=result.model_dump(mode='json', exclude_none=True),
                    )
                    return result
                except Exception as exc:
                    error_text = self._spawn_exception_text(exc)
                    result = self._spawn_runtime_result(spec.goal, error_text=error_text)
                    self._update_spawn_entry(
                        task_id=task.task_id,
                        parent_node_id=parent.node_id,
                        cache_key=cache_key,
                        cached_payload=cached_payload,
                        index=index,
                        status='error',
                        finished_at=_now(),
                        check_status='failed',
                        result=result.model_dump(mode='json', exclude_none=True),
                    )
                    return result

        await asyncio.gather(*[_run_spec(index, spec) for index, spec in enumerate(specs)])
        cached_payload['completed'] = True
        self._save_spawn_cache(task.task_id, parent.node_id, cache_key, cached_payload)
        latest_parent = self._store.get_node(parent.node_id) or parent
        latest_payload = dict((latest_parent.metadata or {}).get('spawn_operations') or {}).get(cache_key) or cached_payload
        return self._spawn_round_results_from_entries(
            task_id=task.task_id,
            entries=[dict(item) for item in list(latest_payload.get('entries') or []) if isinstance(item, dict)],
            specs=specs,
        )

    async def _review_spawn_batch(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        cache_key: str,
    ) -> dict[str, Any]:
        if not specs:
            return {
                'reviewed_at': _now(),
                'requested_specs': [],
                'allowed_indexes': [],
                'blocked_specs': [],
                'error_text': '',
            }
        backend = getattr(self._react_loop, '_chat_backend', None)
        if backend is None or not callable(getattr(backend, 'chat', None)):
            return self._default_spawn_review_result(
                specs=specs,
                reason='RuntimeError: spawn review model chain is unavailable',
            )
        messages = self._spawn_review_messages(
            task=task,
            parent=parent,
            specs=specs,
            cache_key=cache_key,
        )
        tools = [self._spawn_review_tool_schema()]
        model_refs = list(self._acceptance_model_refs or self._execution_model_refs)
        if not model_refs:
            return self._default_spawn_review_result(
                specs=specs,
                reason='RuntimeError: spawn review inspection model chain is empty',
            )
        invalid_response_count = 0
        while True:
            request_messages = (
                [
                    *messages,
                    {'role': 'user', 'content': self._spawn_review_repair_message(attempt_count=invalid_response_count)},
                ]
                if invalid_response_count > 0
                else list(messages)
            )
            try:
                response = await backend.chat(
                    messages=request_messages,
                    tools=tools,
                    model_refs=model_refs,
                )
            except Exception as exc:
                return self._default_spawn_review_result(
                    specs=specs,
                    reason=describe_exception(exc),
                )
            parsed = self._parse_spawn_review_response(response, spec_count=len(specs))
            if parsed is not None:
                return {
                    'reviewed_at': _now(),
                    'requested_specs': [self._spawn_review_requested_spec_payload(index=index, spec=spec) for index, spec in enumerate(specs)],
                    **parsed,
                }
            invalid_response_count += 1
            await asyncio.sleep(_SPAWN_REVIEW_RETRY_DELAY_SECONDS)

    @staticmethod
    def _spawn_review_tool_schema() -> dict[str, Any]:
        return {
            'type': 'function',
            'function': {
                'name': _SPAWN_REVIEW_TOOL_NAME,
                'description': '审查本次 spawn_child_nodes 请求，决定允许原样放行哪些 specs，并给出被拦截项的原因与建议。',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'allowed_indexes': {
                            'type': 'array',
                            'items': {'type': 'integer'},
                            'description': '允许原样放行的原始 spec 索引列表。',
                        },
                        'blocked_specs': {
                            'type': 'array',
                            'description': '被拦截的原始 spec 列表，每项都必须对应原始索引。',
                            'items': {
                                'type': 'object',
                                'properties': {
                                    'index': {'type': 'integer'},
                                    'reason': {'type': 'string'},
                                    'suggestion': {'type': 'string'},
                                },
                                'required': ['index', 'reason', 'suggestion'],
                                'additionalProperties': False,
                            },
                        },
                    },
                    'required': ['allowed_indexes', 'blocked_specs'],
                    'additionalProperties': False,
                },
            },
        }

    def _spawn_review_messages(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        cache_key: str,
    ) -> list[dict[str, Any]]:
        return [
            {'role': 'system', 'content': load_prompt('spawn_child_review.md').strip()},
            {
                'role': 'user',
                'content': json.dumps(
                    self._spawn_review_context(
                        task=task,
                        parent=parent,
                        specs=specs,
                        cache_key=cache_key,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ]

    def _spawn_review_context(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        cache_key: str,
    ) -> dict[str, Any]:
        root = self._store.get_node(task.root_node_id)
        return {
            'task_id': str(task.task_id or ''),
            'parent_node_id': str(parent.node_id or ''),
            'parent_goal': str(parent.goal or ''),
            'task_title': str(getattr(task, 'title', '') or ''),
            'user_request': str(getattr(task, 'user_request', '') or ''),
            'core_requirement': self._resolve_core_requirement(task),
            'root_prompt': str(getattr(root, 'prompt', '') or ''),
            'path_tree_text': self._spawn_review_path_tree_text(task_id=task.task_id, parent=parent),
            'spawn_request': {
                'call_id': str(cache_key or ''),
                'requested_specs': [
                    self._spawn_review_requested_spec_payload(index=index, spec=spec)
                    for index, spec in enumerate(specs)
                ],
            },
        }

    def _spawn_review_path_tree_text(self, *, task_id: str, parent: NodeRecord) -> str:
        path_nodes: list[NodeRecord] = []
        seen: set[str] = set()
        current: NodeRecord | None = parent
        while current is not None:
            node_id = str(current.node_id or '').strip()
            if not node_id or node_id in seen:
                break
            seen.add(node_id)
            path_nodes.append(current)
            parent_id = str(current.parent_node_id or '').strip()
            current = self._store.get_node(parent_id) if parent_id else None
        path_nodes.reverse()
        lines: list[str] = []
        for depth, node in enumerate(path_nodes):
            stage_goal = self._spawn_review_stage_goal(task_id=task_id, node=node)
            lines.append(f'{"  " * depth}- ({node.node_id},{node.status},{stage_goal})')
        return '\n'.join(lines) if lines else '(empty path)'

    def _spawn_review_stage_goal(self, *, task_id: str, node: NodeRecord) -> str:
        frame = self._log_service.read_runtime_frame(task_id, node.node_id) or {}
        stage_goal = str(frame.get('stage_goal') or '').strip()
        if stage_goal:
            return stage_goal
        snapshot = self._log_service.execution_stage_prompt_payload(task_id, node.node_id)
        active_stage = snapshot.get('active_stage') if isinstance(snapshot, dict) else None
        if isinstance(active_stage, dict):
            stage_goal = str(active_stage.get('stage_goal') or '').strip()
            if stage_goal:
                return stage_goal
        return '无阶段目标'

    @staticmethod
    def _spawn_review_requested_spec_payload(*, index: int, spec: SpawnChildSpec) -> dict[str, Any]:
        return {
            'index': int(index),
            'goal': str(spec.goal or ''),
            'prompt': str(spec.prompt or ''),
            'execution_policy': normalize_execution_policy_metadata(spec.execution_policy.model_dump(mode='json')).model_dump(mode='json'),
            'acceptance_prompt': str(spec.acceptance_prompt or ''),
            'requires_acceptance': bool(
                spec.requires_acceptance if spec.requires_acceptance is not None else bool(str(spec.acceptance_prompt or '').strip())
            ),
        }

    @classmethod
    def _parse_spawn_review_response(cls, response: Any, *, spec_count: int) -> dict[str, Any] | None:
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
            if name != _SPAWN_REVIEW_TOOL_NAME:
                continue
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    return None
            if not isinstance(arguments, dict):
                return None
            return cls._normalize_spawn_review_arguments(arguments, spec_count=spec_count)
        return cls._parse_spawn_review_content(getattr(response, 'content', None), spec_count=spec_count)

    @classmethod
    def _parse_spawn_review_content(cls, content: Any, *, spec_count: int) -> dict[str, Any] | None:
        text = str(content or '').strip()
        if not text:
            return None
        for candidate in cls._extract_json_object_candidates(text):
            try:
                payload = json.loads(candidate)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            normalized = cls._normalize_spawn_review_arguments(payload, spec_count=spec_count)
            if normalized is not None:
                return normalized
        return None

    @staticmethod
    def _extract_json_object_candidates(content: Any) -> list[str]:
        text = str(content or '')
        candidates: list[str] = []
        start_index: int | None = None
        depth = 0
        in_string = False
        escape = False
        for index, char in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif char == '\\':
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == '{':
                if depth == 0:
                    start_index = index
                depth += 1
                continue
            if char == '}' and depth > 0:
                depth -= 1
                if depth == 0 and start_index is not None:
                    candidates.append(text[start_index : index + 1])
                    start_index = None
        candidates.reverse()
        return candidates

    @staticmethod
    def _spawn_review_repair_message(*, attempt_count: int) -> str:
        return (
            f'{_SPAWN_REVIEW_REPAIR_PREFIX} '
            f'当前为第 {max(1, int(attempt_count) + 1)} 次修复尝试。'
            '不要输出解释性普通文本。'
            f'优先通过工具调用 `{_SPAWN_REVIEW_TOOL_NAME}` 返回；'
            '如果当前模型不支持工具调用，则只输出一个合法 JSON 对象，'
            '且必须严格包含 `allowed_indexes` 和 `blocked_specs` 两个字段。'
        )

    @classmethod
    def _normalize_spawn_review_arguments(cls, payload: dict[str, Any], *, spec_count: int) -> dict[str, Any] | None:
        raw_allowed = payload.get('allowed_indexes')
        raw_blocked = payload.get('blocked_specs')
        if not isinstance(raw_allowed, list) or not isinstance(raw_blocked, list):
            return None
        allowed_indexes: list[int] = []
        seen_allowed: set[int] = set()
        for item in raw_allowed:
            try:
                index = int(item)
            except (TypeError, ValueError):
                return None
            if index < 0 or index >= spec_count or index in seen_allowed:
                continue
            seen_allowed.add(index)
            allowed_indexes.append(index)
        blocked_by_index: dict[int, dict[str, Any]] = {}
        for item in raw_blocked:
            if not isinstance(item, dict):
                return None
            try:
                index = int(item.get('index'))
            except (TypeError, ValueError):
                return None
            if index < 0 or index >= spec_count or index in seen_allowed:
                continue
            blocked_by_index[index] = {
                'index': index,
                'reason': str(item.get('reason') or '').strip(),
                'suggestion': str(item.get('suggestion') or '').strip(),
            }
        blocked_specs: list[dict[str, Any]] = []
        for index in range(spec_count):
            if index in seen_allowed:
                continue
            blocked_payload = blocked_by_index.get(index) or {
                'index': index,
                'reason': _SPAWN_REVIEW_DEFAULT_BLOCK_REASON,
                'suggestion': _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
            }
            blocked_specs.append(
                {
                    'index': index,
                    'reason': str(blocked_payload.get('reason') or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_REASON,
                    'suggestion': str(blocked_payload.get('suggestion') or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
                }
            )
        return {
            'allowed_indexes': allowed_indexes,
            'blocked_specs': blocked_specs,
            'error_text': '',
        }

    def _default_spawn_review_result(self, *, specs: list[SpawnChildSpec], reason: str) -> dict[str, Any]:
        normalized_reason = str(reason or 'RuntimeError: spawn review failed').strip() or 'RuntimeError: spawn review failed'
        return {
            'reviewed_at': _now(),
            'requested_specs': [self._spawn_review_requested_spec_payload(index=index, spec=spec) for index, spec in enumerate(specs)],
            'allowed_indexes': [],
            'blocked_specs': [
                {
                    'index': index,
                    'reason': normalized_reason,
                    'suggestion': _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
                }
                for index, _spec in enumerate(specs)
            ],
            'error_text': normalized_reason,
        }

    def _apply_spawn_review_results(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        cached_payload: dict[str, Any],
        specs: list[SpawnChildSpec],
        spawn_review: dict[str, Any],
    ) -> list[int]:
        allowed_indexes = [
            int(item)
            for item in list(spawn_review.get('allowed_indexes') or [])
            if 0 <= int(item) < len(specs)
        ]
        allowed_set = set(allowed_indexes)
        blocked_by_index = {
            int(item.get('index') or 0): dict(item)
            for item in list(spawn_review.get('blocked_specs') or [])
            if isinstance(item, dict)
        }
        for index, spec in enumerate(list(specs or [])):
            if index in allowed_set:
                self._update_spawn_entry(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                    review_decision='allowed',
                    blocked_reason='',
                    blocked_suggestion='',
                    synthetic_result_summary='',
                    runtime_error_text='',
                )
                continue
            blocked_payload = blocked_by_index.get(index) or {
                'reason': _SPAWN_REVIEW_DEFAULT_BLOCK_REASON,
                'suggestion': _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
            }
            result = self._spawn_review_blocked_result(
                spec,
                reason=str(blocked_payload.get('reason') or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_REASON,
                suggestion=str(blocked_payload.get('suggestion') or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
            )
            self._update_spawn_entry(
                task_id=task_id,
                parent_node_id=parent_node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                status='success',
                started_at=str(spawn_review.get('reviewed_at') or _now()),
                finished_at=str(spawn_review.get('reviewed_at') or _now()),
                check_status='skipped',
                review_decision='blocked',
                blocked_reason=str(blocked_payload.get('reason') or '').strip(),
                blocked_suggestion=str(blocked_payload.get('suggestion') or '').strip(),
                synthetic_result_summary=str(result.node_output_summary or ''),
                runtime_error_text='',
            )
        cached_payload['spawn_review'] = {
            'round_id': str(cache_key or ''),
            'reviewed_at': str(spawn_review.get('reviewed_at') or _now()),
            'requested_specs': [dict(item) for item in list(spawn_review.get('requested_specs') or []) if isinstance(item, dict)],
            'allowed_indexes': list(allowed_indexes),
            'blocked_specs': [dict(item) for item in list(spawn_review.get('blocked_specs') or []) if isinstance(item, dict)],
            'error_text': str(spawn_review.get('error_text') or '').strip(),
        }
        self._save_spawn_cache(task_id, parent_node_id, cache_key, cached_payload)
        return allowed_indexes

    @staticmethod
    def _spawn_review_blocked_text(*, reason: str, suggestion: str) -> str:
        normalized_reason = str(reason or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_REASON
        normalized_suggestion = str(suggestion or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION
        return f'拦截原因：{normalized_reason}\n操作建议：{normalized_suggestion}'

    @classmethod
    def _spawn_review_blocked_result(cls, spec: SpawnChildSpec, *, reason: str, suggestion: str) -> SpawnChildResult:
        output_text = cls._spawn_review_blocked_text(reason=reason, suggestion=suggestion)
        return SpawnChildResult(
            goal=spec.goal,
            check_result=_SPAWN_REVIEW_BLOCKED_CHECK_RESULT,
            node_output=output_text,
            node_output_summary=output_text,
            node_output_ref='',
            failure_info=None,
        )

    @staticmethod
    def _spawn_review_blocked_text(*, reason: str, suggestion: str) -> str:
        normalized_reason = str(reason or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_REASON
        normalized_suggestion = str(suggestion or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION
        return f'派生已被拦截。原因：{normalized_reason}。建议：{normalized_suggestion}'

    @staticmethod
    def _spawn_review_blocked_summary(*, reason: str) -> str:
        normalized_reason = str(reason or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_REASON
        return f'派生拦截：{normalized_reason}'

    @classmethod
    def _spawn_review_blocked_result(cls, spec: SpawnChildSpec, *, reason: str, suggestion: str) -> SpawnChildResult:
        output_text = cls._spawn_review_blocked_text(reason=reason, suggestion=suggestion)
        summary_text = cls._spawn_review_blocked_summary(reason=reason)
        return SpawnChildResult(
            goal=spec.goal,
            check_result=_SPAWN_REVIEW_BLOCKED_CHECK_RESULT,
            node_output=output_text,
            node_output_summary=summary_text,
            node_output_ref='',
            failure_info=None,
        )

    @classmethod
    def _spawn_review_blocked_result_by_goal(cls, *, goal: str, reason: str, suggestion: str) -> SpawnChildResult:
        output_text = cls._spawn_review_blocked_text(reason=reason, suggestion=suggestion)
        summary_text = cls._spawn_review_blocked_summary(reason=reason)
        return SpawnChildResult(
            goal=str(goal or '').strip(),
            check_result=_SPAWN_REVIEW_BLOCKED_CHECK_RESULT,
            node_output=output_text,
            node_output_summary=summary_text,
            node_output_ref='',
            failure_info=None,
        )

    @staticmethod
    def _normalize_optional_limit(value: int | None | object, *, default: int | None) -> int | None:
        if value is _UNSET:
            value = default
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return max(0, int(value))

    @staticmethod
    def _parallel_slot_count(limit: int | None, item_count: int, *, enabled: bool) -> int:
        if not enabled or item_count <= 1:
            return 1
        if limit is None:
            return max(1, item_count)
        return max(1, int(limit) if int(limit) > 0 else 1)

    def _spawn_entry_node_ids(self, entry: dict[str, Any]) -> list[str]:
        node_ids: list[str] = []
        for field in ('child_node_id', 'acceptance_node_id'):
            node_id = str(entry.get(field) or '').strip()
            if node_id:
                node_ids.append(node_id)
        return node_ids

    def _spawn_entry_is_active(self, entry: dict[str, Any]) -> bool:
        status = str(entry.get('status') or '').strip().lower()
        if status in {'queued', 'running'}:
            return True
        for node_id in self._spawn_entry_node_ids(entry):
            node = self._store.get_node(node_id)
            if node is None:
                continue
            if self._normalized_status(getattr(node, 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}:
                return True
        return False

    def _active_prior_spawn_operations(
        self,
        *,
        parent: NodeRecord,
        exclude_cache_key: str,
    ) -> list[tuple[str, dict[str, Any], list[int]]]:
        operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(operations, dict):
            return []
        active_operations: list[tuple[str, dict[str, Any], list[int]]] = []
        excluded = str(exclude_cache_key or '').strip()
        for cache_key, payload in operations.items():
            normalized_cache_key = str(cache_key or '').strip()
            if normalized_cache_key == excluded or not isinstance(payload, dict):
                continue
            payload_copy = copy.deepcopy(payload)
            active_indexes: list[int] = []
            for index, entry in enumerate(list(payload_copy.get('entries') or [])):
                if isinstance(entry, dict) and self._spawn_entry_is_active(entry):
                    active_indexes.append(index)
            if active_indexes:
                active_operations.append((normalized_cache_key, payload_copy, active_indexes))
        return active_operations

    def _collect_descendant_node_ids(self, root_node_ids: list[str] | set[str]) -> set[str]:
        pending = [str(node_id or '').strip() for node_id in list(root_node_ids or []) if str(node_id or '').strip()]
        collected: set[str] = set()
        while pending:
            node_id = pending.pop()
            if not node_id or node_id in collected:
                continue
            collected.add(node_id)
            for child in list(self._store.list_children(node_id) or []):
                child_id = str(child.node_id or '').strip()
                if child_id and child_id not in collected:
                    pending.append(child_id)
        return collected

    async def _cancel_spawn_subtrees(self, *, task_id: str, node_ids: set[str]) -> None:
        targets = sorted(str(node_id or '').strip() for node_id in list(node_ids or []) if str(node_id or '').strip())
        if not targets:
            return
        executor = self.cancel_node_subtree_executor
        if not callable(executor):
            return
        try:
            await executor(task_id, targets)
        except TaskPausedError:
            raise
        except asyncio.CancelledError:
            raise

    async def _wait_for_terminal_nodes(self, *, node_ids: set[str], timeout_seconds: float = 2.0) -> set[str]:
        pending = {
            str(node_id or '').strip()
            for node_id in list(node_ids or [])
            if str(node_id or '').strip()
            and self._normalized_status(getattr(self._store.get_node(str(node_id or '').strip()), 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}
        }
        if not pending:
            return set()
        deadline = asyncio.get_running_loop().time() + max(0.1, float(timeout_seconds or 0.0))
        while pending and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
            pending = {
                node_id
                for node_id in pending
                if self._normalized_status(getattr(self._store.get_node(node_id), 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}
            }
        return pending

    def _force_superseded_nodes_terminal(self, *, task_id: str, node_ids: set[str], reason_text: str) -> None:
        terminal_result = NodeFinalResult(
            status=STATUS_FAILED,
            delivery_status='blocked',
            summary=reason_text,
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=reason_text,
        )
        nodes: list[NodeRecord] = []
        for node_id in list(node_ids or []):
            node = self._store.get_node(str(node_id or '').strip())
            if node is None:
                continue
            nodes.append(node)
        nodes.sort(key=lambda item: (int(item.depth or 0), str(item.node_id or '')), reverse=True)
        for node in nodes:
            latest = self._store.get_node(node.node_id) or node
            if self._normalized_status(getattr(latest, 'status', '')) == STATUS_SUCCESS:
                continue
            self._mark_finished(task_id, latest.node_id, terminal_result)

    def _rebuild_spawn_entry_result(
        self,
        *,
        task_id: str,
        entry: dict[str, Any],
        fallback_goal: str,
        fallback_reason: str,
        error_source: str = 'runtime',
    ) -> tuple[SpawnChildResult, str, str, str | None]:
        goal = str(entry.get('goal') or fallback_goal or '').strip()
        review_decision = str(entry.get('review_decision') or '').strip().lower()
        if review_decision == 'blocked':
            blocked_reason = str(entry.get('blocked_reason') or '').strip()
            blocked_suggestion = str(entry.get('blocked_suggestion') or '').strip()
            result = self._spawn_review_blocked_result_by_goal(
                goal=goal,
                reason=blocked_reason,
                suggestion=blocked_suggestion,
            )
            return result, 'success', 'skipped', _SPAWN_REVIEW_BLOCKED_CHECK_RESULT
        runtime_error_text = str(entry.get('runtime_error_text') or '').strip()
        if runtime_error_text:
            result = self._spawn_runtime_result(goal, error_text=runtime_error_text)
            return result, 'error', 'failed', runtime_error_text
        requires_acceptance = bool(entry.get('requires_acceptance'))
        child_node_id = str(entry.get('child_node_id') or '').strip()
        acceptance_node_id = str(entry.get('acceptance_node_id') or '').strip()
        child = self._store.get_node(child_node_id) if child_node_id else None
        acceptance = self._store.get_node(acceptance_node_id) if acceptance_node_id else None
        child_summary = ''
        child_ref = ''
        if child is not None:
            child_handoff = self._child_handoff_payload(
                task_id=task_id,
                node=child,
                fallback_output='',
            )
            child_summary = str(child_handoff.get('summary') or '')
            child_ref = str(child_handoff.get('output_ref') or '')

        if requires_acceptance:
            if acceptance is not None and self._normalized_status(getattr(acceptance, 'status', '')) == STATUS_SUCCESS:
                acceptance_record = self._result_from_record(acceptance) if acceptance is not None else None
                check_result = str(
                    (acceptance_record.summary if acceptance_record is not None else '')
                    or acceptance.final_output
                    or acceptance.failure_reason
                    or SKIPPED_CHECK_RESULT
                ).strip() or SKIPPED_CHECK_RESULT
                return (
                    SpawnChildResult(
                        goal=goal,
                        check_result=check_result,
                        node_output=child_summary,
                        node_output_summary=child_summary,
                        node_output_ref=child_ref,
                    ),
                    'success',
                    'passed',
                    check_result,
                )
            if acceptance is not None:
                acceptance_record = self._result_from_record(acceptance) if acceptance is not None else None
                check_result = str(
                    (acceptance_record.summary if acceptance_record is not None else '')
                    or acceptance.final_output
                    or acceptance.failure_reason
                    or fallback_reason
                ).strip() or fallback_reason
                return (
                    SpawnChildResult(
                        goal=goal,
                        check_result=check_result,
                        node_output=child_summary,
                        node_output_summary=child_summary,
                        node_output_ref=child_ref,
                        failure_info=self._spawn_failure_info_from_node(
                            task_id=task_id,
                            node=acceptance,
                            fallback_output=check_result,
                            source='acceptance',
                        ),
                    ),
                    'error',
                    'failed',
                    check_result,
                )
            fallback_error = str(fallback_reason or self._spawn_exception_text(RuntimeError('acceptance node missing'))).strip()
            result = self._spawn_runtime_result(goal, error_text=fallback_error)
            return result, 'error', 'failed', fallback_error

        if child is not None and self._normalized_status(getattr(child, 'status', '')) == STATUS_SUCCESS:
            return (
                SpawnChildResult(
                    goal=goal,
                    check_result=SKIPPED_CHECK_RESULT,
                    node_output=child_summary,
                    node_output_summary=child_summary,
                    node_output_ref=child_ref,
                ),
                'success',
                'skipped',
                SKIPPED_CHECK_RESULT,
            )
        if child is not None:
            return (
                SpawnChildResult(
                    goal=goal,
                    check_result=SKIPPED_CHECK_RESULT,
                    node_output=child_summary,
                    node_output_summary=child_summary,
                    node_output_ref=child_ref,
                    failure_info=self._spawn_failure_info_from_node(
                        task_id=task_id,
                        node=child,
                        fallback_output=fallback_reason,
                        source='execution',
                    ),
                ),
                'error',
                'skipped',
                SKIPPED_CHECK_RESULT,
            )
        fallback_error = str(fallback_reason or self._spawn_exception_text(RuntimeError('child node missing'))).strip()
        result = self._spawn_runtime_result(goal, error_text=fallback_error)
        return result, 'error', 'failed', fallback_error

    def _spawn_round_results_from_entries(
        self,
        *,
        task_id: str,
        entries: list[dict[str, Any]],
        specs: list[SpawnChildSpec] | None = None,
    ) -> list[SpawnChildResult]:
        specs_list = list(specs or [])
        results: list[SpawnChildResult] = []
        for index, entry in enumerate(list(entries or [])):
            if not isinstance(entry, dict):
                continue
            fallback_goal = str(entry.get('goal') or '').strip()
            if not fallback_goal and index < len(specs_list):
                fallback_goal = str(specs_list[index].goal or '').strip()
            result, _status, _check_status, _child_check_result = self._rebuild_spawn_entry_result(
                task_id=task_id,
                entry=entry,
                fallback_goal=fallback_goal,
                fallback_reason=self._spawn_exception_text(RuntimeError('child node missing')),
                error_source='runtime',
            )
            results.append(result)
        return results

    async def _settle_superseded_spawn_operations(
        self,
        *,
        task,
        parent: NodeRecord,
        exclude_cache_key: str,
        replacement_round_id: str,
    ) -> None:
        active_operations = self._active_prior_spawn_operations(parent=parent, exclude_cache_key=exclude_cache_key)
        if not active_operations:
            return
        reason_text = self._spawn_round_superseded_reason(replacement_round_id)
        active_root_node_ids: set[str] = set()
        preterminal_node_ids: set[str] = set()
        for _cache_key, payload, active_indexes in active_operations:
            for index in active_indexes:
                entries = list(payload.get('entries') or [])
                if index >= len(entries) or not isinstance(entries[index], dict):
                    continue
                entry = dict(entries[index] or {})
                for node_id in self._spawn_entry_node_ids(entry):
                    active_root_node_ids.add(node_id)
        subtree_node_ids = self._collect_descendant_node_ids(active_root_node_ids)
        for node_id in list(subtree_node_ids):
            node = self._store.get_node(node_id)
            if node is None:
                continue
            if self._normalized_status(getattr(node, 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}:
                preterminal_node_ids.add(node_id)

        await self._cancel_spawn_subtrees(task_id=task.task_id, node_ids=subtree_node_ids)
        await self._wait_for_terminal_nodes(node_ids=subtree_node_ids)
        self._force_superseded_nodes_terminal(
            task_id=task.task_id,
            node_ids=preterminal_node_ids,
            reason_text=reason_text,
        )

        for cache_key, payload, active_indexes in active_operations:
            specs = list(payload.get('specs') or [])
            for index in active_indexes:
                spec_payload = specs[index] if index < len(specs) and isinstance(specs[index], dict) else {}
                result, status, check_status, child_check_result = self._rebuild_spawn_entry_result(
                    task_id=task.task_id,
                    entry=dict((list(payload.get('entries') or []) or [])[index] or {}),
                    fallback_goal=str(spec_payload.get('goal') or ''),
                    fallback_reason=self._spawn_exception_text(RuntimeError(reason_text)),
                    error_source='runtime',
                )
                child_node_id = str(((list(payload.get('entries') or []) or [])[index] or {}).get('child_node_id') or '').strip()
                if child_node_id and child_check_result is not None:
                    self._log_service.update_node_check_result(task.task_id, child_node_id, child_check_result)
                self._update_spawn_entry(
                    task_id=task.task_id,
                    parent_node_id=parent.node_id,
                    cache_key=cache_key,
                    cached_payload=payload,
                    index=index,
                    status=status,
                    finished_at=_now(),
                    check_status=check_status,
                )
            payload['completed'] = not any(
                self._spawn_entry_is_active(item)
                for item in list(payload.get('entries') or [])
                if isinstance(item, dict)
            )
            self._save_spawn_cache(task.task_id, parent.node_id, cache_key, payload)

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
        if str(entry.get('status') or '').strip().lower() in {'success', 'error'}:
            existing_result = entry.get('result')
            if isinstance(existing_result, dict):
                try:
                    return SpawnChildResult.model_validate(existing_result)
                except Exception:
                    pass
            rebuilt_result, _status, _check_status, _child_check_result = self._rebuild_spawn_entry_result(
                task_id=task.task_id,
                entry=entry,
                fallback_goal=spec.goal,
                fallback_reason=self._spawn_exception_text(RuntimeError('child node missing')),
                error_source='runtime',
            )
            return rebuilt_result

        stop_reason = self._task_terminal_reason(task.task_id, task=task) or self._node_terminal_reason(
            self._store.get_node(parent.node_id) or parent,
            default_failed='parent node failed',
            default_success='parent node already completed',
        )
        if stop_reason:
            return self._spawn_abort_result(spec.goal, stop_reason)

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
                child = self._find_reusable_execution_child(
                    task=task,
                    parent=parent,
                    spec=spec,
                    exclude_node_ids=self._claimed_spawn_node_ids(entries=entries, field='child_node_id', skip_index=index),
                )
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

            child_result = await self._run_nested_node(task.task_id, child.node_id)
            child = self._store.get_node(child.node_id) or child
            child_handoff = self._child_handoff_payload(
                task_id=task.task_id,
                node=child,
                fallback_output=child_result.output,
            )
            child_summary = str(child_handoff.get('summary') or '')
            child_ref = str(child_handoff.get('output_ref') or '')

            if child_result.status != STATUS_SUCCESS:
                self._log_service.update_node_check_result(task.task_id, child.node_id, SKIPPED_CHECK_RESULT)
                result = SpawnChildResult(
                    goal=spec.goal,
                    check_result=SKIPPED_CHECK_RESULT,
                    node_output=child_summary,
                    node_output_summary=child_summary,
                    node_output_ref=child_ref,
                    failure_info=self._spawn_failure_info_from_node(
                        task_id=task.task_id,
                        node=child,
                        fallback_output=child_result.output,
                        source='execution',
                    ),
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
                    runtime_error_text='',
                )
                return result

            stop_reason = self._task_terminal_reason(task.task_id, task=task) or self._node_terminal_reason(
                self._store.get_node(parent.node_id) or parent,
                default_failed='parent node failed',
                default_success='parent node already completed',
            )
            if stop_reason:
                return self._spawn_abort_result(spec.goal, stop_reason)

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
                    runtime_error_text='',
                )
                return result

            acceptance_id = str(entry.get('acceptance_node_id') or '').strip()
            acceptance = self._store.get_node(acceptance_id) if acceptance_id else None
            if acceptance is None:
                await self._admit_spawn_expansion(
                    task_id=task.task_id,
                    parent_node_id=parent.node_id,
                    cache_key=cache_key,
                    index=index,
                    phase='acceptance',
                )
                acceptance_goal = f'accept:{spec.goal}'
                acceptance_prompt = str(spec.acceptance_prompt or '')
                acceptance = self._find_reusable_acceptance_node(
                    task=task,
                    accepted_node=child,
                    goal=acceptance_goal,
                    acceptance_prompt=acceptance_prompt,
                    parent_node_id=child.node_id,
                    exclude_node_ids=self._claimed_spawn_node_ids(entries=entries, field='acceptance_node_id', skip_index=index),
                )
                if acceptance is None:
                    acceptance = self.create_acceptance_node(
                        task=task,
                        accepted_node=child,
                        goal=acceptance_goal,
                        acceptance_prompt=acceptance_prompt,
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

            acceptance_result = await self._run_nested_node(task.task_id, acceptance.node_id)
            acceptance = self._store.get_node(acceptance.node_id) or acceptance
            check_result = str(acceptance_result.summary or acceptance_result.output or acceptance.failure_reason or '').strip() or SKIPPED_CHECK_RESULT
            self._log_service.update_node_check_result(task.task_id, child.node_id, check_result)
            result = SpawnChildResult(
                goal=spec.goal,
                check_result=check_result,
                node_output=child_summary,
                node_output_summary=child_summary,
                node_output_ref=child_ref,
                failure_info=(
                    None
                    if acceptance_result.status == STATUS_SUCCESS
                    else self._spawn_failure_info_from_node(
                        task_id=task.task_id,
                        node=acceptance,
                        fallback_output=acceptance_result.output,
                        source='acceptance',
                    )
                ),
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
                runtime_error_text='',
            )
            return result
        except TaskPausedError:
            raise
        except asyncio.CancelledError as exc:
            if self._should_propagate_child_pipeline_cancellation(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
            ):
                raise
            error_text = self._spawn_exception_text(exc)
            result = self._spawn_runtime_result(spec.goal, error_text=error_text)
            self._update_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                status='error',
                finished_at=_now(),
                check_status='failed',
                runtime_error_text=error_text,
            )
            return result
        except Exception as exc:
            error_text = self._spawn_exception_text(exc)
            result = self._spawn_runtime_result(spec.goal, error_text=error_text)
            self._update_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                status='error',
                finished_at=_now(),
                check_status='failed',
                runtime_error_text=error_text,
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
        review_decision = str(payload.get('review_decision') or '').strip().lower()
        if review_decision not in {'', 'allowed', 'blocked'}:
            review_decision = ''
        return {
            'index': index,
            'goal': spec.goal,
            'requires_acceptance': bool(payload.get('requires_acceptance')) if 'requires_acceptance' in payload else self._requires_acceptance(spec),
            'status': status,
            'started_at': str(payload.get('started_at') or ''),
            'finished_at': str(payload.get('finished_at') or ''),
            'child_node_id': str(payload.get('child_node_id') or ''),
            'acceptance_node_id': str(payload.get('acceptance_node_id') or ''),
            'check_status': check_status,
            'review_decision': review_decision,
            'blocked_reason': str(payload.get('blocked_reason') or ''),
            'blocked_suggestion': str(payload.get('blocked_suggestion') or ''),
            'synthetic_result_summary': str(payload.get('synthetic_result_summary') or ''),
            'runtime_error_text': str(payload.get('runtime_error_text') or ''),
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
        self._save_spawn_cache(task_id, parent_node_id, cache_key, cached_payload)
        return entry

    def _save_spawn_cache(self, task_id: str, parent_node_id: str, cache_key: str, payload: dict[str, Any]) -> None:
        task = self._store.get_task(task_id)
        parent = self._store.get_node(parent_node_id)
        if task is None or parent is None:
            return
        if self._task_terminal_reason(task_id, task=task):
            return
        if self._node_terminal_reason(parent, default_failed='parent node failed', default_success='parent node already completed'):
            return
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
        frame_exists = self._log_service.read_runtime_frame(task_id, parent_node_id) is not None
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

        for operation_index, (_operation_id, payload) in enumerate(spawn_operations.items()):
            if not isinstance(payload, dict):
                continue
            specs = [
                {
                    'goal': str(item.get('goal') or '').strip(),
                    'execution_policy': copy.deepcopy(item.get('execution_policy') or {}),
                    'requires_acceptance': bool(item.get('requires_acceptance')),
                }
                for item in list(payload.get('specs') or [])
                if isinstance(item, dict)
            ]
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

        child_pipelines.sort(key=lambda item: item.get('_sort_key') or ('', 0, 0))
        for item in child_pipelines:
            item.pop('_sort_key', None)
        has_active = any(str(item.get('status') or '').strip().lower() in {'queued', 'running'} for item in child_pipelines)
        return child_pipelines, pending_specs, [], has_active

    async def _admit_spawn_batch(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        spec_count: int,
    ) -> None:
        controller = self._adaptive_tool_budget_controller
        if controller is None:
            return
        lease = await controller.acquire_work_slot(
            task_id=task_id,
            node_id=parent_node_id,
            work_kind='spawn_child_nodes',
            work_id=f'{cache_key}:batch:{max(0, int(spec_count))}',
        )
        controller.release_work_slot(lease)

    def _materialize_spawn_batch_children(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        allowed_indexes: list[int],
        cache_key: str,
        cached_payload: dict[str, Any],
    ) -> None:
        stop_reason = self._task_terminal_reason(task.task_id, task=task) or self._node_terminal_reason(
            self._store.get_node(parent.node_id) or parent,
            default_failed='parent node failed',
            default_success='parent node already completed',
        )
        if stop_reason:
            return
        entries = list(cached_payload.get('entries') or [])
        allowed_set = {int(item) for item in list(allowed_indexes or [])}
        for index, spec in enumerate(list(specs or [])):
            if index >= len(entries):
                break
            if index not in allowed_set:
                continue
            entry = dict(entries[index] or {})
            child_id = str(entry.get('child_node_id') or '').strip()
            if child_id:
                continue
            child = self._find_reusable_execution_child(
                task=task,
                parent=parent,
                spec=spec,
                exclude_node_ids=self._claimed_spawn_node_ids(entries=entries, field='child_node_id', skip_index=index),
            )
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
            entries = list(cached_payload.get('entries') or [])

    async def _admit_spawn_expansion(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        index: int,
        phase: str,
    ) -> None:
        controller = self._adaptive_tool_budget_controller
        if controller is None:
            return
        lease = await controller.acquire_work_slot(
            task_id=task_id,
            node_id=parent_node_id,
            work_kind='spawn_child_nodes',
            work_id=f'{cache_key}:{phase}:{int(index)}',
        )
        controller.release_work_slot(lease)

    def _requires_acceptance(self, spec: SpawnChildSpec) -> bool:
        if spec.requires_acceptance is True:
            return True
        if spec.requires_acceptance is False:
            return False
        return bool(str(spec.acceptance_prompt or '').strip())

    @staticmethod
    def _claimed_spawn_node_ids(*, entries: list[dict[str, Any]], field: str, skip_index: int) -> set[str]:
        claimed: set[str] = set()
        for index, entry in enumerate(list(entries or [])):
            if index == int(skip_index):
                continue
            if not isinstance(entry, dict):
                continue
            node_id = str(entry.get(field) or '').strip()
            if node_id:
                claimed.add(node_id)
        return claimed

    def _find_reusable_execution_child(
        self,
        *,
        task,
        parent: NodeRecord,
        spec: SpawnChildSpec,
        exclude_node_ids: set[str],
    ) -> NodeRecord | None:
        expected_prompt = str(spec.prompt or '')
        expected_fingerprint = self._execution_child_recovery_fingerprint(
            parent_node_id=parent.node_id,
            goal=spec.goal,
            prompt=expected_prompt,
        )
        return self._find_reusable_node(
            parent_node_id=parent.node_id,
            node_kind=KIND_EXECUTION,
            expected_goal=spec.goal,
            expected_prompt=expected_prompt,
            expected_fingerprint=expected_fingerprint,
            exclude_node_ids=exclude_node_ids,
            metadata_filter=None,
        )

    def _find_reusable_acceptance_node(
        self,
        *,
        task,
        accepted_node: NodeRecord,
        goal: str,
        acceptance_prompt: str,
        parent_node_id: str,
        exclude_node_ids: set[str],
    ) -> NodeRecord | None:
        child_handoff = self._child_handoff_payload(
            task_id=task.task_id,
            node=accepted_node,
            fallback_output='',
        )
        expected_prompt = self._compose_acceptance_prompt(
            acceptance_prompt=acceptance_prompt,
            node_output=str(child_handoff.get('summary') or ''),
            node_output_ref=str(child_handoff.get('output_ref') or ''),
            result_payload_ref=str(child_handoff.get('result_payload_ref') or ''),
            evidence_summary=str(child_handoff.get('evidence_summary') or ''),
        )
        expected_fingerprint = self._acceptance_node_recovery_fingerprint(
            parent_node_id=parent_node_id,
            goal=goal,
            prompt=expected_prompt,
            accepted_node_id=accepted_node.node_id,
        )
        return self._find_reusable_node(
            parent_node_id=parent_node_id,
            node_kind=KIND_ACCEPTANCE,
            expected_goal=goal,
            expected_prompt=expected_prompt,
            expected_fingerprint=expected_fingerprint,
            exclude_node_ids=exclude_node_ids,
            metadata_filter={'accepted_node_id': accepted_node.node_id},
        )

    def _find_reusable_node(
        self,
        *,
        parent_node_id: str,
        node_kind: str,
        expected_goal: str,
        expected_prompt: str,
        expected_fingerprint: str,
        exclude_node_ids: set[str],
        metadata_filter: dict[str, str] | None,
    ) -> NodeRecord | None:
        candidates: list[NodeRecord] = []
        for node in list(self._store.list_children(parent_node_id) or []):
            if node.node_id in exclude_node_ids:
                continue
            if str(node.node_kind or '').strip().lower() != str(node_kind or '').strip().lower():
                continue
            if str(node.status or '').strip().lower() != STATUS_SUCCESS:
                continue
            metadata = dict(node.metadata or {})
            if metadata_filter:
                mismatch = False
                for key, expected in metadata_filter.items():
                    if str(metadata.get(key) or '').strip() != str(expected or '').strip():
                        mismatch = True
                        break
                if mismatch:
                    continue
            fingerprint = str(metadata.get(_RECOVERY_FINGERPRINT_KEY) or '').strip()
            if fingerprint:
                if fingerprint != expected_fingerprint:
                    continue
            else:
                if str(node.goal or '') != str(expected_goal or ''):
                    continue
                if str(node.prompt or '') != str(expected_prompt or ''):
                    continue
            candidates.append(node)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (str(item.updated_at or ''), str(item.created_at or ''), str(item.node_id or '')),
            reverse=True,
        )
        return candidates[0]

    @staticmethod
    def _recovery_fingerprint(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _execution_child_recovery_fingerprint(self, *, parent_node_id: str, goal: str, prompt: str) -> str:
        return self._recovery_fingerprint(
            {
                'node_kind': KIND_EXECUTION,
                'parent_node_id': str(parent_node_id or '').strip(),
                'goal': str(goal or ''),
                'prompt': str(prompt or ''),
            }
        )

    def _acceptance_node_recovery_fingerprint(self, *, parent_node_id: str, goal: str, prompt: str, accepted_node_id: str) -> str:
        return self._recovery_fingerprint(
            {
                'node_kind': KIND_ACCEPTANCE,
                'parent_node_id': str(parent_node_id or '').strip(),
                'goal': str(goal or ''),
                'prompt': str(prompt or ''),
                'accepted_node_id': str(accepted_node_id or '').strip(),
            }
        )

    def _resolve_core_requirement(self, task) -> str:
        metadata = task.metadata if isinstance(getattr(task, 'metadata', None), dict) else {}
        return str(metadata.get('core_requirement') or getattr(task, 'user_request', '') or getattr(task, 'title', '') or '').strip()

    def _resolve_execution_policy(self, task, *, node: NodeRecord | None = None):
        task_metadata = task.metadata if isinstance(getattr(task, 'metadata', None), dict) else {}
        node_metadata = node.metadata if node is not None and isinstance(getattr(node, 'metadata', None), dict) else {}
        return normalize_execution_policy_metadata(
            node_metadata.get('execution_policy', task_metadata.get('execution_policy'))
        )

    def _create_execution_child(self, *, task, parent: NodeRecord, spec: SpawnChildSpec) -> NodeRecord:
        execution_policy = normalize_execution_policy_metadata(spec.execution_policy)
        child_prompt = str(spec.prompt or '')
        metadata = {
            'execution_policy': execution_policy.model_dump(mode='json'),
            _RECOVERY_FINGERPRINT_KEY: self._execution_child_recovery_fingerprint(
                parent_node_id=parent.node_id,
                goal=spec.goal,
                prompt=child_prompt,
            )
        }
        child = NodeRecord(
            node_id=new_node_id(),
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            root_node_id=parent.root_node_id,
            depth=parent.depth + 1,
            node_kind=KIND_EXECUTION,
            status='in_progress',
            goal=spec.goal,
            prompt=child_prompt,
            input=child_prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=(parent.depth + 1) < int(task.max_depth),
            created_at=_now(),
            updated_at=_now(),
            token_usage=TokenUsageSummary(tracked=bool(getattr(task.token_usage, 'tracked', False))),
            token_usage_by_model=[],
            metadata=metadata,
        )
        created = self._log_service.create_node(task.task_id, child)
        if callable(self.governance_child_created_observer):
            try:
                self.governance_child_created_observer(task_id=task.task_id, child_node=created)
            except Exception:
                pass
        return created

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
        execution_policy = self._resolve_execution_policy(task, node=accepted_node)
        child_handoff = self._child_handoff_payload(
            task_id=task.task_id,
            node=accepted_node,
            fallback_output='',
        )
        prompt = self._compose_acceptance_prompt(
            acceptance_prompt=str(acceptance_prompt or ''),
            node_output=str(child_handoff.get('summary') or ''),
            node_output_ref=str(child_handoff.get('output_ref') or ''),
            result_payload_ref=str(child_handoff.get('result_payload_ref') or ''),
            evidence_summary=str(child_handoff.get('evidence_summary') or ''),
        )
        base_metadata = {
            'accepted_node_id': accepted_node.node_id,
            'execution_policy': execution_policy.model_dump(mode='json'),
            _RECOVERY_FINGERPRINT_KEY: self._acceptance_node_recovery_fingerprint(
                parent_node_id=parent_node_id or accepted_node.node_id,
                goal=goal,
                prompt=prompt,
                accepted_node_id=accepted_node.node_id,
            ),
        }
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
            metadata={**base_metadata, **dict(metadata or {})},
        )
        return self._log_service.create_node(task.task_id, acceptance)

    def _compose_acceptance_prompt(
        self,
        *,
        acceptance_prompt: str,
        node_output: str,
        node_output_ref: str,
        result_payload_ref: str,
        evidence_summary: str,
    ) -> str:
        normalized_acceptance_prompt = str(acceptance_prompt or '').strip()
        prompt = (
            f'{normalized_acceptance_prompt}\n\n'
            f'子节点输出摘要：\n{node_output or "(empty)"}\n\n'
            f'子节点输出 ref：{node_output_ref or "(none)"}\n'
            f'子节点结果载荷 ref：{result_payload_ref or "(none)"}\n'
            f'子节点证据摘要：\n{evidence_summary or "(none)"}\n'
        )
        return prompt

    def _child_handoff_payload(self, *, task_id: str, node: NodeRecord, fallback_output: str) -> dict[str, str]:
        latest = self._log_service.ensure_node_output_externalized(task_id, node.node_id) or self._store.get_node(node.node_id) or node
        latest = self._log_service.ensure_node_result_payload_externalized(task_id, latest.node_id) or self._store.get_node(latest.node_id) or latest
        result_payload = normalize_result_payload((latest.metadata or {}).get('result_payload'))
        summary = str(getattr(latest, 'final_output', '') or fallback_output or getattr(latest, 'failure_reason', '') or '').strip()
        output_ref = str(getattr(latest, 'final_output_ref', '') or '').strip()
        result_payload_ref = str((latest.metadata or {}).get('result_payload_ref') or '').strip()
        evidence_summary = '\n'.join(f'- {line}' for line in list(result_payload.evidence_summary() if result_payload is not None else []))
        return {
            'summary': summary,
            'output_ref': output_ref,
            'result_payload_ref': result_payload_ref,
            'evidence_summary': evidence_summary,
        }

    def _spawn_failure_info_from_node(
        self,
        *,
        task_id: str,
        node: NodeRecord,
        fallback_output: str,
        source: str,
    ) -> SpawnChildFailureInfo:
        latest = self._log_service.ensure_node_output_externalized(task_id, node.node_id) or self._store.get_node(node.node_id) or node
        latest = self._log_service.ensure_node_result_payload_externalized(task_id, latest.node_id) or self._store.get_node(latest.node_id) or latest
        result_payload = normalize_result_payload((latest.metadata or {}).get('result_payload'))
        summary = str(getattr(latest, 'final_output', '') or fallback_output or getattr(latest, 'failure_reason', '') or '').strip()
        output_ref = str(getattr(latest, 'final_output_ref', '') or '').strip()
        result_payload_ref = str((latest.metadata or {}).get('result_payload_ref') or '').strip()
        delivery_status = 'blocked'
        blocking_reason = ''
        remaining_work: list[str] = []
        if result_payload is not None:
            summary = str(result_payload.summary or result_payload.output or summary or blocking_reason).strip()
            delivery_status = 'blocked' if str(result_payload.delivery_status or '').strip() == 'blocked' else 'final'
            blocking_reason = str(result_payload.blocking_reason or '').strip()
            remaining_work = [str(item).strip() for item in list(result_payload.remaining_work or []) if str(item).strip()]
        return SpawnChildFailureInfo(
            source=str(source or 'runtime').strip() or 'runtime',
            summary=summary,
            delivery_status=delivery_status,
            blocking_reason=blocking_reason,
            remaining_work=remaining_work,
            output_ref=output_ref,
            result_payload_ref=result_payload_ref,
        )

    @staticmethod
    def _runtime_spawn_failure_info(error_text: str) -> SpawnChildFailureInfo:
        text = str(error_text or '错误：子节点流水线失败').strip() or '错误：子节点流水线失败'
        return SpawnChildFailureInfo(
            source='runtime',
            summary=text,
            delivery_status='blocked',
            blocking_reason=text,
            remaining_work=[],
            output_ref='',
            result_payload_ref='',
        )

    async def _submit_next_stage(
        self,
        *,
        task_id: str,
        node_id: str,
        stage_goal: str,
        tool_round_budget: int,
        completed_stage_summary: str,
        key_refs: list[dict[str, Any]],
        final: bool,
    ) -> dict[str, Any]:
        stage = self._log_service.submit_next_stage(
            task_id,
            node_id,
            stage_goal=str(stage_goal or '').strip(),
            tool_round_budget=int(tool_round_budget or 0),
            completed_stage_summary=str(completed_stage_summary or '').strip(),
            key_refs=[dict(item) for item in list(key_refs or []) if isinstance(item, dict)],
            final=bool(final),
        )
        return {
            'stage_id': str(stage.get('stage_id') or ''),
            'stage_index': int(stage.get('stage_index') or 0),
            'stage_kind': str(stage.get('stage_kind') or 'normal'),
            'mode': str(stage.get('mode') or ''),
            'status': str(stage.get('status') or ''),
            'stage_goal': str(stage.get('stage_goal') or ''),
            'tool_round_budget': int(stage.get('tool_round_budget') or 0),
            'tool_rounds_used': int(stage.get('tool_rounds_used') or 0),
            'final_stage': bool(stage.get('final_stage') or False),
        }

    @staticmethod
    async def _submit_final_result(payload: dict[str, Any]) -> dict[str, Any]:
        return dict(payload or {})

    def _mark_finished(self, task_id: str, node_id: str, result: NodeFinalResult) -> NodeFinalResult:
        task = self._store.get_task(task_id)
        if (
            task is not None
            and str(task.root_node_id or '').strip() == str(node_id or '').strip()
            and result.status == STATUS_FAILED
        ):
            capture_retry_snapshot = getattr(self._log_service, 'capture_retry_resume_snapshot', None)
            if callable(capture_retry_snapshot):
                try:
                    capture_retry_snapshot(task_id, node_id, failure_reason=result.failure_text)
                except Exception:
                    pass
        status = STATUS_SUCCESS if result.status == STATUS_SUCCESS else STATUS_FAILED
        self._log_service.finalize_execution_stage(task_id, node_id, status=status)
        if status == STATUS_SUCCESS:
            clear_retry_snapshot = getattr(self._log_service, 'clear_retry_resume_snapshot', None)
            if callable(clear_retry_snapshot):
                try:
                    clear_retry_snapshot(task_id)
                except Exception:
                    pass
            self._log_service.remove_frame(task_id, node_id, publish_snapshot=False)
        self._persist_result_payload(task_id, node_id, result)
        self._log_service.update_node_status(
            task_id,
            node_id,
            status=status,
            final_output=result.output,
            failure_reason='' if status == STATUS_SUCCESS else result.failure_text,
        )
        self._log_service.ensure_node_result_payload_externalized(task_id, node_id)
        latest = self._store.get_node(node_id)
        return self._result_from_record(latest) if latest is not None else result

    def _mark_failed(self, task_id: str, node_id: str, *, reason: str) -> NodeFinalResult:
        text = str(reason or 'node failed').strip() or 'node failed'
        return self._mark_finished(
            task_id,
            node_id,
            NodeFinalResult(
                status=STATUS_FAILED,
                delivery_status='blocked',
                summary=text,
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason=text,
            ),
        )

    def _pause_requested(self, task_id: str) -> bool:
        task = self._store.get_task(task_id)
        if task is None:
            return False
        return bool(task.pause_requested) and not bool(task.cancel_requested)

    def _persist_result_payload(self, task_id: str, node_id: str, result: NodeFinalResult) -> None:
        payload = result.payload_dict()

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            metadata['result_schema_version'] = RESULT_SCHEMA_VERSION
            metadata['result_payload'] = payload
            return metadata

        self._log_service.update_node_metadata(node_id, _mutate)

    @staticmethod
    def _result_from_record(node: NodeRecord) -> NodeFinalResult:
        payload = normalize_result_payload((node.metadata or {}).get('result_payload'))
        if payload is not None and str(payload.status or '').strip().lower() == str(node.status or '').strip().lower():
            return payload
        status = STATUS_SUCCESS if node.status == STATUS_SUCCESS else STATUS_FAILED
        final_output = str(node.final_output or '').strip()
        failure_reason = str(node.failure_reason or '').strip()
        return NodeFinalResult(
            status=status,
            delivery_status=('final' if status == STATUS_FAILED and (final_output or str(node.check_result or '').strip()) else 'blocked') if status == STATUS_FAILED else 'final',
            summary=failure_reason or final_output or 'node finished',
            answer=final_output if status == STATUS_SUCCESS else final_output,
            evidence=[],
            remaining_work=[],
            blocking_reason=failure_reason if status == STATUS_FAILED else '',
        )


def _now() -> str:
    from main.protocol import now_iso

    return now_iso()
