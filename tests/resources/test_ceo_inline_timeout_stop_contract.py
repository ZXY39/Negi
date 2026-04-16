from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport


class _ToolRuntimeStack:
    def push_runtime_context(self, runtime_context: dict[str, object]) -> None:
        _ = runtime_context
        return None

    def pop_runtime_context(self, token: object) -> None:
        _ = token

    def get(self, name: str) -> None:
        _ = name
        return None


class _TimeoutStoppedInlineRegistry:
    def __init__(self) -> None:
        self.register_calls: list[dict[str, object]] = []
        self.discarded: list[str] = []

    async def register_execution(self, **kwargs):
        self.register_calls.append(dict(kwargs))
        return SimpleNamespace(execution_id="inline-tool-exec:1")

    def stop_decision_metadata(self, execution_id: str) -> dict[str, object] | None:
        if str(execution_id or "").strip() != "inline-tool-exec:1":
            return None
        return {
            "reason_code": "sidecar_timeout_stop",
            "decision_source": "sidecar_reminder",
            "elapsed_seconds_at_stop": 120.4,
            "reminder_count": 2,
            "window_seconds": 120.0,
        }

    async def discard_execution(self, execution_id: str) -> None:
        self.discarded.append(str(execution_id or "").strip())


class _FailingExecTool(Tool):
    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Fail after a sidecar timeout stop decision."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: object) -> object:
        _ = kwargs
        raise RuntimeError("user cancelled the process")


@pytest.mark.asyncio
async def test_ceo_support_standardizes_sidecar_timeout_stop_error_text() -> None:
    inline_registry = _TimeoutStoppedInlineRegistry()
    loop = SimpleNamespace(
        tools=_ToolRuntimeStack(),
        resource_manager=None,
        tool_execution_manager=None,
        inline_tool_execution_registry=inline_registry,
        main_task_service=SimpleNamespace(content_store=None),
    )
    support = CeoFrontDoorSupport(loop=loop)

    result, rendered, status, _started_at, _finished_at, _elapsed_seconds = await support._execute_tool_call_with_raw_result(
        tool=_FailingExecTool(),
        tool_name="exec",
        arguments={},
        runtime_context={"actor_role": "ceo", "session_key": "web:test"},
        on_progress=None,
        tool_call_id="call-exec-timeout-stop",
    )

    assert result == rendered
    assert status == "error"
    assert rendered.startswith("Error executing exec: stopped by sidecar timeout decision after 120.4s (2 reminders).")
    assert "actively stopped because the sidecar reminder judged further waiting was not worthwhile" in rendered
    assert inline_registry.discarded == ["inline-tool-exec:1"]
