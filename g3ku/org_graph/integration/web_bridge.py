from __future__ import annotations

from functools import lru_cache

from g3ku.org_graph.config import resolve_org_graph_config
from g3ku.org_graph.service.project_service import ProjectService


@lru_cache(maxsize=1)
def get_org_graph_service() -> ProjectService:
    return ProjectService(resolve_org_graph_config())


async def startup_org_graph_runtime() -> ProjectService:
    service = get_org_graph_service()
    await service.startup()
    return service


async def shutdown_org_graph_runtime() -> None:
    try:
        service = get_org_graph_service()
    except Exception:
        get_org_graph_service.cache_clear()
        return
    await service.close()
    get_org_graph_service.cache_clear()

