"""统一工具 timeout 合同的核心机制测试：解析、硬执行、侧车道自定排程、豁免合同。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor.inline_tool_reminder import (
    DEFAULT_FIRST_CHECK_SECONDS,
    InlineToolExecutionRegistry,
    CeoToolReminderService,
    clamp_next_check_seconds,
)
from g3ku.runtime.cancellation import ToolCancellationToken
from g3ku.runtime.tool_watchdog import (
    DEFAULT_TOOL_TIMEOUT_SECONDS,
    build_tool_timeout_error_text,
    coerce_timeout_argument,
    resolve_effective_tool_timeout,
    run_tool_with_hard_timeout,
    run_tool_with_watchdog,
)


def test_coerce_timeout_argument_variants() -> None:
    assert coerce_timeout_argument(None) is None
    assert coerce_timeout_argument("") is None
    assert coerce_timeout_argument("abc") is None
    assert coerce_timeout_argument(True) is None
    assert coerce_timeout_argument([1]) is None
    assert coerce_timeout_argument(0) == 1.0  # 低于下限抬到 1s
    assert coerce_timeout_argument(-5) == 1.0
    assert coerce_timeout_argument("900") == 900.0
    assert coerce_timeout_argument(86400) == 86400.0  # 无上限
    assert coerce_timeout_argument(float("inf")) is None


def test_resolve_effective_tool_timeout_explicit_wins_without_cap() -> None:
    context = {"tool_watchdog": {"default_timeout_seconds": 600}}
    assert resolve_effective_tool_timeout({"timeout": 3600}, context) == 3600.0
    assert resolve_effective_tool_timeout({"timeout": "42"}, context) == 42.0
    # 无显式参数 → 全局默认
    assert resolve_effective_tool_timeout({}, context) == 600.0
    assert resolve_effective_tool_timeout(None, None) == DEFAULT_TOOL_TIMEOUT_SECONDS


def test_build_tool_timeout_error_text_shape() -> None:
    text = build_tool_timeout_error_text(tool_name="exec", timeout_seconds=600)
    assert text.startswith("Error executing exec: timed out after 600s.")
    assert "超出 600s 运行时长上限被停止" in text
    assert 'timeout' in text


@pytest.mark.asyncio
async def test_hard_timeout_kills_task_and_returns_unified_error() -> None:
    token = ToolCancellationToken(session_key="web:test")

    async def _long_running() -> str:
        await asyncio.sleep(30)
        return "done"

    result = await run_tool_with_hard_timeout(
        _long_running(),
        tool_name="exec",
        timeout_seconds=0.2,
        cancel_token=token,
    )
    assert isinstance(result, str)
    assert "Error executing exec: timed out after" in result
    assert token.is_cancelled()


@pytest.mark.asyncio
async def test_hard_timeout_not_triggered_when_tool_finishes_first() -> None:
    async def _quick() -> str:
        return "done"

    result = await run_tool_with_hard_timeout(
        _quick(),
        tool_name="exec",
        timeout_seconds=5.0,
    )
    assert result == "done"


@pytest.mark.asyncio
async def test_watchdog_inline_hard_timeout_returns_unified_error() -> None:
    async def _long_running() -> str:
        await asyncio.sleep(30)
        return "done"

    outcome = await run_tool_with_watchdog(
        _long_running(),
        tool_name="agent_browser",
        arguments={},
        runtime_context={"tool_watchdog": {"poll_interval_seconds": 0.2}},
        hard_timeout_seconds=0.3,
    )
    assert outcome.timed_out is True
    assert isinstance(outcome.value, str)
    assert "Error executing agent_browser: timed out after" in outcome.value


@pytest.mark.asyncio
async def test_watchdog_self_enforced_tools_get_no_outer_deadline() -> None:
    async def _quick() -> str:
        return "done"

    # hard_timeout_seconds=None：自持工具语义，外层不设硬上限，工具照常完成。
    outcome = await run_tool_with_watchdog(
        _quick(),
        tool_name="exec",
        arguments={"timeout": 600},
        runtime_context={"tool_watchdog": {"poll_interval_seconds": 0.2}},
        hard_timeout_seconds=None,
    )
    assert outcome.timed_out is False
    assert outcome.value == "done"


def test_clamp_next_check_seconds_bounds() -> None:
    assert clamp_next_check_seconds(None) is None
    assert clamp_next_check_seconds("abc") is None
    assert clamp_next_check_seconds(True) is None
    assert clamp_next_check_seconds(0) == 30.0
    assert clamp_next_check_seconds(10) == 30.0
    assert clamp_next_check_seconds(300) == 300.0
    assert clamp_next_check_seconds("300") == 300.0
    assert clamp_next_check_seconds(100000) == 600.0


def test_parse_text_decision_continue_with_seconds() -> None:
    decision, seconds = CeoToolReminderService._parse_text_decision("CONTINUE 300")
    assert decision == "continue"
    assert seconds == 300.0

    decision, seconds = CeoToolReminderService._parse_text_decision("STOP")
    assert decision == "stop"
    assert seconds is None

    decision, seconds = CeoToolReminderService._parse_text_decision(
        '{"decision": "continue", "next_check_seconds": 120}'
    )
    assert decision == "continue"
    assert seconds == 120.0

    # 无效秒数 → None（调用方沿用上一轮间隔）
    decision, seconds = CeoToolReminderService._parse_text_decision("CONTINUE soon")
    assert decision == "continue"
    assert seconds is None

    decision, seconds = CeoToolReminderService._parse_text_decision("随便说点什么")
    assert decision == ""
    assert seconds is None


@pytest.mark.asyncio
async def test_registry_initial_schedule_uses_first_check_window() -> None:
    registry = InlineToolExecutionRegistry()

    async def _never() -> None:
        await asyncio.sleep(30)

    task = asyncio.create_task(_never())
    try:
        record = await registry.register_execution(
            session_key="web:test",
            turn_id="turn-1",
            tool_name="agent_browser",
            tool_call_id="call-1",
            arguments={},
            task=task,
            snapshot_supplier=None,
            cancel_token=None,
            started_at=1000.0,
            runtime_session=None,
            timeout_seconds=600,
        )
        assert record.next_check_at == pytest.approx(1000.0 + DEFAULT_FIRST_CHECK_SECONDS)
        assert record.last_check_interval_seconds == DEFAULT_FIRST_CHECK_SECONDS
        assert record.last_continue_at == 1000.0
        assert record.timeout_seconds == 600.0
        assert record.llm_continue_count == 0
        assert record.skip_continue_count == 0
    finally:
        task.cancel()


def test_default_first_check_is_120_seconds() -> None:
    assert DEFAULT_FIRST_CHECK_SECONDS == 120.0


# ---------------------------------------------------------------------------
# 豁免合同（exempt_universal_timeout）：长时编排/控制类工具不得套外层机械超时
# ---------------------------------------------------------------------------


def _spawn_tool():
    from main.runtime.internal_tools import SpawnChildNodesTool

    async def _noop(specs, call_id=None):
        _ = specs, call_id
        return []

    return SpawnChildNodesTool(_noop)


def _control_tools():
    from g3ku.agent.tools.tool_execution_control import (
        StopToolExecutionTool,
        WaitToolExecutionTool,
    )

    return [
        WaitToolExecutionTool(lambda: None),
        StopToolExecutionTool(lambda: None),
    ]


def test_long_running_orchestration_and_control_tools_are_exempt() -> None:
    """spawn_child_nodes 与 wait/stop_tool_execution 必须整体豁免外层机械超时；
    瞬时协议工具保留 backstop（豁免=False），自持工具走 self_enforced 语义。"""
    spawn = _spawn_tool()
    assert spawn.exempt_universal_timeout is True
    for tool in _control_tools():
        assert tool.exempt_universal_timeout is True

    from main.runtime.internal_tools import (
        SubmitFinalResultTool,
        SubmitMessageDistributionTool,
        SubmitNextStageTool,
    )

    async def _noop(payload):
        return dict(payload or {})

    instant_protocol_tools = [
        SubmitNextStageTool(_noop),
        SubmitFinalResultTool(_noop, node_kind="execution"),
        SubmitMessageDistributionTool(_noop),
    ]
    for tool in instant_protocol_tools:
        # 瞬时协议工具：隐藏 timeout 参数，但保留机械保底（不豁免）。
        assert tool.hide_universal_timeout_parameter is True
        assert tool.exempt_universal_timeout is False

    from g3ku.agent.tools.memory_note import MemoryNoteTool  # noqa: F401  仅作导入冒烟

    from g3ku.agent.tools.base import Tool

    assert Tool.exempt_universal_timeout is False


def test_exempt_tools_do_not_advertise_timeout_parameter() -> None:
    spawn = _spawn_tool()
    properties = spawn.to_model_schema()["function"]["parameters"].get("properties", {})
    assert "timeout" not in properties


class _KwargsRecordingInlineRegistry:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def register_execution(self, **kwargs):
        self.calls.append(dict(kwargs))
        return SimpleNamespace(execution_id="inline-tool-exec:1")

    async def discard_execution(self, execution_id: str) -> None:
        _ = execution_id


@pytest.mark.asyncio
async def test_watchdog_exempt_registration_records_no_timeout_budget() -> None:
    """豁免工具在 inline 登记时 timeout_seconds=None：提醒侧车道不得向模型
    宣称一个并不存在的运行上限；对照：非豁免自持语义仍登记统一解析值。"""

    async def _quick() -> str:
        return "done"

    exempt_registry = _KwargsRecordingInlineRegistry()
    outcome = await run_tool_with_watchdog(
        _quick(),
        tool_name="wait_tool_execution",
        arguments={},
        runtime_context={"tool_watchdog": {"poll_interval_seconds": 0.2}},
        inline_registry=exempt_registry,
        hard_timeout_seconds=None,
        universal_timeout_exempt=True,
    )
    assert outcome.value == "done"
    assert exempt_registry.calls[0]["timeout_seconds"] is None

    self_enforced_registry = _KwargsRecordingInlineRegistry()
    outcome = await run_tool_with_watchdog(
        _quick(),
        tool_name="exec",
        arguments={},
        runtime_context={"tool_watchdog": {"poll_interval_seconds": 0.2}},
        inline_registry=self_enforced_registry,
        hard_timeout_seconds=None,
    )
    assert outcome.value == "done"
    assert self_enforced_registry.calls[0]["timeout_seconds"] == DEFAULT_TOOL_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_watchdog_exempt_tool_has_no_hard_deadline() -> None:
    """豁免工具传入 hard_timeout_seconds=None 时外层永不超时（可被取消链中断）。"""

    async def _slow() -> str:
        await asyncio.sleep(0.5)
        return "finished"

    outcome = await run_tool_with_watchdog(
        _slow(),
        tool_name="spawn_child_nodes",
        arguments={},
        runtime_context={"tool_watchdog": {"poll_interval_seconds": 0.1}},
        hard_timeout_seconds=None,
        universal_timeout_exempt=True,
    )
    assert outcome.timed_out is False
    assert outcome.value == "finished"
