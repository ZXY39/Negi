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
import re
from dataclasses import dataclass, field
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


def parse_message_content(
    message: Any, *, bot_user_id: int
) -> tuple[str, list[str], bool]:
    """Extract (text, image_urls, at_bot) from array or CQ-string messages."""
    texts: list[str] = []
    image_urls: list[str] = []
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
            elif seg_type == "at":
                if bot_user_id and str(data.get("qq") or "") == str(bot_user_id):
                    at_bot = True
        return "".join(texts).strip(), image_urls, at_bot

    raw = str(message or "")
    for match in _CQ_AT_RE.finditer(raw):
        if bot_user_id and match.group(1) == str(bot_user_id):
            at_bot = True
    raw = _CQ_AT_RE.sub("", raw)
    image_urls.extend(m.strip() for m in _CQ_IMAGE_RE.findall(raw) if m.strip().startswith("http"))
    text = _CQ_IMAGE_RE.sub("", raw)
    text = re.sub(r"\[CQ:[^\]]*\]", "", text)
    return text.strip(), image_urls, at_bot


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
            await self._send_to(external_key, text)
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
            await self._send_to(external_key, text)
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
        text, image_urls, at_bot = parse_message_content(
            event.get("message"), bot_user_id=self._bot_user_id
        )
        if external_key.startswith("qq:group:") and self._behavior.group_require_at and not at_bot:
            return
        if not text and not image_urls:
            return

        session_id = await self._g3ku.ensure_session(external_key)
        self.ensure_event_stream(session_id)

        control = normalize_control_command_text(text) if not image_urls else ""
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
            data = await self._onebot.download_bytes(url)
            if data:
                attachments.append(
                    {
                        "kind": "image",
                        "name": url.split("/")[-1][:64] or "image.png",
                        "mime_type": "image/png",
                        "data_base64": base64.b64encode(data).decode("ascii"),
                    }
                )

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
