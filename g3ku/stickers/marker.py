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
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from g3ku.stickers import registry
from g3ku.stickers.catalog import (
    Pack,
    category_owners,
    find_pack,
    scan_packs,
)

MAX_STICKERS_PER_REPLY = 1

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
MARKER_RE = re.compile(rf"^\[\[sticker:({_SEGMENT})/({_SEGMENT})\]\]$")
# 索引取图：`[[sticker:<pack>/<category>/<N>]]`。N 是该分类注册表里的编号，
# 也就是文件名（001.png）；三段数字与两段分类键不可能混淆。
INDEX_MARKER_RE = re.compile(rf"^\[\[sticker:({_SEGMENT})/({_SEGMENT})/(\d{{1,3}})\]\]$")
SHORT_MARKER_RE = re.compile(rf"^\[\[sticker:({_SEGMENT})\]\]$")
_MARKER_PREFIX = "[[sticker:"


def _parse_marker_line(
    line: str, packs: tuple[Pack, ...]
) -> tuple[str, str, int | None] | str | None:
    """Return ``(pack, category, index_or_None)``, ``"ambiguous"``, or ``None``."""
    stripped = line.strip()
    by_index = INDEX_MARKER_RE.match(stripped)
    if by_index is not None:
        return by_index.group(1), by_index.group(2), int(by_index.group(3))
    full = MARKER_RE.match(stripped)
    if full is not None:
        return full.group(1), full.group(2), None
    short = SHORT_MARKER_RE.match(stripped)
    if short is not None:
        category = short.group(1)
        owners = category_owners(packs, category)
        if len(owners) == 1:
            return owners[0].key, category, None
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
    packs: tuple[Pack, ...],
    pack_key: str,
    category_key: str,
    picker: random.Random,
    avoid: set[Path],
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
    registry.ensure_bootstrap(pack.path)
    images = registry.registered_files(pack.path, category_key)
    if not images:
        logger.warning(
            "sticker category {!r} in pack {!r} has no registered image; nothing sent",
            category_key,
            pack_key,
        )
        return None
    # 同一回复里的随机标记自动去重：先从本轮还没发过的图里挑。分类总数小于要发送
    # 的张数时没有可选余地，退回全集——这是唯一允许重复的情形。
    fresh = [path for path in images if path not in avoid]
    return picker.choice(fresh or images)


def _pick_by_index(packs: tuple[Pack, ...], pack_key: str, category_key: str, index: int) -> Path | None:
    """Resolve ``[[sticker:pack/cat/N]]`` against the pack registry."""
    pack = find_pack(packs, pack_key)
    if pack is None:
        logger.warning("sticker pack {!r} is not in the catalog; nothing sent", pack_key)
        return None
    registry.ensure_bootstrap(pack.path)
    path = registry.resolve_index(pack.path, category_key, index)
    if path is None:
        logger.warning(
            "sticker index {}/{} did not resolve in pack {!r}; nothing sent",
            category_key,
            index,
            pack_key,
        )
    return path


def _marker_label(line: str) -> str:
    """Breadcrumb text for a marker line that did not resolve to a key."""
    text = str(line or "").strip()
    if text.startswith(_MARKER_PREFIX) and text.endswith("]]"):
        inner = text[len(_MARKER_PREFIX):-2].strip()
        if inner:
            return inner[:80]
    return text[:80]


@dataclass(frozen=True)
class StickerReply:
    """One reply's sticker intent, and what egress actually produced."""

    text: str
    # 进模型上下文的版本：随机/短标记被替换为实际发送的序号
    # （``[[sticker:pack/category/N]]``），未发出的标记行不进入上下文。
    # 只在 assistant 行第一次写入 baseline 时生成，之后不得再改写。
    context_text: str = ""
    images: tuple[Path, ...] = ()
    delivered: tuple[str, ...] = ()
    undelivered: tuple[str, ...] = ()

    @property
    def memory_text(self) -> str:
        """The reply as the operator saw it, plus whether the sticker landed.

        The memory review window gets this instead of the model's raw output:
        ``[[sticker:pack/category]]`` is meaningless to a text summariser, and a
        marker that never resolved (hidden category, deleted pack, unknown key)
        must not be recorded as "the assistant sent a sticker".
        """
        notes = [f"[已发表情包：{label}]" for label in self.delivered]
        notes.extend(f"[表情包未发出：{label}]" for label in self.undelivered)
        if not notes:
            return self.text
        if not self.text:
            return "\n".join(notes)
        return f"{self.text}\n\n" + "\n".join(notes)


def resolve_reply_stickers(
    text: str,
    *,
    root: Path | None = None,
    max_per_reply: int | None = None,
    rng: random.Random | None = None,
) -> StickerReply:
    """Resolve one reply: strip marker lines, pick images, label what happened.

    Random markers (``pack/category`` and short form) are de-duplicated within
    the reply: each one picks an image not already chosen by an earlier marker.
    A category smaller than the requested count falls back to repeats -- with
    nothing left to choose, sending the same picture twice is the only outcome.
    """
    if not isinstance(text, str):
        return StickerReply(text="")
    if _MARKER_PREFIX not in text:
        return StickerReply(text=text, context_text=text)
    from g3ku.stickers import config as sticker_config

    packs = scan_packs(root)
    picker = rng if rng is not None else random.Random(hashlib.sha256(text.encode("utf-8")).hexdigest())
    limit = max(0, int(max_per_reply if max_per_reply is not None else sticker_config.max_per_reply()))
    kept: list[str] = []
    context_kept: list[str] = []
    chosen: list[Path] = []
    delivered: list[str] = []
    undelivered: list[str] = []
    dropped = False
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        parsed = _parse_marker_line(line, packs)
        if parsed is None:
            if _looks_like_marker(line):
                dropped = True
                undelivered.append(_marker_label(line))
                logger.warning(
                    "sticker marker syntax not recognised; dropped: {!r}", line.strip()[:80]
                )
                continue
            kept.append(raw_line)
            context_kept.append(raw_line)
            continue
        dropped = True
        # From here on the line is always dropped from user-visible text: a marker
        # must never surface. A resolved one re-enters only as the sent index in
        # context_text, never as the user's original two-segment marker.
        if parsed == "ambiguous":
            undelivered.append(_marker_label(line))
            continue
        if len(chosen) >= limit:
            # 配额是策略不是失败：已经在上面记过一次 warning，不再进未发出清单。
            logger.warning(
                "sticker marker dropped: per-reply cap {} already reached", limit
            )
            continue
        pack_key, value, index = parsed
        if index is not None:
            path = _pick_by_index(packs, pack_key, value, index)
        else:
            path = _pick_path(packs, pack_key, value, picker, set(chosen))
        if path is None:
            label = f"{pack_key}/{value}/{index}" if index is not None else f"{pack_key}/{value}"
            undelivered.append(label)
            continue
        chosen.append(path)
        # 发出去的这一张必须带编号：随机取图时只有运行时知道选了哪张，而用户是对
        # 屏幕上那张图表态的。编号即文件名、注册后永不复用，所以这个键长期可解引用。
        sent_index = index if index is not None else registry.index_of(path.name)
        delivered.append(f"{pack_key}/{value}/{sent_index}")
        ending = raw_line[len(line):]
        context_kept.append(f"[[sticker:{pack_key}/{value}/{sent_index}]]{ending}")
    if not dropped:
        return StickerReply(text=text, context_text=text)
    # 删掉末尾的标记行会留下一个悬空换行；渠道正文本来就被 sanitize 收过边。
    return StickerReply(
        text="".join(kept).rstrip(),
        context_text="".join(context_kept).rstrip(),
        images=tuple(chosen),
        delivered=tuple(delivered),
        undelivered=tuple(undelivered),
    )


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
    reply = resolve_reply_stickers(
        text, root=root, max_per_reply=max_per_reply, rng=rng
    )
    return reply.text, reply.images


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
    "StickerReply",
    "render_marker_images",
    "resolve_markers",
    "resolve_reply_stickers",
    "strip_markers_for_stream",
]
