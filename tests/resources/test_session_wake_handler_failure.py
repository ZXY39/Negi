"""Wake-loop safety net: a leaking handler exception must not kill the task.

回归背景（磁盘满事故）：``_emit_node_error_escalation`` 抛出的 OSError 曾穿透
``SessionHeartbeatWakeQueue._run``（无捕获），wake 任务静默死亡，该会话后续
所有心跳事件无人处理，日志里只有一条延迟到 GC 才出现的
"Task exception was never retrieved"。
"""

from __future__ import annotations

import asyncio

import pytest

from g3ku.heartbeat import session_wake as wake_module
from g3ku.heartbeat.session_wake import SessionHeartbeatWakeQueue


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out waiting for wake condition")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_wake_loop_survives_handler_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake_module, "_HANDLER_FAILURE_RETRY_DELAY_SECONDS", 0.01)
    calls = {"count": 0}

    async def handler(session_id: str) -> float | None:
        calls["count"] += 1
        if calls["count"] < 3:
            raise OSError(28, "No space left on device")
        return None  # 第三次正常收尾

    queue = SessionHeartbeatWakeQueue(handler=handler)
    try:
        assert queue.request("ext:qq-official:test", delay_s=0) is True
        # handler 抛异常后循环必须继续重试，直到正常返回 None 收尾。
        await _wait_until(lambda: calls["count"] >= 3)
    finally:
        await queue.close()
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_wake_loop_merges_pending_request_after_failure_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失败退避期间的新 request 登记为 pending（不打断当前 sleep——既有语义）；
    退避结束后 pending 让循环继续而非收尾。"""
    monkeypatch.setattr(wake_module, "_HANDLER_FAILURE_RETRY_DELAY_SECONDS", 0.15)
    calls = {"count": 0}

    async def handler(session_id: str) -> float | None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return None

    queue = SessionHeartbeatWakeQueue(handler=handler)
    try:
        assert queue.request("ext:test:1", delay_s=0) is True
        # 任务还活着：退避期间 request 只登记 pending，返回 False。
        await asyncio.sleep(0.03)
        assert queue.request("ext:test:1", delay_s=0.01) is False
        # 退避结束 → handler#2 返回 None，但 pending 存在 → 循环继续 → handler#3。
        await _wait_until(lambda: calls["count"] >= 3, timeout=2.0)
    finally:
        await queue.close()
