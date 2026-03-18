from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections import deque
from typing import Any

import json_repair

from g3ku.agent.tools.base import Tool
from g3ku.content import parse_content_envelope
from g3ku.runtime.tool_watchdog import run_tool_with_watchdog
from main.errors import TaskPausedError
from main.models import NodeFinalResult
from main.protocol import now_iso

_ARTIFACT_REF_PATTERN = re.compile(r'artifact:artifact:[A-Za-z0-9_-]+')


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
        tool_schemas = [tool.to_schema() for tool in tools.values()]
        breaker = RepeatedActionCircuitBreaker()
        limit = max(2, int(max_iterations or self._max_iterations))
        attempts = 0
        while attempts < limit:
            attempts += 1
            self._check_pause_or_cancel(task.task_id)
            model_messages = self._prepare_messages(messages, runtime_context=runtime_context)
            allowed_content_refs = self._collect_content_refs(model_messages)
            self._log_service.update_node_input(task.task_id, node.node_id, json.dumps(model_messages, ensure_ascii=False, indent=2))
            self._log_service.upsert_frame(
                task.task_id,
                {
                    'node_id': node.node_id,
                    'depth': node.depth,
                    'node_kind': node.node_kind,
                    'phase': 'before_model',
                    'messages': model_messages,
                    'pending_tool_calls': [],
                    'pending_child_specs': [],
                    'partial_child_results': [],
                    'tool_calls': [],
                    'child_pipelines': [],
                    'last_error': '',
                },
                publish_snapshot=True,
            )
            response = await self._chat_backend.chat(
                messages=model_messages,
                tools=tool_schemas or None,
                model_refs=model_refs,
                max_tokens=1200,
                temperature=0.2,
                parallel_tool_calls=(self._parallel_tool_calls_enabled if tool_schemas else None),
            )
            tool_calls = [
                {'id': call.id, 'name': call.name, 'arguments': dict(call.arguments or {})}
                for call in list(response.tool_calls or [])
            ]
            self._log_service.append_node_output(
                task.task_id,
                node.node_id,
                content=str(response.content or ''),
                tool_calls=tool_calls,
                usage_attempts=list(response.attempts or []),
            )
            if response.tool_calls:
                control_only_turn = all(call.name in self._CONTROL_TOOL_NAMES for call in response.tool_calls)
                for call in response.tool_calls:
                    signature = f"{call.name}:{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"
                    if call.name not in self._CONTROL_TOOL_NAMES:
                        breaker.register(signature)
                assistant_tool_calls = [
                    {
                        'id': call.id,
                        'type': 'function',
                        'function': {'name': call.name, 'arguments': json.dumps(call.arguments, ensure_ascii=False)},
                    }
                    for call in response.tool_calls
                ]
                live_tool_calls = [self._live_tool_entry(call) for call in response.tool_calls]
                self._log_service.update_frame(
                    task.task_id,
                    node.node_id,
                    lambda frame: {
                        **frame,
                        'depth': node.depth,
                        'node_kind': node.node_kind,
                        'phase': 'waiting_tool_results',
                        'messages': model_messages,
                        'pending_tool_calls': tool_calls,
                        'tool_calls': live_tool_calls,
                        'last_error': '',
                    },
                    publish_snapshot=True,
                )
                results = await self._execute_tool_calls(
                    task=task,
                    node=node,
                    response_tool_calls=list(response.tool_calls or []),
                    tools=tools,
                    allowed_content_refs=allowed_content_refs,
                    runtime_context=runtime_context,
                )
                tool_messages = [item['tool_message'] for item in results]
                messages.append(
                    {
                        'role': 'assistant',
                        'content': self._externalize_message_content(
                            response.content,
                            runtime_context=runtime_context,
                            display_name=f'assistant:{node.node_id}',
                            source_kind='assistant_message',
                        ),
                        'tool_calls': assistant_tool_calls,
                    }
                )
                messages.extend(tool_messages)
                waiting_messages = self._prepare_messages(messages, runtime_context=runtime_context)
                self._log_service.update_frame(
                    task.task_id,
                    node.node_id,
                    lambda frame: {
                        **frame,
                        'depth': node.depth,
                        'node_kind': node.node_kind,
                        'phase': 'waiting_tool_results',
                        'messages': waiting_messages,
                        'pending_tool_calls': [],
                        'tool_calls': [item['live_state'] for item in results],
                        'last_error': '',
                    },
                    publish_snapshot=True,
                )
                if control_only_turn:
                    attempts = max(0, attempts - 1)
                continue

            parsed = self._parse_final_result(str(response.content or ''))
            if parsed is not None:
                self._log_service.remove_frame(task.task_id, node.node_id, publish_snapshot=True)
                return parsed
            messages.append({'role': 'user', 'content': 'Your previous reply was not valid final JSON. Return only {"status":"success|failed","output":"..."}.'})
        raise RuntimeError('node exceeded maximum ReAct iterations')

    async def _execute_tool_calls(
        self,
        *,
        task,
        node,
        response_tool_calls: list[Any],
        tools: dict[str, Tool],
        allowed_content_refs: list[str],
        runtime_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
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
        tool = tools.get(tool_name)
        if tool is None:
            return f'Error: tool not available: {tool_name}'
        errors = tool.validate_params(arguments)
        if errors:
            return 'Error: ' + '; '.join(errors)
        execute_kwargs = dict(arguments)
        if self._accepts_runtime_context(tool):
            execute_kwargs['__g3ku_runtime'] = runtime_context
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
    def _parse_final_result(content: str) -> NodeFinalResult | None:
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
            return NodeFinalResult(status=status, output=str(parsed.get('output') or ''))
        return None

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
        sig = inspect.signature(tool.execute)
        if '__g3ku_runtime' in sig.parameters:
            return True
        return any(param.kind is inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values())

    @staticmethod
    def _render_tool_result(result: Any) -> str:
        try:
            return json.dumps(result, ensure_ascii=False)
        except TypeError:
            return str(result)

    def _prepare_messages(self, messages: list[dict[str, Any]], *, runtime_context: dict[str, Any]) -> list[dict[str, Any]]:
        store = getattr(self._log_service, '_content_store', None)
        if store is None:
            return list(messages)
        return store.prepare_messages_for_model(
            list(messages),
            runtime=runtime_context,
            source_prefix='react',
        )

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
        )
