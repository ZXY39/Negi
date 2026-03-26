from __future__ import annotations

import asyncio
import copy
import json
import os
import platform
import re
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.runtime.memory_scope import normalize_memory_scope
from g3ku.runtime.project_environment import current_project_environment
from main.errors import TaskPausedError
from main.ids import new_node_id
from main.models import (
    NodeFinalResult,
    NodeRecord,
    RESULT_SCHEMA_VERSION,
    SpawnChildFailureInfo,
    SpawnChildResult,
    SpawnChildSpec,
    TokenUsageSummary,
    normalize_final_acceptance_metadata,
    normalize_result_payload,
)
from main.prompts import load_prompt
from main.runtime.internal_tools import SpawnChildNodesTool, SubmitNextStageTool
from main.runtime.stage_messages import build_execution_stage_result_block_message
from main.types import KIND_ACCEPTANCE, KIND_EXECUTION, STATUS_FAILED, STATUS_SUCCESS

SKIPPED_CHECK_RESULT = '未检验'
_RECOVERY_FINGERPRINT_KEY = 'recovery_fingerprint'
ACCEPTANCE_REF_GUIDANCE = (
    'If more detail is needed, use content.search first and then content.open for targeted reads. '
    'Do not request the full document body.'
)
ACCEPTANCE_EVIDENCE_CONSISTENCY_GUIDANCE = (
    'Evidence consistency check: when the child output or evidence notes cite concrete identifiers '
    'such as function, method, class, field, config key, CLI command, or search keyword names, '
    'verify that those identifiers actually appear in the cited file lines or a targeted reopened local slice. '
    'If the cited identifiers drift from the cited source, reject the deliverable with failed+final.'
)
CORE_REQUIREMENT_NOTICE_TEMPLATE = '注意：你正在完成的任务是核心需求【{core_requirement}】的细分任务之一，不要做与核心需求或以下细分任务无关的事。'
CORE_REQUIREMENT_NOTICE_PATTERN = re.compile(
    r'^注意：你正在完成的任务是核心需求【.*】的细分任务之一，不要做与核心需求或以下细分任务无关的事。$'
)


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
            return self._result_from_record(node)
        if self._pause_requested(task_id):
            self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            raise TaskPausedError(task_id)
        if task.cancel_requested:
            return self._mark_failed(task_id, node.node_id, reason='canceled')
        try:
            tools = self._build_tools(task=task, node=node)
            react_state = await self._resume_react_state(task=task, node=node)
            result = await self._react_loop.run(
                task=task,
                node=node,
                messages=list(react_state.get('messages') or []),
                tools=tools,
                model_refs=self._model_refs_for(node),
                runtime_context=self._runtime_context(task=task, node=node),
                max_iterations=self._max_iterations_for(node),
            )
            if self._pause_requested(task_id):
                self._mark_finished(task_id, node.node_id, result)
                self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
                raise TaskPausedError(task_id)
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
            return self._mark_failed(task_id, node.node_id, reason=str(exc))

    async def _resume_react_state(self, *, task, node: NodeRecord) -> dict[str, Any]:
        state = self._log_service.read_runtime_state(task.task_id) or {}
        for frame in list(state.get('frames') or []):
            if str(frame.get('node_id') or '') != node.node_id:
                continue
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

        candidate_text = self._latest_output_candidate_text(latest)
        if not str(candidate_text or '').strip():
            return None

        parsed = self._react_loop._parse_final_result(candidate_text)
        if parsed is None:
            return None

        stage_gate = self._react_loop._execution_stage_gate(
            task_id=task_id,
            node_id=node_id,
            node_kind=latest.node_kind,
        )
        if build_execution_stage_result_block_message(
            node_kind=latest.node_kind,
            stage_gate=stage_gate,
        ):
            return None

        result, raw_payload = parsed
        messages = self._runtime_frame_messages(task_id=task_id, node_id=node_id)
        has_tool_results = self._react_loop._has_tool_results(messages)
        if not has_tool_results:
            has_tool_results = any(list(getattr(entry, 'tool_calls', []) or []) for entry in list(latest.output or []))
        violations = self._react_loop._validate_final_result(
            result=result,
            raw_payload=raw_payload,
            has_tool_results=has_tool_results,
        )
        if violations:
            return None
        return self._mark_finished(task_id, node_id, result)

    def _latest_output_candidate_text(self, node: NodeRecord) -> str:
        for entry in reversed(list(node.output or [])):
            ref = str(getattr(entry, 'content_ref', '') or '').strip()
            if ref:
                resolved = self._resolve_content_ref(ref)
                if str(resolved or '').strip():
                    return str(resolved or '')
            text = str(getattr(entry, 'content', '') or '')
            if text.strip():
                return text

        detail_getter = getattr(self._store, 'get_task_node_detail', None)
        if callable(detail_getter):
            detail = detail_getter(node.node_id)
            if detail is not None:
                ref = str(getattr(detail, 'output_ref', '') or '').strip()
                if ref:
                    resolved = self._resolve_content_ref(ref)
                    if str(resolved or '').strip():
                        return str(resolved or '')
                text = str(getattr(detail, 'output_text', '') or '')
                if text.strip():
                    return text
        return ''

    def _resolve_content_ref(self, ref: str) -> str:
        content_store = getattr(self._log_service, '_content_store', None)
        resolver = getattr(content_store, '_resolve', None) if content_store is not None else None
        if not callable(resolver):
            return ''
        try:
            text, _handle = resolver(ref=ref, path=None)
        except Exception:
            return ''
        return str(text or '')

    def _runtime_frame_messages(self, *, task_id: str, node_id: str) -> list[dict[str, Any]]:
        state = self._log_service.read_runtime_state(task_id) or {}
        for frame in list(state.get('frames') or []):
            if str(frame.get('node_id') or '') != str(node_id or ''):
                continue
            messages = frame.get('messages')
            if isinstance(messages, list):
                return [item for item in messages if isinstance(item, dict)]
            return []
        return []

    def _build_tools(self, *, task, node: NodeRecord) -> dict[str, Tool]:
        tools = dict(self._tool_provider(node) or {})
        if node.node_kind in {KIND_EXECUTION, KIND_ACCEPTANCE}:
            tools['submit_next_stage'] = SubmitNextStageTool(
                lambda stage_goal, tool_round_budget: self._submit_next_stage(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    stage_goal=stage_goal,
                    tool_round_budget=tool_round_budget,
                )
            )
        else:
            tools.pop('submit_next_stage', None)
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
        payload: dict[str, Any] = {
            'task_id': task.task_id,
            'node_id': node.node_id,
            'node_kind': node.node_kind,
            'depth': node.depth,
            'can_spawn_children': bool(node.can_spawn_children),
            'goal': node.goal,
            'prompt': node.prompt,
            'core_requirement': self._resolve_core_requirement(task),
            'runtime_environment': self._runtime_environment_payload(),
        }
        if node.node_kind in {KIND_EXECUTION, KIND_ACCEPTANCE}:
            payload['execution_stage'] = self._execution_stage_payload(task=task, node=node)
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
        prompt = load_prompt(system_name).strip()
        environment_guidance = self._environment_context_guidance(node=node)
        return f'{prompt}\n\n{environment_guidance}'

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

    def _max_iterations_for(self, node: NodeRecord) -> int:
        return int(self._acceptance_max_iterations if node.node_kind == KIND_ACCEPTANCE else self._execution_max_iterations)

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
        }

    @staticmethod
    def _workspace_root() -> Path:
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

    def _runtime_environment_payload(self) -> dict[str, Any]:
        project_environment = current_project_environment(
            shell_family=self._shell_family(),
            workspace_root=self._workspace_root(),
            process_cwd=self._process_cwd(),
        )
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
            'path_policy': {
                'relative_paths_bind_to_workspace': False,
                'filesystem_requires_absolute_path': True,
                'content_requires_absolute_path': True,
                'exec_default_working_dir': 'process_cwd',
                'exec_requires_explicit_working_dir_for_target_dir': True,
            },
            'tool_guidance': {
                'filesystem': 'Use absolute paths. Prefer filesystem.search for recursive directory searches.',
                'content': 'Use ref navigation or absolute file paths for a single content body; do not expect directory search here.',
                'exec': (
                    'Exec runs in PowerShell on Windows and in the host shell elsewhere. '
                    'It inherits the same Python environment as the current G3KU process and injects '
                    'that interpreter onto PATH. Do not assume bash heredocs, rg, or Unix shell '
                    'builtins such as `true` are available. Pass an explicit working_dir when you '
                    f"need a specific directory. When exact interpreter choice matters, prefer `{project_environment.get('project_python_hint') or 'python'}` "
                    'instead of assuming bare `python` resolves correctly.'
                ),
            },
        }

    def _environment_context_guidance(self, *, node: NodeRecord) -> str:
        env = self._runtime_environment_payload()
        path_policy = env['path_policy']
        tool_guidance = env['tool_guidance']
        lines = [
            'Runtime environment:',
            f"- OS family: {env['os_family']}",
            f"- Shell family for `exec`: {env['shell_family']}",
            f"- Current process cwd: {env['process_cwd']}",
            f"- Workspace root: {env['workspace_root']}",
            f"- Project Python for exact `exec` calls: {env['project_python']}",
            f"- Project Python shell hint: {env['project_python_hint']}",
            '- Relative path policy: Do not assume relative paths bind to workspace. '
            f"`filesystem` absolute-only={str(path_policy['filesystem_requires_absolute_path']).lower()}, "
            f"`content` absolute-only={str(path_policy['content_requires_absolute_path']).lower()}, "
            f"`exec` default working_dir={path_policy['exec_default_working_dir']}.",
            '- Tool usage guidance:',
        ]
        if str(env.get('project_virtual_env') or '').strip():
            lines.append(f"- Active virtual environment: {env['project_virtual_env']}")
        lines.extend(
            [
                f"- `filesystem`: {tool_guidance['filesystem']}",
                f"- `content`: {tool_guidance['content']}",
                f"- `exec`: {tool_guidance['exec']}",
                '- If the real target project is outside the current workspace, use explicit absolute paths to that '
                'target instead of broad fallback searches inside the current repo.',
            ]
        )
        return '\n'.join(lines)

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
        self._log_service.mark_execution_stage_contains_spawn(task.task_id, parent.node_id)
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
        cached_payload['results'] = [item.model_dump(mode='json', exclude_none=True) for item in results]
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

            child_result = await self.run_node(task.task_id, child.node_id)
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
                    result=result.model_dump(mode='json', exclude_none=True),
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
                    result=result.model_dump(mode='json', exclude_none=True),
                )
                return result

            acceptance_id = str(entry.get('acceptance_node_id') or '').strip()
            acceptance = self._store.get_node(acceptance_id) if acceptance_id else None
            if acceptance is None:
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

            acceptance_result = await self.run_node(task.task_id, acceptance.node_id)
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
                result=result.model_dump(mode='json', exclude_none=True),
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
                failure_info=self._runtime_spawn_failure_info(f'Error: {exc}'),
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
                result=result.model_dump(mode='json', exclude_none=True),
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
        expected_prompt = self._inject_core_requirement_notice(spec.prompt, self._resolve_core_requirement(task))
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
            core_requirement=self._resolve_core_requirement(task),
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

    @staticmethod
    def _core_requirement_notice(core_requirement: str) -> str:
        return CORE_REQUIREMENT_NOTICE_TEMPLATE.format(core_requirement=str(core_requirement or '').strip())

    def _resolve_core_requirement(self, task) -> str:
        metadata = task.metadata if isinstance(getattr(task, 'metadata', None), dict) else {}
        return str(metadata.get('core_requirement') or getattr(task, 'user_request', '') or getattr(task, 'title', '') or '').strip()

    def _inject_core_requirement_notice(self, prompt: str, core_requirement: str) -> str:
        normalized_core_requirement = str(core_requirement or '').strip()
        base_lines = []
        for line in str(prompt or '').splitlines():
            if CORE_REQUIREMENT_NOTICE_PATTERN.fullmatch(str(line or '').strip()):
                continue
            base_lines.append(line)
        base_prompt = '\n'.join(base_lines).strip()
        if not normalized_core_requirement:
            return base_prompt
        notice = self._core_requirement_notice(normalized_core_requirement)
        if not base_prompt:
            return notice
        return f'{notice}\n\n{base_prompt}'

    def _create_execution_child(self, *, task, parent: NodeRecord, spec: SpawnChildSpec) -> NodeRecord:
        child_prompt = self._inject_core_requirement_notice(spec.prompt, self._resolve_core_requirement(task))
        metadata = {
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
            core_requirement=self._resolve_core_requirement(task),
        )
        base_metadata = {
            'accepted_node_id': accepted_node.node_id,
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
        core_requirement: str,
    ) -> str:
        prompt = (
            f'{acceptance_prompt}\n\n'
            f'Child node output summary:\n{node_output or "(empty)"}\n\n'
            f'Child node output ref: {node_output_ref or "(none)"}\n'
            f'Child node result payload ref: {result_payload_ref or "(none)"}\n'
            f'Child node evidence summary:\n{evidence_summary or "(none)"}\n'
        )
        if node_output_ref or result_payload_ref:
            prompt = f'{prompt}\n{ACCEPTANCE_REF_GUIDANCE}\n'
        prompt = f'{prompt}\n{ACCEPTANCE_EVIDENCE_CONSISTENCY_GUIDANCE}\n'
        return self._inject_core_requirement_notice(prompt, core_requirement)

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
        text = str(error_text or 'Error: child pipeline failed').strip() or 'Error: child pipeline failed'
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
    ) -> dict[str, Any]:
        stage = self._log_service.submit_next_stage(
            task_id,
            node_id,
            stage_goal=str(stage_goal or '').strip(),
            tool_round_budget=int(tool_round_budget or 0),
        )
        return {
            'stage_id': str(stage.get('stage_id') or ''),
            'stage_index': int(stage.get('stage_index') or 0),
            'mode': str(stage.get('mode') or ''),
            'status': str(stage.get('status') or ''),
            'stage_goal': str(stage.get('stage_goal') or ''),
            'tool_round_budget': int(stage.get('tool_round_budget') or 0),
            'tool_rounds_used': int(stage.get('tool_rounds_used') or 0),
        }

    def _mark_finished(self, task_id: str, node_id: str, result: NodeFinalResult) -> NodeFinalResult:
        status = STATUS_SUCCESS if result.status == STATUS_SUCCESS else STATUS_FAILED
        self._log_service.finalize_execution_stage(task_id, node_id, status=status)
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
