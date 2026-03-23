from __future__ import annotations

import json
from typing import Any, Callable

from g3ku.agent.tools.base import Tool


class _ToolExecutionControlTool(Tool):
    def __init__(
        self,
        manager_getter: Callable[[], Any],
        task_service_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._manager_getter = manager_getter
        self._task_service_getter = task_service_getter

    def _manager(self) -> Any:
        return self._manager_getter()

    def _task_service(self) -> Any:
        if self._task_service_getter is None:
            return None
        return self._task_service_getter()


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
        return (
            "Stop a previously detached long-running tool execution, including any "
            "registered subprocesses, when you decide waiting is no longer worthwhile. "
            "If the supplied identifier is actually an async task id, fall back to "
            "cancelling that task."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "execution_id": {
                    "type": "string",
                    "description": (
                        "The execution id returned by a previous long-running tool handoff. "
                        "If you only have an async task id, this tool will try to cancel "
                        "that task as a fallback."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason for stopping the background execution.",
                },
            },
            "required": ["execution_id"],
        }

    async def _stop_task_by_identifier(self, identifier: str) -> dict[str, Any] | None:
        service = self._task_service()
        if service is None or not hasattr(service, "cancel_task"):
            return None

        startup = getattr(service, "startup", None)
        if callable(startup):
            maybe_started = startup()
            if hasattr(maybe_started, "__await__"):
                try:
                    await maybe_started
                except Exception:
                    return None

        normalized_identifier = str(identifier or "").strip()
        normalize_task_id = getattr(service, "normalize_task_id", None)
        if callable(normalize_task_id):
            normalized_identifier = (
                str(normalize_task_id(normalized_identifier) or "").strip()
                or normalized_identifier
            )

        get_task = getattr(service, "get_task", None)
        if not callable(get_task) or not normalized_identifier:
            return None

        task = get_task(normalized_identifier)
        if task is None:
            return None

        task_status = str(getattr(task, "status", "") or "").strip().lower()
        if task_status and task_status != "in_progress" and not bool(getattr(task, "is_paused", False)):
            return {
                "status": task_status,
                "execution_id": str(identifier or ""),
                "task_id": str(getattr(task, "task_id", normalized_identifier) or normalized_identifier),
                "target_type": "task",
                "task_status": task_status,
                "cancel_requested": bool(getattr(task, "cancel_requested", False)),
                "is_paused": bool(getattr(task, "is_paused", False)),
                "message": (
                    "提供的标识对应的是异步任务 task_id，不是后台工具 execution_id；"
                    "该任务已经处于终态，无需再次停止。"
                ),
            }

        latest = await service.cancel_task(normalized_identifier)
        current = latest if latest is not None else task
        return {
            "status": "stopped",
            "execution_id": str(identifier or ""),
            "task_id": str(getattr(current, "task_id", normalized_identifier) or normalized_identifier),
            "target_type": "task",
            "task_status": str(getattr(current, "status", "") or ""),
            "cancel_requested": bool(getattr(current, "cancel_requested", True)),
            "is_paused": bool(getattr(current, "is_paused", False)),
            "message": (
                "提供的标识对应的是异步任务 task_id，不是后台工具 execution_id；"
                "已按 task_id 发起取消。"
            ),
        }

    async def execute(
        self,
        execution_id: str,
        reason: str = "agent_requested_stop",
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        del __g3ku_runtime, kwargs
        normalized_identifier = str(execution_id or "").strip()
        manager = self._manager()
        payload: dict[str, Any] | None = None
        if manager is not None and hasattr(manager, "stop_execution"):
            payload = await manager.stop_execution(
                normalized_identifier,
                reason=str(reason or "agent_requested_stop").strip() or "agent_requested_stop",
            )
            if str((payload or {}).get("status") or "").strip().lower() != "not_found":
                return json.dumps(payload, ensure_ascii=False)

        task_payload = await self._stop_task_by_identifier(normalized_identifier)
        if task_payload is not None:
            return json.dumps(task_payload, ensure_ascii=False)

        if payload is not None:
            return json.dumps(payload, ensure_ascii=False)

        return json.dumps(
            {
                "status": "unavailable",
                "execution_id": str(execution_id or ""),
                "message": "后台工具执行管理器当前不可用，无法停止该执行。",
            },
            ensure_ascii=False,
        )
