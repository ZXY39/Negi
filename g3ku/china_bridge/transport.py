from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from g3ku.china_bridge.protocol import (
    build_deliver_frame,
    build_turn_complete_frame,
    build_turn_error_frame,
    normalize_inbound_frame,
    sanitize_channel_outbound_text,
)
from g3ku.china_bridge.session_keys import (
    build_memory_chat_id,
    build_runtime_chat_id,
    build_session_key,
)
from g3ku.runtime.frontdoor.cron_hidden_prompt import strip_cron_hidden_prompt
from g3ku.china_bridge.registry import china_channel_id_set
from g3ku.core.messages import UserInputMessage
from g3ku.runtime.bridge import SessionRuntimeBridge, cli_event_text
from g3ku.runtime.session_agent import TURN_FAILED_FRIENDLY_TEXT

CHINA_CHANNELS = china_channel_id_set()

Sender = Callable[[dict[str, Any]], asyncio.Future | Any]

# QQ 暂停命令触发词（与宿主 QQBOT_PAUSE_TRIGGERS 对齐）。
QQBOT_PAUSE_COMMANDS = {"/pause", "pause", "暂停", "暫停"}
STOP_COMMANDS = {"/stop", "停止"}
# 镜像宿主 QQBOT_ABORT_TRAILING_PUNCTUATION_RE，保证「停止。」「暂停！」等
# 带标点变体在 Python 侧与宿主侧的判定一致。
_CONTROL_TRAILING_PUNCTUATION_RE = re.compile(r"[.!?…,，。;；:：'\"’”)\]}]+$")
# QQ 官方机器人 API 有频率限制且不能编辑已发消息，过程信息只能按
# 里程碑节流下发，不能做 token 级流式。
QQBOT_PROGRESS_MIN_INTERVAL_SECONDS = 5.0
QQBOT_PROGRESS_MAX_LINES_PER_FRAME = 3


class _QQBotProgressCollector:
    """按回合收集 QQ 会话过程事件，节流合并后以 progress 帧下发。

    QQ 官方机器人 API 不能编辑已发消息且有频率限制，因此过程信息
    （工具调用、阶段进展）以里程碑式的节流消息送达，而不是逐字流式。
    监听器回调位于会话事件派发路径上，必须保持非阻塞且不抛异常。
    """

    def __init__(
        self,
        *,
        transport: "ChinaBridgeTransport",
        envelope: Any,
        session_key: str,
        min_interval_seconds: float = QQBOT_PROGRESS_MIN_INTERVAL_SECONDS,
        max_lines_per_frame: int = QQBOT_PROGRESS_MAX_LINES_PER_FRAME,
    ) -> None:
        self._transport = transport
        self._envelope = envelope
        self._session_key = session_key
        self._min_interval_seconds = float(min_interval_seconds)
        self._max_lines_per_frame = max(1, int(max_lines_per_frame))
        self._buffer: list[str] = []
        self._flush_task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())

    def stop(self) -> None:
        self._stopped = True
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

    async def listener(self, event) -> None:
        if self._stopped:
            return
        try:
            line = self._format_event_line(event)
        except Exception:
            line = ""
        if line:
            self._buffer.append(line)

    @staticmethod
    def _format_event_line(event) -> str:
        kind, text = cli_event_text(event)
        text = str(text or "").strip()
        if not text:
            return ""
        if kind == "tool":
            return f"🔧 {text}"
        if kind == "tool_error":
            return f"⚠️ {text}"
        if kind in {"progress", "analysis"}:
            return text
        return ""

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._min_interval_seconds)
                await self._flush()
        except asyncio.CancelledError:
            return

    async def _flush(self) -> None:
        if not self._buffer or self._stopped:
            return
        lines = self._buffer[: self._max_lines_per_frame]
        del self._buffer[: self._max_lines_per_frame]
        text = "\n".join(lines)
        if not text.strip():
            return
        envelope = self._envelope
        frame = build_deliver_frame(
            event_id=envelope.event_id,
            delivery_id=uuid.uuid4().hex,
            channel=envelope.channel,
            account_id=envelope.account_id,
            target_kind=envelope.peer_kind,
            target_id=envelope.peer_id,
            text=text,
            mode="progress",
            reply_to=envelope.message_id,
            metadata={"session_key": self._session_key, "progress_kind": "milestone"},
        )
        if frame is None:
            return
        await self._transport._emit(frame)


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
        # 宿主侧可能把 cron 隐藏契约追加进用户正文；引擎这里剥除，
        # 使持久化/显示的用户内容只保留原始消息。规范改由 cron toolskill 提供。
        text = strip_cron_hidden_prompt(text)
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
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())

    @classmethod
    def _normalize_control_command_text(cls, text: str) -> str:
        """与宿主 normalizeQQBotAbortTriggerText 对齐的控制命令归一化。"""
        normalized = str(text or "").strip().lower()
        normalized = normalized.replace("’", "'").replace("`", "'")
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = _CONTROL_TRAILING_PUNCTUATION_RE.sub("", normalized).strip()
        return normalized

    @staticmethod
    def _session_is_running(session) -> bool:
        state = getattr(session, "state", None)
        status = str(getattr(state, "status", "") or "").strip().lower()
        return bool(getattr(state, "is_running", False)) or status == "running"

    async def _deliver(
        self,
        *,
        envelope,
        session_key: str,
        text: str,
        mode: str = "final",
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        metadata: dict[str, Any] = {"session_key": session_key}
        if extra_metadata:
            metadata.update(extra_metadata)
        frame = build_deliver_frame(
            event_id=envelope.event_id,
            delivery_id=uuid.uuid4().hex,
            channel=envelope.channel,
            account_id=envelope.account_id,
            target_kind=envelope.peer_kind,
            target_id=envelope.peer_id,
            text=text,
            mode=mode,
            reply_to=envelope.message_id,
            metadata=metadata,
        )
        if frame is None:
            return
        await self._emit(frame)

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
        # 暂停/运行中注入/过程信息流仅对 QQ 渠道生效（范围决定）；
        # 其他中国渠道保持原有行为。
        is_qqbot = envelope.channel == "qqbot"
        try:
            control_text = self._normalize_control_command_text(text)
            if control_text in STOP_COMMANDS:
                total = await self._runtime_bridge.cancel(session_key, reason="china_stop")
                await self._deliver(
                    envelope=envelope,
                    session_key=session_key,
                    text=f"Stopped {total} task(s)." if total else "No active task to stop.",
                )
                await self._emit(build_turn_complete_frame(event_id=envelope.event_id))
                return
            if is_qqbot and control_text in QQBOT_PAUSE_COMMANDS:
                paused = await self._runtime_bridge.pause(session_key, manual=True)
                await self._deliver(
                    envelope=envelope,
                    session_key=session_key,
                    text="已暂停。" if paused else "当前没有正在进行的任务。",
                )
                await self._emit(build_turn_complete_frame(event_id=envelope.event_id))
                return

            user_message = self._build_user_message(
                text=text,
                metadata=metadata,
                attachments=list(envelope.attachments or []),
            )

            if is_qqbot:
                # 运行中收到的消息立即入 follow-up 队列（对齐 Web 行为），
                # 由 frontdoor 在下一次调模型前注入，或由本回合结束后的
                # 排空兜底循环续跑，不再阻塞等待最终回复。
                existing_session = self._runtime_bridge.get_existing_session(session_key)
                if self._session_is_running(existing_session):
                    await existing_session.queue_follow_up_batch(
                        [user_message], persist_transcript=True
                    )
                    await self._deliver(
                        envelope=envelope,
                        session_key=session_key,
                        text="收到，将在当前任务中一并处理。",
                    )
                    await self._emit(build_turn_complete_frame(event_id=envelope.event_id))
                    return

            collector: _QQBotProgressCollector | None = None
            listeners = None
            if is_qqbot:
                collector = _QQBotProgressCollector(
                    transport=self,
                    envelope=envelope,
                    session_key=session_key,
                    min_interval_seconds=QQBOT_PROGRESS_MIN_INTERVAL_SECONDS,
                    max_lines_per_frame=QQBOT_PROGRESS_MAX_LINES_PER_FRAME,
                )
                collector.start()
                listeners = [collector.listener]
            try:
                result = await self._runtime_bridge.prompt(
                    user_message,
                    session_key=session_key,
                    channel=envelope.channel,
                    chat_id=runtime_chat_id,
                    runtime_channel=envelope.channel,
                    runtime_chat_id=runtime_chat_id,
                    runtime_memory_channel=envelope.channel,
                    runtime_memory_chat_id=memory_chat_id,
                    listeners=listeners,
                    register_task=self._register_task,
                )
                if getattr(result, "output", None):
                    await self._deliver(
                        envelope=envelope,
                        session_key=session_key,
                        text=str(result.output),
                    )
                if is_qqbot:
                    await self._drain_queued_follow_ups(
                        envelope=envelope,
                        session_key=session_key,
                        runtime_chat_id=runtime_chat_id,
                        memory_chat_id=memory_chat_id,
                        listeners=listeners,
                    )
                await self._emit(build_turn_complete_frame(event_id=envelope.event_id))
            finally:
                if collector is not None:
                    collector.stop()
        except asyncio.CancelledError:
            # 会话被暂停/取消时（例如 Web 端手动暂停渠道会话），CancelledError
            # 是 BaseException，会穿过 except Exception。若不发终止帧，宿主侧
            # 该回合的 pending Promise 永不 settle，串行派发队列永久卡死。
            await self._emit(build_turn_complete_frame(event_id=envelope.event_id))
            raise
        except Exception as exc:
            # Channel users see a fixed friendly message; the raw exception is
            # preserved in the frame's ``detail`` for troubleshooting only.
            await self._emit(
                build_turn_error_frame(
                    event_id=envelope.event_id,
                    error=TURN_FAILED_FRIENDLY_TEXT,
                    detail=str(exc),
                )
            )

    async def _drain_queued_follow_ups(
        self,
        *,
        envelope,
        session_key: str,
        runtime_chat_id: str,
        memory_chat_id: str,
        listeners,
    ) -> None:
        """排空兜底循环：对齐 websocket_ceo._run_user_turn 的 follow-up 续跑。

        prompt 正常返回后，运行中排队进来的消息可能仍留在会话队列里
        （例如到达时模型已进入最后一段生成）。这里循环续跑直到排空，
        整个过程仍只由调用方发送一个 turn_complete。
        """
        while True:
            session = self._runtime_bridge.get_existing_session(session_key)
            drained = (
                session.drain_queued_follow_up_messages() if session is not None else []
            )
            if not drained:
                return
            archive = getattr(session, "archive_follow_up_chain_transition", None)
            if callable(archive):
                follow_up_turn_ids = {
                    str((getattr(item, "metadata", None) or {}).get("_transcript_turn_id") or "").strip()
                    for item in drained
                }
                follow_up_turn_ids.discard("")
                await archive(pending_follow_up_turn_ids=follow_up_turn_ids)
            result = await self._runtime_bridge.prompt_batch(
                drained,
                session_key=session_key,
                channel=envelope.channel,
                chat_id=runtime_chat_id,
                runtime_channel=envelope.channel,
                runtime_chat_id=runtime_chat_id,
                runtime_memory_channel=envelope.channel,
                runtime_memory_chat_id=memory_chat_id,
                listeners=listeners,
                register_task=self._register_task,
            )
            if getattr(result, "output", None):
                await self._deliver(
                    envelope=envelope,
                    session_key=session_key,
                    text=str(result.output),
                )

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
        sanitized = sanitize_channel_outbound_text(str(msg.content or ""))
        if not sanitized:
            # The message was internal-only (e.g. a bare Task Ledger echo).
            # Returning normally lets the drain loop ack it; raising would
            # trigger a retry storm for content that must never be delivered.
            return
        if self._sender is None:
            # Bus-driven outbound must never be dropped silently: the drain
            # loop treats RuntimeError as transient and retries once the
            # bridge sender is initialized / connected.
            raise RuntimeError("china bridge sender is not initialized")
        parsed_target = self._parse_chat_id_target(getattr(msg, "chat_id", None) or "")
        account_id = str(metadata.get("_china_account_id") or (parsed_target or {}).get("account_id") or "default").strip() or "default"
        peer_kind = str(metadata.get("_china_peer_kind") or (parsed_target or {}).get("peer_kind") or "user").strip() or "user"
        peer_id = str(metadata.get("_china_peer_id") or (parsed_target or {}).get("peer_id") or msg.chat_id or "").strip()
        if not peer_id:
            raise ValueError(
                f"cannot resolve china peer for outbound chat_id={getattr(msg, 'chat_id', '')!r}"
            )
        await self._emit(
            build_deliver_frame(
                event_id=str((msg.metadata or {}).get("_china_event_id") or uuid.uuid4().hex),
                delivery_id=uuid.uuid4().hex,
                channel=str(msg.channel or ""),
                account_id=account_id,
                target_kind=peer_kind,
                target_id=peer_id,
                text=sanitized,
                mode="final",
                reply_to=str(msg.reply_to or (msg.metadata or {}).get("message_id") or "").strip() or None,
                metadata={
                    "session_key": str((msg.metadata or {}).get("session_key") or ""),
                    "task_id": str((msg.metadata or {}).get("task_id") or ""),
                },
            )
        )
