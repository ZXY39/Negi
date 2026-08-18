"""CEO web media middle layer.

Rewrites assistant markdown content at the snapshot egress so local file
references become servable: raster images get a compressed thumbnail staged
inside the session upload dir (an already-allowed serving root) plus a signed
"original view" URL in the title slot; other local files get their link href
replaced with the signed viewer URL. The viewer endpoint serves originals via
unguessable HMAC tokens so external callers cannot pick arbitrary paths.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import mimetypes
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from g3ku.runtime.web_ceo_sessions import upload_dir_for_session, workspace_path
from g3ku.utils.helpers import safe_filename

router = APIRouter()

THUMBNAIL_MAX_BYTES = 100 * 1024
ORIGINAL_TOKEN_TTL_SECONDS = 24 * 60 * 60
VIEWER_ROUTE = "/api/ceo/media/original"

_TOKEN_SECRET = secrets.token_bytes(32)
_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_THUMB_LADDER = ((1280, 85), (1280, 70), (960, 70), (720, 60), (512, 50), (384, 45), (256, 40))

_MD_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+(?:\s+"[^"]*")?)\)')
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)\s]+(?:\s+"[^"]*")?)\)')
_TITLE_RE = re.compile(r'\s+"([^"]*)"$')
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def guess_media_mime(name: str) -> str:
    return str(mimetypes.guess_type(str(name or ""))[0] or "application/octet-stream")


def is_raster_image(name: str) -> bool:
    return Path(str(name or "")).suffix.lower() in _RASTER_SUFFIXES


def _resolve_local_source(raw_src: str) -> Path | None:
    src = str(raw_src or "").strip()
    if not src or src.startswith(("#", "data:")):
        return None
    if src.startswith("/api/ceo/media/original") or src.startswith("/api/ceo/uploads/file"):
        return None
    if not re.match(r"^[A-Za-z]:[\\/]", src) and re.match(r"^[a-z][a-z0-9+.-]*:", src, re.I):
        return None
    candidate = Path(src).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_path() / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def mint_original_token(path: Path) -> str:
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"p": str(path), "exp": int(time.time()) + ORIGINAL_TOKEN_TTL_SECONDS},
                separators=(",", ":"),
            ).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    sig = hmac.new(_TOKEN_SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()[:43]
    return f"{body}.{sig}"


def verify_original_token(token: str) -> Path | None:
    try:
        body, sig = str(token or "").rsplit(".", 1)
        expected = hmac.new(_TOKEN_SECRET, body.encode("ascii"), hashlib.sha256).hexdigest()[:43]
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        raw_path = str(payload.get("p") or "").strip()
        if not raw_path or int(payload.get("exp") or 0) < int(time.time()):
            return None
        return Path(raw_path)
    except Exception:
        return None


def original_view_url(path: Path) -> str:
    return f"{VIEWER_ROUTE}?token={mint_original_token(path)}"


def _thumbnail_path_for(session_id: str, source: Path) -> Path:
    stat = source.stat()
    digest = hashlib.sha1(
        f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:12]
    name = safe_filename(source.stem) or "image"
    return upload_dir_for_session(session_id) / "inline_media" / f"thumb_{digest}_{name}.jpg"


def _build_thumbnail(source: Path, dest: Path) -> None:
    from PIL import Image

    with Image.open(source) as img:
        img.load()
        if img.mode != "RGB":
            base = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            base.paste(rgba, mask=rgba.getchannel("A"))
            img = base
        best: bytes = b""
        for size, quality in _THUMB_LADDER:
            trial = img.copy()
            trial.thumbnail((size, size))
            buf = io.BytesIO()
            trial.save(buf, format="JPEG", quality=quality)
            best = buf.getvalue()
            if len(best) <= THUMBNAIL_MAX_BYTES:
                break
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(best)


def _stage_thumbnail(session_id: str, source: Path) -> Path | None:
    thumb = _thumbnail_path_for(session_id, source)
    if thumb.exists():
        return thumb
    try:
        _build_thumbnail(source, thumb)
    except Exception:
        return None
    return thumb if thumb.exists() else None


def rewrite_assistant_media_content(session_id: str, content: Any) -> Any:
    if not isinstance(content, str) or not content.strip():
        return content
    placeholders: list[str] = []

    def stash(markdown: str) -> str:
        placeholders.append(markdown)
        return f"\x00{len(placeholders) - 1}\x00"

    def replace_image(match: re.Match[str]) -> str:
        alt, target = match.group(1), match.group(2)
        raw_src = _TITLE_RE.sub("", target).strip()
        source = _resolve_local_source(raw_src)
        if source is None or not source.is_file():
            return match.group(0)
        if is_raster_image(source.name):
            thumb = _stage_thumbnail(session_id, source)
            if thumb is None:
                return match.group(0)
            return stash(f'![{alt}]({thumb} "{original_view_url(source)}")')
        return stash(f"[{alt or source.name}]({original_view_url(source)})")

    def replace_link(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        raw_src = _TITLE_RE.sub("", target).strip()
        source = _resolve_local_source(raw_src)
        if source is None or not source.is_file():
            return match.group(0)
        return f"[{label}]({original_view_url(source)})"

    text = _MD_IMAGE_RE.sub(replace_image, content)
    text = _MD_LINK_RE.sub(replace_link, text)
    return _PLACEHOLDER_RE.sub(lambda m: placeholders[int(m.group(1))], text)


@router.get("/ceo/media/original")
async def get_ceo_media_original(token: str = Query(...)):
    path = verify_original_token(token)
    if path is None:
        raise HTTPException(status_code=403, detail="invalid_media_token")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media_file_not_found")
    mime = guess_media_mime(path.name)
    inline = (
        (mime.startswith("image/") and mime != "image/svg+xml")
        or mime == "application/pdf"
        or mime.startswith("text/")
    )
    return FileResponse(
        str(path),
        media_type=mime,
        filename=path.name,
        content_disposition_type="inline" if inline else "attachment",
    )
