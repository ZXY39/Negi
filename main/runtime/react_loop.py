from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections import deque
from typing import Any

import json_repair

from g3ku.agent.tools.base import Tool
from g3ku.content import content_summary_and_ref, parse_content_envelope
from g3ku.runtime.tool_history import analyze_tool_call_history, extract_call_id
from g3ku.runtime.tool_watchdog import actor_role_allows_watchdog, run_tool_with_watchdog
from main.errors import TaskPausedError
from main.models import NodeEvidenceItem, NodeFinalResult, RESULT_SCHEMA_VERSION
from main.runtime.chat_backend import build_stable_prompt_cache_key
from main.runtime.stage_budget import (
    STAGE_TOOL_NAME,
    stage_gate_error_for_tool,
    visible_tools_for_stage_iteration,
)
from main.runtime.stage_messages import (
    build_execution_stage_overlay,
    build_execution_stage_result_block_message,
)
from main.protocol import now_iso

_ARTIFACT_REF_PATTERN = re.compile(r'artifact:artifact:[A-Za-z0-9_-]+')
_COMPACT_HISTORY_PREFIX = '[[G3KU_COMPACT_HISTORY_V1]]'
_COMPACT_HISTORY_MESSAGE_LIMIT = 30
_COMPACT_HISTORY_CHAR_LIMIT = 60_000
_COMPACT_HISTORY_KEEP_RECENT = 12
_COMPACT_HISTORY_MAX_STEPS = 12
_COMPACT_HISTORY_STEP_MAX_CHARS = 160
_ORPHAN_TOOL_RESULT_THRESHOLD = 3
_STAGE_SPAWN_TOOL_NAME = 'spawn_child_nodes'
_RESULT_REQUIRED_KEYS = (
    'status',
    'delivery_status',
    'summary',
    'answer',
    'evidence',
    'remaining_work',
    'blocking_reason',
)
_STAGE_BUDGET_NODE_KINDS = {'execution', 'acceptance'}


class RepeatedActionCircuitBreaker:
    def __init__(self, *, window: int = 3, threshold: int = 3) -> None:
        self._recent: deque[str] = deque(maxlen=max(1, int(window)))
        self._threshold = max(1, int(threshold))

    def register(self, signature: str) -> None:
        self._recent.append(signature)
        if len(self._recent) < self._threshold:
            return
        tail = list(self._recent)[-self._threshold :]
        if len(set(tail)) == 1:
            raise RuntimeError(f'repeated tool call detected: {tail[-1]}')


class ReActToolLoop:
    _CONTROL_TOOL_NAMES = {'wait_tool_execution', 'stop_tool_execution'}

    def __init__(
        self,
        *,
        chat_backend,
        log_service,
        max_iterations: int = 16,
        parallel_tool_calls_enabled: bool = True,
        max_parallel_tool_calls: int = 10,
    ) -> None:
        self._chat_backend = chat_backend
        self._log_service = log_service
        self._max_iterations = max(2, int(max_iterations or 16))
        self._parallel_tool_calls_enabled = bool(parallel_tool_calls_enabled)
        self._max_parallel_tool_calls = max(1, int(max_parallel_tool_calls or 1))

    async def run(
        self,
        *,
        task,
        node,
        messages: list[dict[str, Any]],
        tools: dict[str, Tool],
        model_refs: list[str],
        runtime_context: dict[str, Any],
        max_iterations: int | None = None,
    ) -> NodeFinalResult:
        breaker = RepeatedActionCircuitBreaker()
        limit = max(2, int(max_iterations or self._max_iterations))
        attempts = 0
        last_contract_violations: list[str] = []
        message_history = list(messages or [])
        orphan_tool_result_strikes = 0
        repair_overlay_text: str | None = None
        while attempts < limit:
            attempts += 1
            self._check_pause_or_cancel(task.task_id)
            stage_gate = self._execution_stage_gate(
                task_id=task.task_id,
                node_id=node.node_id,
                node_kind=node.node_kind,
            )
            visible_tools = self._visible_tools_for_iteration(
                tools=tools,
                node_kind=node.node_kind,
                stage_gate=stage_gate,
            )
            tool_schemas = [tool.to_schema() for tool in visible_tools.values()]
            model_messages = self._prepare_messages(message_history, runtime_context=runtime_context)
            overlay_parts = [
                build_execution_stage_overlay(node_kind=node.node_kind, stage_gate=stage_gate),
                repair_overlay_text,
            ]
            request_messages = self._apply_temporary_system_overlay(
                model_messages,
                overlay_text='\n\n'.join(str(part or '').strip() for part in overlay_parts if str(part or '').strip()),
            )
            repair_overlay_text = None
            allowed_content_refs = self._collect_content_refs(request_messages)
            self._log_service.update_node_input(task.task_id, node.node_id, json.dumps(model_messages, ensure_ascii=False, indent=2))
            tool_history = analyze_tool_call_history(request_messages)
            if tool_history.has_orphan_tool_results:
                orphan_tool_result_strikes += 1
                if orphan_tool_result_strikes >= _ORPHAN_TOOL_RESULT_THRESHOLD:
                    return self._orphan_tool_result_failure(
                        call_ids=tool_history.orphan_tool_result_ids,
                        strike_count=orphan_tool_result_strikes,
                    )
            self._log_service.upsert_frame(
                task.task_id,
                {
                    'node_id': node.node_id,
                    'depth': node.depth,
                    'node_kind': node.node_kind,
                    'phase': 'before_model',
                    'messages': message_history,
                    'pending_tool_calls': [],
                    'pending_child_specs': [],
                    'partial_child_results': [],
                    'tool_calls': [],
                    'child_pipelines': [],
                    **self._execution_stage_frame_payload(node_kind=node.node_kind, stage_gate=stage_gate),
                    'last_error': '',
                },
                publish_snapshot=True,
            )
            response = await self._chat_with_optional_extensions(
                messages=request_messages,
                tools=tool_schemas or None,
                model_refs=model_refs,
                max_tokens=1200,
                temperature=0.2,
                parallel_tool_calls=(self._parallel_tool_calls_enabled if tool_schemas else None),
                prompt_cache_key=self._execution_prompt_cache_key(
                    model_messages=model_messages,
                    tool_schemas=tool_schemas,
                    model_refs=model_refs,
                ),
            )
            response_tool_calls = list(response.tool_calls or [])
            tool_calls = [
                {'id': call.id, 'name': call.name, 'arguments': dict(call.arguments or {})}
                for call in response_tool_calls
            ]
            updated_node = self._log_service.append_node_output(
                task.task_id,
                node.node_id,
                content=str(response.content or ''),
                tool_calls=tool_calls,
                usage_attempts=list(response.attempts or []),
                model_messages=model_messages,
                request_message_count=getattr(response, 'request_message_count', None),
                request_message_chars=getattr(response, 'request_message_chars', None),
            )
            if response_tool_calls:
                control_only_turn = all(call.name in self._CONTROL_TOOL_NAMES for call in response_tool_calls)
                for call in response_tool_calls:
                    signature = f"{call.name}:{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"
                    if call.name not in self._CONTROL_TOOL_NAMES and call.name != STAGE_TOOL_NAME:
                        breaker.register(signature)
                if self._should_record_execution_stage_round(
                    node_kind=node.node_kind,
                    stage_gate=stage_gate,
                    response_tool_calls=tool_calls,
                ):
                    created_at = ''
                    if updated_node is not None and list(getattr(updated_node, 'output', []) or []):
                        created_at = str(updated_node.output[-1].created_at or '')
                    self._log_service.record_execution_stage_round(
                        task.task_id,
                        node.node_id,
                        tool_calls=tool_calls,
                        created_at=created_at or now_iso(),
                    )
                assistant_tool_calls = [
                    {
                        'id': call.id,
                        'type': 'function',
                        'function': {'name': call.name, 'arguments': json.dumps(call.arguments, ensure_ascii=False)},
                    }
                    for call in response_tool_calls
                ]
                live_tool_calls = [self._live_tool_entry(call) for call in response_tool_calls]
                self._log_service.update_frame(
                    task.task_id,
                    node.node_id,
                    lambda frame: {
                        **frame,
                        'depth': node.depth,
                        'node_kind': node.node_kind,
                        'phase': 'waiting_tool_results',
                        'messages': message_history,
                        'pending_tool_calls': tool_calls,
                        'tool_calls': live_tool_calls,
                        **self._execution_stage_frame_payload(
                            node_kind=node.node_kind,
                            stage_gate=self._execution_stage_gate(
                                task_id=task.task_id,
                                node_id=node.node_id,
                                node_kind=node.node_kind,
                            ),
                        ),
                        'last_error': '',
                    },
                    publish_snapshot=True,
                )
                results = await self._execute_tool_calls(
                    task=task,
                    node=node,
                    response_tool_calls=response_tool_calls,
                    tools=tools,
                    allowed_content_refs=allowed_content_refs,
                    runtime_context={
                        **runtime_context,
                        'stage_turn_granted': bool(
                            stage_gate.get('enabled')
                            and stage_gate.get('has_active_stage')
                            and not stage_gate.get('transition_required')
                        ),
                    },
                    prior_overflow_signatures=self._overflowed_search_signatures(message_history),
                )
                assistant_message = {
                    'role': 'assistant',
                    'content': self._externalize_message_content(
                        response.content,
                        runtime_context=runtime_context,
                        display_name=f'assistant:{node.node_id}',
                        source_kind='assistant_message',
                    ),
                    'tool_calls': assistant_tool_calls,
                }
                message_history.append(assistant_message)
                tool_messages = self._dedupe_tool_messages(
                    [item['tool_message'] for item in results],
                    existing_messages=message_history,
                )
                message_history.extend(tool_messages)
                prepared_history = self._prepare_messages(message_history, runtime_context=runtime_context)
                self._log_service.update_node_input(
                    task.task_id,
                    node.node_id,
                    json.dumps(prepared_history, ensure_ascii=False, indent=2),
                )
                self._log_service.update_frame(
                    task.task_id,
                    node.node_id,
                    lambda frame: {
                        **frame,
                        'depth': node.depth,
                        'node_kind': node.node_kind,
                        'phase': 'waiting_tool_results',
                        'messages': message_history,
                        'pending_tool_calls': [],
                        'tool_calls': [item['live_state'] for item in results],
                        **self._execution_stage_frame_payload(
                            node_kind=node.node_kind,
                            stage_gate=self._execution_stage_gate(
                                task_id=task.task_id,
                                node_id=node.node_id,
                                node_kind=node.node_kind,
                            ),
                        ),
                        'last_error': '',
                    },
                    publish_snapshot=True,
                )
                if control_only_turn:
                    attempts = max(0, attempts - 1)
                continue

            parsed = self._parse_final_result(str(response.content or ''))
            if parsed is not None:
                stage_block_message = build_execution_stage_result_block_message(
                    node_kind=node.node_kind,
                    stage_gate=stage_gate,
                )
                if stage_block_message:
                    message_history.append({'role': 'user', 'content': stage_block_message})
                    continue
                result, raw_payload = parsed
                contract_violations = self._validate_final_result(
                    result=result,
                    raw_payload=raw_payload,
                    has_tool_results=self._has_tool_results(message_history),
                )
                if not contract_violations:
                    self._log_service.remove_frame(task.task_id, node.node_id, publish_snapshot=True)
                    return result
                last_contract_violations = list(contract_violations)
                repair_overlay_text = self._result_contract_violation_message(
                    contract_violations,
                    node_kind=node.node_kind,
                )
                continue

            if str(response.finish_reason or '').strip().lower() == 'error':
                error_text = str(getattr(response, 'error_text', None) or response.content or 'model response failed').strip() or 'model response failed'
                raise RuntimeError(error_text)

            repair_overlay_text = self._result_protocol_message(node_kind=node.node_kind)

        if last_contract_violations:
            raise RuntimeError('result contract violation: ' + '; '.join(last_contract_violations))
        raise RuntimeError('node exceeded maximum ReAct iterations')

    def _execution_stage_gate(self, *, task_id: str, node_id: str, node_kind: str) -> dict[str, Any]:
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return {'enabled': False, 'has_active_stage': False, 'transition_required': False, 'active_stage': None}
        getter = getattr(self._log_service, 'execution_stage_gate_snapshot', None)
        if not callable(getter):
            return {'enabled': False, 'has_active_stage': False, 'transition_required': False, 'active_stage': None}
        payload = getter(task_id, node_id)
        if not isinstance(payload, dict):
            return {'enabled': False, 'has_active_stage': False, 'transition_required': False, 'active_stage': None}
        return {'enabled': True, **payload}

    @staticmethod
    def _visible_tools_for_iteration(*, tools: dict[str, Tool], node_kind: str, stage_gate: dict[str, Any]) -> dict[str, Tool]:
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return dict(tools or {})
        if not bool(stage_gate.get('enabled')):
            return dict(tools or {})
        return visible_tools_for_stage_iteration(
            tools,
            has_active_stage=bool(stage_gate.get('has_active_stage')),
            transition_required=bool(stage_gate.get('transition_required')),
            stage_tool_name=STAGE_TOOL_NAME,
        )

    @staticmethod
    def _execution_stage_frame_payload(*, node_kind: str, stage_gate: dict[str, Any]) -> dict[str, Any]:
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return {}
        if not bool(stage_gate.get('enabled')):
            return {}
        active = stage_gate.get('active_stage') if isinstance(stage_gate, dict) else None
        if not isinstance(active, dict):
            return {
                'stage_mode': '',
                'stage_status': '',
                'stage_goal': '',
                'stage_total_steps': 0,
            }
        return {
            'stage_mode': str(active.get('mode') or ''),
            'stage_status': str(active.get('status') or ''),
            'stage_goal': str(active.get('stage_goal') or ''),
            'stage_total_steps': int(active.get('tool_round_budget') or 0),
        }

    @staticmethod
    def _should_record_execution_stage_round(*, node_kind: str, stage_gate: dict[str, Any], response_tool_calls: list[dict[str, Any]]) -> bool:
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return False
        if not bool(stage_gate.get('enabled')):
            return False
        if not bool(stage_gate.get('has_active_stage')) or bool(stage_gate.get('transition_required')):
            return False
        names = [str(item.get('name') or '').strip() for item in list(response_tool_calls or []) if str(item.get('name') or '').strip()]
        return any(name != STAGE_TOOL_NAME for name in names)

    @staticmethod
    def _execution_prompt_cache_key(*, model_messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], model_refs: list[str]) -> str:
        return build_stable_prompt_cache_key(
            model_messages,
            tool_schemas or None,
            '|'.join(str(item or '').strip() for item in list(model_refs or []) if str(item or '').strip()),
        )

    async def _execute_tool_calls(
        self,
        *,
        task,
        node,
        response_tool_calls: list[Any],
        tools: dict[str, Tool],
        allowed_content_refs: list[str],
        runtime_context: dict[str, Any],
        prior_overflow_signatures: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if any(str(getattr(call, 'name', '') or '').strip() == STAGE_TOOL_NAME for call in list(response_tool_calls or [])) and len(list(response_tool_calls or [])) != 1:
            return [
                {
                    'index': index,
                    'live_state': {
                        'tool_call_id': str(call.id or ''),
                        'tool_name': str(call.name or 'tool'),
                        'status': 'error',
                        'started_at': '',
                        'finished_at': '',
                        'elapsed_seconds': None,
                    },
                    'tool_message': {
                        'role': 'tool',
                        'tool_call_id': call.id,
                        'name': call.name,
                        'content': 'Error: submit_next_stage must be the only tool call in its turn',
                        'started_at': '',
                        'finished_at': '',
                        'elapsed_seconds': None,
                    },
                }
                for index, call in enumerate(list(response_tool_calls or []))
            ]
        semaphore = asyncio.Semaphore(self._max_parallel_tool_calls if self._parallel_tool_calls_enabled else 1)

        async def _run_call(index: int, call: Any) -> dict[str, Any]:
            async with semaphore:
                self._check_pause_or_cancel(task.task_id)
                started_at = now_iso()
                started_monotonic = time.monotonic()
                self._update_tool_live_state(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    tool_call_id=call.id,
                    status='running',
                    started_at=started_at,
                    finished_at='',
                    elapsed_seconds=None,
                )
                try:
                    tool_content = await self._execute_tool(
                        tools=tools,
                        tool_name=call.name,
                        arguments=dict(call.arguments or {}),
                        runtime_context={
                            **runtime_context,
                            'current_tool_call_id': call.id,
                            'allowed_content_refs': allowed_content_refs,
                            'enforce_content_ref_allowlist': str(runtime_context.get('node_kind') or '').strip().lower() == 'acceptance',
                            'prior_overflow_signatures': sorted(prior_overflow_signatures or set()),
                        },
                    )
                except TaskPausedError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive fallback
                    tool_content = f'Error executing {call.name}: {exc}'
                finished_at = now_iso()
                elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
                status = self._tool_message_status(tool_content)
                self._update_tool_live_state(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    tool_call_id=call.id,
                    status=status,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=elapsed_seconds,
                )
                return {
                    'index': index,
                    'live_state': {
                        'tool_call_id': str(call.id or ''),
                        'tool_name': str(call.name or 'tool'),
                        'status': status,
                        'started_at': started_at,
                        'finished_at': finished_at,
                        'elapsed_seconds': elapsed_seconds,
                    },
                    'tool_message': {
                        'role': 'tool',
                        'tool_call_id': call.id,
                        'name': call.name,
                        'content': tool_content,
                        'started_at': started_at,
                        'finished_at': finished_at,
                        'elapsed_seconds': elapsed_seconds,
                    },
                }

        gathered = await asyncio.gather(*[_run_call(index, call) for index, call in enumerate(response_tool_calls)])
        return [item for item in sorted(gathered, key=lambda value: int(value['index']))]

    def _update_tool_live_state(
        self,
        *,
        task_id: str,
        node_id: str,
        tool_call_id: str,
        status: str,
        started_at: str,
        finished_at: str,
        elapsed_seconds: float | None,
    ) -> None:
        def _mutate(frame: dict[str, Any]) -> dict[str, Any]:
            next_calls: list[dict[str, Any]] = []
            matched = False
            for item in list(frame.get('tool_calls') or []):
                payload = dict(item or {})
                if str(payload.get('tool_call_id') or '') == str(tool_call_id or ''):
                    matched = True
                    payload.update(
                        {
                            'status': status,
                            'started_at': started_at or str(payload.get('started_at') or ''),
                            'finished_at': finished_at,
                            'elapsed_seconds': elapsed_seconds,
                        }
                    )
                next_calls.append(payload)
            if not matched:
                next_calls.append(
                    {
                        'tool_call_id': str(tool_call_id or ''),
                        'tool_name': 'tool',
                        'status': status,
                        'started_at': started_at,
                        'finished_at': finished_at,
                        'elapsed_seconds': elapsed_seconds,
                    }
                )
            frame['tool_calls'] = next_calls
            frame['phase'] = 'waiting_tool_results'
            return frame

        self._log_service.update_frame(task_id, node_id, _mutate, publish_snapshot=True)

    @staticmethod
    def _live_tool_entry(call: Any) -> dict[str, Any]:
        return {
            'tool_call_id': str(call.id or ''),
            'tool_name': str(call.name or 'tool'),
            'status': 'queued',
            'started_at': '',
            'finished_at': '',
            'elapsed_seconds': None,
        }

    @staticmethod
    def _tool_message_status(tool_content: str) -> str:
        text = str(tool_content or '').strip()
        return 'error' if text.startswith('Error') else 'success'

    async def _execute_tool(self, *, tools: dict[str, Tool], tool_name: str, arguments: dict[str, Any], runtime_context: dict[str, Any]) -> str:
        stage_gate_error = self._execution_tool_gate_error(tool_name=tool_name, runtime_context=runtime_context)
        if stage_gate_error:
            return f'Error: {stage_gate_error}'
        tool = tools.get(tool_name)
        if tool is None:
            return f'Error: tool not available: {tool_name}'
        search_signature = self._search_overflow_signature_for_call(tool_name=tool_name, arguments=arguments)
        prior_overflow_signatures = {
            str(item or '').strip()
            for item in list(runtime_context.get('prior_overflow_signatures') or [])
            if str(item or '').strip()
        }
        if search_signature and search_signature in prior_overflow_signatures:
            return 'Error: previous search overflowed; refine query before retrying'
        errors = tool.validate_params(arguments)
        if errors:
            return 'Error: ' + '; '.join(errors)
        execute_kwargs = dict(arguments)
        runtime_param_name = self._runtime_context_parameter_name(tool)
        if runtime_param_name is not None:
            execute_kwargs[runtime_param_name] = runtime_context
        if not actor_role_allows_watchdog(runtime_context):
            return await tool.execute(**execute_kwargs)
        outcome = await run_tool_with_watchdog(
            tool.execute(**execute_kwargs),
            tool_name=tool_name,
            arguments=arguments,
            runtime_context=runtime_context,
            snapshot_supplier=self._snapshot_supplier(runtime_context),
            manager=getattr(self, '_tool_execution_manager', None),
            on_poll=lambda _poll: self._on_tool_watchdog_poll(runtime_context),
        )
        result = outcome.value
        rendered = result if isinstance(result, str) else self._render_tool_result(result)
        return self._externalize_message_content(
            rendered,
            runtime_context=runtime_context,
            display_name=f'tool:{tool_name}',
            source_kind=f'tool_result:{tool_name}',
        )

    def _execution_tool_gate_error(self, *, tool_name: str, runtime_context: dict[str, Any]) -> str:
        node_kind = str(runtime_context.get('node_kind') or '').strip().lower()
        if node_kind not in _STAGE_BUDGET_NODE_KINDS:
            return ''
        normalized_tool_name = str(tool_name or '').strip()
        if normalized_tool_name in self._CONTROL_TOOL_NAMES or normalized_tool_name == STAGE_TOOL_NAME:
            return ''
        if bool(runtime_context.get('stage_turn_granted')):
            return ''
        stage_gate = self._execution_stage_gate(
            task_id=str(runtime_context.get('task_id') or ''),
            node_id=str(runtime_context.get('node_id') or ''),
            node_kind=node_kind,
        )
        if not bool(stage_gate.get('enabled')):
            return ''
        return stage_gate_error_for_tool(
            normalized_tool_name,
            has_active_stage=bool(stage_gate.get('has_active_stage')),
            transition_required=bool(stage_gate.get('transition_required')),
            stage_tool_name=STAGE_TOOL_NAME,
        )

    @classmethod
    def _overflowed_search_signatures(cls, messages: list[dict[str, Any]]) -> set[str]:
        signatures: set[str] = set()
        for message in list(messages or []):
            if str(message.get('role') or '').strip().lower() != 'tool':
                continue
            signature = cls._search_overflow_signature_from_tool_message(message)
            if signature:
                signatures.add(signature)
        return signatures

    @classmethod
    def _search_overflow_signature_from_tool_message(cls, message: dict[str, Any]) -> str:
        tool_name = str(message.get('name') or '').strip()
        if tool_name not in {'filesystem', 'content'}:
            return ''
        content = message.get('content')
        if isinstance(content, str):
            text = content.strip()
            if not text.startswith('{'):
                return ''
            try:
                payload = json.loads(text)
            except Exception:
                return ''
        elif isinstance(content, dict):
            payload = content
        else:
            return ''
        if not isinstance(payload, dict) or not bool(payload.get('overflow')):
            return ''
        query = str(payload.get('query') or '').strip()
        scope = str(payload.get('path') or '').strip() or str(payload.get('ref') or '').strip()
        if not query or not scope:
            return ''
        return f'{tool_name}|{scope}|{query}'

    @staticmethod
    def _search_overflow_signature_for_call(*, tool_name: str, arguments: dict[str, Any]) -> str:
        normalized_tool = str(tool_name or '').strip()
        if normalized_tool not in {'filesystem', 'content'}:
            return ''
        payload = dict(arguments or {})
        if str(payload.get('action') or '').strip().lower() != 'search':
            return ''
        query = str(payload.get('query') or '').strip()
        scope = str(payload.get('path') or '').strip() or str(payload.get('ref') or '').strip()
        if not query or not scope:
            return ''
        return f'{normalized_tool}|{scope}|{query}'

    async def _on_tool_watchdog_poll(self, runtime_context: dict[str, Any]) -> None:
        task_id = str(runtime_context.get('task_id') or '').strip()
        if not task_id:
            return
        self._check_pause_or_cancel(task_id)

    def _snapshot_supplier(self, runtime_context: dict[str, Any]):
        task_id = str(runtime_context.get('task_id') or '').strip()
        builder = getattr(self._log_service, '_snapshot_payload_builder', None)
        if not task_id or not callable(builder):
            return None
        return lambda: builder(task_id)

    def _check_pause_or_cancel(self, task_id: str) -> None:
        task = self._log_service._store.get_task(task_id)
        if task is None:
            return
        if bool(task.cancel_requested):
            raise RuntimeError('canceled')
        if bool(task.pause_requested):
            self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            raise TaskPausedError(task_id)

    @staticmethod
    def _parse_final_result(content: str) -> tuple[NodeFinalResult, dict[str, Any]] | None:
        text = str(content or '').strip()
        if not text:
            return None
        candidates = [text, *ReActToolLoop._extract_json_object_candidates(text)]
        seen: set[str] = set()
        for candidate in candidates:
            normalized = str(candidate or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            try:
                parsed = json_repair.loads(normalized)
            except Exception:
                parsed = None
            if not isinstance(parsed, dict):
                continue
            status = str(parsed.get('status') or '').strip().lower()
            if status not in {'success', 'failed'}:
                continue
            evidence_items: list[NodeEvidenceItem] = []
            for item in list(parsed.get('evidence') or []):
                if not isinstance(item, dict):
                    continue
                try:
                    evidence_items.append(NodeEvidenceItem.model_validate(item))
                except Exception:
                    continue
            remaining_work_raw = parsed.get('remaining_work')
            remaining_work = [
                str(item or '').strip()
                for item in (remaining_work_raw if isinstance(remaining_work_raw, list) else [])
                if str(item or '').strip()
            ]
            delivery_status = str(parsed.get('delivery_status') or '').strip().lower()
            normalized_delivery_status = delivery_status if delivery_status in {'final', 'partial', 'blocked'} else 'final'
            return (
                NodeFinalResult(
                    status=status,
                    delivery_status=normalized_delivery_status,
                    summary=str(parsed.get('summary') or '').strip(),
                    answer=str(parsed.get('answer') or '').strip(),
                    evidence=evidence_items,
                    remaining_work=remaining_work,
                    blocking_reason=str(parsed.get('blocking_reason') or '').strip(),
                ),
                dict(parsed),
            )
        return None

    @staticmethod
    def _validate_final_result(
        *,
        result: NodeFinalResult,
        raw_payload: dict[str, Any],
        has_tool_results: bool,
    ) -> list[str]:
        violations: list[str] = []
        missing_keys = [key for key in _RESULT_REQUIRED_KEYS if key not in raw_payload]
        violations.extend([f'missing required field: {key}' for key in missing_keys])

        raw_delivery_status = str(raw_payload.get('delivery_status') or '').strip().lower()
        if raw_delivery_status not in {'final', 'partial', 'blocked'}:
            violations.append('delivery_status must be one of final|partial|blocked')
        if not str(result.summary or '').strip():
            violations.append('summary must not be empty')

        if result.status == 'success':
            if result.delivery_status != 'final':
                violations.append('success requires delivery_status=final')
            if not str(result.answer or '').strip():
                violations.append('success requires non-empty answer')
            if list(result.remaining_work or []):
                violations.append('success requires remaining_work to be empty')
            if str(result.blocking_reason or '').strip():
                violations.append('success requires blocking_reason to be empty')
            if has_tool_results and not list(result.evidence or []):
                violations.append('success after tool usage requires at least one evidence item')

        if result.status == 'failed' and result.delivery_status == 'partial' and not list(result.remaining_work or []):
            violations.append('failed+partial requires non-empty remaining_work')
        if result.status == 'failed' and result.delivery_status == 'blocked' and not str(result.blocking_reason or '').strip():
            violations.append('failed+blocked requires non-empty blocking_reason')

        raw_evidence = raw_payload.get('evidence')
        if not isinstance(raw_evidence, list):
            violations.append('evidence must be an array')
        else:
            for index, item in enumerate(raw_evidence):
                if not isinstance(item, dict):
                    violations.append(f'evidence[{index}] must be an object')
                    continue
                kind = str(item.get('kind') or '').strip().lower()
                if kind not in {'file', 'artifact', 'url'}:
                    violations.append(f'evidence[{index}].kind must be file|artifact|url')
                if not any(str(item.get(key) or '').strip() for key in ('path', 'ref', 'note')):
                    violations.append(f'evidence[{index}] must include at least one of path/ref/note')
                start_line = item.get('start_line')
                end_line = item.get('end_line')
                if start_line not in {None, ''}:
                    try:
                        start_value = int(start_line)
                    except (TypeError, ValueError):
                        violations.append(f'evidence[{index}].start_line must be an integer')
                    else:
                        if start_value <= 0:
                            violations.append(f'evidence[{index}].start_line must be >= 1')
                if end_line not in {None, ''}:
                    try:
                        end_value = int(end_line)
                    except (TypeError, ValueError):
                        violations.append(f'evidence[{index}].end_line must be an integer')
                    else:
                        if end_value <= 0:
                            violations.append(f'evidence[{index}].end_line must be >= 1')
                        if start_line not in {None, ''}:
                            try:
                                if int(start_line) > end_value:
                                    violations.append(f'evidence[{index}].end_line must be >= start_line')
                            except (TypeError, ValueError):
                                pass

        remaining_work_raw = raw_payload.get('remaining_work')
        if not isinstance(remaining_work_raw, list):
            violations.append('remaining_work must be an array')
        elif any(not str(item or '').strip() for item in remaining_work_raw):
            violations.append('remaining_work items must be non-empty strings')
        return violations

    @staticmethod
    def _has_tool_results(messages: list[dict[str, Any]]) -> bool:
        for message in list(messages or []):
            if str(message.get('role') or '').strip().lower() == 'tool':
                return True
            if list(message.get('tool_calls') or []):
                return True
        return False

    @staticmethod
    def _result_protocol_message(*, node_kind: str = 'execution') -> str:
        guidance = ReActToolLoop._result_repair_guidance(node_kind=node_kind)
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                f'Your previous reply was not valid result JSON for schema v{RESULT_SCHEMA_VERSION}. '
                'Reply with only one JSON object using exactly these keys: '
                '{"status":"success|failed","delivery_status":"final|partial|blocked","summary":"...",'
                '"answer":"...","evidence":[{"kind":"file|artifact|url","path":"","ref":"","start_line":1,"end_line":1,"note":"..."}],'
                '"remaining_work":["..."],"blocking_reason":"..."}. '
                'Do not use Markdown. '
                f'{guidance}'
            )
        return (
            f'Your previous reply was not valid result JSON for schema v{RESULT_SCHEMA_VERSION}. '
            'If you are ending the node now, reply with only one JSON object using exactly these keys: '
            '{"status":"success|failed","delivery_status":"final|partial|blocked","summary":"...",'
            '"answer":"...","evidence":[{"kind":"file|artifact|url","path":"","ref":"","start_line":1,"end_line":1,"note":"..."}],'
            '"remaining_work":["..."],"blocking_reason":"..."}. '
            'If the task is not complete yet, do not emit prose or a premature result JSON; continue with tool calls, '
            'stage transitions, or child-node actions instead. '
            'Do not use Markdown when you do return the final JSON. '
            f'{guidance}'
        )

    @staticmethod
    def _result_contract_violation_message(violations: list[str], *, node_kind: str = 'execution') -> str:
        bullet_text = '; '.join(str(item or '').strip() for item in violations if str(item or '').strip()) or 'result contract violation'
        guidance = ReActToolLoop._result_repair_guidance(node_kind=node_kind)
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                f'Your previous reply produced parseable JSON but violated result schema v{RESULT_SCHEMA_VERSION}: {bullet_text}. '
                'Fix every violation and reply with only one JSON object. '
                'Do not claim success unless the deliverable is fully complete. '
                f'{guidance}'
            )
        return (
            f'Your previous reply produced parseable JSON but violated result schema v{RESULT_SCHEMA_VERSION}: {bullet_text}. '
            'If you are ending the node now, fix every violation and reply with only one JSON object. '
            'If the task is not actually complete yet, do not force another premature result JSON; continue with tool '
            'calls, stage transitions, or child-node actions instead. '
            'Do not claim success unless the deliverable is fully complete. '
            f'{guidance}'
        )

    @staticmethod
    def _result_repair_guidance(*, node_kind: str) -> str:
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                'Do not use delivery_status="partial" for acceptance nodes. '
                'If you are rejecting the deliverable, return failed+final. '
                'If missing evidence, unreadable artifacts, or insufficient context block verification, return failed+blocked.'
            )
        return (
            'Do not use delivery_status="partial" for execution nodes. '
            'If the task is not actually complete yet, continue working with tool calls or stage transitions instead of emitting more result JSON. '
            'Only return failed+blocked when you are truly blocked under the current permissions, environment, and tools.'
        )

    @staticmethod
    def _extract_json_object_candidates(content: str) -> list[str]:
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
    def _collect_content_refs(value: Any) -> list[str]:
        found: set[str] = set()

        def _visit(item: Any) -> None:
            envelope = parse_content_envelope(item)
            if envelope is not None and str(envelope.ref or '').strip():
                found.add(str(envelope.ref or '').strip())
            if isinstance(item, dict):
                for nested in item.values():
                    _visit(nested)
                return
            if isinstance(item, list):
                for nested in item:
                    _visit(nested)
                return
            if isinstance(item, str):
                text = str(item or '')
                for match in _ARTIFACT_REF_PATTERN.finditer(text):
                    found.add(match.group(0))
                stripped = text.strip()
                if stripped.startswith('{') or stripped.startswith('['):
                    try:
                        parsed = json.loads(stripped)
                    except Exception:
                        parsed = None
                    if parsed is not None:
                        _visit(parsed)

        _visit(value)
        return sorted(found)

    @staticmethod
    def _accepts_runtime_context(tool: Tool) -> bool:
        return ReActToolLoop._runtime_context_parameter_name(tool) is not None

    @staticmethod
    def _runtime_context_parameter_name(tool: Tool) -> str | None:
        sig = inspect.signature(tool.execute)
        if '__g3ku_runtime' in sig.parameters:
            return '__g3ku_runtime'
        for name in sig.parameters:
            if str(name).endswith('__g3ku_runtime'):
                return str(name)
        if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
            return '__g3ku_runtime'
        return None

    @staticmethod
    def _render_tool_result(result: Any) -> str:
        try:
            return json.dumps(result, ensure_ascii=False)
        except TypeError:
            return str(result)

    async def _chat_with_optional_extensions(self, **kwargs) -> Any:
        chat = getattr(self._chat_backend, 'chat')
        signature = inspect.signature(chat)
        accepts_kwargs = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
        if accepts_kwargs:
            return await chat(**kwargs)
        filtered = {key: value for key, value in kwargs.items() if key in signature.parameters}
        return await chat(**filtered)

    def _compact_history(self, messages: list[dict[str, Any]], *, preserve_non_system: int) -> list[dict[str, Any]]:
        message_list = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        existing_compact_count = sum(1 for item in message_list if self._is_compact_history_message(item))
        try:
            serialized = json.dumps(message_list, ensure_ascii=False, default=str)
        except Exception:
            serialized = str(message_list)
        should_compact = (
            len(message_list) > _COMPACT_HISTORY_MESSAGE_LIMIT
            or len(serialized) > _COMPACT_HISTORY_CHAR_LIMIT
            or existing_compact_count > 1
        )
        if not should_compact:
            return message_list

        preserved_prefix: list[dict[str, Any]] = []
        remainder = list(message_list)
        if remainder and str(remainder[0].get('role') or '').strip().lower() == 'system':
            preserved_prefix.append(remainder.pop(0))
        if remainder and str(remainder[0].get('role') or '').strip().lower() == 'user':
            preserved_prefix.append(remainder.pop(0))

        existing_payloads = [
            payload
            for payload in (self._parse_compact_history_payload(item) for item in remainder)
            if payload is not None
        ]
        regular_messages = [item for item in remainder if not self._is_compact_history_message(item)]
        if not regular_messages:
            return preserved_prefix + ([self._make_compact_history_message(self._merge_compact_payloads(existing_payloads, []))] if existing_payloads else [])

        keep_count = max(_COMPACT_HISTORY_KEEP_RECENT, int(preserve_non_system or 0))
        segments = self._segment_history_for_compaction(regular_messages)
        recent_segments: list[list[dict[str, Any]]] = []
        recent_message_count = 0
        for segment in reversed(segments):
            if recent_segments and recent_message_count >= keep_count:
                break
            recent_segments.append(segment)
            recent_message_count += len(segment)
        recent_segments.reverse()
        older_segment_count = max(0, len(segments) - len(recent_segments))
        older_messages = [item for segment in segments[:older_segment_count] for item in segment]
        recent_messages = [item for segment in recent_segments for item in segment]
        if not older_messages and not existing_payloads:
            return preserved_prefix + recent_messages

        compact_payload = self._merge_compact_payloads(existing_payloads, older_messages)
        compact_message = self._make_compact_history_message(compact_payload)
        return preserved_prefix + [compact_message] + recent_messages

    @staticmethod
    def _segment_history_for_compaction(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        segments: list[list[dict[str, Any]]] = []
        index = 0
        message_list = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        while index < len(message_list):
            message = dict(message_list[index] or {})
            role = str(message.get('role') or '').strip().lower()
            if role == 'assistant' and list(message.get('tool_calls') or []):
                segment = [message]
                call_ids = {
                    extract_call_id((tool_call or {}).get('id'))
                    for tool_call in list(message.get('tool_calls') or [])
                    if extract_call_id((tool_call or {}).get('id'))
                }
                index += 1
                while index < len(message_list):
                    tool_message = dict(message_list[index] or {})
                    if str(tool_message.get('role') or '').strip().lower() != 'tool':
                        break
                    tool_call_id = extract_call_id(tool_message.get('tool_call_id'))
                    if tool_call_id and tool_call_id in call_ids:
                        segment.append(tool_message)
                        index += 1
                        continue
                    break
                segments.append(segment)
                continue
            segments.append([message])
            index += 1
        return segments

    def _merge_compact_payloads(self, payloads: list[dict[str, Any]], older_messages: list[dict[str, Any]]) -> dict[str, Any]:
        completed_steps: list[str] = []
        durable_refs: set[str] = set()
        open_threads: list[str] = []
        repair_prompt_count = 0

        for payload in list(payloads or []):
            completed_steps.extend([
                self._truncate_compact_text(item)
                for item in list(payload.get('completed_steps') or [])
                if self._truncate_compact_text(item)
            ])
            durable_refs.update(str(item or '').strip() for item in list(payload.get('durable_refs') or []) if str(item or '').strip())
            open_threads.extend([
                self._truncate_compact_text(item)
                for item in list(payload.get('open_threads') or [])
                if self._truncate_compact_text(item)
            ])
            contract_state = payload.get('result_contract_state') if isinstance(payload.get('result_contract_state'), dict) else {}
            repair_prompt_count += int(contract_state.get('repair_prompt_count') or 0)

        for message in list(older_messages or []):
            step = self._compact_step_from_message(message)
            if step:
                completed_steps.append(step)
            durable_refs.update(self._message_refs(message))
            open_thread = self._open_thread_from_message(message)
            if open_thread:
                open_threads.append(open_thread)
            if self._is_result_contract_prompt(message):
                repair_prompt_count += 1

        return {
            'completed_steps': self._unique_compact_items(completed_steps, limit=_COMPACT_HISTORY_MAX_STEPS),
            'durable_refs': sorted(durable_refs),
            'open_threads': self._unique_compact_items(open_threads, limit=_COMPACT_HISTORY_MAX_STEPS),
            'result_contract_state': {
                'entered': bool(repair_prompt_count > 0),
                'repair_prompt_count': repair_prompt_count,
            },
        }

    @staticmethod
    def _truncate_compact_text(value: Any) -> str:
        text = ' '.join(str(value or '').split())
        if not text:
            return ''
        if len(text) <= _COMPACT_HISTORY_STEP_MAX_CHARS:
            return text
        return text[: _COMPACT_HISTORY_STEP_MAX_CHARS - 3].rstrip() + '...'

    @staticmethod
    def _unique_compact_items(items: list[str], *, limit: int) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in list(items or []):
            text = str(item or '').strip()
            if not text or text in seen:
                continue
            seen.add(text)
            result.append(text)
            if len(result) >= max(1, int(limit or 1)):
                break
        return result

    @classmethod
    def _is_compact_history_message(cls, message: dict[str, Any]) -> bool:
        return (
            str((message or {}).get('role') or '').strip().lower() == 'assistant'
            and str((message or {}).get('content') or '').startswith(_COMPACT_HISTORY_PREFIX)
        )

    @classmethod
    def _parse_compact_history_payload(cls, message: dict[str, Any]) -> dict[str, Any] | None:
        if not cls._is_compact_history_message(message):
            return None
        text = str((message or {}).get('content') or '')[len(_COMPACT_HISTORY_PREFIX) :].strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _make_compact_history_message(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            'role': 'assistant',
            'content': f'{_COMPACT_HISTORY_PREFIX}\n{json.dumps(payload, ensure_ascii=False, sort_keys=True)}',
        }

    def _compact_step_from_message(self, message: dict[str, Any]) -> str:
        role = str((message or {}).get('role') or '').strip().lower()
        if role == 'assistant':
            tool_calls = list((message or {}).get('tool_calls') or [])
            if tool_calls:
                names = ', '.join(
                    str(((item or {}).get('function') or {}).get('name') or (item or {}).get('name') or '').strip()
                    for item in tool_calls
                    if str(((item or {}).get('function') or {}).get('name') or (item or {}).get('name') or '').strip()
                )
                if names:
                    return self._truncate_compact_text(f'Assistant requested tools: {names}')
            summary, _ref = content_summary_and_ref((message or {}).get('content'))
            if summary:
                return self._truncate_compact_text(f'Assistant: {summary}')
            return ''
        if role == 'tool':
            tool_name = str((message or {}).get('name') or 'tool').strip() or 'tool'
            summary, _ref = content_summary_and_ref((message or {}).get('content'))
            return self._truncate_compact_text(f'{tool_name}: {summary}') if summary else ''
        if role == 'user':
            summary, _ref = content_summary_and_ref((message or {}).get('content'))
            if summary and not self._is_result_contract_prompt(message):
                return self._truncate_compact_text(f'User follow-up: {summary}')
        return ''

    def _open_thread_from_message(self, message: dict[str, Any]) -> str:
        role = str((message or {}).get('role') or '').strip().lower()
        if role == 'tool':
            summary, _ref = content_summary_and_ref((message or {}).get('content'))
            lowered = summary.lower()
            if lowered.startswith('error') or '"status":"error"' in lowered or '"status": "error"' in lowered:
                tool_name = str((message or {}).get('name') or 'tool').strip() or 'tool'
                return self._truncate_compact_text(f'Investigate failed tool result: {tool_name}')
        if role == 'user' and self._is_result_contract_prompt(message):
            return 'Return valid result-schema JSON once implementation is complete'
        return ''

    @staticmethod
    def _message_refs(message: dict[str, Any]) -> set[str]:
        refs = set(ReActToolLoop._collect_content_refs(message))
        return {str(item or '').strip() for item in refs if str(item or '').strip()}

    @classmethod
    def _is_result_contract_prompt(cls, message: dict[str, Any]) -> bool:
        if str((message or {}).get('role') or '').strip().lower() != 'user':
            return False
        content = str((message or {}).get('content') or '').strip()
        return (
            content.startswith('Your previous reply was not valid result JSON for schema v')
            or content.startswith('Your previous reply produced parseable JSON but violated result schema v')
        )

    def _dedupe_tool_messages(self, tool_messages: list[dict[str, Any]], *, existing_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen_signatures: dict[str, dict[str, str]] = {}
        for message in list(existing_messages or []):
            if str(message.get('role') or '').strip().lower() != 'tool':
                continue
            signature = self._tool_result_signature(message.get('content'))
            if not signature:
                continue
            seen_signatures.setdefault(
                signature,
                {
                    'tool_call_id': str(message.get('tool_call_id') or '').strip(),
                    'ref': self._best_tool_ref(message.get('content')),
                    'summary': self._tool_summary(message.get('content')),
                },
            )

        deduped: list[dict[str, Any]] = []
        for message in list(tool_messages or []):
            payload = dict(message or {})
            signature = self._tool_result_signature(payload.get('content'))
            if not signature:
                deduped.append(payload)
                continue
            prior = seen_signatures.get(signature)
            if prior is None:
                seen_signatures[signature] = {
                    'tool_call_id': str(payload.get('tool_call_id') or '').strip(),
                    'ref': self._best_tool_ref(payload.get('content')),
                    'summary': self._tool_summary(payload.get('content')),
                }
                deduped.append(payload)
                continue
            payload['content'] = json.dumps(
                {
                    'status': 'reused',
                    'same_as': prior.get('tool_call_id') or '',
                    'ref': prior.get('ref') or '',
                    'summary': prior.get('summary') or '',
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            deduped.append(payload)
        return deduped

    @staticmethod
    def _tool_result_signature(content: Any) -> str:
        if content is None:
            return ''
        if isinstance(content, str):
            text = content.strip()
            if not text:
                return ''
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = text
        else:
            parsed = content
        try:
            normalized = json.dumps(parsed, ensure_ascii=True, sort_keys=True, default=str)
        except Exception:
            normalized = str(parsed)
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

    @staticmethod
    def _best_tool_ref(content: Any) -> str:
        refs = ReActToolLoop._collect_content_refs(content)
        return refs[0] if refs else ''

    def _tool_summary(self, content: Any) -> str:
        if isinstance(content, str):
            text = content.strip()
            if text.startswith('{') or text.startswith('['):
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    summary_value = parsed.get('summary')
                    if str(summary_value or '').strip():
                        return self._truncate_compact_text(summary_value)
        summary, _ref = content_summary_and_ref(content)
        return self._truncate_compact_text(summary)

    @staticmethod
    def _orphan_tool_result_failure(*, call_ids: list[str], strike_count: int) -> NodeFinalResult:
        recent_ids = [str(item or '').strip() for item in list(call_ids or []) if str(item or '').strip()]
        recent_text = ', '.join(recent_ids[:8]) if recent_ids else '<missing>'
        summary = 'orphan tool result circuit breaker triggered'
        blocking_reason = (
            f'Detected orphan tool results {int(strike_count or 0)} times during the same node run. '
            f'Recent orphan call IDs: {recent_text}. '
            'Possible history compaction split or tool replay corruption.'
        )
        return NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary=summary,
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=blocking_reason,
        )

    def _prepare_messages(self, messages: list[dict[str, Any]], *, runtime_context: dict[str, Any]) -> list[dict[str, Any]]:
        _ = runtime_context
        return list(messages)

    @staticmethod
    def _apply_temporary_system_overlay(messages: list[dict[str, Any]], *, overlay_text: str | None) -> list[dict[str, Any]]:
        base_messages = list(messages or [])
        text = str(overlay_text or '').strip()
        if not text:
            return base_messages
        return [{'role': 'system', 'content': text}, *base_messages]

    def _externalize_message_content(
        self,
        value: Any,
        *,
        runtime_context: dict[str, Any],
        display_name: str,
        source_kind: str,
    ) -> Any:
        store = getattr(self._log_service, '_content_store', None)
        if store is None:
            return value
        return store.externalize_for_message(
            value,
            runtime=runtime_context,
            display_name=display_name,
            source_kind=source_kind,
            compact=True,
        )
