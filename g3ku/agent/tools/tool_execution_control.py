from __future__ import annotations

import json
from typing import Any, Callable

from g3ku.agent.tools.base import Tool


class _ToolExecutionControlTool(Tool):
    hide_universal_timeout_parameter = True
    # stop 自带受控停止窗口，外层再套统一硬超时会与它赛跑并把动作中途掐断，
    # 因此整族豁免外层机械超时。
    exempt_universal_timeout = True

    def __init__(
        self,
        task_service_getter: Callable[[], Any] | None = None,
        inline_registry_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._task_service_getter = task_service_getter
        self._inline_registry_getter = inline_registry_getter

    def _task_service(self) -> Any:
        if self._task_service_getter is None:
            return None
        return self._task_service_getter()

    def _inline_registry(self) -> Any:
        if self._inline_registry_getter is None:
            return None
        return self._inline_registry_getter()


class StopToolExecutionTool(_ToolExecutionControlTool):
    @property
    def name(self) -> str:
        return "stop_tool_execution"

    @property
    def description(self) -> str:
        return (
            "Stop the tool that is currently running inline in this session, including "
            "any registered subprocesses, when you decide it should end. "
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
                        "The execution id of the running tool. "
                        "If you only have an async task id, this tool will try to cancel "
                        "that task as a fallback."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason for stopping the execution.",
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
        inline_registry = self._inline_registry()
        if inline_registry is not None and hasattr(inline_registry, "stop_execution"):
            payload = await inline_registry.stop_execution(
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
                "status": "not_found",
                "execution_id": str(execution_id or ""),
                "message": "既没有找到本会话在跑的内联工具执行，也没有按这个标识找到可取消的异步任务。",
            },
            ensure_ascii=False,
        )
