from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry


class _SlowCompleteTool(Tool):
    @property
    def name(self) -> str:
        return "slow_complete"

    @property
    def description(self) -> str:
        return "Complete after a short delay."

    @property
    def parameters(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs) -> str:
        _ = kwargs
        await asyncio.sleep(0.25)
        return "done"


@pytest.mark.asyncio
async def test_tool_registry_keeps_watchdog_inline_for_execution_role() -> None:
    registry = ToolRegistry()
    registry.register(_SlowCompleteTool())
    loop = SimpleNamespace(resource_manager=None)

    token = registry.push_runtime_context(
        {
            "actor_role": "execution",
            "loop": loop,
            "tool_watchdog": {
                "poll_interval_seconds": 0.01,
                "handoff_after_seconds": 0.03,
            },
            "tool_snapshot_supplier": lambda: {
                "status": "running",
                "assistant_text": "Execution node should never detach this tool",
            },
            "session_key": "web:test-no-watchdog",
        }
    )
    try:
        tools = registry.to_langchain_tools_filtered(["slow_complete"])
        payload = await tools[0].ainvoke({})
    finally:
        registry.pop_runtime_context(token)

    assert payload == "done"


@pytest.mark.asyncio
async def test_tool_registry_passes_runtime_context_to_name_mangled_class_tool() -> None:
    class _RuntimeCaptureTool(Tool):
        @property
        def name(self) -> str:
            return "capture_runtime"

        @property
        def description(self) -> str:
            return "Capture runtime context."

        @property
        def parameters(self) -> dict[str, object]:
            return {
                "type": "object",
                "properties": {
                    "value": {"type": "string", "description": "value"},
                },
                "required": ["value"],
            }

        async def execute(self, value: str, __g3ku_runtime: dict[str, object] | None = None, **kwargs) -> str:
            runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
            return json.dumps(
                {
                    "value": value,
                    "current_tool_call_id": runtime.get("current_tool_call_id"),
                    "kwargs_runtime": kwargs.get("__g3ku_runtime"),
                },
                ensure_ascii=False,
            )

    registry = ToolRegistry()
    tool = _RuntimeCaptureTool()

    payload = await registry._execute_tool_with_runtime(
        tool=tool,
        tool_name=tool.name,
        params={"value": "demo"},
        runtime_context={"current_tool_call_id": "call:registry-runtime"},
    )

    parsed = json.loads(payload)
    assert parsed["value"] == "demo"
    assert parsed["current_tool_call_id"] == "call:registry-runtime"
    assert parsed["kwargs_runtime"] is None
