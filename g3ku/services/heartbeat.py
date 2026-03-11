"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Literal

from loguru import logger

from g3ku.agent.chatmodel_utils import ensure_chat_model



class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the model via an official
    tool call whether there are active tasks.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.
    """

    def __init__(
        self,
        workspace: Path,
        provider: Any,
        model: str,
        runtime_bridge: Any | None = None,
        target_resolver: Callable[[], tuple[str, str]] | None = None,
        session_key: str = "heartbeat",
        task_registrar: Callable[[str, asyncio.Task[Any]], None] | None = None,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.model_client = ensure_chat_model(
            provider,
            default_model=model,
            default_temperature=0.0,
            default_max_tokens=256,
            default_reasoning_effort=None,
        )
        self.runtime_bridge = runtime_bridge
        self.target_resolver = target_resolver
        self.session_key = str(session_key or "heartbeat")
        self.task_registrar = task_registrar
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def heartbeat_file(self) -> Path:
        return self.workspace / "HEARTBEAT.md"

    def _read_heartbeat_file(self) -> str | None:
        if self.heartbeat_file.exists():
            try:
                return self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None
        return None

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask model to decide skip/run via official tool call."""
        from langchain.agents import create_agent
        from langchain.tools import tool

        decision: dict[str, str] = {"action": "skip", "tasks": ""}

        @tool
        def heartbeat(action: Literal["skip", "run"], tasks: str = "") -> str:
            """Report heartbeat decision after reviewing tasks."""

            decision["action"] = action
            decision["tasks"] = tasks
            return "recorded"

        agent = create_agent(
            model=self.model_client,
            tools=[heartbeat],
            name="g3ku_heartbeat_runtime",
        )

        await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "system",
                        "content": "你是一个心跳代理。调用 heartbeat 工具报告你的决定。",
                    },
                    {
                        "role": "user",
                        "content": (
                            "查看以下 HEARTBEAT.md 并决定是否有活动任务。\n\n"
                            f"{content}"
                        ),
                    },
                ]
            },
            config={"recursion_limit": 6},
        )

        return decision.get("action", "skip"), decision.get("tasks", "")

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        content = self._read_heartbeat_file()
        if not content:
            logger.debug("Heartbeat: HEARTBEAT.md missing or empty")
            return

        logger.info("Heartbeat: checking for tasks...")

        try:
            action, tasks = await self._decide(content)

            if action != "run":
                logger.info("Heartbeat: OK (nothing to report)")
                return

            logger.info("Heartbeat: tasks found, executing...")
            response = await self._execute_tasks(tasks)
            if response and self.on_notify:
                logger.info("Heartbeat: completed, delivering response")
                await self.on_notify(response)
        except Exception:
            logger.exception("Heartbeat execution failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        content = self._read_heartbeat_file()
        if not content:
            return None
        action, tasks = await self._decide(content)
        if action != "run":
            return None
        return await self._execute_tasks(tasks)

    async def _execute_tasks(self, tasks: str) -> str | None:
        """Execute decided heartbeat tasks via runtime bridge or legacy callback."""
        if self.runtime_bridge is not None:
            channel, chat_id = self._resolve_target()
            result = await self.runtime_bridge.prompt(
                tasks,
                session_key=self.session_key,
                channel=channel,
                chat_id=chat_id,
                register_task=self.task_registrar,
            )
            return str(result.output or "")
        if self.on_execute is not None:
            return await self.on_execute(tasks)
        return None

    def _resolve_target(self) -> tuple[str, str]:
        if self.target_resolver is None:
            return "cli", "direct"
        channel, chat_id = self.target_resolver()
        return str(channel or "cli"), str(chat_id or "direct")




