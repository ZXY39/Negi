from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from langchain.agents import create_agent
from langchain_core.tools import StructuredTool
from loguru import logger

try:
    from langgraph.errors import GraphRecursionError
except Exception:  # pragma: no cover - optional dependency fallback
    class GraphRecursionError(RuntimeError):
        pass

from g3ku.agent.tools.registry import ToolRegistry
from g3ku.integrations.langchain_runtime import extract_final_response
from g3ku.runtime.multi_agent.dynamic.category_resolver import CategoryResolver
from g3ku.runtime.multi_agent.dynamic.circuit_breaker import DynamicSubagentTerminationGuard
from g3ku.runtime.multi_agent.dynamic.model_chain import ModelChainExecutor
from g3ku.runtime.multi_agent.dynamic.prompt_builder import COMPLETION_PROMISE_TOKEN, DynamicPromptBuilder
from g3ku.runtime.multi_agent.dynamic.session_store import DynamicSubagentSessionStore
from g3ku.runtime.multi_agent.dynamic.tracing import TraceContext, trace_payload
from g3ku.runtime.multi_agent.dynamic.types import (
    DynamicSubagentRequest,
    DynamicSubagentResult,
    DynamicSubagentSessionRecord,
)


class DynamicSubagentController:
    def __init__(
        self,
        *,
        loop,
        session_store: DynamicSubagentSessionStore,
        category_resolver: CategoryResolver,
        prompt_builder: DynamicPromptBuilder,
        model_chain_executor: ModelChainExecutor,
        freeze_ttl_seconds: int = 86400,
        repeated_action_window: int = 3,
        repeated_action_threshold: int = 3,
        sync_subagent_timeout_seconds: int | None = None,
        max_browser_steps_per_subagent: int | None = None,
        browser_no_progress_threshold: int | None = None,
    ) -> None:
        self._loop = loop
        self.session_store = session_store
        self.category_resolver = category_resolver
        self.prompt_builder = prompt_builder
        self.model_chain_executor = model_chain_executor
        self.freeze_ttl_seconds = max(1, int(freeze_ttl_seconds or 1))
        self.repeated_action_window = max(1, int(repeated_action_window or 1))
        self.repeated_action_threshold = max(1, int(repeated_action_threshold or 1))
        cfg = getattr(loop, 'multi_agent_config', None)
        self.sync_subagent_timeout_seconds = max(
            30,
            int(sync_subagent_timeout_seconds or getattr(cfg, 'sync_subagent_timeout_seconds', 180) or 180),
        )
        self.max_browser_steps_per_subagent = max(
            3,
            int(max_browser_steps_per_subagent or getattr(cfg, 'max_browser_steps_per_subagent', 10) or 10),
        )
        self.browser_no_progress_threshold = max(
            1,
            int(browser_no_progress_threshold or getattr(cfg, 'browser_no_progress_threshold', 3) or 3),
        )
        self.background_pool = None

    @staticmethod
    def utcnow() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def allocate_session_id() -> str:
        return f"subagent:{uuid.uuid4().hex[:12]}"

    @staticmethod
    def allocate_task_id() -> str:
        return f"task:{uuid.uuid4().hex[:12]}"

    def set_background_pool(self, pool) -> None:
        self.background_pool = pool

    async def delegate_sync(
        self,
        *,
        request: DynamicSubagentRequest,
        channel: str,
        chat_id: str,
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> DynamicSubagentResult:
        return await self._spawn_and_execute(
            request=request,
            channel=channel,
            chat_id=chat_id,
            on_progress=on_progress,
            task_id=None,
            session_id=None,
            continuation_record=None,
            background=False,
        )

    async def delegate_background(self, *, request: DynamicSubagentRequest):
        if self.background_pool is None:
            raise RuntimeError('Background pool is not initialized.')
        return await self.background_pool.launch(request)

    async def continue_session(
        self,
        *,
        parent_session_id: str,
        session_id: str,
        prompt: str,
        channel: str,
        chat_id: str,
        on_progress: Callable[..., Awaitable[None]] | None = None,
        destroy_after_accept: bool | None = None,
        background: bool = False,
        task_id: str | None = None,
    ) -> DynamicSubagentResult:
        record = self.session_store.get(session_id)
        if record is None:
            raise RuntimeError(f'Dynamic subagent session not found: {session_id}')
        if record.parent_session_id != parent_session_id:
            raise RuntimeError('Continuation rejected because parent_session_id does not match the original task lineage.')
        if record.status == 'destroyed':
            raise RuntimeError('Continuation rejected because the dynamic subagent session is already destroyed.')
        metadata = dict(record.metadata or {})
        last_prompt = str(metadata.get('last_continuation_prompt') or '').strip()
        new_prompt = str(prompt or '').strip()
        if last_prompt and last_prompt == new_prompt:
            raise RuntimeError('Repeated continuation prompt circuit breaker triggered.')
        metadata['last_continuation_prompt'] = new_prompt
        request = DynamicSubagentRequest(
            parent_session_id=parent_session_id,
            category=record.category,
            prompt=new_prompt,
            load_skills=list(record.injected_skills),
            tools_allow=list(record.granted_tools),
            output_schema=metadata.get('output_schema'),
            run_mode='background' if background else 'sync',
            continue_session_id=session_id,
            action_rules=list(metadata.get('action_rules') or []),
            context_constraints=list(metadata.get('context_constraints') or []),
            metadata=metadata,
        )
        return await self._spawn_and_execute(
            request=request,
            channel=channel,
            chat_id=chat_id,
            on_progress=on_progress,
            task_id=task_id,
            session_id=session_id,
            continuation_record=record,
            background=background,
            destroy_after_accept=destroy_after_accept,
        )

    async def run_background_job(self, *, task_id: str, request: DynamicSubagentRequest, session_id: str | None = None) -> DynamicSubagentResult:
        return await self._spawn_and_execute(
            request=request,
            channel=request.metadata.get('origin_channel') or 'web',
            chat_id=request.metadata.get('origin_chat_id') or request.parent_session_id,
            on_progress=None,
            task_id=task_id,
            session_id=session_id,
            continuation_record=self.session_store.get(session_id) if session_id else None,
            background=True,
        )

    async def _spawn_and_execute(
        self,
        *,
        request: DynamicSubagentRequest,
        channel: str,
        chat_id: str,
        on_progress: Callable[..., Awaitable[None]] | None,
        task_id: str | None,
        session_id: str | None,
        continuation_record: DynamicSubagentSessionRecord | None,
        background: bool,
        destroy_after_accept: bool | None = None,
    ) -> DynamicSubagentResult:
        spec = self.category_resolver.resolve(request)
        run_mode = 'background' if background else 'sync'
        session_id = session_id or self.allocate_session_id()
        trace = TraceContext(
            parent_session_id=request.parent_session_id,
            current_session_id=session_id,
            task_id=task_id,
            category=spec.name,
            run_mode=run_mode,
        )
        await self._emit_progress(on_progress, 'Dynamic subagent spawning', 'analysis', trace_payload(trace, lifecycle_status='pending'))
        prompt, system_fingerprint, injected_skills = self.prompt_builder.build(
            request=request,
            spec=spec,
            continuation_record=continuation_record,
        )
        created_at = continuation_record.created_at if continuation_record is not None else self.utcnow()
        metadata = dict(request.metadata or {})
        metadata.update(
            {
                'context_constraints': list(request.context_constraints),
                'action_rules': list(request.action_rules),
                'output_schema': request.output_schema,
                'origin_channel': channel,
                'origin_chat_id': chat_id,
                'max_iterations': spec.max_iterations,
                'sync_timeout_seconds': metadata.get('sync_timeout_seconds') or self.sync_subagent_timeout_seconds,
                'max_browser_steps': metadata.get('max_browser_steps') or self.max_browser_steps_per_subagent,
                'browser_no_progress_threshold': metadata.get('browser_no_progress_threshold') or self.browser_no_progress_threshold,
                'role_label': metadata.get('role_label') or self._role_label(spec),
                'delegated_prompt': str(request.prompt or '').strip(),
                'delegated_prompt_preview': _preview_text(request.prompt, limit=280),
                'system_prompt_preview': _preview_text(prompt, limit=1200),
                'granted_tools': list(spec.tools_allow),
                'injected_skills': list(injected_skills),
                'current_action': metadata.get('current_action') or 'Spawning subagent',
                'result_summary': metadata.get('result_summary') or '',
            }
        )
        record = DynamicSubagentSessionRecord(
            session_id=session_id,
            parent_session_id=request.parent_session_id,
            task_id=task_id,
            category=spec.name,
            status='pending',
            run_mode=run_mode,
            model_chain=list(spec.model_chain),
            granted_tools=list(spec.tools_allow),
            injected_skills=injected_skills,
            system_fingerprint=system_fingerprint,
            created_at=created_at,
            updated_at=self.utcnow(),
            last_anchor_index=continuation_record.last_anchor_index if continuation_record is not None else 0,
            last_result_summary=continuation_record.last_result_summary if continuation_record is not None else '',
            freeze_expires_at=continuation_record.freeze_expires_at if continuation_record is not None else None,
            destroy_after_accept=spec.destroy_after_sync if destroy_after_accept is None else bool(destroy_after_accept),
            metadata=metadata,
        )
        self.session_store.save(record)
        if background and self.background_pool is not None and task_id:
            self.background_pool.store.update(task_id, session_id=session_id, category=spec.name, status='injecting')
        self.session_store.update(session_id, status='injecting', metadata={**metadata, 'current_action': 'Injecting delegated context'})
        await self._emit_progress(
            on_progress,
            'Dynamic subagent injecting context',
            'analysis',
            trace_payload(trace, lifecycle_status='injecting', **self._trace_details(record, current_action='Injecting delegated context')),
        )

        async def _factory(model_client, provider_model: str):
            self.session_store.update(
                session_id,
                status='active',
                metadata={**metadata, 'active_provider_model': provider_model, 'current_action': 'Executing delegated task'},
            )
            if background and self.background_pool is not None and task_id:
                self.background_pool.store.update(task_id, status='running', category=spec.name, session_id=session_id)
            await self._emit_progress(
                on_progress,
                'Dynamic subagent active',
                'analysis',
                trace_payload(
                    trace,
                    lifecycle_status='active',
                    provider_model=provider_model,
                    **self._trace_details(record, current_action='Executing delegated task'),
                ),
            )
            return await self._invoke_dynamic_agent(
                record=self.session_store.get(session_id) or record,
                model_client=model_client,
                system_prompt=prompt,
                user_prompt=request.prompt,
                channel=channel,
                chat_id=chat_id,
                on_progress=on_progress,
                trace=trace,
            )

        try:
            output, anchor_index = await self.model_chain_executor.ainvoke_with_fallback(factory=_factory, model_chain=record.model_chain)
        except Exception as exc:
            failure_message = self._failure_message(exc)
            logger.exception(
                'Dynamic subagent failed parent_session_id={} current_session_id={} task_id={} category={}',
                request.parent_session_id,
                session_id,
                task_id,
                spec.name,
            )
            failure_metadata = {
                **metadata,
                'current_action': 'Dynamic subagent failed',
                'result_summary': failure_message,
                'last_error': failure_message,
                'failure_type': type(exc).__name__,
            }
            failed_record = self.session_store.update(
                session_id,
                status='failed',
                last_result_summary=failure_message[:240],
                metadata=failure_metadata,
            ) or record
            if background and self.background_pool is not None and task_id:
                self.background_pool.store.update(
                    task_id,
                    status='failed',
                    error=failure_message,
                    result_summary=failure_message[:240],
                    metadata=failure_metadata,
                )
            notice_writer = getattr(self._loop, 'record_session_notice', None)
            if callable(notice_writer):
                notice_writer(
                    request.parent_session_id,
                    source='dynamic_subagent',
                    level='error',
                    text=self._failure_notice_text(
                        category=spec.name,
                        session_id=session_id,
                        task_id=task_id,
                        error=failure_message,
                    ),
                    metadata={
                        'task_id': task_id,
                        'session_id': session_id,
                        'category': spec.name,
                        'status': 'failed',
                        'run_mode': run_mode,
                        'error': failure_message,
                    },
                )
            await self._emit_progress(
                on_progress,
                'Dynamic subagent failed',
                'analysis',
                trace_payload(
                    trace,
                    lifecycle_status='failed',
                    error=failure_message,
                    **self._trace_details(failed_record, current_action='Dynamic subagent failed', result_summary=failure_message),
                ),
            )
            return DynamicSubagentResult(
                session_id=session_id,
                task_id=task_id,
                parent_session_id=request.parent_session_id,
                category=spec.name,
                run_mode=run_mode,
                status='failed',
                ok=False,
                output=failure_message,
                error=failure_message,
                system_fingerprint=system_fingerprint,
                metadata=trace_payload(
                    trace,
                    lifecycle_status='failed',
                    error=failure_message,
                    failure_type=type(exc).__name__,
                    granted_tools=failed_record.granted_tools,
                    injected_skills=failed_record.injected_skills,
                ),
            )

        result_summary = _summarize_output(output)
        yielded = self.session_store.update(
            session_id,
            status='yielded',
            last_anchor_index=anchor_index,
            last_result_summary=result_summary,
            metadata={**metadata, 'current_action': 'Yielded result', 'result_summary': result_summary},
        ) or record
        await self._emit_progress(
            on_progress,
            'Dynamic subagent yielded result',
            'analysis',
            trace_payload(trace, lifecycle_status='yielded', **self._trace_details(yielded, current_action='Yielded result', result_summary=result_summary)),
        )

        if background:
            yielded = self.session_store.mark_frozen(session_id, ttl_seconds=self.freeze_ttl_seconds) or yielded
            if self.background_pool is not None and task_id:
                self.background_pool.store.update(task_id, status='completed', result_summary=_summarize_output(output), session_id=session_id)
        elif yielded.destroy_after_accept:
            yielded = self.session_store.update(session_id, status='destroyed') or yielded
        else:
            yielded = self.session_store.mark_frozen(session_id, ttl_seconds=self.freeze_ttl_seconds) or yielded

        trace.lifecycle_status = yielded.status
        await self._emit_progress(
            on_progress,
            f'Dynamic subagent {yielded.status}',
            'analysis',
            trace_payload(trace, **self._trace_details(yielded, current_action=f'Dynamic subagent {yielded.status}', result_summary=yielded.last_result_summary)),
        )
        return DynamicSubagentResult(
            session_id=session_id,
            task_id=task_id,
            parent_session_id=request.parent_session_id,
            category=spec.name,
            run_mode=run_mode,
            status=yielded.status,
            ok=True,
            output=output,
            error=None,
            system_fingerprint=system_fingerprint,
            metadata=trace_payload(trace, last_anchor_index=yielded.last_anchor_index, granted_tools=yielded.granted_tools, injected_skills=yielded.injected_skills),
        )

    async def _invoke_dynamic_agent(
        self,
        *,
        record: DynamicSubagentSessionRecord,
        model_client,
        system_prompt: str,
        user_prompt: str,
        channel: str,
        chat_id: str,
        on_progress: Callable[..., Awaitable[None]] | None,
        trace: TraceContext,
    ) -> tuple[str, int]:
        await self._loop._ensure_checkpointer_ready()
        guard = DynamicSubagentTerminationGuard(
            window=self.repeated_action_window,
            threshold=self.repeated_action_threshold,
            max_browser_steps=self._browser_step_budget(record),
            browser_no_progress_threshold=self._browser_no_progress_limit(record),
        )
        tools = self._build_worker_tools(record=record, channel=channel, chat_id=chat_id, on_progress=on_progress, guard=guard)
        agent = create_agent(
            model=model_client,
            tools=tools,
            checkpointer=self._loop._checkpointer,
            store=self._loop._store,
            name=f"dynamic_subagent_{record.session_id.replace(':', '_')}",
        )
        config: dict[str, Any] = {'recursion_limit': max(8, int((record.metadata or {}).get('max_iterations') or 8) * 2 + 4)}
        config['configurable'] = {'thread_id': record.session_id}
        timeout_s = self._sync_timeout_seconds(record)
        try:
            result = await asyncio.wait_for(
                agent.ainvoke(
                    {
                        'messages': [
                            {'role': 'system', 'content': system_prompt},
                            {'role': 'user', 'content': user_prompt},
                        ]
                    },
                    config=config,
                ),
                timeout=float(timeout_s),
            )
        except asyncio.TimeoutError:
            reason = f'Sync dynamic subagent timeout after {timeout_s} seconds'
            await self._emit_progress(on_progress, reason, 'analysis', trace_payload(trace, lifecycle_status='active', stop_reason='timeout'))
            return self._safe_stop_output(guard.build_fallback_output(reason=reason)), max(1, guard.summary_count)
        except GraphRecursionError:
            reason = f'LangGraph recursion limit reached ({config["recursion_limit"]}) before a stop condition'
            await self._emit_progress(on_progress, reason, 'analysis', trace_payload(trace, lifecycle_status='active', stop_reason='recursion_limit'))
            return self._safe_stop_output(guard.build_fallback_output(reason=reason)), max(1, guard.summary_count)

        result_messages = list(result.get('messages') or [])
        final = extract_final_response(result_messages)
        output = final.content if final and final.content else 'Dynamic subagent completed without a final response.'
        return self._safe_stop_output(output), len(result_messages)

    def _build_worker_tools(self, *, record: DynamicSubagentSessionRecord, channel: str, chat_id: str, on_progress, guard: DynamicSubagentTerminationGuard):
        tools = []
        for tool_name in record.granted_tools:
            tool = self._loop.tools.get(tool_name)
            if tool is None:
                continue
            args_schema = ToolRegistry._build_args_schema(tool)
            runtime_context = SimpleNamespace(
                channel=channel,
                chat_id=chat_id,
                message_id=None,
                on_progress=on_progress,
                session_key=record.session_id,
                iteration=1,
                trace_meta=trace_payload(
                    TraceContext(
                        parent_session_id=record.parent_session_id,
                        current_session_id=record.session_id,
                        task_id=record.task_id,
                        category=record.category,
                        run_mode=record.run_mode,
                    ),
                    lifecycle_status='active',
                    **self._trace_details(record, current_action='Executing delegated task'),
                ),
            )

            async def _invoke(__tool_name=tool_name, **kwargs: Any) -> str:
                guard_message = guard.before_tool(__tool_name, kwargs)
                if guard_message is not None:
                    return guard_message
                result = await self._loop.tool_bridge.execute_named_tool(
                    name=__tool_name,
                    arguments=kwargs,
                    tool_call_id=f"{__tool_name}:{uuid.uuid4().hex[:8]}",
                    runtime_context=runtime_context,
                    emit_progress=True,
                )
                content = str(getattr(result, 'content', ''))
                guard_message = guard.after_tool(__tool_name, kwargs, content)
                return guard_message or content

            tools.append(
                StructuredTool.from_function(
                    coroutine=_invoke,
                    name=tool_name,
                    description=tool.description,
                    args_schema=args_schema,
                    infer_schema=False,
                )
            )
        return tools

    async def _emit_progress(self, on_progress, text: str, event_kind: str, event_data: dict[str, Any]) -> None:
        if on_progress is None:
            return
        await on_progress(text, event_kind=event_kind, event_data=event_data)

    def _sync_timeout_seconds(self, record: DynamicSubagentSessionRecord) -> int:
        return max(30, int((record.metadata or {}).get('sync_timeout_seconds') or self.sync_subagent_timeout_seconds or 180))

    def _browser_step_budget(self, record: DynamicSubagentSessionRecord) -> int:
        return max(3, int((record.metadata or {}).get('max_browser_steps') or self.max_browser_steps_per_subagent or 10))

    def _browser_no_progress_limit(self, record: DynamicSubagentSessionRecord) -> int:
        return max(1, int((record.metadata or {}).get('browser_no_progress_threshold') or self.browser_no_progress_threshold or 3))

    @staticmethod
    def _safe_stop_output(output: str) -> str:
        text = str(output or '').replace(COMPLETION_PROMISE_TOKEN, '').strip()
        return text or 'Dynamic subagent stopped without a final answer.'

    @staticmethod
    def _role_label(spec) -> str:
        role_name = str(getattr(spec, 'name', '') or 'dynamic_worker').replace('_', ' ').strip()
        description = str(getattr(spec, 'description', '') or '').strip()
        return description or role_name or 'dynamic worker'

    @staticmethod
    def _failure_message(exc: Exception) -> str:
        text = ' '.join(str(exc or '').split()).strip()
        return text or exc.__class__.__name__ or 'Dynamic subagent failed.'

    @staticmethod
    def _failure_notice_text(*, category: str, session_id: str, task_id: str | None, error: str) -> str:
        task_part = f' task={task_id}' if task_id else ''
        return f'Dynamic subagent failure in category={category}{task_part} session={session_id}: {error}'

    @staticmethod
    def _trace_details(record: DynamicSubagentSessionRecord, *, current_action: str | None = None, result_summary: str | None = None) -> dict[str, Any]:
        def _keep(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, (list, dict, tuple, set)):
                return len(value) > 0
            return True

        metadata = dict(record.metadata or {})
        payload: dict[str, Any] = {
            'role_label': metadata.get('role_label') or record.category,
            'delegated_prompt_preview': metadata.get('delegated_prompt_preview') or _preview_text(metadata.get('delegated_prompt', ''), limit=280),
            'system_prompt_preview': metadata.get('system_prompt_preview') or '',
            'granted_tools': list(record.granted_tools),
            'injected_skills': list(record.injected_skills),
            'result_summary': result_summary if result_summary is not None else metadata.get('result_summary') or record.last_result_summary or '',
            'current_action': current_action if current_action is not None else metadata.get('current_action') or '',
        }
        return {key: value for key, value in payload.items() if _keep(value)}


def _summarize_output(output: str) -> str:
    text = ' '.join(str(output or '').split())
    return text[:240] + ('...' if len(text) > 240 else '')




def _preview_text(value: Any, *, limit: int = 240) -> str:
    text = ' '.join(str(value or '').split())
    if len(text) <= limit:
        return text
    return text[:limit] + '...'



