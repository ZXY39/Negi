"""Dispatch logic: OneBot 11 events -> g3ku turns, g3ku events -> QQ messages.

Behavior parity with the legacy China transport QQ semantics (the subsystem
has since been removed; the live contracts are docs/architecture/
external-agent-api.md plus this bridge's README), with the key
responsibility transfer of the rebuild: g3ku emits full-volume progress
events and this bridge owns throttling, message splitting, trigger rules,
and control-command recognition.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import BehaviorConfig
from .g3ku_client import G3kuClient
from .onebot import OnebotClient

PAUSE_COMMANDS = {"/pause", "pause", "暂停", "暫停"}
STOP_COMMANDS = {"/stop", "停止"}
# Mirrors the legacy _CONTROL_TRAILING_PUNCTUATION_RE normalization.
_CONTROL_TRAILING_PUNCTUATION_RE = re.compile(r"[.!?…,，。;；:：'\"’”)\]}]+$")
_CQ_IMAGE_RE = re.compile(r"\[CQ:image[^\]]*?url=([^,\]]+)[^\]]*\]")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")
_CQ_FILE_RE = re.compile(r"\[CQ:file,?([^\]]*)\]")
_CQ_PARAM_RE = re.compile(r"([A-Za-z_]+)=([^,\]]*)")

# 与服务端 /api/v1 双上限一致（图片 5MiB / 文件 20MiB，见
# g3ku/runtime/api/external_v1.py），超限附件提交必然 413，桥侧预过滤。
_MAX_INBOUND_IMAGE_BYTES = 5 * 1024 * 1024
_MAX_INBOUND_FILE_BYTES = 20 * 1024 * 1024
_MAX_INBOUND_ATTACHMENTS = 4
# 出站附件下载上限（服务端产出文件统一按文件上限约束）。
_MAX_OUTBOUND_ATTACHMENT_BYTES = 20 * 1024 * 1024

# 部分 Python 环境的 mimetypes 不含常见办公文档映射（如 .docx），附件 mime 会
# 影响服务端描述与平台侧展示，这里保底（与核心 ceo_media 的同名映射保持一致）。
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


def mime_for_name(name: str, *, fallback: str) -> str:
    suffix = Path(str(name or "")).suffix.lower()
    if suffix in _KNOWN_DOCUMENT_MIME:
        return _KNOWN_DOCUMENT_MIME[suffix]
    return str(mimetypes.guess_type(str(name or ""))[0] or fallback)

PAUSED_RECEIPT = "已暂停。"
NO_ACTIVE_TURN_RECEIPT = "当前没有正在进行的任务。"


def normalize_control_command_text(text: str) -> str:
    normalized = str(text or "").strip().lower()
    normalized = normalized.replace("’", "'").replace("`", "'")
    normalized = re.sub(r"\s+", " ", normalized)
    return _CONTROL_TRAILING_PUNCTUATION_RE.sub("", normalized).strip()


def external_key_for_event(event: dict[str, Any]) -> str | None:
    post_type = str(event.get("post_type") or "")
    if post_type != "message":
        return None
    message_type = str(event.get("message_type") or "")
    if message_type == "private":
        return f"qq:dm:{event.get('user_id')}"
    if message_type == "group":
        return f"qq:group:{event.get('group_id')}"
    return None


def _parse_cq_file_params(inner: str) -> dict[str, Any]:
    """Parse a ``[CQ:file,...]`` body into a file-ref dict.

    NapCat/LLOneBot variants differ in which keys they emit (``url`` for
    private-message files, ``id``+``busid`` for group files); collect every
    ``key=value`` pair and normalize to the bridge's file-ref shape.
    """
    params = {str(key): str(value) for key, value in _CQ_PARAM_RE.findall(inner or "")}
    url = str(params.get("url") or params.get("file") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        url = ""
    name = str(params.get("name") or "").strip()
    if not name and url:
        name = url.split("/")[-1][:64]
    size_raw = str(params.get("size") or "").strip()
    try:
        size: int | None = int(size_raw) if size_raw else None
    except ValueError:
        size = None
    return {
        "url": url or None,
        "name": name or "file",
        "size": size,
        "id": str(params.get("id") or "").strip() or None,
        "busid": str(params.get("busid") or "").strip() or None,
    }


def parse_message_content(
    message: Any, *, bot_user_id: int
) -> tuple[str, list[str], list[dict[str, Any]], bool]:
    """Extract (text, image_urls, file_refs, at_bot) from array or CQ-string
    messages.

    ``file_refs`` items carry ``{url?, name, size?, id?, busid?}``: private
    file messages usually expose a direct ``url``, group files often only
    ``id``+``busid`` (the URL is resolved later via ``get_group_file_url``).
    """
    texts: list[str] = []
    image_urls: list[str] = []
    file_refs: list[dict[str, Any]] = []
    at_bot = False

    if isinstance(message, list):
        for segment in message:
            if not isinstance(segment, dict):
                continue
            seg_type = str(segment.get("type") or "")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if seg_type == "text":
                texts.append(str(data.get("text") or ""))
            elif seg_type == "image":
                url = str(data.get("url") or data.get("file") or "").strip()
                if url.startswith("http"):
                    image_urls.append(url)
            elif seg_type == "file":
                inner = ",".join(
                    f"{key}={value}" for key, value in data.items() if value is not None
                )
                file_refs.append(_parse_cq_file_params(inner))
            elif seg_type == "at":
                if bot_user_id and str(data.get("qq") or "") == str(bot_user_id):
                    at_bot = True
        return "".join(texts).strip(), image_urls, file_refs, at_bot

    raw = str(message or "")
    for match in _CQ_AT_RE.finditer(raw):
        if bot_user_id and match.group(1) == str(bot_user_id):
            at_bot = True
    raw = _CQ_AT_RE.sub("", raw)
    image_urls.extend(m.strip() for m in _CQ_IMAGE_RE.findall(raw) if m.strip().startswith("http"))
    file_refs.extend(_parse_cq_file_params(inner) for inner in _CQ_FILE_RE.findall(raw))
    text = _CQ_IMAGE_RE.sub("", raw)
    text = re.sub(r"\[CQ:[^\]]*\]", "", text)
    return text.strip(), image_urls, file_refs, at_bot


def split_outbound_text(text: str, *, max_length: int) -> list[str]:
    """Split long replies into sendable chunks, preferring line boundaries."""
    body = str(text or "")
    if len(body) <= max_length:
        return [body] if body.strip() else []
    chunks: list[str] = []
    current = ""
    for line in body.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_length:
            if current:
                chunks.append(current)
                current = ""
            while len(line) > max_length:
                chunks.append(line[:max_length])
                line = line[max_length:]
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def parse_target(external_key: str | None) -> tuple[str, str] | None:
    """Map an external_key back to a send target: (kind, id)."""
    raw = str(external_key or "")
    if raw.startswith("qq:dm:"):
        return "private", raw[len("qq:dm:"):]
    if raw.startswith("qq:group:"):
        return "group", raw[len("qq:group:"):]
    return None


@dataclass(slots=True)
class _ProgressBuffer:
    lines: list[str] = field(default_factory=list)


class Dispatcher:
    def __init__(
        self,
        *,
        g3ku: G3kuClient,
        onebot: OnebotClient,
        behavior: BehaviorConfig,
        bot_user_id: int = 0,
    ):
        self._g3ku = g3ku
        self._onebot = onebot
        self._behavior = behavior
        self._bot_user_id = int(bot_user_id or behavior.bot_user_id or 0)
        self._progress: dict[str, _ProgressBuffer] = {}
        self._sse_tasks: dict[str, asyncio.Task] = {}
        self._streaming = asyncio.Event()

    # -- outbound to QQ ------------------------------------------------------

    async def _send_to(self, external_key: str | None, text: str) -> None:
        target = parse_target(external_key)
        body = str(text or "").strip()
        if target is None or not body:
            return
        kind, target_id = target
        for chunk in split_outbound_text(body, max_length=self._behavior.max_message_length):
            if kind == "private":
                await self._onebot.send_private_msg(target_id, chunk)
            else:
                await self._onebot.send_group_msg(target_id, chunk)

    async def _deliver_attachment(self, external_key: str, attachment: dict[str, Any]) -> bool:
        """Send one event attachment as a real QQ file/image message.

        Images go out as ``[CQ:image,file=base64://...]`` messages, other files
        via the OneBot ``upload_private_file``/``upload_group_file`` actions.
        Returns False on any failure so the caller degrades the attachment to a
        signed download-link line.
        """
        target = parse_target(external_key)
        if target is None:
            return False
        kind, target_id = target
        url = str(attachment.get("url") or "").strip()
        name = str(attachment.get("name") or "attachment")
        if not url:
            return False
        data = await self._g3ku.download_media(
            url, max_bytes=_MAX_OUTBOUND_ATTACHMENT_BYTES
        )
        if not data:
            return False
        payload = f"base64://{base64.b64encode(data).decode('ascii')}"
        try:
            if str(attachment.get("kind") or "") == "image":
                segment = f"[CQ:image,file={payload}]"
                if kind == "private":
                    await self._onebot.send_private_msg(target_id, segment)
                else:
                    await self._onebot.send_group_msg(target_id, segment)
                return True
            if kind == "private":
                await self._onebot.call_action(
                    "upload_private_file", user_id=int(target_id), file=payload, name=name
                )
            else:
                await self._onebot.call_action(
                    "upload_group_file", group_id=int(target_id), file=payload, name=name
                )
            return True
        except Exception:
            return False

    async def _deliver_event(
        self, external_key: str | None, text: str, attachments: Any
    ) -> None:
        """Attachments first, then the text body; failed attachments degrade
        into signed-URL lines appended to the text (better a clickable link
        than a silently missing file)."""
        fallback_lines: list[str] = []
        for attachment in list(attachments or []):
            if not isinstance(attachment, dict):
                continue
            if await self._deliver_attachment(str(external_key or ""), attachment):
                continue
            name = str(attachment.get("name") or "attachment")
            url = str(attachment.get("url") or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                url = f"{self._g3ku.base_url}{url if url.startswith('/') else '/' + url}"
            fallback_lines.append(f"📎 {name}: {url}")
        body = "\n".join(
            part for part in [str(text or "").strip(), *fallback_lines] if part
        ).strip()
        if body:
            await self._send_to(external_key, body)

    async def _flush_progress(self, session_id: str) -> None:
        buffer = self._progress.get(session_id)
        if buffer is None or not buffer.lines:
            return
        limit = max(1, self._behavior.progress_max_lines_per_message)
        lines = buffer.lines[:limit]
        del buffer.lines[:limit]
        external_key = self._g3ku.external_key_for(session_id)
        await self._send_to(external_key, "\n".join(lines))

    # -- g3ku event stream ---------------------------------------------------

    def ensure_event_stream(self, session_id: str) -> None:
        existing = self._sse_tasks.get(session_id)
        if existing is not None and not existing.done():
            return

        async def on_event(event: dict[str, Any]) -> None:
            await self._handle_g3ku_event(session_id, event)

        self._sse_tasks[session_id] = asyncio.create_task(
            self._g3ku.stream_events(
                session_id,
                on_event=on_event,
                backoff_seconds=self._behavior.reconnect_backoff_seconds,
                stop=self._streaming,
            )
        )

    async def _handle_g3ku_event(self, session_id: str, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "progress":
            if self._behavior.final_only:
                return
            buffer = self._progress.setdefault(session_id, _ProgressBuffer())
            buffer.lines.append(str(event.get("text") or "").strip())
            return
        if event_type == "reply.final":
            await self._flush_progress(session_id)
            text = str(event.get("text") or "")
            external_key = self._g3ku.external_key_for(session_id)
            await self._deliver_event(external_key, text, event.get("attachments"))
            return
        if event_type == "turn.failed":
            await self._flush_progress(session_id)
            error = str(event.get("error") or "").strip()
            if error:
                external_key = self._g3ku.external_key_for(session_id)
                await self._send_to(external_key, error)
            self._g3ku.forget_turn(session_id)
            return
        if event_type == "turn.completed":
            self._g3ku.forget_turn(session_id)
            return
        if event_type == "outbound.created":
            text = str(event.get("text") or "")
            external_key = str(event.get("external_key") or "") or self._g3ku.external_key_for(session_id)
            await self._deliver_event(external_key, text, event.get("attachments"))
            return

    async def flush_all_progress(self) -> None:
        for session_id in list(self._progress.keys()):
            await self._flush_progress(session_id)

    async def progress_loop(self) -> None:
        """Throttled milestone flusher (legacy QQBOT_PROGRESS_MIN_INTERVAL)."""
        interval = max(0.5, self._behavior.progress_min_interval_seconds)
        while not self._streaming.is_set():
            await asyncio.sleep(interval)
            for session_id in list(self._progress.keys()):
                try:
                    await self._flush_progress(session_id)
                except Exception:
                    pass

    # -- inbound from QQ -----------------------------------------------------

    async def handle_onebot_event(self, event: dict[str, Any]) -> None:
        external_key = external_key_for_event(event)
        if external_key is None:
            return
        text, image_urls, file_refs, at_bot = parse_message_content(
            event.get("message"), bot_user_id=self._bot_user_id
        )
        if external_key.startswith("qq:group:") and self._behavior.group_require_at and not at_bot:
            return
        if not text and not image_urls and not file_refs:
            return

        session_id = await self._g3ku.ensure_session(external_key)
        self.ensure_event_stream(session_id)

        control = normalize_control_command_text(text) if not image_urls and not file_refs else ""
        if control in STOP_COMMANDS:
            cancelled = await self._g3ku.cancel_session(session_id)
            receipt = f"Stopped {cancelled} task(s)." if cancelled else "No active task to stop."
            await self._send_to(external_key, receipt)
            return
        if control in PAUSE_COMMANDS:
            turn_id = self._g3ku.active_turn_id(session_id)
            paused = await self._g3ku.pause_turn(turn_id) if turn_id else None
            receipt = PAUSED_RECEIPT if paused else NO_ACTIVE_TURN_RECEIPT
            await self._send_to(external_key, receipt)
            return

        attachments: list[dict[str, Any]] = []
        for url in image_urls:
            if len(attachments) >= _MAX_INBOUND_ATTACHMENTS:
                break
            data = await self._onebot.download_bytes(
                url, max_bytes=_MAX_INBOUND_IMAGE_BYTES
            )
            if data:
                name = url.split("/")[-1][:64] or "image.png"
                mime = mime_for_name(name, fallback="image/png")
                attachments.append(
                    {
                        "kind": "image",
                        "name": name,
                        "mime_type": mime,
                        "data_base64": base64.b64encode(data).decode("ascii"),
                    }
                )

        file_notes: list[str] = []
        group_id = event.get("group_id")
        for ref in file_refs:
            if len(attachments) >= _MAX_INBOUND_ATTACHMENTS:
                break
            name = str(ref.get("name") or "file")
            size = ref.get("size")
            if isinstance(size, int) and size > _MAX_INBOUND_FILE_BYTES:
                file_notes.append(f"[文件 {name} 超过大小上限，未接收]")
                continue
            url = str(ref.get("url") or "").strip()
            if not url and ref.get("id") and ref.get("busid") and group_id is not None:
                # 群文件通常只带 id+busid，下载链接要用 OneBot 扩展 action 换取。
                try:
                    data = await self._onebot.call_action(
                        "get_group_file_url",
                        group_id=int(group_id),
                        file_id=str(ref["id"]),
                        busid=str(ref["busid"]),
                    )
                    url = str(data.get("url") or "").strip()
                except Exception:
                    url = ""
            if not url.startswith("http"):
                file_notes.append(f"[文件 {name} 未能获取]")
                continue
            data = await self._onebot.download_bytes(
                url, max_bytes=_MAX_INBOUND_FILE_BYTES
            )
            if not data:
                file_notes.append(f"[文件 {name} 下载失败]")
                continue
            attachments.append(
                {
                    "kind": "file",
                    "name": name[:64],
                    "mime_type": mime_for_name(name, fallback="application/octet-stream"),
                    "data_base64": base64.b64encode(data).decode("ascii"),
                }
            )
        if file_notes:
            text = "\n".join(part for part in [text, *file_notes] if part).strip()

        event_id = str(event.get("event_id") or "").strip() or f"ob:{event.get('message_id')}"
        sender = {"id": str(event.get("user_id") or ""), "name": str(event.get("sender", {}).get("nickname") or "")} if isinstance(event.get("sender"), dict) else None

        result = await self._g3ku.send_message(
            session_id,
            text=text,
            attachments=attachments or None,
            sender=sender,
            idempotency_key=event_id,
        )
        if result.get("status") == "queued":
            receipt = str(result.get("receipt") or "")
            if receipt:
                await self._send_to(external_key, receipt)

    async def stop(self) -> None:
        self._streaming.set()
        for task in self._sse_tasks.values():
            task.cancel()
