from __future__ import annotations


def build(runtime):
    from g3ku.stickers.config import semantic_search_enabled

    if not semantic_search_enabled():
        return None
    from g3ku.agent.tools.sticker_search import StickerSearchTool

    return StickerSearchTool()
