from __future__ import annotations

import json
from typing import Any, Callable

from g3ku.agent.tools.base import Tool


class _ToolExecutionControlTool(Tool):
    def __init__(self, manager_getter: Callable[[], Any]) -> None:
        self._manager_getter = manager_getter

    def _manager(self) -> Any:
        return self._manager_getter()


class WaitToolExecutionTool(_ToolExecutionControlTool):
    @property
    def name(self) -> str:
        return "wait_tool_execution"

    @property
    def description(self) -> str:
        return "Wait for a previously detached long-running tool execution. If wait_seconds is omitted, the default window grows as 30s, 60s, 120s, 240s, then 600s."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "execution_id": {
                    "type": "string",
                    "description": "The execution id returned by a previous long-running tool handoff.",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Optional custom wait window in seconds before returning another snapshot. If omitted, the default wait grows as 30s, 60s, 120s, 240s, then 600s.",
                    "minimum": 0.1,
                    "maximum": 600,
                },
            },
            "required": ["execution_id"],
        }

    async def execute(
        self,
        execution_id: str,
        wait_seconds: float | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        del __g3ku_runtime, kwargs
        manager = self._manager()
        if manager is None or not hasattr(manager, "wait_execution"):
            return json.dumps(
                {
                    "status": "unavailable",
                    "execution_id": str(execution_id or ""),
                    "message": "后台工具执行管理器当前不可用，无法继续等待该执行。",
                },
                ensure_ascii=False,
            )
        payload = await manager.wait_execution(
            str(execution_id or "").strip(),
            wait_seconds=(max(0.1, float(wait_seconds)) if wait_seconds is not None else 0.0),
        )
        return json.dumps(payload, ensure_ascii=False)


class StopToolExecutionTool(_ToolExecutionControlTool):
    @property
    def name(self) -> str:
        return "stop_tool_execution"

    @property
    def description(self) -> str:
        return "Stop a previously detached long-running tool execution, including any registered subprocesses, when you decide waiting is no longer worthwhile."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "execution_id": {
                    "type": "string",
                    "description": "The execution id returned by a previous long-running tool handoff.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason for stopping the background execution.",
                },
            },
            "required": ["execution_id"],
        }

    async def execute(
        self,
        execution_id: str,
        reason: str = "agent_requested_stop",
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        del __g3ku_runtime, kwargs
        manager = self._manager()
        if manager is None or not hasattr(manager, "stop_execution"):
            return json.dumps(
                {
                    "status": "unavailable",
                    "execution_id": str(execution_id or ""),
                    "message": "后台工具执行管理器当前不可用，无法停止该执行。",
                },
                ensure_ascii=False,
            )
        payload = await manager.stop_execution(
            str(execution_id or "").strip(),
            reason=str(reason or "agent_requested_stop").strip() or "agent_requested_stop",
        )
        return json.dumps(payload, ensure_ascii=False)
