"""Web shell runtime bootstrap for the converged runtime architecture."""

from __future__ import annotations

import os
from typing import Optional

from loguru import logger

from g3ku.agent.loop import AgentLoop
from g3ku.bus.queue import MessageBus
from g3ku.cli.commands import _make_provider
from g3ku.config.live_runtime import get_runtime_config
from g3ku.runtime import SessionRuntimeManager
from g3ku.runtime.config_refresh import refresh_loop_runtime_config

_global_agent: Optional[AgentLoop] = None
_global_bus: Optional[MessageBus] = None
_global_runtime_manager: Optional[SessionRuntimeManager] = None


def debug_trace_enabled() -> bool:
    raw = str(os.getenv("G3KU_DEBUG_TRACE", "")).strip().lower()
    return raw in {"1", "true", "yes", "on", "debug"}


def get_agent() -> AgentLoop:
    global _global_agent, _global_bus, _global_runtime_manager
    if not _global_agent:
        config, revision, _changed = get_runtime_config(force=False)
        provider_name, model_name = config.get_role_model_target("ceo")
        provider = _make_provider(config, scope="ceo")
        middlewares = []
        try:
            from g3ku.agent.middleware import build_middlewares
        except ModuleNotFoundError:
            if config.agents.defaults.middlewares:
                logger.warning(
                    "Runtime middleware unavailable in web mode because optional langchain middleware package is missing; "
                    "starting without custom middleware."
                )
        else:
            try:
                middlewares = build_middlewares(config.agents.defaults.middlewares)
            except ValueError as exc:
                logger.error("Invalid middleware config in web mode: {}", exc)
                middlewares = []

        _global_bus = MessageBus()
        debug_mode = debug_trace_enabled()
        if debug_mode:
            logger.info("Web API debug trace enabled (G3KU_DEBUG_TRACE=1)")
        _global_agent = AgentLoop(
            bus=_global_bus,
            provider=provider,
            workspace=config.workspace_path,
            model=model_name,
            provider_name=provider_name,
            temperature=config.agents.defaults.temperature,
            max_tokens=config.agents.defaults.max_tokens,
            max_iterations=config.agents.defaults.max_tool_iterations,
            memory_window=config.agents.defaults.memory_window,
            reasoning_effort=config.agents.defaults.reasoning_effort,
            multi_agent_config=config.agents.multi_agent,
            app_config=config,
            brave_api_key=config.tools.web.search.api_key or None,
            web_proxy=config.tools.web.proxy or None,
            exec_config=config.tools.exec,
            memory_config=config.tools.memory,
            file_vault_config=config.tools.file_vault,
            resource_config=config.resources,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            mcp_servers=config.tools.mcp_servers,
            channels_config=config.channels,
            picture_washing_config=config.tools.picture_washing.model_dump(),
            agent_browser_config=config.tools.agent_browser.model_dump(),
            debug_mode=debug_mode,
            middlewares=middlewares,
        )
        _global_agent._runtime_model_revision = revision
        _global_agent._runtime_default_model_key = config.resolve_role_model_key("ceo")
        _global_runtime_manager = SessionRuntimeManager(_global_agent)
    elif _global_runtime_manager is None or _global_runtime_manager.loop is not _global_agent:
        _global_runtime_manager = SessionRuntimeManager(_global_agent)
    return _global_agent


async def refresh_web_agent_runtime(force: bool = False, reason: str = "runtime") -> bool:
    return refresh_loop_runtime_config(get_agent(), force=force, reason=reason)


def get_runtime_manager(agent: AgentLoop | None = None) -> SessionRuntimeManager:
    runtime_agent = agent or get_agent()
    global _global_runtime_manager
    if _global_runtime_manager is None or _global_runtime_manager.loop is not runtime_agent:
        _global_runtime_manager = SessionRuntimeManager(runtime_agent)
    return _global_runtime_manager


async def shutdown_web_runtime() -> None:
    global _global_agent, _global_bus, _global_runtime_manager

    agent = _global_agent
    runtime_manager = _global_runtime_manager

    _global_agent = None
    _global_bus = None
    _global_runtime_manager = None

    if agent is None:
        return

    session_keys: set[str] = set()
    if runtime_manager is not None:
        try:
            session_keys.update(key for key in runtime_manager.list_sessions() if str(key or '').strip())
        except Exception:
            logger.debug("Runtime manager session enumeration skipped during shutdown")
    try:
        active_tasks = getattr(agent, '_active_tasks', None)
        if isinstance(active_tasks, dict):
            session_keys.update(key for key in active_tasks.keys() if str(key or '').strip())
    except Exception:
        logger.debug("Active session enumeration skipped during shutdown")

    for session_key in sorted(session_keys):
        try:
            await agent.cancel_session_tasks(session_key)
        except Exception:
            logger.debug("Session cancel skipped during shutdown for {}", session_key)

    pool = getattr(agent, 'background_pool', None)
    if pool is not None and hasattr(pool, 'close'):
        try:
            await pool.close()
        except Exception:
            logger.debug("Background pool close skipped during shutdown")

    main_task_service = getattr(agent, 'main_task_service', None)
    if main_task_service is not None:
        try:
            await main_task_service.close()
        except Exception:
            logger.debug("main task service close skipped during shutdown")

    try:
        await agent.close_mcp()
    except Exception:
        logger.debug("Agent runtime close skipped during shutdown")


def run_web_shell(*, host: str, port: int, reload: bool, debug: bool, set_debug_mode) -> None:
    """Start the web UI shell."""
    import uvicorn

    set_debug_mode(debug)
    uvicorn.run(
        "g3ku.web.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level="debug" if debug else "info",
    )


__all__ = ["debug_trace_enabled", "get_agent", "get_runtime_manager", "refresh_web_agent_runtime", "run_web_shell", "shutdown_web_runtime"]
