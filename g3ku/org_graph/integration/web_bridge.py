from __future__ import annotations

from functools import lru_cache

from g3ku.org_graph.config import resolve_org_graph_config
from g3ku.org_graph.service.project_service import ProjectService
from g3ku.resources import get_shared_resource_manager


@lru_cache(maxsize=1)
def _fallback_service() -> ProjectService:
    config = resolve_org_graph_config()
    manager = get_shared_resource_manager(config.raw.workspace_path, app_config=config.raw)
    return ProjectService(config, resource_manager=manager)


def get_org_graph_service() -> ProjectService:
    try:
        from g3ku.shells.web import get_agent

        agent = get_agent()
        service = getattr(agent, 'org_graph_service', None)
        if service is not None:
            return service
    except Exception:
        pass
    return _fallback_service()


async def startup_org_graph_runtime() -> ProjectService:
    service = get_org_graph_service()
    await service.startup()
    return service


async def shutdown_org_graph_runtime() -> None:
    try:
        service = get_org_graph_service()
    except Exception:
        _fallback_service.cache_clear()
        return
    await service.close()
    _fallback_service.cache_clear()

