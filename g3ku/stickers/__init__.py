"""Sticker packs: a directory tree the runtime can turn into images on egress.

Two halves, neither of which touches the transport:

- ``catalog`` scans ``<workspace>/stickers/<pack>/<category>/<image>`` and
  renders the model-facing listing.
- ``marker`` turns a whole-line ``[[sticker:<pack>/<category>]]`` marker into
  local image paths, so the existing markdown/attachment egress does the rest.
"""

from __future__ import annotations

from g3ku.stickers.catalog import (
    MANIFEST_NAME,
    STICKER_HEADER,
    STICKERS_DIRNAME,
    Pack,
    StickerCategory,
    render_catalog_lines,
    safe_scan_packs,
    scan_packs,
    sticker_contract_sections,
    sticker_contract_sections_from_text,
    sticker_pack_sections,
    sticker_sections_from_pinned_text,
    stickers_root,
)
from g3ku.stickers.marker import (
    MAX_STICKERS_PER_REPLY,
    StickerReply,
    render_marker_images,
    resolve_markers,
    resolve_reply_stickers,
    strip_markers_for_stream,
)

__all__ = [
    "MANIFEST_NAME",
    "MAX_STICKERS_PER_REPLY",
    "STICKERS_DIRNAME",
    "STICKER_HEADER",
    "Pack",
    "StickerCategory",
    "StickerReply",
    "render_catalog_lines",
    "render_marker_images",
    "resolve_markers",
    "resolve_reply_stickers",
    "safe_scan_packs",
    "scan_packs",
    "sticker_contract_sections",
    "sticker_contract_sections_from_text",
    "sticker_pack_sections",
    "sticker_sections_from_pinned_text",
    "stickers_root",
    "strip_markers_for_stream",
]
