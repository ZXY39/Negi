from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime.ceo_async_task_guard import (
    build_guard_state,
    maybe_build_intercept_message,
    record_completed_tool_round,
)
from g3ku.runtime.tool_bridge import ToolExecutionBridge


class _ImmediateTool(Tool):
    @property
    def name(self) -> str:
        return 'immediate_tool'

    @property
    def description(self) -> str:
        return 'Return immediately.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {},
            'required': [],
        }

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return 'done'


class _CreateAsyncTaskTool(Tool):
    @property
    def name(self) -> str:
        return 'create_async_task'

    @property
    def description(self) -> str:
        return 'Dispatch background work.'

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {},
            'required': [],
        }

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return 'queued'


class _LoopStub:
    def __init__(self, tmp_path: Path) -> None:
        self.tools = ToolRegistry()
        self.tools.register(_ImmediateTool())
        self.middlewares: list[Any] = []
        self.temp_dir = tmp_path
        self.debug_trace = False
        self.resource_manager = None
        self.tool_execution_manager = None
        self.main_task_service = None

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _emit_progress_event(self, *args: Any, **kwargs: Any) -> None:
        _ = args, kwargs
        return None


def test_ceo_async_task_guard_uses_expected_thresholds() -> None:
    context = {
        'actor_role': 'ceo',
        'ceo_async_task_guard_state': build_guard_state(completed_rounds=20),
    }

    first = maybe_build_intercept_message(context, tool_name='filesystem')
    second = maybe_build_intercept_message(context, tool_name='filesystem')

    assert first == '由于执行轮数超过20轮，工具执行暂被拦截，立即评估是否需要将剩余工作改成异步任务，如果继续自行执行，请重新调用工具。'
    assert second is None

    context['ceo_async_task_guard_state'] = build_guard_state(completed_rounds=70)
    repeated = maybe_build_intercept_message(context, tool_name='filesystem')
    assert repeated == '由于执行轮数超过70轮，工具执行暂被拦截，立即评估是否需要将剩余工作改成异步任务，如果继续自行执行，请重新调用工具。'


def test_ceo_async_task_guard_bypasses_create_async_task() -> None:
    context = {
        'actor_role': 'ceo',
        'ceo_async_task_guard_state': build_guard_state(completed_rounds=20),
    }

    assert maybe_build_intercept_message(context, tool_name='create_async_task') is None
    record_completed_tool_round(context, tool_name='create_async_task')
    assert context['ceo_async_task_guard_state']['completed_rounds'] == 20


@pytest.mark.asyncio
async def test_tool_registry_intercepts_then_allows_retry_for_ceo_frontdoor_path() -> None:
    registry = ToolRegistry()
    registry.register(_ImmediateTool())
    registry.register(_CreateAsyncTaskTool())

    token = registry.push_runtime_context(
        {
            'actor_role': 'ceo',
            'ceo_async_task_guard_state': build_guard_state(completed_rounds=20),
        }
    )
    try:
        tools = registry.to_langchain_tools_filtered(['immediate_tool', 'create_async_task'])
        tool_by_name = {tool.name: tool for tool in tools}
        first = await tool_by_name['immediate_tool'].ainvoke({})
        second = await tool_by_name['immediate_tool'].ainvoke({})
        queued = await tool_by_name['create_async_task'].ainvoke({})
    finally:
        registry.pop_runtime_context(token)

    assert '超过20轮' in str(first)
    assert second == 'done'
    assert queued == 'queued'


@pytest.mark.asyncio
async def test_tool_execution_bridge_intercepts_then_allows_retry(tmp_path: Path) -> None:
    loop = _LoopStub(tmp_path)
    bridge = ToolExecutionBridge(loop)
    runtime_context = SimpleNamespace(
        actor_role='ceo',
        session_key='web:test-ceo-guard',
        channel='web',
        chat_id='test-ceo-guard',
        message_id=None,
        iteration=1,
        on_progress=None,
        cancel_token=None,
        tool_watchdog=None,
        ceo_async_task_guard_state=build_guard_state(completed_rounds=50),
    )

    first = await bridge.execute_named_tool(
        name='immediate_tool',
        arguments={},
        tool_call_id='call-guard-1',
        runtime_context=runtime_context,
        emit_progress=False,
    )
    second = await bridge.execute_named_tool(
        name='immediate_tool',
        arguments={},
        tool_call_id='call-guard-2',
        runtime_context=runtime_context,
        emit_progress=False,
    )

    assert '超过50轮' in str(first.content)
    assert second.content == 'done'
    assert second.status == 'success'
