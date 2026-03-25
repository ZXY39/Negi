from __future__ import annotations

from typing import Any

from g3ku.config.schema import Config


def make_provider(config: Config, *, scope: str = "ceo"):
    """Create the configured chat model for a runtime scope."""
    from g3ku.providers.chatmodels import build_chat_model

    return build_chat_model(config, role=scope)


def make_agent_loop(
    config: Config,
    bus,
    provider,
    *,
    debug_mode: bool = False,
    cron_service=None,
    session_manager=None,
):
    """Create the configured agent runtime without CLI-specific side effects."""
    runtime = (config.agents.defaults.runtime or "langgraph").lower()
    if runtime != "langgraph":
        raise ValueError(
            "Original field: agents.defaults.runtime\n"
            f"Current value: {runtime!r}\n"
            "New supported value: 'langgraph' only."
        )

    from g3ku.agent.loop import AgentLoop

    try:
        from g3ku.agent.middleware import build_middlewares
    except ModuleNotFoundError as exc:
        if config.agents.defaults.middlewares:
            raise RuntimeError(
                "Runtime middleware requires optional langchain dependency. "
                "Install project extras before enabling middlewares."
            ) from exc
        middlewares: list[Any] = []
    else:
        middlewares = build_middlewares(config.agents.defaults.middlewares)

    provider_name, model_id = config.get_scope_model_target("ceo")

    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=model_id,
        provider_name=provider_name,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.get_role_max_iterations("ceo"),
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        multi_agent_config=config.agents.multi_agent,
        app_config=config,
        resource_config=config.resources,
        cron_service=cron_service,
        session_manager=session_manager,
        channels_config=config.china_bridge,
        debug_mode=debug_mode,
        middlewares=middlewares,
    )


__all__ = ["make_agent_loop", "make_provider"]
