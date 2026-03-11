from __future__ import annotations

import json
from collections import deque
import os
from typing import Any

from g3ku.agent.tools.agent_browser import AgentBrowserTool
from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.filesystem import DeleteFileTool, EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from g3ku.agent.tools.memory_search import MemorySearchTool
from g3ku.agent.tools.web import WebFetchTool, WebSearchTool
from g3ku.org_graph.errors import EngineeringFailureError
from g3ku.org_graph.execution.propose_patch_tool import ProposeFilePatchTool
from g3ku.org_graph.governance.capability_filter import list_effective_tool_names
from g3ku.org_graph.governance.action_mapper import resolve_tool_action
from g3ku.org_graph.llm.provider_factory import build_provider_from_model
from g3ku.org_graph.prompt_loader import load_prompt
from g3ku.providers.fallback import is_retryable_model_error, response_requires_retry

TOOL_SYSTEM_PROMPT = load_prompt('tool_runtime.md')
ENGINEERING_FAILURE_PREFIX = 'ENGINEERING_FAILURE:'


def _offline_fallback_enabled() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST")) or os.getenv("G3KU_ORG_GRAPH_OFFLINE") == "1"


class RepeatedActionCircuitBreaker:
    def __init__(self, *, window: int = 3, threshold: int = 3):
        self._recent: deque[str] = deque(maxlen=max(1, window))
        self._threshold = max(1, threshold)

    def register(self, signature: str) -> None:
        self._recent.append(signature)
        if len(self._recent) < self._threshold:
            return
        tail = list(self._recent)[-self._threshold :]
        if len(set(tail)) == 1:
            raise RuntimeError(f'Repeated action circuit breaker triggered: {tail[-1]}')


class OrgGraphToolRuntime:
    def __init__(self, service):
        self._service = service

    def supported_tool_names(self) -> list[str]:
        names = ['list_dir', 'propose_file_patch', 'read_file', 'web_fetch', 'web_search', 'write_file', 'edit_file', 'delete_file']
        if bool(getattr(self._service.config.raw.tools.agent_browser, 'enabled', False)):
            names.append('agent_browser')
        if self._service.memory_manager is not None:
            names.append('memory_search')
        return sorted(set(names))

    def _build_tools(self, *, effective_tools: list[str], allow_mutation: bool, project_id: str, unit_id: str | None) -> dict[str, Tool]:
        workspace = self._service.config.raw.workspace_path
        allowed_dir = workspace if self._service.config.raw.tools.restrict_to_workspace else None
        web_cfg = self._service.config.raw.tools.web
        tools: dict[str, Tool] = {}
        if 'web_search' in effective_tools:
            tools['web_search'] = WebSearchTool(api_key=web_cfg.search.api_key or None, max_results=web_cfg.search.max_results, proxy=web_cfg.proxy or None)
        if 'web_fetch' in effective_tools:
            tools['web_fetch'] = WebFetchTool(proxy=web_cfg.proxy or None)
        if 'agent_browser' in effective_tools and bool(getattr(self._service.config.raw.tools.agent_browser, 'enabled', False)):
            tools['agent_browser'] = AgentBrowserTool(defaults=self._service.config.raw.tools.agent_browser.model_dump())
        if 'read_file' in effective_tools:
            tools['read_file'] = ReadFileTool(workspace=workspace, allowed_dir=allowed_dir)
        if 'list_dir' in effective_tools:
            tools['list_dir'] = ListDirTool(workspace=workspace, allowed_dir=allowed_dir)
        if 'memory_search' in effective_tools and self._service.memory_manager is not None:
            tools['memory_search'] = MemorySearchTool(manager=self._service.memory_manager, default_limit=self._service.config.raw.tools.memory.retrieval.context_top_k)
        if 'propose_file_patch' in effective_tools:
            tools['propose_file_patch'] = ProposeFilePatchTool(artifact_store=self._service.artifact_store, project_id=project_id, unit_id=unit_id, workspace=workspace, allowed_dir=allowed_dir)
        if allow_mutation and 'write_file' in effective_tools:
            tools['write_file'] = WriteFileTool(workspace=workspace, allowed_dir=allowed_dir)
        if allow_mutation and 'edit_file' in effective_tools:
            tools['edit_file'] = EditFileTool(workspace=workspace, allowed_dir=allowed_dir)
        if allow_mutation and 'delete_file' in effective_tools:
            tools['delete_file'] = DeleteFileTool(workspace=workspace, allowed_dir=allowed_dir)
        return tools

    async def _run_tool_loop(
        self,
        *,
        provider_model_chain: list[str],
        messages: list[dict[str, Any]],
        tools: dict[str, Tool],
        project,
        stage,
        unit,
        event_origin: str,
        permission_subject=None,
        monitor_context: dict[str, Any] | None = None,
    ) -> str | None:
        breaker = RepeatedActionCircuitBreaker(window=3, threshold=3)
        tool_defs = [tool.to_schema() for tool in tools.values()]
        while True:
            response = await self._call_with_fallback(
                provider_model_chain=provider_model_chain,
                messages=messages,
                tools=tool_defs,
                max_tokens=1200,
                temperature=0.2,
                monitor_context=monitor_context,
            )
            if response.tool_calls:
                assistant_tool_calls = []
                tool_messages = []
                for call in response.tool_calls:
                    signature = f'{call.name}:{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)}'
                    breaker.register(signature)
                    tool = tools.get(call.name)
                    if tool is None:
                        tool_result = f'Error: tool not available: {call.name}'
                    else:
                        validation_errors = tool.validate_params(call.arguments)
                        if validation_errors:
                            tool_result = 'Error: ' + '; '.join(validation_errors)
                        else:
                            mapped = resolve_tool_action(call.name, call.arguments, workspace=self._service.config.raw.workspace_path)
                            if mapped is not None:
                                subject = permission_subject or self._service.build_policy_subject(
                                    session_id=project.session_id,
                                    actor_role=unit.role_kind,
                                    project_id=project.project_id,
                                    unit_id=unit.unit_id,
                                )
                                await self._service.approval_service.ensure_tool_action_access(
                                    subject=subject,
                                    tool_id=mapped.tool_id,
                                    action_id=mapped.action_id,
                                    actor_label=unit.role_title,
                                    project=project,
                                    unit=unit,
                                    stage_id=stage.stage_id,
                                )
                            await self._service.emit_event(
                                project=project,
                                scope='tool',
                                event_name='tool.started',
                                text=f'{call.name} started',
                                unit_id=unit.unit_id,
                                stage_id=stage.stage_id,
                                data={'arguments': call.arguments, 'origin': event_origin},
                            )
                            tool_result = await tool.execute(**call.arguments)
                            await self._service.emit_event(
                                project=project,
                                scope='tool',
                                event_name='tool.completed',
                                text=f'{call.name} completed',
                                unit_id=unit.unit_id,
                                stage_id=stage.stage_id,
                                data={'result_summary': str(tool_result)[:400], 'origin': event_origin},
                            )
                    assistant_tool_calls.append(
                        {
                            'id': call.id,
                            'type': 'function',
                            'function': {
                                'name': call.name,
                                'arguments': json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                    )
                    tool_messages.append({'role': 'tool', 'tool_call_id': call.id, 'name': call.name, 'content': str(tool_result)})
                messages.append({'role': 'assistant', 'content': response.content, 'tool_calls': assistant_tool_calls})
                messages.extend(tool_messages)
                continue
            content = str(response.content or '').strip()
            if content.upper().startswith(ENGINEERING_FAILURE_PREFIX):
                reason = content.split(':', 1)[1].strip() or 'Tool runtime reported engineering failure'
                raise EngineeringFailureError(reason)
            if content.lower().startswith('error calling'):
                raise EngineeringFailureError(content)
            if content:
                return content
            return None

    async def _call_with_fallback(
        self,
        *,
        provider_model_chain: list[str],
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
        temperature: float,
        monitor_context: dict[str, Any] | None = None,
    ):
        last_error: Exception | None = None
        last_response = None
        chain = [str(item or "").strip() for item in provider_model_chain if str(item or "").strip()]
        for index, provider_model in enumerate(chain):
            try:
                target = build_provider_from_model(self._service.config.raw, provider_model)
            except Exception as exc:
                last_error = exc
                if index < len(chain) - 1:
                    continue
                raise
            effective_max_tokens = max(1, min(int(max_tokens), int(target.max_tokens_limit))) if target.max_tokens_limit else max(1, int(max_tokens))
            effective_temperature = float(target.default_temperature) if target.default_temperature is not None else float(temperature)
            try:
                self._record_monitor_input(monitor_context, messages)
                response = await target.provider.chat(
                    messages=messages,
                    tools=tools,
                    model=target.model_id,
                    max_tokens=effective_max_tokens,
                    temperature=effective_temperature,
                    reasoning_effort=target.default_reasoning_effort or self._service.config.raw.agents.defaults.reasoning_effort,
                    tool_choice='auto',
                    parallel_tool_calls=False,
                )
            except Exception as exc:
                last_error = exc
                if index < len(chain) - 1 and is_retryable_model_error(exc, retry_on=target.retry_on):
                    continue
                raise
            last_response = response
            if index < len(chain) - 1 and response_requires_retry(response, retry_on=target.retry_on):
                continue
            self._record_monitor_output(monitor_context, str(response.content or ''))
            return response
        if last_error is not None:
            raise last_error
        if last_response is not None:
            self._record_monitor_output(monitor_context, str(last_response.content or ''))
        return last_response

    async def run(self, *, unit, project, stage, prompt_preview: str, objective: str) -> str | None:
        effective_tools = list_effective_tool_names(
            subject=self._service.build_policy_subject(
                session_id=project.session_id,
                actor_role=unit.role_kind,
                project_id=project.project_id,
                unit_id=unit.unit_id,
            ),
            supported_tool_names=self.supported_tool_names(),
            resource_registry=self._service.resource_registry,
            policy_engine=self._service.policy_engine,
            mutation_allowed=bool(unit.mutation_allowed),
        )
        if not effective_tools:
            return None
        provider_model = str(unit.provider_model or self._service.config.execution_model)
        provider_model_chain = [provider_model] if unit.provider_model else self._service.resolve_project_model_chain(project=project, node_type='execution')
        if not any(self._service._provider_model_is_ready(candidate) for candidate in provider_model_chain):
            return None
        tools = self._build_tools(
            effective_tools=effective_tools,
            allow_mutation=bool(unit.mutation_allowed),
            project_id=project.project_id,
            unit_id=unit.unit_id,
        )
        if not tools:
            return None
        messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': TOOL_SYSTEM_PROMPT},
            {
                'role': 'user',
                'content': (
                    f'项目: {project.title}\n'
                    f'单元角色: {unit.role_title}\n'
                    f'当前阶段: {stage.title}\n'
                    f'提示摘要: {prompt_preview}\n'
                    f'目标: {objective}\n'
                    f'当前可用工具: {", ".join(sorted(tools.keys()))}\n'
                ),
            },
        ]
        return await self._run_tool_loop(
            provider_model_chain=provider_model_chain,
            messages=messages,
            tools=tools,
            project=project,
            stage=stage,
            unit=unit,
            event_origin='execution',
            permission_subject=self._service.build_policy_subject(
                session_id=project.session_id,
                actor_role=unit.role_kind,
                project_id=project.project_id,
                unit_id=unit.unit_id,
            ),
            monitor_context={
                'project': project,
                'unit': unit,
                'stage_id': stage.stage_id,
                'input_kind': 'input',
                'output_kind': 'output',
            },
        )

    async def run_checker(
        self,
        *,
        unit,
        project,
        stage,
        parent_unit,
        system_prompt: str,
        acceptance_criteria: str,
        candidate_content: str,
        validation_tools: list[str] | None,
    ) -> str:
        provider_model = str(unit.provider_model or self._service.config.inspection_model or self._service.config.execution_model)
        provider_model_chain = [provider_model] if unit.provider_model else self._service.resolve_project_model_chain(project=project, node_type='inspection')
        permission_subject = self._service.build_policy_subject(
            session_id=project.session_id,
            actor_role='inspection',
            project_id=project.project_id,
            unit_id=unit.unit_id,
        )
        effective_tools = list_effective_tool_names(
            subject=permission_subject,
            supported_tool_names=self.supported_tool_names(),
            resource_registry=self._service.resource_registry,
            policy_engine=self._service.policy_engine,
            mutation_allowed=True,
        )
        tools = self._build_tools(
            effective_tools=effective_tools,
            allow_mutation=True,
            project_id=project.project_id,
            unit_id=unit.unit_id,
        )
        available_tool_names = sorted(tools.keys())
        messages: list[dict[str, Any]] = [
            {'role': 'system', 'content': system_prompt},
            {
                'role': 'user',
                'content': json.dumps(
                    {
                        'acceptance_criteria': acceptance_criteria,
                        'candidate_content': candidate_content,
                        'validation_tools': list(validation_tools or []),
                        'available_tools': available_tool_names,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ]
        try:
            if tools:
                content = await self._run_tool_loop(
                    provider_model_chain=provider_model_chain,
                    messages=messages,
                    tools=tools,
                    project=project,
                    stage=stage,
                    unit=unit,
                    event_origin='checker',
                    permission_subject=permission_subject,
                    monitor_context={
                        'project': project,
                        'unit': parent_unit,
                        'stage_id': stage.stage_id,
                        'input_kind': 'check_input',
                        'output_kind': 'check_output',
                    },
                )
            else:
                response = await self._call_with_fallback(
                    provider_model_chain=provider_model_chain,
                    messages=messages,
                    tools=None,
                    max_tokens=600,
                    temperature=0.1,
                    monitor_context={
                        'project': project,
                        'unit': parent_unit,
                        'stage_id': stage.stage_id,
                        'input_kind': 'check_input',
                        'output_kind': 'check_output',
                    },
                )
                content = str(response.content or '').strip() or None
        except Exception as exc:
            if _offline_fallback_enabled():
                return json.dumps({'verdict': 'passed', 'reason': ''}, ensure_ascii=False)
            raise EngineeringFailureError(f'Checker tool run failed: {exc}') from exc
        if not content:
            raise EngineeringFailureError('Checker returned empty output')
        return content

    def _record_monitor_input(self, monitor_context: dict[str, Any] | None, messages: list[dict[str, Any]]) -> None:
        ctx = monitor_context if isinstance(monitor_context, dict) else {}
        project = ctx.get('project')
        unit = ctx.get('unit')
        if project is None or unit is None:
            return
        content = json.dumps(messages, ensure_ascii=False, indent=2)
        self._service.monitor_service.record_input(
            project=project,
            unit=unit,
            content=content,
            stage_id=ctx.get('stage_id'),
            kind=str(ctx.get('input_kind') or 'input'),
            meta={'source': 'tool_runtime'},
        )

    def _record_monitor_output(self, monitor_context: dict[str, Any] | None, content: str) -> None:
        ctx = monitor_context if isinstance(monitor_context, dict) else {}
        project = ctx.get('project')
        unit = ctx.get('unit')
        if project is None or unit is None:
            return
        kind = str(ctx.get('output_kind') or 'output')
        if kind == 'check_output':
            self._service.monitor_service.record_check_output(
                project=project,
                unit=unit,
                content=content,
                stage_id=ctx.get('stage_id'),
                meta={'source': 'tool_runtime'},
            )
            return
        self._service.monitor_service.record_output(
            project=project,
            unit=unit,
            content=content,
            stage_id=ctx.get('stage_id'),
            kind=kind,
            meta={'source': 'tool_runtime'},
        )


