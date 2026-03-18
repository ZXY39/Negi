from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.registry import ToolRegistry
from g3ku.runtime.tool_watchdog import ToolExecutionManager


class _HeartbeatRecorder:
    def __init__(self) -> None:
        self.terminal_calls: list[tuple[str, dict[str, object]]] = []

    def enqueue_tool_terminal(self, *, session_id: str, payload: dict[str, object]) -> None:
        self.terminal_calls.append((str(session_id or ""), dict(payload)))


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
    heartbeat = _HeartbeatRecorder()
    loop = SimpleNamespace(
        tool_execution_manager=ToolExecutionManager(),
        resource_manager=None,
        web_session_heartbeat=heartbeat,
    )
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
            "session_key": "web:test-watchdog",
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

    await asyncio.sleep(0.3)

    assert len(heartbeat.terminal_calls) == 1
    session_id, terminal_payload = heartbeat.terminal_calls[0]
    assert session_id == "web:test-watchdog"
    assert terminal_payload["status"] == "completed"
    assert terminal_payload["execution_id"] == payload["execution_id"]
    assert terminal_payload["tool_name"] == "slow_complete"
    assert terminal_payload["final_result"] == "done"
    assert float(terminal_payload["elapsed_seconds"]) == pytest.approx(0.3, abs=0.2)

    wait_payload = await loop.tool_execution_manager.wait_execution(
        payload["execution_id"],
        wait_seconds=0.4,
        poll_interval_seconds=0.01,
    )

    assert wait_payload["status"] == "completed"
    assert wait_payload["execution_id"] == payload["execution_id"]
    assert wait_payload["final_result"] == "done"
