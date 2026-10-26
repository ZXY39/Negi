"""Per-image sticker metadata: captions, tags and content-addressed ids.

``<pack>/semantic/metadata.json`` holds one entry per image::

    {"entries": {"<entry_id>": {"category": "happy", "path": "happy/h1.png",
                                "caption": "...", "tags": ["..."],
                                "visible_text": "...", "sha256": "..."}}}

``entry_id`` is ``sha256(image bytes + category + relative path)[:16]``. Identity
changes when the content, the category or the path changes, which is what makes
an id safe to hand to the model: a stale or forged id either fails to resolve or
resolves to a file whose bytes no longer match.

Search is lexical on purpose: packs are tens-to-hundreds of images, the model
already writes the query, and a token index over captions+tags beats standing up
an embedding pipeline for that scale. The interface (``query -> candidates``)
does not change if a vector backend is added later.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from g3ku.stickers.catalog import (
    DISABLED_PREFIX,
    Pack,
    pack_images,
    scan_packs,
)

METADATA_DIRNAME = "semantic"
METADATA_FILENAME = "metadata.json"
ENTRY_ID_LEN = 16
ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9_]+")
REVIEW_CATEGORY = "_review"


@dataclass(frozen=True)
class StickerEntry:
    entry_id: str
    pack: str
    category: str
    relpath: str
    caption: str
    tags: tuple[str, ...]
    visible_text: str
    sha256: str

    @property
    def path(self) -> Path:
        return Path(self.relpath)


def entry_id_for(*, image_bytes: bytes, category: str, relpath: str) -> str:
    digest = hashlib.sha256()
    digest.update(image_bytes)
    digest.update(str(category).encode("utf-8"))
    digest.update(str(relpath).replace("\\", "/").encode("utf-8"))
    return digest.hexdigest()[:ENTRY_ID_LEN]


def metadata_path(pack: Pack) -> Path:
    return pack.path / METADATA_DIRNAME / METADATA_FILENAME


def load_entries(pack: Pack) -> tuple[StickerEntry, ...]:
    """Validated entries for one pack; malformed rows are dropped, not raised."""
    path = metadata_path(pack)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    raw_entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(raw_entries, dict):
        return ()
    entries: list[StickerEntry] = []
    for raw_id, item in raw_entries.items():
        if not isinstance(item, dict):
            continue
        entry_id = str(raw_id or "").strip().lower()
        relpath = str(item.get("path") or "").strip().replace("\\", "/")
        category = str(item.get("category") or "").strip()
        if not ID_RE.match(entry_id) or not relpath or not category:
            continue
        if ".." in Path(relpath).parts or not (pack.path / relpath).is_file():
            # 文件被删/被换路径的条目不再作为候选；哈希校验留给 resolve_entry
            # （读取字节比 stat 贵，只在真的选定那一张时做）。
            continue
        tags = item.get("tags")
        entries.append(
            StickerEntry(
                entry_id=entry_id,
                pack=pack.key,
                category=category,
                relpath=relpath,
                caption=str(item.get("caption") or "").strip(),
                tags=tuple(str(tag).strip() for tag in tags if str(tag).strip())
                if isinstance(tags, list)
                else (),
                visible_text=str(item.get("visible_text") or "").strip(),
                sha256=str(item.get("sha256") or "").strip().lower(),
            )
        )
    return tuple(entries)


def _terms(text: str) -> set[str]:
    normalized = str(text or "").lower()
    terms: set[str] = set(_WORD_RE.findall(normalized))
    for run in _CJK_RE.findall(normalized):
        if len(run) == 1:
            terms.add(run)
            continue
        # 中文没有空格：用二元组当词项，够用且不需要分词器。
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return {term for term in terms if len(term) >= 2 or not term.isascii()}


def _score(entry: StickerEntry, terms: set[str]) -> int:
    haystack = " ".join((entry.caption, " ".join(entry.tags), entry.visible_text, entry.category)).lower()
    return sum(1 for term in terms if term in haystack)


@dataclass(frozen=True)
class StickerHit:
    pack: str
    entry_id: str
    category: str
    caption: str
    tags: tuple[str, ...]


def search(
    query: str,
    *,
    packs: Iterable[Pack] | None = None,
    limit: int = 5,
    min_score: int = 1,
) -> tuple[StickerHit, ...]:
    """Lexical candidates ordered by term overlap then id (deterministic)."""
    terms = _terms(query)
    if not terms:
        return ()
    resolved = tuple(packs) if packs is not None else scan_packs()
    scored: list[tuple[int, str, StickerEntry]] = []
    for pack in resolved:
        for entry in load_entries(pack):
            score = _score(entry, terms)
            if score >= min_score:
                scored.append((score, entry.entry_id, entry))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return tuple(
        StickerHit(
            pack=entry.pack,
            entry_id=entry.entry_id,
            category=entry.category,
            caption=entry.caption,
            tags=entry.tags,
        )
        for _score_value, _id, entry in scored[: max(1, int(limit))]
    )


def resolve_entry(pack: Pack, id_prefix: str) -> StickerEntry | None:
    """Unique prefix match whose file still exists and matches the stored hash."""
    prefix = str(id_prefix or "").strip().lower()
    if not ID_RE.match(prefix):
        return None
    matches = [entry for entry in load_entries(pack) if entry.entry_id.startswith(prefix)]
    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning("sticker id prefix {!r} is ambiguous in pack {!r}", prefix, pack.key)
        return None
    entry = matches[0]
    if entry.category.startswith(DISABLED_PREFIX) or entry.category == REVIEW_CATEGORY:
        return None
    images = {image.resolve() for image in pack_images(pack, entry.category)}
    target = (pack.path / entry.relpath).resolve()
    if target not in images or not target.is_file():
        logger.warning("sticker entry {!r} is gone or hidden; nothing sent", entry.entry_id)
        return None
    if entry.sha256:
        try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError:
            return None
        if digest != entry.sha256:
            logger.warning("sticker entry {!r} content changed; nothing sent", entry.entry_id)
            return None
    return entry


def write_metadata(
    pack: Pack,
    rows: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Write ``metadata.json`` from ``{relpath: {caption, tags, ...}}``.

    ``category``/``sha256``/``entry_id`` are derived here, never taken from the
    caller: identity must follow the bytes on disk.
    """
    entries: dict[str, dict[str, Any]] = {}
    for relpath, row in rows.items():
        normalized = str(relpath or "").strip().replace("\\", "/")
        if not normalized or ".." in Path(normalized).parts:
            continue
        target = pack.path / normalized
        if not target.is_file():
            continue
        category = normalized.split("/", 1)[0]
        try:
            image_bytes = target.read_bytes()
        except OSError:
            continue
        entry_id = entry_id_for(image_bytes=image_bytes, category=category, relpath=normalized)
        entries[entry_id] = {
            "category": category,
            "path": normalized,
            "caption": str(row.get("caption") or "").strip(),
            "tags": [str(tag).strip() for tag in (row.get("tags") or []) if str(tag).strip()],
            "visible_text": str(row.get("visible_text") or "").strip(),
            "sha256": hashlib.sha256(image_bytes).hexdigest(),
        }
    payload = {"schema_version": 1, "pack": pack.key, "entries": entries}
    path = metadata_path(pack)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


__all__ = [
    "ENTRY_ID_LEN",
    "ID_RE",
    "METADATA_DIRNAME",
    "METADATA_FILENAME",
    "REVIEW_CATEGORY",
    "StickerEntry",
    "StickerHit",
    "entry_id_for",
    "load_entries",
    "metadata_path",
    "resolve_entry",
    "search",
    "write_metadata",
]
