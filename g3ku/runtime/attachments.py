"""附件的共享形状：模型可见说明行、网页显示描述符、读文件地址。

单源理由：同一条入站消息会被三条道画出来——回合进行中的实时帧、刷新后的
快照、会话列表的预览。它们各自取正文与附件，只要有一份规则写在调用方，
同一行就会在不同时刻长得不一样（渠道附件实测在实时帧里露出说明行、
刷新后才变成缩略图）。所以说明行的生成与剥离、显示描述符的抬升、
读文件地址的拼接都只在这里做一次。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

# 渠道附件不在 web 上传根下，必须由这条专用读路由供；路由本体在
# ``g3ku/runtime/api/websocket_ceo.py``，这里只留一个地址作者。
EXTERNAL_UPLOAD_FILE_ROUTE = "/api/ceo/external-upload-file"


def guess_upload_mime_type(name: str, content_type: str | None = None) -> str:
    if isinstance(content_type, str) and content_type.strip():
        return content_type.strip()
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def upload_kind(*, mime_type: str, name: str) -> str:
    mime = str(mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        # 语音条要渲染成可播放的气泡，不是文件药丸；网页麦克风录出来的就是 WAV。
        return "audio"
    guessed, _ = mimetypes.guess_type(name)
    if isinstance(guessed, str):
        if guessed.lower().startswith("image/"):
            return "image"
        if guessed.lower().startswith("audio/"):
            return "audio"
    return "file"


def external_upload_file_url(*, session_id: str, path: str) -> str:
    params = urlencode({"session_id": session_id, "path": path})
    return f"{EXTERNAL_UPLOAD_FILE_ROUTE}?{params}"


def channel_attachment_note(attachments: list[dict[str, Any]]) -> str:
    """模型可见的渠道附件说明行。

    它只属于模型输入：三条显示道都按同一份描述符把这段说明剥掉
    （``strip_channel_attachment_note``），所以两处必须由这一个函数生成。
    """
    lines = ["Channel attachments:"]
    for item in attachments:
        kind = str(item.get("kind") or "")
        if kind == "audio":
            # 语音条的内容已经在正文的转写里；再给一行路径只会让模型以为要去
            # 打开一个文件。它是给人回放的素材，不是给模型的输入。
            continue
        label = "image" if kind == "image" else "file"
        name = str(item.get("name") or "").strip()
        source = str(item.get("path") or item.get("url") or "").strip()
        suffix = f" (local path: {source})" if source else ""
        lines.append(f"- {label}: {name}{suffix}")
    if len(lines) <= 1:
        return ""
    lines.append("You may inspect the local file paths or URLs above when helpful.")
    return "\n".join(lines)


def strip_channel_attachment_note(content: str, metadata: dict[str, Any]) -> str:
    """把模型可见的渠道附件说明从显示正文里剥掉，只留用户原文。

    说明在这里按同一份描述符重建，所以说明已经写进 content 的历史行也能剥干净，
    不必改写转录。
    """
    attachments = metadata.get("external_attachments") if isinstance(metadata, dict) else None
    if not isinstance(attachments, list) or not attachments:
        return content
    note = channel_attachment_note([item for item in attachments if isinstance(item, dict)])
    if not note:
        return content
    if content == note:
        return ""
    suffix = f"\n\n{note}"
    return content[: -len(suffix)] if content.endswith(suffix) else content


def channel_attachment_descriptors(metadata: Any, session_id: str) -> list[dict[str, Any]]:
    """把渠道附件描述符抬成显示形状：图片出缩略图、其余出文件卡、音频走语音条。

    每一条都带 ``external-upload-file`` 的读文件地址。只按 ``path`` 让前端回退到
    ``uploads/file`` 会得到 404 的破图——那个路由根在 web 上传目录，进不了渠道落点。
    """
    raw_items = metadata.get("external_attachments") if isinstance(metadata, dict) else None
    if not isinstance(raw_items, list) or not raw_items:
        return []
    session_key = str(session_id or "").strip()
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "").strip()
        if not path or not session_key:
            continue
        name = str(raw.get("name") or Path(path).name).strip() or Path(path).name or path
        mime_type = str(raw.get("mime_type") or "").strip()
        kind = str(raw.get("kind") or "").strip() or upload_kind(mime_type=mime_type, name=name)
        item: dict[str, Any] = {
            "path": path,
            "name": name,
            "mime_type": mime_type or guess_upload_mime_type(name),
            "kind": kind,
            "url": external_upload_file_url(session_id=session_key, path=path),
        }
        size = raw.get("size")
        if isinstance(size, (int, float)):
            item["size"] = int(size)
        items.append(item)
    return items
