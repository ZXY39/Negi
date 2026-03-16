"""Legacy compatibility wrapper for the runtime engine."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from g3ku.agent.chatmodel_utils import ensure_chat_model
from g3ku.agent.rag_memory import MemoryManager
from g3ku.runtime.engine import AgentRuntimeEngine
from g3ku.runtime.model_bridge import (
    LoopRuntimeContext as _LoopRuntimeContext,
    LoopRuntimeMiddleware as _LoopRuntimeMiddleware,
)
from g3ku.session.manager import SessionManager

if TYPE_CHECKING:
    from pathlib import Path

    from g3ku.bus.queue import MessageBus
    from g3ku.config.schema import ChinaBridgeConfig, MultiAgentConfig, ResourceRuntimeConfig
    from g3ku.cron.service import CronService


class AgentLoop(AgentRuntimeEngine):
    """Backward-compatible wrapper over `g3ku.runtime.engine.AgentRuntimeEngine`."""

    def __init__(
        self,
        bus: 'MessageBus',
        provider: Any,
        workspace: 'Path',
        model: str | None = None,
        provider_name: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        reasoning_effort: str | None = None,
        resource_config: 'ResourceRuntimeConfig | None' = None,
        multi_agent_config: 'MultiAgentConfig | None' = None,
        app_config: Any | None = None,
        cron_service: 'CronService | None' = None,
        session_manager: SessionManager | None = None,
        channels_config: 'ChinaBridgeConfig | None' = None,
        debug_mode: bool = False,
        middlewares: list[Any] | None = None,
    ) -> None:
        super().__init__(
            bus=bus,
            provider=provider,
            workspace=workspace,
            model=model,
            provider_name=provider_name,
            max_iterations=max_iterations,
            temperature=temperature,
            max_tokens=max_tokens,
            memory_window=memory_window,
            reasoning_effort=reasoning_effort,
            resource_config=resource_config,
            multi_agent_config=multi_agent_config,
            app_config=app_config,
            cron_service=cron_service,
            session_manager=session_manager,
            channels_config=channels_config,
            memory_manager_cls=MemoryManager,
            session_manager_cls=SessionManager,
            chat_model_factory=ensure_chat_model,
            debug_mode=debug_mode,
            middlewares=middlewares,
        )


__all__ = [
    'AgentLoop',
    'AgentRuntimeEngine',
    'ensure_chat_model',
    'MemoryManager',
    'SessionManager',
    '_LoopRuntimeContext',
    '_LoopRuntimeMiddleware',
]
