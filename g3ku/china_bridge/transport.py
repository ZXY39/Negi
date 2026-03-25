from __future__ import annotations

import asyncio
import base64
import mimetypes
import uuid
from pathlib import Path
from typing import Any, Callable

from g3ku.china_bridge.protocol import (
    build_deliver_frame,
    build_turn_complete_frame,
    build_turn_error_frame,
    normalize_inbound_frame,
)
from g3ku.china_bridge.session_keys import (
    build_memory_chat_id,
    build_runtime_chat_id,
    build_session_key,
)
from g3ku.china_bridge.registry import china_channel_id_set
from g3ku.core.messages import UserInputMessage
from g3ku.runtime.bridge import SessionRuntimeBridge

CHINA_CHANNELS = china_channel_id_set()

Sender = Callable[[dict[str, Any]], asyncio.Future | Any]


class ChinaBridgeTransport:
    def __init__(
        self,
        *,
        runtime_bridge: SessionRuntimeBridge,
        app_config: Any = None,
        register_task: Callable[[str | None, asyncio.Task], None] | None = None,
    ):
        self._runtime_bridge = runtime_bridge
        self._app_config = app_config
        self._register_task = register_task
        self._sender: Callable[[dict[str, Any]], Any] | None = None

    def set_sender(self, sender: Callable[[dict[str, Any]], Any]) -> None:
        self._sender = sender

    @staticmethod
    def _attachment_kind(item) -> str:
        kind = str(getattr(item, "kind", "") or "").strip().lower()
        if kind:
            return kind
        mime_type = str(getattr(item, "mime_type", "") or "").strip().lower()
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("audio/"):
            return "audio"
        if mime_type.startswith("video/"):
            return "video"
        return "file"

    @staticmethod
    def _attachment_name(item) -> str:
        explicit_name = str(getattr(item, "file_name", "") or "").strip()
        if explicit_name:
            return explicit_name
        source = str(getattr(item, "path", "") or getattr(item, "url", "") or "").strip()
        if not source:
            return "attachment"
        try:
            return Path(source).name or source
        except Exception:
            return source

    @classmethod
    def _attachment_descriptor(cls, item) -> dict[str, Any] | None:
        path = str(getattr(item, "path", "") or "").strip()
        url = str(getattr(item, "url", "") or "").strip()
        if not path and not url:
            return None
        mime_type = str(getattr(item, "mime_type", "") or "").strip()
        if not mime_type:
            guessed_mime, _ = mimetypes.guess_type(path or url)
            mime_type = str(guessed_mime or "").strip()
        descriptor = {
            "kind": cls._attachment_kind(item),
            "name": cls._attachment_name(item),
            "mime_type": mime_type,
        }
        if path:
            descriptor["path"] = path
        if url:
            descriptor["url"] = url
        size_bytes = getattr(item, "size_bytes", None)
        if isinstance(size_bytes, int):
            descriptor["size"] = size_bytes
        return descriptor

    @staticmethod
    def _attachment_note(attachments: list[dict[str, Any]]) -> str:
        if not attachments:
            return ""
        lines = ["Channel attachments:"]
        for item in attachments:
            label = "image" if str(item.get("kind") or "") == "image" else "file"
            source = str(item.get("path") or item.get("url") or "").strip()
            suffix = f" (local path: {source})" if source else ""
            lines.append(f"- {label}: {item['name']}{suffix}")
        lines.append("You may inspect the local file paths or URLs above when helpful.")
        return "\n".join(lines)

    @staticmethod
    def _image_url_from_attachment(attachment: dict[str, Any]) -> str | None:
        mime_type = str(attachment.get("mime_type") or "").strip() or "image/png"
        path = str(attachment.get("path") or "").strip()
        if path:
            candidate = Path(path)
            if candidate.exists() and candidate.is_file():
                encoded = base64.b64encode(candidate.read_bytes()).decode("ascii")
                return f"data:{mime_type};base64,{encoded}"
        url = str(attachment.get("url") or "").strip()
        return url or None

    @classmethod
    def _build_user_message(
        cls,
        *,
        text: str,
        metadata: dict[str, Any],
        attachments: list[Any],
    ) -> str | UserInputMessage:
        normalized_attachments = [
            descriptor
            for descriptor in (cls._attachment_descriptor(item) for item in attachments)
            if descriptor is not None
        ]
        if not normalized_attachments and not metadata:
            return text
        message_metadata = dict(metadata)
        if normalized_attachments:
            message_metadata["china_bridge_attachments"] = normalized_attachments
        if not normalized_attachments:
            return UserInputMessage(content=text, metadata=message_metadata)

        note = cls._attachment_note(normalized_attachments)
        text_value = str(text or "")
        merged_text = f"{text_value}\n\n{note}" if (note and text_value) else (note or text_value)
        content: list[dict[str, Any]] = []
        if merged_text:
            content.append({"type": "text", "text": merged_text})
        for attachment in normalized_attachments:
            if str(attachment.get("kind") or "") != "image":
                continue
            image_url = cls._image_url_from_attachment(attachment)
            if not image_url:
                continue
            content.append({"type": "image_url", "image_url": {"url": image_url}})
        attachment_refs = [
            str(item.get("path") or item.get("url") or "").strip()
            for item in normalized_attachments
            if str(item.get("path") or item.get("url") or "").strip()
        ]
        return UserInputMessage(
            content=content or [{"type": "text", "text": note or text_value}],
            attachments=attachment_refs,
            metadata=message_metadata,
        )

    async def handle_frame(self, payload: dict[str, Any]) -> None:
        frame_type = str(payload.get("type") or "").strip()
        if frame_type != "inbound_message":
            return
        task = asyncio.create_task(self._run_turn(payload))
        if callable(self._register_task):
            self._register_task(None, task)
        else:
            task.add_done_callback(lambda t: t.exception())

    async def _run_turn(self, payload: dict[str, Any]) -> None:
        envelope = normalize_inbound_frame(payload)
        if envelope is None:
            return
        session_key = build_session_key(
            channel=envelope.channel,
            account_id=envelope.account_id,
            peer_kind=envelope.peer_kind,
            peer_id=envelope.peer_id,
            thread_id=envelope.thread_id,
        )
        runtime_chat_id = build_runtime_chat_id(
            account_id=envelope.account_id,
            peer_kind=envelope.peer_kind,
            peer_id=envelope.peer_id,
            thread_id=envelope.thread_id,
        )
        memory_chat_id = build_memory_chat_id(
            account_id=envelope.account_id,
            peer_kind=envelope.peer_kind,
            peer_id=envelope.peer_id,
            thread_id=envelope.thread_id,
        )
        metadata = dict(envelope.metadata or {})
        metadata.update(
            {
                "_china_event_id": envelope.event_id,
                "_china_account_id": envelope.account_id,
                "_china_peer_kind": envelope.peer_kind,
                "_china_peer_id": envelope.peer_id,
                "_china_thread_id": envelope.thread_id,
                "message_id": envelope.message_id or metadata.get("message_id"),
            }
        )
        text = str(envelope.text or "")
        try:
            if text.strip().lower() in {"/stop", "停止"}:
                total = await self._runtime_bridge.cancel(session_key, reason="china_stop")
                await self._emit(
                    build_deliver_frame(
                        event_id=envelope.event_id,
                        delivery_id=uuid.uuid4().hex,
                        channel=envelope.channel,
                        account_id=envelope.account_id,
                        target_kind=envelope.peer_kind,
                        target_id=envelope.peer_id,
                        text=f"Stopped {total} task(s)." if total else "No active task to stop.",
                        mode="final",
                        reply_to=envelope.message_id,
                        metadata={"session_key": session_key},
                    )
                )
                await self._emit(build_turn_complete_frame(event_id=envelope.event_id))
                return

            user_message = self._build_user_message(
                text=text,
                metadata=metadata,
                attachments=list(envelope.attachments or []),
            )

            result = await self._runtime_bridge.prompt(
                user_message,
                session_key=session_key,
                channel=envelope.channel,
                chat_id=runtime_chat_id,
                runtime_channel=envelope.channel,
                runtime_chat_id=runtime_chat_id,
                runtime_memory_channel=envelope.channel,
                runtime_memory_chat_id=memory_chat_id,
                register_task=self._register_task,
            )
            if getattr(result, "output", None):
                await self._emit(
                    build_deliver_frame(
                        event_id=envelope.event_id,
                        delivery_id=uuid.uuid4().hex,
                        channel=envelope.channel,
                        account_id=envelope.account_id,
                        target_kind=envelope.peer_kind,
                        target_id=envelope.peer_id,
                        text=str(result.output),
                        mode="final",
                        reply_to=envelope.message_id,
                        metadata={"session_key": session_key},
                    )
                )
            await self._emit(build_turn_complete_frame(event_id=envelope.event_id))
        except Exception as exc:
            await self._emit(build_turn_error_frame(event_id=envelope.event_id, error=str(exc)))

    async def _emit(self, payload: dict[str, Any]) -> None:
        if self._sender is None:
            return
        result = self._sender(payload)
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    def _parse_chat_id_target(chat_id: str) -> dict[str, str] | None:
        raw = str(chat_id or "").strip()
        if not raw:
            return None
        parts = raw.split(":")
        if len(parts) < 3:
            return None
        account_id = str(parts[0] or "").strip() or "default"
        scope = str(parts[1] or "").strip().lower()
        peer_id = str(parts[2] or "").strip()
        if not peer_id:
            return None
        if scope == "dm":
            kind = "user"
        elif scope == "group":
            kind = "group"
        else:
            return None
        return {
            "account_id": account_id,
            "peer_kind": kind,
            "peer_id": peer_id,
        }

    async def send_outbound(self, msg) -> None:
        metadata = dict(msg.metadata or {})
        if bool(metadata.get("_progress")) or bool(metadata.get("_tool_hint")) or bool(metadata.get("_session_event")):
            return
        parsed_target = self._parse_chat_id_target(getattr(msg, "chat_id", None) or "")
        account_id = str(metadata.get("_china_account_id") or (parsed_target or {}).get("account_id") or "default").strip() or "default"
        peer_kind = str(metadata.get("_china_peer_kind") or (parsed_target or {}).get("peer_kind") or "user").strip() or "user"
        peer_id = str(metadata.get("_china_peer_id") or (parsed_target or {}).get("peer_id") or msg.chat_id or "").strip()
        if not peer_id:
            return
        await self._emit(
            build_deliver_frame(
                event_id=str((msg.metadata or {}).get("_china_event_id") or uuid.uuid4().hex),
                delivery_id=uuid.uuid4().hex,
                channel=str(msg.channel or ""),
                account_id=account_id,
                target_kind=peer_kind,
                target_id=peer_id,
                text=str(msg.content or ""),
                mode="final",
                reply_to=str(msg.reply_to or (msg.metadata or {}).get("message_id") or "").strip() or None,
                metadata={
                    "session_key": str((msg.metadata or {}).get("session_key") or ""),
                    "task_id": str((msg.metadata or {}).get("task_id") or ""),
                },
            )
        )
