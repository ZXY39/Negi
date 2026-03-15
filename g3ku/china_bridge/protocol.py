from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from g3ku.china_bridge.models import ChinaAttachment, ChinaInboundEnvelope


def now_iso() -> str:
    return datetime.now().isoformat()


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def build_auth_frame(token: str) -> dict[str, Any]:
    return {"type": "auth", "token": str(token or ""), "client": "g3ku-python"}


def build_deliver_frame(
    *,
    event_id: str,
    delivery_id: str,
    channel: str,
    account_id: str,
    target_kind: str,
    target_id: str,
    text: str,
    mode: str,
    reply_to: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "deliver_message",
        "event_id": event_id,
        "delivery_id": delivery_id,
        "channel": channel,
        "account_id": account_id,
        "target": {"kind": target_kind, "id": target_id},
        "reply_to": reply_to,
        "payload": {"text": text, "attachments": [], "mode": mode},
        "metadata": dict(metadata or {}),
        "timestamp": now_iso(),
    }


def build_turn_complete_frame(*, event_id: str) -> dict[str, Any]:
    return {"type": "turn_complete", "event_id": event_id, "timestamp": now_iso()}


def build_turn_error_frame(*, event_id: str, error: str) -> dict[str, Any]:
    return {
        "type": "turn_error",
        "event_id": event_id,
        "error": str(error or "unknown error"),
        "timestamp": now_iso(),
    }


def normalize_inbound_frame(payload: dict[str, Any] | None) -> ChinaInboundEnvelope | None:
    if not isinstance(payload, dict):
        return None
    if str(payload.get("type") or "").strip() != "inbound_message":
        return None
    event_id = str(payload.get("event_id") or "").strip()
    channel = str(payload.get("channel") or "").strip()
    account_id = str(payload.get("account_id") or "default").strip() or "default"
    peer = payload.get("peer") if isinstance(payload.get("peer"), dict) else {}
    peer_kind = str(peer.get("kind") or "user").strip() or "user"
    peer_id = str(peer.get("id") or "").strip()
    if not event_id or not channel or not peer_id:
        return None
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    attachments: list[ChinaAttachment] = []
    for item in list(message.get("attachments") or []):
        if not isinstance(item, dict):
            continue
        size_bytes_raw = item.get("size_bytes")
        try:
            size_bytes = int(size_bytes_raw) if size_bytes_raw is not None else None
        except Exception:
            size_bytes = None
        attachments.append(
            ChinaAttachment(
                kind=str(item.get("kind") or "unknown"),
                url=str(item.get("url") or "").strip() or None,
                path=str(item.get("path") or "").strip() or None,
                mime_type=str(item.get("mime_type") or "").strip() or None,
                file_name=str(item.get("file_name") or "").strip() or None,
                size_bytes=size_bytes,
            )
        )
    return ChinaInboundEnvelope(
        event_id=event_id,
        channel=channel,
        account_id=account_id,
        peer_kind=peer_kind,
        peer_id=peer_id,
        peer_display_name=str(peer.get("display_name") or "").strip() or None,
        thread_id=str(payload.get("thread_id") or "").strip() or None,
        message_id=str(message.get("id") or "").strip() or None,
        text=str(message.get("text") or ""),
        attachments=attachments,
        metadata=dict(payload.get("metadata") or {}),
    )
