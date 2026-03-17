from __future__ import annotations

import inspect
import json
from collections import deque
from typing import Any

import json_repair

from g3ku.agent.tools.base import Tool
from main.errors import TaskPausedError
from main.models import NodeFinalResult


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
    def __init__(self, *, chat_backend, log_service, max_iterations: int = 16) -> None:
        self._chat_backend = chat_backend
        self._log_service = log_service
        self._max_iterations = max(2, int(max_iterations or 16))

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
        for _ in range(limit):
            self._check_pause_or_cancel(task.task_id)
            model_messages = self._prepare_messages(messages, runtime_context=runtime_context)
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
                    'last_error': '',
                },
            )
            response = await self._chat_backend.chat(
                messages=model_messages,
                tools=tool_schemas or None,
                model_refs=model_refs,
                max_tokens=1200,
                temperature=0.2,
            )
            tool_calls = [
                {'id': call.id, 'name': call.name, 'arguments': dict(call.arguments or {})}
                for call in list(response.tool_calls or [])
            ]
            self._log_service.append_node_output(task.task_id, node.node_id, content=str(response.content or ''), tool_calls=tool_calls)
            if response.tool_calls:
                assistant_tool_calls = []
                tool_messages = []
                self._log_service.upsert_frame(
                    task.task_id,
                    {
                        'node_id': node.node_id,
                        'depth': node.depth,
                        'node_kind': node.node_kind,
                        'phase': 'after_model',
                        'messages': model_messages,
                        'pending_tool_calls': tool_calls,
                        'pending_child_specs': [],
                        'partial_child_results': [],
                        'last_error': '',
                    },
                )
                for call in response.tool_calls:
                    self._check_pause_or_cancel(task.task_id)
                    signature = f"{call.name}:{json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)}"
                    breaker.register(signature)
                    assistant_tool_calls.append(
                        {
                            'id': call.id,
                            'type': 'function',
                            'function': {'name': call.name, 'arguments': json.dumps(call.arguments, ensure_ascii=False)},
                        }
                    )
                    tool_content = await self._execute_tool(
                        tools=tools,
                        tool_name=call.name,
                        arguments=dict(call.arguments or {}),
                        runtime_context={**runtime_context, 'current_tool_call_id': call.id},
                    )
                    tool_messages.append({'role': 'tool', 'tool_call_id': call.id, 'name': call.name, 'content': tool_content})
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
                self._log_service.upsert_frame(
                    task.task_id,
                    {
                        'node_id': node.node_id,
                        'depth': node.depth,
                        'node_kind': node.node_kind,
                        'phase': 'waiting_tool_results',
                        'messages': waiting_messages,
                        'pending_tool_calls': [],
                        'pending_child_specs': [],
                        'partial_child_results': [],
                        'last_error': '',
                    },
                )
                continue

            parsed = self._parse_final_result(str(response.content or ''))
            if parsed is not None:
                self._log_service.remove_frame(task.task_id, node.node_id)
                return parsed
            messages.append({'role': 'user', 'content': '你的上一条回复不是合法的最终 JSON。请只返回 {"status":"success|failed","output":"..."}。'})
        raise RuntimeError('node exceeded maximum ReAct iterations')

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
        result = await tool.execute(**execute_kwargs)
        rendered = result if isinstance(result, str) else self._render_tool_result(result)
        return self._externalize_message_content(
            rendered,
            runtime_context=runtime_context,
            display_name=f'tool:{tool_name}',
            source_kind=f'tool_result:{tool_name}',
        )

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
        try:
            parsed = json_repair.loads(text)
        except Exception:
            parsed = None
        if not isinstance(parsed, dict):
            return None
        status = str(parsed.get('status') or '').strip().lower()
        if status not in {'success', 'failed'}:
            return None
        return NodeFinalResult(status=status, output=str(parsed.get('output') or ''))

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
