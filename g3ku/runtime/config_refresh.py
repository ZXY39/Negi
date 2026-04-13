from __future__ import annotations

from loguru import logger

from g3ku.config.live_runtime import get_runtime_config
from g3ku.providers.chatmodels import build_chat_model
from g3ku.security import get_bootstrap_security_service


def refresh_loop_runtime_config(loop, *, force: bool = False, reason: str = "runtime") -> bool:
    if force:
        security = get_bootstrap_security_service(
            getattr(getattr(loop, "app_config", None), "workspace_path", None)
        )
        security.reload_overlay_from_disk()
    config, revision, changed = get_runtime_config(force=force)
    if not changed and int(getattr(loop, "_runtime_model_revision", 0) or 0) == int(revision or 0):
        return False

    provider_name, model_name = config.get_role_model_target("ceo")
    provider = build_chat_model(config, role="ceo")
    loop.app_config = config
    loop.provider = provider
    loop.model_client = provider
    loop.multi_agent_config = config.agents.multi_agent
    loop.provider_name = provider_name
    loop.model = model_name
    loop.temperature = config.agents.defaults.temperature
    loop.max_tokens = config.agents.defaults.max_tokens
    loop.max_iterations = config.get_role_max_iterations("ceo")
    loop.reasoning_effort = config.agents.defaults.reasoning_effort
    loop._runtime_model_revision = revision
    loop._runtime_default_model_key = config.resolve_role_model_key("ceo")

    resource_manager = getattr(loop, "resource_manager", None)
    if resource_manager is not None and hasattr(resource_manager, "bind_app_config"):
        resource_manager.bind_app_config(config)
        resource_manager.reload_now(trigger=reason)

    bootstrap = getattr(loop, "_bootstrap", None)
    if bootstrap is not None and hasattr(bootstrap, "sync_internal_tool_runtimes"):
        bootstrap.sync_internal_tool_runtimes(force=True, reason=reason)

    service = getattr(loop, "main_task_service", None)
    if service is not None and hasattr(service, "ensure_runtime_config_current"):
        service.ensure_runtime_config_current(force=False, reason=reason)

    if hasattr(loop, "_ceo_model_chain_cache_key"):
        loop._ceo_model_chain_cache_key = None
    if hasattr(loop, "_ceo_model_client_cache"):
        loop._ceo_model_client_cache = None

    logger.info("Loop runtime config refreshed revision={} reason={}", revision, reason)
    return True


__all__ = ["refresh_loop_runtime_config"]
