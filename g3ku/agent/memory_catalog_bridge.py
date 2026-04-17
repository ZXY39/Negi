from __future__ import annotations

from pathlib import Path
from typing import Any

from g3ku.agent.rag_memory import MemoryManager as LegacyCatalogManager


class MemoryCatalogBridge:
    """Thin adapter that preserves catalog/search capabilities during the rewrite."""

    def __init__(self, workspace: Path, config: Any) -> None:
        self._legacy = LegacyCatalogManager(workspace, config)
        self.store = getattr(self._legacy, "store", None)

    async def sync_catalog(self, service: Any) -> Any:
        return await self._legacy.sync_catalog(service)

    async def ensure_catalog_bootstrap(self, service: Any) -> Any:
        return await self._legacy.ensure_catalog_bootstrap(service)

    async def semantic_search_context_records(self, **kwargs: Any) -> Any:
        return await self._legacy.semantic_search_context_records(**kwargs)

    async def list_context_records(self, **kwargs: Any) -> Any:
        return await self._legacy.list_context_records(**kwargs)

    async def put_context_record(self, **kwargs: Any) -> Any:
        return await self._legacy.put_context_record(**kwargs)

    async def delete_context_record(self, **kwargs: Any) -> Any:
        return await self._legacy.delete_context_record(**kwargs)

    def close(self) -> None:
        self._legacy.close()
