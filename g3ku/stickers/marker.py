"""Sticker markers: the model writes a key, the runtime finds the files.

The model never names a path. It emits a whole line::

    [[sticker:atri/happy]]

but models occasionally glue the same token to the end of the sentence
(``…呀？[[sticker:pack/category]]``). Both shapes resolve here: a marker at a
line tail is normalised onto its own line before parsing, so every lane sees
one canonical shape. Two properties matter:

- **A marker never survives.** Every recognised marker line is removed from the
  text, including the ones that fail to resolve. A bad key degrades to "no
  image", never to a leaked marker or a stray file name. The same holds for a
  marker glued to a line tail and for a half-written marker in a stream.
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
# 行尾单枚标记：键里不允许方括号，所以最后一个 `]]` 必须直接收在行尾；
# `[[1]]` 这类普通方括号不会被误当成标记的一部分。
_TAIL_MARKER_RE = re.compile(re.escape(_MARKER_PREFIX) + r"[^\[\]\r\n]*\]\]$")


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


def _split_line_ending(raw_line: str) -> tuple[str, str]:
    line = raw_line.rstrip("\r\n")
    return line, raw_line[len(line):]


def _trailing_marker_token(line: str) -> tuple[int, str] | None:
    """Return ``(offset, token)`` for a marker-like token that ends the line."""
    match = _TAIL_MARKER_RE.search(line)
    if match is None:
        return None
    return match.start(), match.group(0)


def _lift_trailing_markers(text: str) -> str:
    """Put markers glued to a line's end onto their own line.

    The prompt asks for whole marker lines; a model that appends the token to
    its sentence must not leak the syntax. Lifting the tail into the canonical
    shape lets the normal line parser resolve it, record a failed send, or drop
    an unknown shape -- without a second parsing lane. Only the line tail is
    lifted: a marker quoted mid-sentence stays prose.
    """
    if _MARKER_PREFIX not in text:
        return text
    out: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        line, ending = _split_line_ending(raw_line)
        head = line.rstrip()
        markers: list[str] = []
        while True:
            token = _trailing_marker_token(head)
            if token is None:
                break
            at, marker = token
            if at == 0:
                break
            markers.append(marker)
            head = head[:at].rstrip()
        if not markers or not head:
            out.append(raw_line)
            continue
        out.append("\n".join([head, *reversed(markers)]) + ending)
    return "".join(out)


def _strip_trailing_markers(line: str) -> str:
    """Drop marker tokens glued to one line's end, leaving prose untouched."""
    head = line.rstrip()
    stripped = False
    while True:
        token = _trailing_marker_token(head)
        if token is None:
            break
        at, _marker = token
        stripped = True
        head = head[:at].rstrip()
    return head if stripped else line


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
    # 定稿版本：只把**已经取到图**的随机/短标记换成序号形态，其余行（无法识别的、
    # 有歧义的、超每张上限的）逐字留在原位。投递前用它，之后任何车道都不再掷骰子。
    settled_text: str = ""
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
        return StickerReply(text=text, context_text=text, settled_text=text)
    # 行尾粘连的标记先归一成独占一行：解析、配额、未发出读数与定稿都复用同一条
    # 行车道，投递侧不再需要"行内标记"这第二种形状。
    text = _lift_trailing_markers(text)
    from g3ku.stickers import config as sticker_config

    packs = scan_packs(root)
    picker = rng if rng is not None else random.Random(hashlib.sha256(text.encode("utf-8")).hexdigest())
    limit = max(0, int(max_per_reply if max_per_reply is not None else sticker_config.max_per_reply()))
    kept: list[str] = []
    context_kept: list[str] = []
    settled_kept: list[str] = []
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
                settled_kept.append(raw_line)
                logger.warning(
                    "sticker marker syntax not recognised; dropped: {!r}", line.strip()[:80]
                )
                continue
            kept.append(raw_line)
            context_kept.append(raw_line)
            settled_kept.append(raw_line)
            continue
        dropped = True
        # From here on the line is always dropped from user-visible text: a marker
        # must never surface. A resolved one re-enters only as the sent index in
        # context_text, never as the user's original two-segment marker.
        # 未取到图的那些在 settled_text 里原样留着：谁投递谁负责剥掉它，
        # 而定稿这一步不许改变"发不出去"的形状（记忆面要靠它记未发出）。
        if parsed == "ambiguous":
            undelivered.append(_marker_label(line))
            settled_kept.append(raw_line)
            continue
        if len(chosen) >= limit:
            # 配额是策略不是失败：已经在上面记过一次 warning，不再进未发出清单。
            logger.warning(
                "sticker marker dropped: per-reply cap {} already reached", limit
            )
            settled_kept.append(raw_line)
            continue
        pack_key, value, index = parsed
        if index is not None:
            path = _pick_by_index(packs, pack_key, value, index)
        else:
            path = _pick_path(packs, pack_key, value, picker, set(chosen))
        if path is None:
            label = f"{pack_key}/{value}/{index}" if index is not None else f"{pack_key}/{value}"
            undelivered.append(label)
            settled_kept.append(raw_line)
            continue
        chosen.append(path)
        # 发出去的这一张必须带编号：随机取图时只有运行时知道选了哪张，而用户是对
        # 屏幕上那张图表态的。编号即文件名、注册后永不复用，所以这个键长期可解引用。
        sent_index = index if index is not None else registry.index_of(path.name)
        delivered.append(f"{pack_key}/{value}/{sent_index}")
        ending = raw_line[len(line):]
        marker_line = f"[[sticker:{pack_key}/{value}/{sent_index}]]{ending}"
        context_kept.append(marker_line)
        settled_kept.append(marker_line)
    if not dropped:
        return StickerReply(text=text, context_text=text, settled_text=text)
    # 删掉末尾的标记行会留下一个悬空换行；渠道正文本来就被 sanitize 收过边。
    return StickerReply(
        text="".join(kept).rstrip(),
        context_text="".join(context_kept).rstrip(),
        settled_text="".join(settled_kept),
        images=tuple(chosen),
        delivered=tuple(delivered),
        undelivered=tuple(undelivered),
    )


def settle_reply_sticker_markers(text: str, *, root: Path | None = None) -> str:
    """定稿这一次回复的表情包：随机标记换成它实际取到的那张的序号。

    取骰子的种子是**整段回复正文**的哈希，而投递侧有好几条车道各自洗一遍正文
    （剥回声、把本地 markdown 引用抽成附件、收边），任何一处先于取图跑，种子就变、
    图就换一张 —— 实盘 2026-11-06 01:59:59 会话里显示 ``greeting/010.gif``、QQ 收到
    ``greeting/009.gif``。所以随机只在正文产出的这一刻掷一次，之后各车道只按序号取图。
    """
    if not isinstance(text, str):
        return ""
    if _MARKER_PREFIX not in text:
        return text
    return resolve_reply_stickers(text, root=root).settled_text


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
    """Drop a trailing fragment that may still grow into a marker.

    The fragment may follow visible prose on the same line (``好耶[[sticker:at``),
    so hold from the last ``[[`` instead of discarding the whole line.
    """
    head, sep, tail = text.rpartition("\n")
    candidate = tail.rstrip()
    if not candidate or candidate.endswith("]]"):
        return text
    opener = candidate.rfind("[[")
    if opener < 0:
        return text
    fragment = candidate[opener:]
    if not (_MARKER_PREFIX.startswith(fragment) or fragment.startswith(_MARKER_PREFIX)):
        return text
    # A closed token followed by prose is a mid-sentence mention, not a pending
    # marker: only a fragment without its ``]]`` is held back.
    if "]]" in fragment:
        return text
    kept_tail = candidate[:opener].rstrip()
    prefix = head + sep if sep else ""
    return prefix + kept_tail


def strip_markers_for_stream(text: str) -> str:
    """Stream-safe scrub: no complete marker, no half-written marker."""
    if not isinstance(text, str) or "[[" not in text:
        return text
    kept: list[str] = []
    for raw_line in text.splitlines(keepends=True):
        line, ending = _split_line_ending(raw_line)
        if _looks_like_marker(line):
            continue
        kept.append(_strip_trailing_markers(line) + ending)
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
