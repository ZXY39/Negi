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
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from g3ku.runtime.web_ceo_sessions import upload_dir_for_session, workspace_path
from g3ku.utils.helpers import safe_filename

router = APIRouter()

THUMBNAIL_MAX_BYTES = 100 * 1024
ORIGINAL_TOKEN_TTL_SECONDS = 24 * 60 * 60
VIEWER_ROUTE = "/api/ceo/media/original"
# 单条渠道回复最多转为文件消息的附件数；超出的链接保留原样，由签名改写兜底。
MAX_CHANNEL_ATTACHMENTS = 4

_TOKEN_SECRET = secrets.token_bytes(32)
_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
# 动图不走缩略图：`_build_thumbnail` 只取首帧并把透明通道铺白，GIF/WebP 会被压成
# 一张静态 JPEG，网页端表情包就不动了。这两类直接引用签名原图。
_ANIMATED_RASTER_SUFFIXES = {".gif", ".webp"}
# `/ceo/media/original` 的 inline 判定原本只看 `text/*`，可 `.json` / `.yaml` / 代码这些
# Python 认成 application/* 的其实也是纯文本，进 iframe 只会变成一次下载。这一组在**这条
# 路由上**按 text/plain 供；`guess_media_mime` 本身不动——渠道文件消息的 MIME 另有用途。
_INLINE_TEXT_SUFFIXES = {
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".log",
    ".xml", ".rst", ".srt", ".vtt",
    ".py", ".sh", ".bash", ".bat", ".ps1", ".sql",
    ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".rb", ".php", ".lua", ".kt", ".swift",
}
_THUMB_LADDER = ((1280, 85), (1280, 70), (960, 70), (720, 60), (512, 50), (384, 45), (256, 40))
# 部分 Python 环境的 mimetypes 不含常见办公文档映射（如 .docx），渠道文件消息
# 的 mime 会影响平台侧展示，这里保底。
_KNOWN_DOCUMENT_MIME = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".csv": "text/csv",
    ".md": "text/markdown",
}

_MD_IMAGE_RE = re.compile(r'!\[([^\]]*)\]\(([^)\s]+(?:\s+"[^"]*")?)\)')
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)\s]+(?:\s+"[^"]*")?)\)')
_TITLE_RE = re.compile(r'\s+"([^"]*)"$')
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def guess_media_mime(name: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in _KNOWN_DOCUMENT_MIME:
        return _KNOWN_DOCUMENT_MIME[suffix]
    # 部分 Windows 的 mimetypes 没有 `.webp` 映射（实测 guess_type → (None, None)），
    # 落到 application/octet-stream 会让渠道桥把表情包当文件发出去。栅格后缀以本表为准。
    if suffix in _RASTER_SUFFIXES:
        return "image/jpeg" if suffix in {".jpg", ".jpeg"} else f"image/{suffix.lstrip('.')}"
    return str(mimetypes.guess_type(str(name or ""))[0] or "application/octet-stream")


def is_raster_image(name: str) -> bool:
    return Path(str(name or "")).suffix.lower() in _RASTER_SUFFIXES


def channel_attachment_roots() -> tuple[Path, ...]:
    """Roots a channel-bound reply is allowed to reference.

    Channel replies leave the machine (QQ / OneBot), so a model-injected
    ``![](C:\\...)`` must not be able to hand out arbitrary local files. The
    workspace covers both agent output and ``<workspace>/.g3ku/external-uploads``
    (where inbound channel attachments land).

    Web snapshots pass ``None`` instead: the operator is looking at their own
    machine, so restricting there would only break legitimate previews.
    """
    return (workspace_path(),)


def _file_url_to_path(src: str) -> str:
    """``file:///C:/x/y.mp3`` → ``C:/x/y.mp3``; anything else comes back unchanged.

    Markdown authors emit ``file://`` for local files (it is what a browser gives
    you when you copy a local link), and that shape used to die in the scheme
    guard below, so the channel reply left with raw markdown text in it instead of
    a file message. Percent-escapes are undone because a path with spaces arrives
    as ``%20``. The allowed-roots gate still decides what may be sent.
    """
    if not src.lower().startswith("file://"):
        return src
    rest = src[len("file://"):]
    if rest.lower().startswith("localhost/"):
        rest = rest[len("localhost"):]
    rest = unquote(rest)
    if re.match(r"^/[A-Za-z]:[\\/]", rest):
        # Windows 的三斜杠形态：file:///C:/x 去掉前缀后是 /C:/x
        rest = rest[1:]
    if re.match(r"^[A-Za-z]:[\\/]", rest) or rest.startswith("/"):
        return rest
    return src


def _resolve_local_source(raw_src: str, allowed_roots: tuple[Path, ...] | None = None) -> Path | None:
    src = _file_url_to_path(str(raw_src or "").strip())
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
        resolved = candidate.resolve()
    except OSError:
        return None
    if allowed_roots is not None:
        roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots)
        if not any(resolved.is_relative_to(root) for root in roots):
            return None
    return resolved


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


def build_attachment_descriptor(source: Path, *, after_text: bool = False) -> dict[str, Any]:
    """Channel attachment descriptor for one local file.

    ``kind`` is what the OneBot bridge keys on to send ``[CQ:image]`` instead of
    a file upload. ``after_text`` keeps the bridge from sending the attachment
    before the reply body at all (stickers read as punctuation, not as a
    lead-in).
    """
    try:
        size = source.stat().st_size
    except OSError:
        size = 0
    mime = guess_media_mime(source.name)
    descriptor: dict[str, Any] = {
        "name": source.name,
        "mime_type": mime,
        "kind": "image" if mime.startswith("image/") else "file",
        "size": int(size),
        "url": original_view_url(source),
    }
    if after_text:
        descriptor["after_text"] = True
    return descriptor


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


def _stage_animated_original(session_id: str, source: Path) -> Path | None:
    """Copy an animated image byte-for-byte into the session upload dir.

    The frontend only renders an inline ``<img>`` when the markdown src is a
    local file it can serve itself; pointing src at the signed viewer route
    degrades the image to a clickable "image" label. So animation is preserved
    by staging the original bytes (no re-encode) and keeping the signed URL in
    the title slot, which is the same shape the thumbnail path uses.
    """
    suffix = source.suffix.lower()
    try:
        stat = source.stat()
    except OSError:
        return None
    digest = hashlib.sha1(
        f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:12]
    name = safe_filename(source.stem) or "image"
    dest = upload_dir_for_session(session_id) / "inline_media" / f"anim_{digest}_{name}{suffix}"
    if dest.exists():
        return dest
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    except Exception:
        return None
    return dest if dest.exists() else None


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
        source = _resolve_local_source(raw_src, None)
        if source is None or not source.is_file():
            return match.group(0)
        if is_raster_image(source.name):
            url = original_view_url(source)
            if source.suffix.lower() in _ANIMATED_RASTER_SUFFIXES:
                staged = _stage_animated_original(session_id, source)
                if staged is None:
                    return match.group(0)
                return stash(f'![{alt}]({staged} "{url}")')
            thumb = _stage_thumbnail(session_id, source)
            if thumb is None:
                return match.group(0)
            return stash(f'![{alt}]({thumb} "{url}")')
        # 非图片保留 `![]` 形状：前端靠 src 位的文件名判类型（预览 or 保存），
        # 改成普通链接会把扩展名一起丢掉，点开就只能下载。
        return stash(f'![{alt or source.name}]({source.name} "{original_view_url(source)}")')

    def replace_link(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        raw_src = _TITLE_RE.sub("", target).strip()
        source = _resolve_local_source(raw_src, None)
        if source is None or not source.is_file():
            return match.group(0)
        return f"[{label}]({original_view_url(source)})"

    text = _MD_IMAGE_RE.sub(replace_image, content)
    text = _MD_LINK_RE.sub(replace_link, text)
    return _PLACEHOLDER_RE.sub(lambda m: placeholders[int(m.group(1))], text)


def render_assistant_reply_media(session_id: str, content: Any) -> Any:
    """Resolve sticker markers and stage local media for one user-visible reply.

    Both web render points use this: the socket lane applies it to
    ``message_end`` text and to replayed transcript rows, and the heartbeat
    service applies it before publishing ``ceo.reply.final`` (that frame never
    reaches the socket's ``message_end`` branch). Selection is deterministic in
    the reply text, so the image staged here is the same one the channel lane
    sends.
    """
    if not isinstance(content, str) or not content.strip():
        return content
    from g3ku.stickers import resolve_markers

    cleaned, paths = resolve_markers(content)
    text = cleaned if isinstance(cleaned, str) else content
    if paths:
        text = text + "".join(f"\n\n![]({path})" for path in paths)
    return rewrite_assistant_media_content(session_id, text)


def rewrite_media_links_signed(
    content: Any, *, allowed_roots: tuple[Path, ...] | None
) -> Any:
    """Rewrite local file references into signed viewer URLs only.

    External-facing variant of ``rewrite_assistant_media_content``: no
    thumbnail staging, no local paths left behind — every resolvable local
    image/link href becomes the HMAC-signed original viewer URL, which the
    caller can fetch without extra authentication.

    ``allowed_roots`` is required on purpose (pass ``channel_attachment_roots()``
    for channel-bound text): a signed URL is still a working download link, so
    leaving this unrestricted would re-open the same leak the attachment
    whitelist closes.
    """
    if not isinstance(content, str) or not content.strip():
        return content

    def replace_image(match: re.Match[str]) -> str:
        alt, target = match.group(1), match.group(2)
        raw_src = _TITLE_RE.sub("", target).strip()
        source = _resolve_local_source(raw_src, allowed_roots)
        if source is None or not source.is_file():
            return match.group(0)
        return f"![{alt}]({original_view_url(source)})"

    def replace_link(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        raw_src = _TITLE_RE.sub("", target).strip()
        source = _resolve_local_source(raw_src, allowed_roots)
        if source is None or not source.is_file():
            return match.group(0)
        return f"[{label}]({original_view_url(source)})"

    text = _MD_IMAGE_RE.sub(replace_image, content)
    return _MD_LINK_RE.sub(replace_link, text)


def extract_local_media_attachments(
    content: Any, *, allowed_roots: tuple[Path, ...] | None
) -> tuple[str, list[dict[str, Any]]]:
    """Turn markdown references to existing local files into channel attachments.

    Channel-facing variant of the signed rewrite: every markdown image/link
    whose target resolves to an existing local file is REPLACED by its label
    (fallback: the file name) and collected as an attachment descriptor with a
    signed viewer URL (root-relative; bridges join it with their /api base
    URL). Bare paths are never matched — only explicit markdown references are
    sent as file messages. Tokens beyond ``MAX_CHANNEL_ATTACHMENTS`` (and
    duplicate paths) are left untouched so the caller's signed-URL rewrite can
    still turn them into clickable fallback links.

    ``allowed_roots`` is required on purpose (pass ``channel_attachment_roots()``
    for channel-bound text): it is the only thing standing between a
    model-echoed path and a file the recipient should never receive.
    """
    if not isinstance(content, str) or not content.strip():
        return (content if isinstance(content, str) else ""), []

    attachments: list[dict[str, Any]] = []
    seen: set[str] = set()

    def stash(source: Path, label: str) -> str | None:
        key = str(source)
        if key in seen or len(attachments) >= MAX_CHANNEL_ATTACHMENTS:
            return None
        seen.add(key)
        attachments.append(build_attachment_descriptor(source))
        return label.strip() or source.name

    def replace_image(match: re.Match[str]) -> str:
        alt, target = match.group(1), match.group(2)
        source = _resolve_local_source(_TITLE_RE.sub("", target).strip(), allowed_roots)
        if source is None or not source.is_file():
            return match.group(0)
        replacement = stash(source, alt)
        return match.group(0) if replacement is None else replacement

    def replace_link(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        source = _resolve_local_source(_TITLE_RE.sub("", target).strip(), allowed_roots)
        if source is None or not source.is_file():
            return match.group(0)
        replacement = stash(source, label)
        return match.group(0) if replacement is None else replacement

    text = _MD_IMAGE_RE.sub(replace_image, content)
    text = _MD_LINK_RE.sub(replace_link, text)
    return text, attachments


@router.get("/ceo/media/original")
async def get_ceo_media_original(token: str = Query(...)):
    path = verify_original_token(token)
    if path is None:
        raise HTTPException(status_code=403, detail="invalid_media_token")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media_file_not_found")
    mime = guess_media_mime(path.name)
    suffix = path.suffix.lower()
    if suffix in _INLINE_TEXT_SUFFIXES and not mime.startswith("text/"):
        mime = "text/plain; charset=utf-8"
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
