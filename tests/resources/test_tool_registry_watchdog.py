from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime.tool_watchdog import ToolExecutionManager


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
async def test_tool_registry_langchain_tool_hands_off_long_tool_for_direct_ceo_path() -> None:
    registry = ToolRegistry()
    registry.register(_SlowCompleteTool())
    loop = SimpleNamespace(tool_execution_manager=ToolExecutionManager(), resource_manager=None)
    captured_updates: list[dict[str, object]] = []

    async def _on_progress(content: str, **kwargs) -> None:
        captured_updates.append({"content": content, **kwargs})

    token = registry.push_runtime_context(
        {
            "loop": loop,
            "tool_watchdog": {
                "poll_interval_seconds": 0.01,
                "handoff_after_seconds": 0.03,
            },
            "tool_snapshot_supplier": lambda: {
                "status": "running",
                "assistant_text": "Fetching skill files from remote repository",
            },
            "on_progress": _on_progress,
            "emit_lifecycle": True,
        }
    )
    try:
        tools = registry.to_langchain_tools_filtered(["slow_complete"])
        payload = await tools[0].ainvoke({})
    finally:
        registry.pop_runtime_context(token)

    assert payload["status"] == "background_running"
    assert payload["tool_name"] == "slow_complete"
    assert payload["runtime_snapshot"]["snapshot_type"] == "ceo_inflight_turn"
    assert any(item.get("event_kind") == "tool" for item in captured_updates)

    wait_payload = await loop.tool_execution_manager.wait_execution(
        payload["execution_id"],
        wait_seconds=0.4,
        poll_interval_seconds=0.01,
    )

    assert wait_payload["status"] == "completed"
    assert wait_payload["execution_id"] == payload["execution_id"]
    assert wait_payload["final_result"] == "done"
