"""会话道后台进程的全域退出观察。

`exec background=true` 起的进程在当世代由 per-session 观察器盯；但视角有三处漏：
重启认领回来的会话条目没有会话对象给它挂钩子、会话对象先于进程被回收、
以及会话尚未构造时进程就已退出。这里用一个全域轮询补上：只扫会话域
（`task_id` 为空）的登记条目，身份不符/已死就投 `interrupted` 并摘条目。

节点条目（有 `task_id`）不归这里：节点道在认领时已挂无句柄观察器。两边都以
归属条目为令牌——谁先摘谁报；并发窗口里的双报由 `dedupe_key`（含
pid@create_time）在事件队列处收掉。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from g3ku.runtime import owned_processes
from g3ku.runtime.tool_jobs import (
    OWNED_PROCESS_EXIT_POLL_SECONDS,
    TOOL_JOB_STATUS_INTERRUPTED,
    build_owned_process_event_payload,
)
from main.protocol import now_iso


class SessionOwnedProcessScanner:
    def __init__(
        self,
        *,
        heartbeat: Any,
        interval_seconds: float = OWNED_PROCESS_EXIT_POLL_SECONDS,
    ) -> None:
        self._heartbeat = heartbeat
        self._interval = max(0.5, float(interval_seconds))
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="session-owned-process-scanner")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("session owned process scan failed")
            await asyncio.sleep(self._interval)

    async def scan_once(self) -> int:
        """扫一轮会话域条目；返回本轮交代掉的条目数。"""
        entries = await asyncio.to_thread(owned_processes.list_session_owned)
        reported = 0
        for entry in entries:
            scope = str(entry.get("scope") or "").strip()
            session_key = str(entry.get("session_key") or "").strip()
            try:
                pid = int(entry.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if not scope or not session_key or pid <= 0:
                continue
            try:
                if await asyncio.to_thread(owned_processes.identity_matches, entry):
                    continue
            except Exception:
                continue
            # 条目还在＝这条死亡归我们交代；被别的车道先摘走时这里自然跳过。
            current = await asyncio.to_thread(owned_processes.find, scope, pid)
            if current is None:
                continue
            preview = (
                "This background process is no longer running; its exit code is unavailable "
                "(it may have exited on its own or been terminated externally). "
                "Inspect the log before deciding whether to restart it."
            )
            tail = await asyncio.to_thread(owned_processes.read_log_tail, current)
            if tail:
                preview = f"{preview}\n{tail}"
            payload = build_owned_process_event_payload(
                current,
                session_id=session_key,
                finished_at=now_iso(),
                status=TOOL_JOB_STATUS_INTERRUPTED,
                result_preview=preview,
                exit_code=None,
            )
            try:
                self._heartbeat.enqueue_tool_job(payload)
            except Exception:
                # 投递异常时保留条目，下一轮重试；不让一次抖动吞掉死亡凭证。
                logger.exception("session owned process exit enqueue failed pid={}", pid)
                continue
            await asyncio.to_thread(owned_processes.drop, scope, pid)
            reported += 1
        return reported
