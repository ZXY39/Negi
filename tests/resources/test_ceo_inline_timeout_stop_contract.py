from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.agent.tools.base import Tool
import g3ku.runtime.frontdoor._ceo_support as ceo_support_module
from g3ku.runtime.cancellation import ToolCancellationToken
from g3ku.runtime.frontdoor._ceo_support import CeoFrontDoorSupport
from g3ku.runtime.tool_watchdog import ToolWatchdogRunResult


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


class _RecordingInlineRegistry:
    def __init__(self) -> None:
        self.register_calls: list[dict[str, object]] = []
        self.discarded: list[str] = []

    async def register_execution(self, **kwargs):
        self.register_calls.append(dict(kwargs))
        return SimpleNamespace(execution_id="inline-tool-exec:1")

    def stop_decision_metadata(self, execution_id: str) -> dict[str, object] | None:
        _ = execution_id
        return None

    async def discard_execution(self, execution_id: str) -> None:
        self.discarded.append(str(execution_id or "").strip())


class _RuntimeAwareSuccessTool(Tool):
    def __init__(self, name: str = "agent_browser") -> None:
        self._name = name
        self.runtime_payloads: list[dict[str, object]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Return success while capturing runtime context."

    @property
    def parameters(self) -> dict[str, object]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, __g3ku_runtime=None, **kwargs: object) -> object:
        _ = kwargs
        self.runtime_payloads.append(dict(__g3ku_runtime or {}))
        return "ok"


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


@pytest.mark.asyncio
async def test_ceo_support_skips_watchdog_for_timeout_bearing_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    inline_registry = _RecordingInlineRegistry()
    parent_token = ToolCancellationToken(session_key="web:test")
    loop = SimpleNamespace(
        tools=_ToolRuntimeStack(),
        resource_manager=None,
        tool_execution_manager=None,
        inline_tool_execution_registry=inline_registry,
        main_task_service=SimpleNamespace(content_store=None),
    )
    support = ceo_support_module.CeoFrontDoorSupport(loop=loop)
    tool = _RuntimeAwareSuccessTool(name="agent_browser")

    async def _unexpected_run_tool_with_watchdog(*args, **kwargs):
        _ = args, kwargs
        raise AssertionError("watchdog should be bypassed when tool arguments already declare a timeout budget")

    monkeypatch.setattr(ceo_support_module, "run_tool_with_watchdog", _unexpected_run_tool_with_watchdog)

    result, rendered, status, _started_at, _finished_at, _elapsed_seconds = await support._execute_tool_call_with_raw_result(
        tool=tool,
        tool_name="agent_browser",
        arguments={"timeout_seconds": 60},
        runtime_context={
            "actor_role": "ceo",
            "session_key": "web:test",
            "cancel_token": parent_token,
        },
        on_progress=None,
        tool_call_id="call-agent-browser-timeout",
    )

    assert result == "ok"
    assert rendered == "ok"
    assert status == "success"
    assert inline_registry.register_calls == []
    assert tool.runtime_payloads[0] == {}
    assert parent_token.is_cancelled() is False


@pytest.mark.asyncio
async def test_ceo_support_uses_child_cancel_token_for_watchdog_inline_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    inline_registry = _RecordingInlineRegistry()
    parent_token = ToolCancellationToken(session_key="web:test")
    loop = SimpleNamespace(
        tools=_ToolRuntimeStack(),
        resource_manager=None,
        tool_execution_manager=None,
        inline_tool_execution_registry=inline_registry,
        main_task_service=SimpleNamespace(content_store=None),
    )
    support = ceo_support_module.CeoFrontDoorSupport(loop=loop)
    tool = _RuntimeAwareSuccessTool(name="exec")
    captured: dict[str, object] = {}

    async def _fake_run_tool_with_watchdog(awaitable, **kwargs):
        captured["runtime_context"] = dict(kwargs["runtime_context"])
        child_token = kwargs["runtime_context"]["cancel_token"]
        child_token.cancel(reason="sidecar_timeout_stop")
        assert parent_token.is_cancelled() is False
        value = await awaitable
        return ToolWatchdogRunResult(
            completed=True,
            value=value,
            elapsed_seconds=0.0,
            poll_count=0,
            snapshot=None,
            execution_id="",
        )

    monkeypatch.setattr(ceo_support_module, "run_tool_with_watchdog", _fake_run_tool_with_watchdog)

    result, rendered, status, _started_at, _finished_at, _elapsed_seconds = await support._execute_tool_call_with_raw_result(
        tool=tool,
        tool_name="exec",
        arguments={},
        runtime_context={
            "actor_role": "ceo",
            "session_key": "web:test",
            "cancel_token": parent_token,
        },
        on_progress=None,
        tool_call_id="call-exec-child-token",
    )

    assert result == "ok"
    assert rendered == "ok"
    assert status == "success"
    assert captured["runtime_context"]["cancel_token"] is not parent_token
    assert parent_token.is_cancelled() is False
