from __future__ import annotations

import json
from types import SimpleNamespace

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


class _FakeTaskService:
    def __init__(self, tasks: list[SimpleNamespace]) -> None:
        self._tasks = {str(task.task_id): task for task in list(tasks or [])}
        self.startup_calls = 0
        self.cancel_calls: list[str] = []

    async def startup(self) -> None:
        self.startup_calls += 1

    def normalize_task_id(self, task_id: str) -> str:
        raw = str(task_id or "").strip()
        if not raw or raw.startswith("task:") or ":" in raw:
            return raw
        return f"task:{raw}"

    def get_task(self, task_id: str) -> SimpleNamespace | None:
        return self._tasks.get(str(task_id or "").strip())

    async def cancel_task(self, task_id: str) -> SimpleNamespace | None:
        normalized = str(task_id or "").strip()
        self.cancel_calls.append(normalized)
        task = self._tasks.get(normalized)
        if task is None:
            return None
        updated = SimpleNamespace(**{**task.__dict__, "cancel_requested": True})
        self._tasks[normalized] = updated
        return updated


class _FakeInlineExecutionRegistry:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = dict(payload)
        self.calls: list[tuple[str, str]] = []

    async def stop_execution(self, execution_id: str, *, reason: str = "agent_requested_stop") -> dict[str, object]:
        self.calls.append((str(execution_id or ""), str(reason or "")))
        return dict(self.payload)


@pytest.mark.asyncio
async def test_stop_tool_execution_falls_back_to_async_task_cancel_when_execution_not_found() -> None:
    manager = _FakeToolExecutionManager(
        [
            {
                "status": "not_found",
                "execution_id": "feaa1ac81292",
                "message": "没有找到对应的后台工具执行记录",
            }
        ]
    )
    service = _FakeTaskService(
        [
            SimpleNamespace(
                task_id="task:feaa1ac81292",
                status="in_progress",
                cancel_requested=False,
                is_paused=False,
            )
        ]
    )
    tool = StopToolExecutionTool(lambda: manager, task_service_getter=lambda: service)

    payload = json.loads(await tool.execute("feaa1ac81292"))

    assert manager.calls == [("feaa1ac81292", "agent_requested_stop")]
    assert service.startup_calls == 1
    assert service.cancel_calls == ["task:feaa1ac81292"]
    assert payload["status"] == "stopped"
    assert payload["target_type"] == "task"
    assert payload["task_id"] == "task:feaa1ac81292"
    assert payload["cancel_requested"] is True
    assert "task_id" in payload["message"]


@pytest.mark.asyncio
async def test_stop_tool_execution_reports_terminal_async_task_without_recancel() -> None:
    manager = _FakeToolExecutionManager(
        [
            {
                "status": "not_found",
                "execution_id": "done-task",
                "message": "没有找到对应的后台工具执行记录",
            }
        ]
    )
    service = _FakeTaskService(
        [
            SimpleNamespace(
                task_id="task:done-task",
                status="completed",
                cancel_requested=False,
                is_paused=False,
            )
        ]
    )
    tool = StopToolExecutionTool(lambda: manager, task_service_getter=lambda: service)

    payload = json.loads(await tool.execute("done-task"))

    assert manager.calls == [("done-task", "agent_requested_stop")]
    assert service.cancel_calls == []
    assert payload["status"] == "completed"
    assert payload["target_type"] == "task"
    assert payload["task_id"] == "task:done-task"
    assert "终态" in payload["message"]
