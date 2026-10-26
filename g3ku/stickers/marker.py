"""Sticker markers: the model writes a key, the runtime finds the files.

The model never names a path. It emits a whole line::

    [[sticker:atri/happy]]

and this module turns it into local image paths. Two properties matter:

- **A marker never survives.** Every recognised marker line is removed from the
  text, including the ones that fail to resolve. A bad key degrades to "no
  image", never to a leaked marker or a stray file name.
- **Resolution is stateless.** ``strip_markers_for_stream`` works on the
  cumulative reply text, so the streaming relay needs no parser state and can
  hold back a half-written marker without remembering anything.
"""

from __future__ import annotations

import random
import re
from pathlib import Path

from loguru import logger

from g3ku.stickers.catalog import (
    Pack,
    category_owners,
    find_pack,
    pack_images,
    scan_packs,
)

MAX_STICKERS_PER_REPLY = 1

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
MARKER_RE = re.compile(rf"^\[\[sticker:({_SEGMENT})/({_SEGMENT})\]\]$")
SHORT_MARKER_RE = re.compile(rf"^\[\[sticker:({_SEGMENT})\]\]$")
_MARKER_PREFIX = "[[sticker:"


def _parse_marker_line(line: str, packs: tuple[Pack, ...]) -> tuple[str, str] | str | None:
    """Return ``(pack, category)``, ``"ambiguous"``, or ``None`` (not a marker)."""
    stripped = line.strip()
    full = MARKER_RE.match(stripped)
    if full is not None:
        return full.group(1), full.group(2)
    short = SHORT_MARKER_RE.match(stripped)
    if short is not None:
        category = short.group(1)
        owners = category_owners(packs, category)
        if len(owners) == 1:
            return owners[0].key, category
        # Zero owners and cross-pack duplicates both fail closed: guessing here
        # is how a reply ends up with an image from the wrong pack.
        logger.warning(
            "sticker short marker is ambiguous ({} packs expose {!r}); nothing sent",
            len(owners),
            category,
        )
        return "ambiguous"
    return None


def _is_complete_marker_syntax(line: str) -> bool:
    stripped = line.strip()
    return bool(MARKER_RE.match(stripped) or SHORT_MARKER_RE.match(stripped))


def _pick_path(
    packs: tuple[Pack, ...], pack_key: str, category_key: str, picker: random.Random
) -> Path | None:
    pack = find_pack(packs, pack_key)
    if pack is None:
        logger.warning("sticker pack {!r} is not in the catalog; nothing sent", pack_key)
        return None
    if pack.category(category_key) is None:
        logger.warning(
            "sticker category {!r} does not exist in pack {!r}; nothing sent",
            category_key,
            pack_key,
        )
        return None
    images = pack_images(pack, category_key)
    if not images:
        logger.warning(
            "sticker category {!r} in pack {!r} has no readable image; nothing sent",
            category_key,
            pack_key,
        )
        return None
    return picker.choice(images)


def resolve_markers(
    text: str,
    *,
    root: Path | None = None,
    max_per_reply: int = MAX_STICKERS_PER_REPLY,
    rng: random.Random | None = None,
) -> tuple[str, tuple[Path, ...]]:
    """Drop every sticker marker line and return the images they asked for.

    ``max_per_reply <= 0`` is a kill switch: markers are still removed, but no
    image is produced.
    """
    if not isinstance(text, str) or _MARKER_PREFIX not in text:
        return text, ()
    packs = scan_packs(root)
    picker = rng if rng is not None else random
    limit = max(0, int(max_per_reply))
    kept: list[str] = []
    chosen: list[Path] = []
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        parsed = _parse_marker_line(line, packs)
        if parsed is None:
            kept.append(raw_line)
            continue
        # From here on the line is always dropped: a marker must never surface.
        if parsed == "ambiguous":
            continue
        if len(chosen) >= limit:
            logger.warning(
                "sticker marker dropped: per-reply cap {} already reached", limit
            )
            continue
        path = _pick_path(packs, parsed[0], parsed[1], picker)
        if path is not None:
            chosen.append(path)
    return "".join(kept), tuple(chosen)


def _hold_partial_marker(text: str) -> str:
    """Drop a trailing line that may still grow into a marker."""
    head, sep, tail = text.rpartition("\n")
    candidate = tail.strip()
    if not candidate or "]]" in candidate:
        return text
    if candidate.startswith(_MARKER_PREFIX) or _MARKER_PREFIX.startswith(candidate):
        return head + sep if sep else ""
    return text


def strip_markers_for_stream(text: str) -> str:
    """Stream-safe scrub: no complete marker, no half-written marker."""
    if not isinstance(text, str) or "[[" not in text:
        return text
    kept = [
        raw_line
        for raw_line in text.splitlines(keepends=True)
        if not _is_complete_marker_syntax(raw_line.rstrip("\r\n"))
    ]
    return _hold_partial_marker("".join(kept))


__all__ = [
    "MARKER_RE",
    "MAX_STICKERS_PER_REPLY",
    "SHORT_MARKER_RE",
    "resolve_markers",
    "strip_markers_for_stream",
]
