"""watchdog 快照采集隔离回归测试。

修复缺陷：快照 supplier 抛异常会穿透到 run_tool_with_watchdog 的
except BaseException 分支并触发 request_tool_cancellation，误杀仍在执行的
长时工具。快照采集是只读观测旁路：失败必须降级为最近一次有效快照，
不得传播（取消类异常除外）。
"""

from __future__ import annotations

import asyncio
import time

from g3ku.runtime.tool_watchdog import _wait_for_task_window


class _ExplodingSupplier:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise RuntimeError("snapshot backend unavailable")


async def test_snapshot_failure_does_not_abort_running_tool() -> None:
    async def _slow_tool() -> str:
        await asyncio.sleep(0.25)
        return "tool-result"

    task = asyncio.create_task(_slow_tool())
    supplier = _ExplodingSupplier()
    try:
        result = await _wait_for_task_window(
            task=task,
            tool_name="slow_tool",
            started_at=time.monotonic(),
            snapshot_supplier=supplier,
            poll_interval_seconds=0.05,
            handoff_after_seconds=10.0,
            text_char_limit=280,
            list_limit=3,
        )
    finally:
        if not task.done():
            task.cancel()
    assert supplier.calls >= 1, "轮询期间必须尝试过采集快照"
    assert result.completed is True, "快照失败不得中断工具执行"
    assert result.value == "tool-result"


async def test_snapshot_failure_at_handoff_keeps_previous_snapshot() -> None:
    async def _slow_tool() -> None:
        await asyncio.sleep(5.0)

    task = asyncio.create_task(_slow_tool())
    previous = {"snapshot_type": "runtime", "summary_text": "旧快照"}
    try:
        result = await _wait_for_task_window(
            task=task,
            tool_name="slow_tool",
            started_at=time.monotonic() - 60.0,
            snapshot_supplier=_ExplodingSupplier(),
            poll_interval_seconds=0.05,
            handoff_after_seconds=0.1,
            text_char_limit=280,
            list_limit=3,
        )
    finally:
        if not task.done():
            task.cancel()
    assert result.completed is False
    assert result.snapshot is None or result.snapshot is not previous  # 无旧快照可用时不得抛错


async def test_healthy_supplier_still_summarized() -> None:
    async def _slow_tool() -> str:
        await asyncio.sleep(0.2)
        return "ok"

    payload = {
        "task": {"node_id": "root-1"},
        "root_node": {"node_id": "root-1", "execution_trace": {}},
        "frontier": [],
    }
    task = asyncio.create_task(_slow_tool())
    try:
        result = await _wait_for_task_window(
            task=task,
            tool_name="slow_tool",
            started_at=time.monotonic(),
            snapshot_supplier=lambda: payload,
            poll_interval_seconds=0.05,
            handoff_after_seconds=10.0,
            text_char_limit=280,
            list_limit=3,
        )
    finally:
        if not task.done():
            task.cancel()
    assert result.completed is True
    assert result.value == "ok"
