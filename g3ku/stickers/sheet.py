"""Contact-sheet rendering for one sticker category.

One registered category → one image, every tile badged with its registry index.
The measured recipe (see work/sticker-contact-sheet-plan.md) is load-bearing:

- 200px tiles: bigger tiles bought nothing (a 1648px sheet scored the same as
  1048px), smaller ones lose the fine cue that separates sibling emoji.
- content-bbox crop + fit-resize: stickers whose art occupies a corner of a big
  canvas otherwise shrink below legible size. ``Image.thumbnail`` only shrinks,
  so the crop result has to be explicitly resized up.
- equal aspect fit (letterbox): never stretch; aspect ratio decides how much of
  the tile the art fills.
- badge under the art: a filled dark rounded rectangle + white numeral, because
  bare white text disappears on pale stickers.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from PIL import Image, ImageChops, ImageDraw, ImageFont

from g3ku.stickers import registry
from g3ku.stickers.catalog import Pack, stickers_root

SHEET_SPEC_VERSION = 1
TILE_PX = 200
GAP_PX = 8
MAX_SHEET_BYTES = 300 * 1024
CACHE_KEEP = 200
_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
)
_BADGE_FILL = (200, 32, 48)
_BACKGROUND = (24, 26, 30)
_BORDER = (90, 96, 106)


@dataclass(frozen=True)
class StickerSheet:
    path: Path
    mime_type: str
    count: int
    columns: int
    rows: int
    sheet_id: str
    bytes: int

    @property
    def width(self) -> int:
        return self.columns * TILE_PX + (self.columns + 1) * GAP_PX

    @property
    def height(self) -> int:
        return self.rows * TILE_PX + (self.rows + 1) * GAP_PX


def sheet_cache_dir() -> Path:
    """Under ``.g3ku/`` on purpose: anything inside ``stickers/`` gets scanned."""
    return stickers_root().parent / ".g3ku" / "sticker-sheets"


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _first_frame(path: Path) -> Image.Image:
    with Image.open(path) as handle:
        try:
            handle.seek(0)
        except (EOFError, AttributeError):
            pass
        return handle.convert("RGBA")


def _content_crop(frame: Image.Image, *, pad: int = 6) -> Image.Image:
    alpha = frame.getchannel("A")
    bbox = alpha.getbbox() if alpha.getextrema()[0] < 255 else None
    if bbox is None:
        background = frame.getpixel((0, 0))
        solid = Image.new("RGBA", frame.size, background)
        # 阈值二值化：GIF 深色画布带抖动噪声，裸差分的 bbox 会退化成整幅。
        diff = (
            ImageChops.difference(frame, solid)
            .convert("L")
            .point(lambda value: 255 if value > 24 else 0)
        )
        bbox = diff.getbbox()
    if not bbox:
        return frame
    left, top, right, bottom = bbox
    return frame.crop(
        (
            max(0, left - pad),
            max(0, top - pad),
            min(frame.width, right + pad),
            min(frame.height, bottom + pad),
        )
    )


def sheet_id_for(pack: Pack, category: str, entries: tuple[tuple[int, Path], ...]) -> str:
    rows = registry.category_rows(pack.path, category)
    payload = {
        "spec": SHEET_SPEC_VERSION,
        "pack": pack.key,
        "category": category,
        "entries": [
            [index, path.name, str((rows.get(path.name) or {}).get("sha256") or "")]
            for index, path in entries
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _prune_cache(keep: int = CACHE_KEEP) -> None:
    cache = sheet_cache_dir()
    try:
        files = sorted(
            (item for item in cache.iterdir() if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except OSError:
            continue


def render_sheet(pack: Pack, category: str, *, refresh: bool = False) -> StickerSheet | None:
    """Render (or reuse) the contact sheet for one category; ``None`` when empty."""
    registry.ensure_bootstrap(pack.path)
    entries = registry.registered_entries(pack.path, category)
    if not entries:
        return None
    entries = entries[: registry.MAX_IMAGES_PER_CATEGORY]
    sheet_id = sheet_id_for(pack, category, entries)
    cache = sheet_cache_dir()
    png_path = cache / f"{sheet_id}.png"
    jpg_path = cache / f"{sheet_id}.jpg"
    columns = math.ceil(math.sqrt(len(entries)))
    rows = math.ceil(len(entries) / columns)
    for cached, mime in ((png_path, "image/png"), (jpg_path, "image/jpeg")):
        if cached.is_file() and not refresh:
            return StickerSheet(
                path=cached,
                mime_type=mime,
                count=len(entries),
                columns=columns,
                rows=rows,
                sheet_id=sheet_id,
                bytes=cached.stat().st_size,
            )

    cache.mkdir(parents=True, exist_ok=True)
    width = columns * TILE_PX + (columns + 1) * GAP_PX
    height = rows * TILE_PX + (rows + 1) * GAP_PX
    sheet = Image.new("RGB", (width, height), _BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    badge_font = _font(max(14, int(TILE_PX * 0.22)))

    for position, (index, path) in enumerate(entries):
        row, column = divmod(position, columns)
        left = GAP_PX + column * (TILE_PX + GAP_PX)
        top = GAP_PX + row * (TILE_PX + GAP_PX)
        try:
            frame = _content_crop(_first_frame(path))
        except (OSError, ValueError):
            logger.warning("sticker sheet skipped unreadable image {}", path)
            continue
        scale = min(TILE_PX / frame.width, TILE_PX / frame.height)
        if scale != 1:
            frame = frame.resize(
                (max(1, round(frame.width * scale)), max(1, round(frame.height * scale))),
                Image.LANCZOS,
            )
        offset_x = left + (TILE_PX - frame.width) // 2
        offset_y = top + (TILE_PX - frame.height) // 2
        sheet.paste(frame, (offset_x, offset_y), frame)
        draw.rectangle(
            [left, top, left + TILE_PX - 1, top + TILE_PX - 1],
            outline=_BORDER,
            width=2,
        )
        label = f"{index:03d}" if index >= 100 else str(index)
        box = draw.textbbox((0, 0), label, font=badge_font)
        text_w, text_h = box[2] - box[0], box[3] - box[1]
        pad = max(4, int(TILE_PX * 0.05))
        badge = [
            left + 3,
            top + 3,
            left + 3 + text_w + pad * 2,
            top + 3 + text_h + pad * 2,
        ]
        draw.rounded_rectangle(badge, radius=max(4, pad), fill=_BADGE_FILL)
        draw.text(
            (badge[0] + pad - box[0], badge[1] + pad - box[1]),
            label,
            font=badge_font,
            fill=(255, 255, 255),
        )

    target = png_path
    sheet.save(target, "PNG", optimize=True)
    mime = "image/png"
    if target.stat().st_size > MAX_SHEET_BYTES:
        quality = 85
        while True:
            sheet.convert("RGB").save(jpg_path, "JPEG", quality=quality, optimize=True)
            if jpg_path.stat().st_size <= MAX_SHEET_BYTES or quality <= 45:
                break
            quality -= 10
        try:
            png_path.unlink()
        except OSError:
            pass
        target, mime = jpg_path, "image/jpeg"

    _prune_cache()
    return StickerSheet(
        path=target,
        mime_type=mime,
        count=len(entries),
        columns=columns,
        rows=rows,
        sheet_id=sheet_id,
        bytes=target.stat().st_size,
    )


__all__ = [
    "MAX_SHEET_BYTES",
    "SHEET_SPEC_VERSION",
    "StickerSheet",
    "TILE_PX",
    "render_sheet",
    "sheet_cache_dir",
    "sheet_id_for",
]
