from __future__ import annotations

import json

import pytest

from g3ku.agent.tools.tool_execution_control import StopToolExecutionTool


class _FakeToolExecutionManager:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._payloads = [dict(item) for item in payloads]
        self.calls: list[tuple[str, str]] = []

    async def stop_execution(self, execution_id: str, *, reason: str = "agent_requested_stop") -> dict[str, object]:
        self.calls.append((str(execution_id or ""), str(reason or "")))
        if self._payloads:
            return self._payloads.pop(0)
        return {
            "status": "not_found",
            "execution_id": str(execution_id or ""),
            "message": "missing",
        }


class _FakeInlineExecutionRegistry:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = dict(payload)
        self.calls: list[tuple[str, str]] = []

    async def stop_execution(self, execution_id: str, *, reason: str = "agent_requested_stop") -> dict[str, object]:
        self.calls.append((str(execution_id or ""), str(reason or "")))
        return dict(self.payload)


@pytest.mark.asyncio
async def test_stop_tool_execution_uses_inline_execution_registry_before_task_fallback() -> None:
    manager = _FakeToolExecutionManager(
        [
            {
                "status": "not_found",
                "execution_id": "inline-tool-exec:1",
                "message": "missing",
            }
        ]
    )
    inline_registry = _FakeInlineExecutionRegistry(
        {
            "status": "stopped",
            "execution_id": "inline-tool-exec:1",
            "target_type": "inline_tool",
            "message": "inline execution stopped",
        }
    )
    tool = StopToolExecutionTool(
        lambda: manager,
        task_service_getter=lambda: None,
        inline_registry_getter=lambda: inline_registry,
    )

    payload = json.loads(await tool.execute("inline-tool-exec:1"))

    assert manager.calls == [("inline-tool-exec:1", "agent_requested_stop")]
    assert inline_registry.calls == [("inline-tool-exec:1", "agent_requested_stop")]
    assert payload["status"] == "stopped"
    assert payload["target_type"] == "inline_tool"
