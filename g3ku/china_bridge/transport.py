from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable

from g3ku.china_bridge.protocol import (
    build_deliver_frame,
    build_turn_complete_frame,
    build_turn_error_frame,
    normalize_inbound_frame,
)
from g3ku.china_bridge.session_keys import build_chat_id, build_session_key
from g3ku.core.messages import UserInputMessage
from g3ku.runtime.bridge import SessionRuntimeBridge, cli_event_text

CHINA_CHANNELS = {"qqbot", "dingtalk", "wecom", "wecom-app", "feishu-china"}

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
        chat_id = build_chat_id(
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
        attachments = [item.path or item.url or "" for item in envelope.attachments if (item.path or item.url)]
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

            user_message: str | UserInputMessage = text
            if attachments or metadata:
                user_message = UserInputMessage(content=text, attachments=attachments, metadata=metadata)

            async def _listener(event) -> None:
                kind, text_payload = cli_event_text(event)
                if not text_payload:
                    return
                if kind in {"tool", "tool_plan", "tool_result", "tool_error"}:
                    if not bool(getattr(self._app_config.channels, "send_tool_hints", False)):
                        return
                    mode = "tool_hint"
                else:
                    if not bool(getattr(self._app_config.channels, "send_progress", True)):
                        return
                    mode = "progress"
                await self._emit(
                    build_deliver_frame(
                        event_id=envelope.event_id,
                        delivery_id=uuid.uuid4().hex,
                        channel=envelope.channel,
                        account_id=envelope.account_id,
                        target_kind=envelope.peer_kind,
                        target_id=envelope.peer_id,
                        text=str(text_payload),
                        mode=mode,
                        reply_to=envelope.message_id,
                        metadata={"session_key": session_key},
                    )
                )

            result = await self._runtime_bridge.prompt(
                user_message,
                session_key=session_key,
                channel=envelope.channel,
                chat_id=chat_id,
                listeners=[_listener],
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
