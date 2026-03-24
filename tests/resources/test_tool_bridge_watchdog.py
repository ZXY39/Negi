from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime.tool_bridge import ToolExecutionBridge


class _ImmediateTool(Tool):
    @property
    def name(self) -> str:
        return "immediate_tool"

    @property
    def description(self) -> str:
        return "Return immediately."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs
        return "done"


class _LoopStub:
    def __init__(self, tmp_path: Path) -> None:
        self.tools = ToolRegistry()
        self.tools.register(_ImmediateTool())
        self.middlewares: list[Any] = []
        self.temp_dir = tmp_path
        self.debug_trace = False
        self.resource_manager = None
        self.tool_execution_manager = object()
        self.main_task_service = None

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _emit_progress_event(self, *args: Any, **kwargs: Any) -> None:
        _ = args, kwargs
        return None


@pytest.mark.asyncio
async def test_tool_execution_bridge_bypasses_watchdog_for_non_ceo_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import g3ku.runtime.tool_bridge as tool_bridge_module

    async def _forbidden_watchdog(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("non-CEO tool bridge path must not call run_tool_with_watchdog")

    monkeypatch.setattr(tool_bridge_module, "run_tool_with_watchdog", _forbidden_watchdog)

    loop = _LoopStub(tmp_path)
    bridge = ToolExecutionBridge(loop)
    runtime_context = SimpleNamespace(
        actor_role="execution",
        session_key="web:test-execution",
        channel="web",
        chat_id="test-execution",
        message_id=None,
        iteration=1,
        on_progress=None,
        cancel_token=None,
        tool_watchdog={"poll_interval_seconds": 0.01, "handoff_after_seconds": 0.03},
    )

    result = await bridge.execute_named_tool(
        name="immediate_tool",
        arguments={},
        tool_call_id="call-1",
        runtime_context=runtime_context,
        emit_progress=False,
    )

    assert result.content == "done"
    assert result.status == "success"


@pytest.mark.asyncio
async def test_tool_execution_bridge_uses_watchdog_for_ceo_roles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import g3ku.runtime.tool_bridge as tool_bridge_module

    calls: list[dict[str, str]] = []

    async def _record_watchdog(awaitable: Any, *, tool_name: str, runtime_context: Any, **kwargs: Any) -> Any:
        _ = kwargs
        calls.append(
            {
                "tool_name": str(tool_name or ""),
                "actor_role": str(getattr(runtime_context, "actor_role", "") or ""),
            }
        )
        value = await awaitable
        return SimpleNamespace(value=value, completed=True)

    monkeypatch.setattr(tool_bridge_module, "run_tool_with_watchdog", _record_watchdog)

    loop = _LoopStub(tmp_path)
    bridge = ToolExecutionBridge(loop)
    runtime_context = SimpleNamespace(
        actor_role="ceo",
        session_key="web:test-ceo",
        channel="web",
        chat_id="test-ceo",
        message_id=None,
        iteration=1,
        on_progress=None,
        cancel_token=None,
        tool_watchdog={"poll_interval_seconds": 0.01, "handoff_after_seconds": 0.03},
    )

    result = await bridge.execute_named_tool(
        name="immediate_tool",
        arguments={},
        tool_call_id="call-2",
        runtime_context=runtime_context,
        emit_progress=False,
    )

    assert calls == [{"tool_name": "immediate_tool", "actor_role": "ceo"}]
    assert result.content == "done"
    assert result.status == "success"
