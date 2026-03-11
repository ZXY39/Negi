"""Legacy compatibility wrapper for the runtime engine."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from g3ku.agent.chatmodel_utils import ensure_chat_model
from g3ku.agent.context import ContextBuilder
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
    from g3ku.config.schema import (
        CapabilityToolsConfig,
        ChannelsConfig,
        MultiAgentConfig,
        ExecToolConfig,
        FileVaultConfig,
        MemoryToolsConfig,
    )
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
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: 'ExecToolConfig | None' = None,
        memory_config: 'MemoryToolsConfig | None' = None,
        file_vault_config: 'FileVaultConfig | None' = None,
        capability_config: 'CapabilityToolsConfig | None' = None,
        multi_agent_config: 'MultiAgentConfig | None' = None,
        app_config: Any | None = None,
        cron_service: 'CronService | None' = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: 'ChannelsConfig | None' = None,
        picture_washing_config: dict[str, Any] | None = None,
        agent_browser_config: dict[str, Any] | None = None,
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
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            exec_config=exec_config,
            memory_config=memory_config,
            file_vault_config=file_vault_config,
            capability_config=capability_config,
            multi_agent_config=multi_agent_config,
            app_config=app_config,
            cron_service=cron_service,
            restrict_to_workspace=restrict_to_workspace,
            session_manager=session_manager,
            mcp_servers=mcp_servers,
            channels_config=channels_config,
            picture_washing_config=picture_washing_config,
            agent_browser_config=agent_browser_config,
            context_builder_cls=ContextBuilder,
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
    'ContextBuilder',
    'MemoryManager',
    'SessionManager',
    '_LoopRuntimeContext',
    '_LoopRuntimeMiddleware',
]

