from __future__ import annotations

from pathlib import Path
from typing import Any

from g3ku.agent.catalog_store import CatalogStoreManager


class MemoryCatalogBridge:
    """Catalog-only adapter for tool/skill narrowing after long-term memory rewrite."""

    def __init__(self, workspace: Path, config: Any) -> None:
        self._catalog = CatalogStoreManager(workspace, config)
        self.store = getattr(self._catalog, "store", None)

    async def sync_catalog(self, service: Any) -> Any:
        return await self._catalog.sync_catalog(service)

    async def ensure_catalog_bootstrap(self, service: Any) -> Any:
        return await self._catalog.ensure_catalog_bootstrap(service)

    async def semantic_search_context_records(self, **kwargs: Any) -> Any:
        return await self._catalog.semantic_search_context_records(**kwargs)

    async def list_context_records(self, **kwargs: Any) -> Any:
        return await self._catalog.list_context_records(**kwargs)

    async def put_context_record(self, **kwargs: Any) -> Any:
        return await self._catalog.put_context_record(**kwargs)

    async def delete_context_record(self, **kwargs: Any) -> Any:
        return await self._catalog.delete_context_record(**kwargs)

    def close(self) -> None:
        self._catalog.close()
