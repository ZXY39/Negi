"""Search the sticker library and hand back candidate ids."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.stickers import semantic


class StickerSearchTool(Tool):
    """Lexical search over per-image captions and tags.

    Registered only when ``stickers.semantic_search`` is on: adding a tool costs
    one `tools[]` prefix invalidation, and the feature needs captions that a
    fresh library does not have yet.
    """

    hide_universal_timeout_parameter = True

    @property
    def name(self) -> str:
        return "search_stickers"

    @property
    def description(self) -> str:
        return (
            "Search the sticker library by what you want to express. Use it when a "
            "guard-rail category is too coarse: pass a short description of the mood, "
            "attitude or bit you want to convey — keep the object of that attitude "
            "(self-mockery vs teasing the user). Returns candidate ids; to send one, "
            "put `[[sticker:<pack>/@<id>]]` alone on the last line. Search at most once "
            "per reply, and do not invent ids — an id outside the candidate list sends "
            "nothing. If no candidate fits, reply normally without a sticker."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Short description of the emotion/attitude/bit you want to send, "
                        "in the language of the reply."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Optional candidate count (default 5, max 10).",
                },
            },
            "required": ["query"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get("query") or "").strip():
            errors.append("query must not be empty")
        return errors

    async def execute(self, query: str, limit: int = 5, **kwargs: Any) -> str:
        _ = kwargs
        hits = semantic.search(str(query or ""), limit=max(1, min(10, int(limit or 5))))
        if not hits:
            return json.dumps(
                {
                    "ok": False,
                    "reason": (
                        "no candidate matched; reply normally without a sticker, or try a "
                        "query closer to the catalog's own wording"
                    ),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "candidates": [
                    {
                        "id": hit.entry_id,
                        "pack": hit.pack,
                        "category": hit.category,
                        "caption": hit.caption,
                        "tags": list(hit.tags),
                    }
                    for hit in hits
                ],
                "how_to_send": "Put `[[sticker:<pack>/@<id>]]` alone on the last line.",
            },
            ensure_ascii=False,
        )


__all__ = ["StickerSearchTool"]
