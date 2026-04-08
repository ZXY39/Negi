from __future__ import annotations

from typing import Any


async def rewrite_frontdoor_catalog_queries(
    *,
    loop: Any,
    memory_manager: Any | None,
    query_text: str,
    visible_skills: list[Any],
    visible_families: list[Any],
) -> dict[str, str]:
    raise NotImplementedError("rewrite_frontdoor_catalog_queries is not implemented")


async def rerank_frontdoor_catalog_records(
    *,
    memory_manager: Any | None,
    query_text: str,
    records: list[Any],
    top_n: int,
) -> list[Any]:
    raise NotImplementedError("rerank_frontdoor_catalog_records is not implemented")


async def build_frontdoor_catalog_selection(
    *,
    loop: Any,
    memory_manager: Any | None,
    query_text: str,
    visible_skills: list[Any],
    visible_families: list[Any],
    skill_limit: int,
    tool_limit: int,
) -> dict[str, Any]:
    raise NotImplementedError("build_frontdoor_catalog_selection is not implemented")
