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

import hashlib
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
# 精确取图：`[[sticker:<pack>/@<id>]]`。`@` 不在合法键字符集里，所以与分类键
# 不可能混淆；id 是内容哈希前缀，由 `search_stickers` 的候选表给出。
ID_MARKER_RE = re.compile(rf"^\[\[sticker:({_SEGMENT})/@([0-9a-f]{{12,64}})\]\]$")
SHORT_MARKER_RE = re.compile(rf"^\[\[sticker:({_SEGMENT})\]\]$")
_MARKER_PREFIX = "[[sticker:"


def _parse_marker_line(line: str, packs: tuple[Pack, ...]) -> tuple[str, str, str] | str | None:
    """Return ``(kind, pack, value)``, ``"ambiguous"``, or ``None`` (not a marker)."""
    stripped = line.strip()
    by_id = ID_MARKER_RE.match(stripped)
    if by_id is not None:
        return "id", by_id.group(1), by_id.group(2)
    full = MARKER_RE.match(stripped)
    if full is not None:
        return "category", full.group(1), full.group(2)
    short = SHORT_MARKER_RE.match(stripped)
    if short is not None:
        category = short.group(1)
        owners = category_owners(packs, category)
        if len(owners) == 1:
            return "category", owners[0].key, category
        # Zero owners and cross-pack duplicates both fail closed: guessing here
        # is how a reply ends up with an image from the wrong pack.
        logger.warning(
            "sticker short marker is ambiguous ({} packs expose {!r}); nothing sent",
            len(owners),
            category,
        )
        return "ambiguous"
    return None


def _looks_like_marker(line: str) -> bool:
    """True for any whole line that opens with our marker prefix.

    Deliberately looser than the grammar: a well-formed marker that does not
    parse (an unknown shape, or a ``_``-prefixed category the segment pattern
    rejects) must still be dropped. Leaking the raw syntax to the user is worse
    than sending nothing -- and now that imports can produce hidden ``_``
    categories, that shape is reachable.
    """
    return str(line or "").strip().startswith(_MARKER_PREFIX)


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


def _pick_by_id(packs: tuple[Pack, ...], pack_key: str, id_prefix: str) -> Path | None:
    """Resolve a ``[[sticker:pack/@id]]`` marker against the pack's metadata."""
    pack = find_pack(packs, pack_key)
    if pack is None:
        logger.warning("sticker pack {!r} is not in the catalog; nothing sent", pack_key)
        return None
    from g3ku.stickers import semantic

    entry = semantic.resolve_entry(pack, id_prefix)
    if entry is None:
        logger.warning(
            "sticker id {!r} did not resolve in pack {!r}; nothing sent", id_prefix, pack_key
        )
        return None
    return (pack.path / entry.relpath).resolve()


def resolve_markers(
    text: str,
    *,
    root: Path | None = None,
    max_per_reply: int | None = None,
    rng: random.Random | None = None,
) -> tuple[str, tuple[Path, ...]]:
    """Drop every sticker marker line and return the images they asked for.

    ``max_per_reply`` defaults to the configured ``stickers.max_per_reply``
    (0 is a kill switch: markers are still removed, but no image is produced).

    The pick is deterministic in the reply text unless ``rng`` is given: the
    persisted transcript and the live frame resolve the same reply separately,
    and they must land on the same image.
    """
    if not isinstance(text, str) or _MARKER_PREFIX not in text:
        return text, ()
    from g3ku.stickers import config as sticker_config

    packs = scan_packs(root)
    picker = rng if rng is not None else random.Random(hashlib.sha256(text.encode("utf-8")).hexdigest())
    limit = max(0, int(max_per_reply if max_per_reply is not None else sticker_config.max_per_reply()))
    kept: list[str] = []
    chosen: list[Path] = []
    dropped = False
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        parsed = _parse_marker_line(line, packs)
        if parsed is None:
            if _looks_like_marker(line):
                dropped = True
                logger.warning(
                    "sticker marker syntax not recognised; dropped: {!r}", line.strip()[:80]
                )
                continue
            kept.append(raw_line)
            continue
        dropped = True
        # From here on the line is always dropped: a marker must never surface.
        if parsed == "ambiguous":
            continue
        if len(chosen) >= limit:
            logger.warning(
                "sticker marker dropped: per-reply cap {} already reached", limit
            )
            continue
        kind, pack_key, value = parsed
        if kind == "id":
            path = _pick_by_id(packs, pack_key, value)
        else:
            path = _pick_path(packs, pack_key, value, picker)
        if path is not None:
            chosen.append(path)
    if not dropped:
        return text, ()
    # 删掉末尾的标记行会留下一个悬空换行；渠道正文本来就被 sanitize 收过边。
    return "".join(kept).rstrip(), tuple(chosen)


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
        if not _looks_like_marker(raw_line.rstrip("\r\n"))
    ]
    return _hold_partial_marker("".join(kept))


def render_marker_images(text: str, *, root: Path | None = None) -> str:
    """Rewrite marker lines into image markdown, for persisted transcripts.

    Live egress turns the paths into attachments (channel) or thumbnails (web);
    a transcript has to carry something self-describing, and image markdown is
    already rendered by the history views. The pick is the same one the live
    frame used because resolution is deterministic in the reply text.
    """
    if not isinstance(text, str) or _MARKER_PREFIX not in text:
        return text
    cleaned, paths = resolve_markers(text, root=root)
    if not paths:
        return cleaned
    return cleaned + "".join(f"\n\n![]({path})" for path in paths)


__all__ = [
    "MARKER_RE",
    "MAX_STICKERS_PER_REPLY",
    "SHORT_MARKER_RE",
    "render_marker_images",
    "resolve_markers",
    "strip_markers_for_stream",
]
