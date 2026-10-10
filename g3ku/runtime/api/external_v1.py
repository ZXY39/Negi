"""External Agent API — channel-agnostic headless surface (``/api/v1``).

Consumed by third-party bridge applications (IM bots, automation bridges).
Contract owner: ``docs/architecture/external-agent-api.md``.

Endpoints (all Bearer-authenticated via ``require_external_api``; the global
bootstrap lock middleware returns 423 while the project is locked):

- ``POST   /sessions``                     get-or-create by ``external_key``
- ``GET    /sessions``                     list this bridge's sessions
- ``PATCH  /sessions/{session_id}``        rename (title)
- ``DELETE /sessions/{session_id}``        clear semantics (context reset, entry kept)
- ``GET    /sessions/{session_id}/state``  running/queued/inflight snapshot
- ``POST   /sessions/{session_id}/messages`` submit turn (or queue follow-up)
- ``POST   /turns/{turn_id}/pause``        pause the turn's session
- ``POST   /sessions/{session_id}/cancel`` cancel session tasks
- ``GET    /sessions/{session_id}/events`` SSE event stream (replay via Last-Event-ID)
- ``GET    /outbox/pending``               pending durable pushes for this bridge (pump warm-up)
- ``POST   /sessions/{session_id}/outbox/{outbox_id}/ack`` mark a durable push delivered
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from loguru import logger

from g3ku.core.messages import UserInputMessage
from g3ku.runtime.api.ceo_sessions import _publish_ceo_sessions_snapshot
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.external_events import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    get_session_event_hub,
)
from g3ku.runtime.api.external_turns import get_external_turn_service
from g3ku.runtime.ceo_catalog_offload import build_ceo_session_catalog_async
from g3ku.runtime.external_outbox import ack_outbound_message, load_pending_outbound
from g3ku.runtime.external_sessions import (
    ExternalSessionEntry,
    get_external_session_registry,
)
from g3ku.runtime.session_keys import sanitize_channel_outbound_text
from g3ku.runtime.web_ceo_sessions import (
    EXTERNAL_UPLOAD_ROOT,
    WEB_CEO_IMAGE_UPLOAD_MAX_BYTES,
    WebCeoStateStore,
    clear_web_ceo_session_artifacts,
    workspace_path,
)
from g3ku.session.manager import SessionManager
from g3ku.shells.web import get_runtime_manager, peek_global_agent
from g3ku.utils.helpers import ensure_dir, safe_filename

router = APIRouter()

# 图片附件沿用 web CEO 上传上限；文档/文件类附件单独放宽（渠道传文档常超
# 5MiB）。超限一律 413 ``attachment_too_large``，桥侧按同值预过滤避免必然失败。
EXTERNAL_FILE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
# 语音条转码的内存预算：60 秒 24 kHz WAV 是 2.9 MB，留到 8 MB 已经宽到不会把
# 真正的语音条挡在压缩外面，也不会因为有人从渠道丢大文件而吸进整块内存。
VOICE_CLIP_TRANSCODE_MAX_BYTES = 8 * 1024 * 1024


def _registry():
    return get_external_session_registry()


def _own_entry(session_id: str, principal: ExternalApiPrincipal) -> ExternalSessionEntry:
    """本桥名下会话的唯一判据：跨桥寻址绝不走这里。

    注册表生命周期（改名、清空）与 outbox 销账都归"会话属于签发它的那一号"，
    放宽就等于让一个桥替另一个桥的账本做主。
    """
    raw = str(session_id or "").strip()
    entry = _registry().get_by_session_key(raw)
    if entry is None or entry.bridge_id != principal.bridge_id:
        raise HTTPException(status_code=404, detail="session_not_found")
    return entry


def _resolve_entry(session_id: str, principal: ExternalApiPrincipal) -> ExternalSessionEntry:
    """读、发信与中止车道的作用域判据。

    本桥名下 → 注册表条目原样返回。跨桥只在 token 被允许时放行，并且只认真实存在的
    两样东西：别桥的注册会话，或有转录文件的会话键（`web:ceo-*` / `qq:*` 这些不进
    注册表，没有转录就 404——否则拿到 token 的人能凭空造键）。

    合成条目的 `bridge_id` 记**发起方**：回合归属、幂等位与事件流都按发起方算，
    跨桥发进去的那一轮回复因此回到正在等的这个调用方，而不是目标会话原来的桥。
    """
    raw = str(session_id or "").strip()
    entry = _registry().get_by_session_key(raw)
    if entry is not None and entry.bridge_id == principal.bridge_id:
        return entry
    if not principal.cross_session:
        raise HTTPException(status_code=404, detail="session_not_found")
    if entry is None and not _session_manager().has_transcript(raw):
        raise HTTPException(status_code=404, detail="session_not_found")
    return ExternalSessionEntry(
        bridge_id=principal.bridge_id,
        external_key=str(entry.external_key or raw) if entry is not None else raw,
        session_key=raw,
        created_at=str(entry.created_at or "") if entry is not None else "",
        title=str(entry.title or "") if entry is not None else "",
    )


def _session_manager() -> SessionManager:
    return SessionManager(workspace_path())


def _publish_ceo_catalog_best_effort() -> None:
    """Push a fresh CEO session catalog to connected web clients.

    External sessions appear in the web CEO sidebar through the catalog; the
    browser only refetches the REST list on page load, so live visibility of a
    newly created/renamed ``ext:*`` session depends on this global publish.
    Best-effort by contract: it must never fail the /api/v1 call. It only
    observes the running web runtime (``peek_global_agent`` never constructs
    the agent); with no live runtime there is no browser to notify.
    """
    try:
        agent = peek_global_agent()
        if agent is None:
            return
        session_manager = getattr(agent, "sessions", None)
        if session_manager is None:
            return
        runtime_manager = get_runtime_manager(agent)
        state_store = WebCeoStateStore(workspace_path())
        _publish_ceo_sessions_snapshot(agent, session_manager, runtime_manager, state_store)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("external api ceo catalog publish skipped: {}", exc)


# -- sessions ---------------------------------------------------------------


@router.post("/sessions")
async def create_external_session(
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    external_key = str(payload.get("external_key") or "").strip()
    if not external_key:
        raise HTTPException(status_code=400, detail="external_key_required")
    title = str(payload.get("title") or "").strip() or None
    entry, created = _registry().resolve_or_create(
        bridge_id=principal.bridge_id, external_key=external_key, title=title
    )
    if created:
        _publish_ceo_catalog_best_effort()
    return {
        "ok": True,
        "session_id": entry.session_key,
        "external_key": entry.external_key,
        "title": entry.title,
        "created_at": entry.created_at,
        "created": created,
    }


async def _list_all_sessions(principal: ExternalApiPrincipal) -> dict[str, Any]:
    """全量会话目录（跨桥作用域）：复用网页那份装配，不另开一条枚举道。

    store 必须取**运行时那一份** `agent.sessions`，不能现场 new：目录构造里逐键
    `get_or_create` 的异常是被 `except: continue` 吞掉的，第二个实例在同一批转录上
    整表跳过时，接口只会安静地返回空 items（实盘第一版就栽在这，200 + 0 条，而
    `/api/ceo/sessions` 同刻给 175 条）。运行档位也顺手用同一个 runtime_manager 读，
    与 `/ws/ceo` 的判据同形。

    目录构建要遍历全部转录（渠道会话单份可达数十 MB），统一在 CEO 专用线程里跑并吃
    3 秒 TTL 缓存，事件循环只 await。条目形状以 `build_session_summary` 为唯一来源，
    这里只补 `transcript_path`——运行时算出的安全名（含键长超限的 digest 逃生），
    调用方不要自己把 session key 拼成文件名。
    """
    if not principal.cross_session:
        raise HTTPException(status_code=403, detail="cross_session_scope_required")
    agent = peek_global_agent()
    manager = getattr(agent, "sessions", None) if agent is not None else None
    if manager is None:
        manager = _session_manager()
    runtime_manager = get_runtime_manager(agent) if agent is not None else None

    def _session_state(session_key: str) -> Any:
        if runtime_manager is None:
            return None
        getter = getattr(runtime_manager, "get", None)
        if not callable(getter):
            return None
        session = getter(str(session_key or "").strip())
        return getattr(session, "state", None) if session is not None else None

    catalog = await build_ceo_session_catalog_async(
        manager,
        active_session_id=f"ext-api:{principal.bridge_id}",
        is_running_resolver=lambda key: bool(getattr(_session_state(key), "is_running", False))
        or str(getattr(_session_state(key), "status", "") or "").strip().lower() == "running",
        status_resolver=lambda key: str(getattr(_session_state(key), "status", "") or "").strip().lower(),
    )
    # 目录把渠道会话放在 channel_groups 里，"全部会话"必须把两侧合起来：
    # 只回 items 就等于把渠道端那些号藏起来。
    raw_entries = list(catalog.get("items") or [])
    for group in list(catalog.get("channel_groups") or []):
        if isinstance(group, dict):
            raw_entries.extend(list(group.get("items") or []))
    items: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for raw_item in raw_entries:
        if not isinstance(raw_item, dict):
            continue
        session_id = str(raw_item.get("session_id") or "").strip()
        if not session_id or session_id in seen_keys:
            continue
        seen_keys.add(session_id)
        item = dict(raw_item)
        item["transcript_path"] = str(manager.get_path(session_id))
        items.append(item)
    return {
        "ok": True,
        "bridge_id": principal.bridge_id,
        "scope": "all",
        "items": items,
    }


@router.get("/sessions")
async def list_external_sessions(
    external_key: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    registry = _registry()
    if str(scope or "").strip().lower() == "all":
        return await _list_all_sessions(principal)
    if external_key:
        key = registry.get_session_key(bridge_id=principal.bridge_id, external_key=str(external_key).strip())
        entries = [registry.get_by_session_key(key)] if key else []
    else:
        entries = registry.list_bridge_sessions(principal.bridge_id)
    manager = _session_manager()
    return {
        "ok": True,
        "bridge_id": principal.bridge_id,
        "items": [
            {
                "session_id": entry.session_key,
                "external_key": entry.external_key,
                "title": entry.title,
                "created_at": entry.created_at,
                # 注册表条目与转录是两个轴：桥一注册就有线上条目（还没有转录文件）。
                # 这个读数说的是**文件在不在**——网页侧「删除渠道会话」会删掉它（空壳），
                # 桥侧 `DELETE /sessions/{id}` 的清除语义是清空并保存（文件留着）。
                # 目录侧不重复这个字段：那里每行本来就带 message_count。
                "has_transcript": manager.has_transcript(entry.session_key),
            }
            for entry in entries
            if entry is not None
        ],
    }


@router.patch("/sessions/{session_id}")
async def rename_external_session(
    session_id: str,
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    entry = _own_entry(session_id, principal)
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title_required")
    _registry().update_title(entry.session_key, title)
    _publish_ceo_catalog_best_effort()
    return {"ok": True, "session_id": entry.session_key, "title": title}


@router.delete("/sessions/{session_id}")
async def clear_external_session(
    session_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """Clear semantics (matches the Channel Session Clear Contract): the
    registry entry stays visible, the transcript and side artifacts reset so
    the next turn starts from an empty context."""
    entry = _own_entry(session_id, principal)
    session_key = entry.session_key
    manager = _session_manager()
    session = manager.get_or_create(session_key)
    session.clear()
    manager.save(session)
    manager.invalidate(session_key)
    clear_web_ceo_session_artifacts(session_id=session_key)
    return {"ok": True, "cleared": True, "session_id": session_key}


@router.get("/sessions/{session_id}/state")
async def get_external_session_state(
    session_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    entry = _resolve_entry(session_id, principal)
    service = get_external_turn_service()
    session = service._runtime_bridge.get_existing_session(entry.session_key)
    running = service._runtime_bridge.session_is_running(session)
    queued = 0
    last_error = None
    if session is not None:
        state = getattr(session, "state", None)
        queued = len(list(getattr(state, "queued_follow_up_messages", None) or []))
        error = getattr(state, "last_error", None)
        if error is not None:
            last_error = {
                "code": str(getattr(error, "code", "") or ""),
                "message": str(getattr(error, "message", "") or ""),
            }
    return {
        "ok": True,
        "session_id": entry.session_key,
        "external_key": entry.external_key,
        "running": running,
        "queued_follow_ups": queued,
        "inflight_turn_id": service.inflight_turn_id_for(entry.session_key),
        "last_error": last_error,
    }


# -- messages / turns -------------------------------------------------------


def _attachment_descriptor(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "").strip().lower()
    mime_type = str(item.get("mime_type") or "").strip().lower()
    path = str(item.get("path") or "").strip()
    url = str(item.get("url") or "").strip()
    if not kind:
        if mime_type.startswith("image/"):
            kind = "image"
        elif mime_type.startswith("audio/"):
            kind = "audio"
        elif mime_type.startswith("video/"):
            kind = "video"
        else:
            kind = "file"
    if not mime_type and path:
        mime_type = str(mimetypes.guess_type(path)[0] or "").strip()
    descriptor: dict[str, Any] = {
        "kind": kind,
        "name": str(item.get("name") or "").strip()
        or (Path(path).name if path else (url.split("/")[-1] if url else "attachment")),
        "mime_type": mime_type,
    }
    if path:
        descriptor["path"] = path
    if url:
        descriptor["url"] = url
    return descriptor


def _store_base64_attachment(
    session_key: str, item: dict[str, Any], *, max_bytes: int
) -> tuple[str, int]:
    raw = str(item.get("data_base64") or "")
    # 先按 base64 长度廉价估算拒绝明显超限载荷，避免白白解码大对象。
    if len(raw) > (max_bytes // 3 + 1) * 4 + 4:
        raise HTTPException(status_code=413, detail="attachment_too_large")
    data = base64.b64decode(raw)
    size = len(data)
    if size > max_bytes:
        raise HTTPException(status_code=413, detail="attachment_too_large")
    name = safe_filename(str(item.get("name") or "attachment")) or "attachment"
    target_dir = ensure_dir(workspace_path() / EXTERNAL_UPLOAD_ROOT / safe_filename(session_key))
    target = target_dir / f"{uuid.uuid4().hex[:8]}-{name}"
    target.write_bytes(data)
    return str(target), size


async def _compressed_voice_clip(item: dict[str, Any]) -> dict[str, Any]:
    """语音条落盘前先压成 MP3，压不动就原样交回。

    渠道语音到这里的字节是引擎解码后的 PCM WAV（QQ 的原始 silk 浏览器播不了），
    32 KB/秒 地留在盘上没有意义：实测 22.85 秒样本 731 KB → 92 KB，耗时 0.18 秒。
    没有 ffmpeg、不是 WAV、或大得不像语音条时一律退回原形状——压缩是优化，
    不是这条车道的前提。
    """
    from g3ku.stt import audio as stt_audio

    if str(item.get("kind") or "").strip().lower() != "audio":
        return item
    try:
        raw = base64.b64decode(str(item.get("data_base64") or ""), validate=False)
    except (binascii.Error, ValueError):
        return item
    if not raw or len(raw) > VOICE_CLIP_TRANSCODE_MAX_BYTES or not stt_audio.is_wav(raw):
        return item
    encoded = await asyncio.to_thread(stt_audio.encode_to_mp3, raw)
    if not encoded:
        return item
    stem = Path(str(item.get("name") or "voice")).stem or "voice"
    return {
        **item,
        "name": f"{stem}.mp3",
        "mime_type": "audio/mpeg",
        "data_base64": base64.b64encode(encoded).decode("ascii"),
    }


def _image_url_from_attachment(attachment: dict[str, Any]) -> str | None:
    mime_type = str(attachment.get("mime_type") or "").strip() or "image/png"
    path = str(attachment.get("path") or "").strip()
    if path:
        candidate = Path(path)
        if candidate.exists() and candidate.is_file():
            encoded = base64.b64encode(candidate.read_bytes()).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"
    return str(attachment.get("url") or "").strip() or None


def _build_external_user_message(
    *,
    text: str,
    attachments: list[dict[str, Any]],
    sender: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> str | UserInputMessage:
    """Generalized from the legacy China transport message builder: attachment
    note + provider-visible image blocks for the current turn only."""
    message_metadata: dict[str, Any] = dict(metadata or {})
    if sender:
        message_metadata["external_sender"] = sender
    if not attachments and not message_metadata:
        return text
    if attachments:
        message_metadata["external_attachments"] = attachments
    if not attachments:
        return UserInputMessage(content=text, metadata=message_metadata)

    lines = ["Channel attachments:"]
    for item in attachments:
        kind = str(item.get("kind") or "")
        if kind == "audio":
            # 语音条的内容已经在正文的转写里；再给一行路径只会让模型以为要去
            # 打开一个文件。它是给人回放的素材，不是给模型的输入。
            continue
        label = "image" if kind == "image" else "file"
        source = str(item.get("path") or item.get("url") or "").strip()
        suffix = f" (local path: {source})" if source else ""
        lines.append(f"- {label}: {item['name']}{suffix}")
    if len(lines) > 1:
        lines.append("You may inspect the local file paths or URLs above when helpful.")
    note = "\n".join(lines) if len(lines) > 1 else ""

    text_value = str(text or "")
    merged_text = f"{text_value}\n\n{note}" if (note and text_value) else (note or text_value)
    content: list[dict[str, Any]] = []
    if merged_text:
        content.append({"type": "text", "text": merged_text})
    for attachment in attachments:
        if str(attachment.get("kind") or "") != "image":
            continue
        image_url = _image_url_from_attachment(attachment)
        if not image_url:
            continue
        content.append({"type": "image_url", "image_url": {"url": image_url}})
    attachment_refs = [
        str(item.get("path") or item.get("url") or "").strip()
        for item in attachments
        if str(item.get("kind") or "") != "audio"
        and str(item.get("path") or item.get("url") or "").strip()
    ]
    return UserInputMessage(
        content=content or [{"type": "text", "text": note or text_value}],
        attachments=attachment_refs,
        metadata=message_metadata,
    )


@router.post("/sessions/{session_id}/messages")
async def post_external_message(
    session_id: str,
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    entry = _resolve_entry(session_id, principal)
    text = sanitize_channel_outbound_text(str(payload.get("text") or ""))
    raw_attachments = payload.get("attachments")
    if raw_attachments is not None and not isinstance(raw_attachments, list):
        raise HTTPException(status_code=400, detail="attachments_must_be_list")
    if not text and not raw_attachments:
        raise HTTPException(status_code=400, detail="message_required")

    descriptors: list[dict[str, Any]] = []
    for item in list(raw_attachments or []):
        if isinstance(item, dict) and str(item.get("data_base64") or "").strip():
            item = await _compressed_voice_clip(item)
        descriptor = _attachment_descriptor(item)
        if descriptor is None:
            continue
        if isinstance(item, dict) and str(item.get("data_base64") or "").strip():
            max_bytes = (
                WEB_CEO_IMAGE_UPLOAD_MAX_BYTES
                if str(descriptor.get("kind") or "") == "image"
                else EXTERNAL_FILE_UPLOAD_MAX_BYTES
            )
            path, size = _store_base64_attachment(
                entry.session_key, item, max_bytes=max_bytes
            )
            descriptor["path"] = path
            descriptor["size"] = size
        if not descriptor.get("path") and not descriptor.get("url"):
            continue
        descriptors.append(descriptor)

    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else None
    sender_payload = None
    if sender:
        sender_payload = {
            "id": str(sender.get("id") or "").strip(),
            "name": str(sender.get("name") or "").strip(),
        }
    extra_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None

    user_message = _build_external_user_message(
        text=text,
        attachments=descriptors,
        sender=sender_payload,
        metadata=extra_metadata,
    )

    try:
        service = get_external_turn_service()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    result = await service.submit(
        entry=entry,
        user_message=user_message,
        idempotency_key=idempotency_key,
    )
    return {"ok": True, "session_id": entry.session_key, **result}


@router.post("/turns/{turn_id}/pause")
async def pause_external_turn(
    turn_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    try:
        service = get_external_turn_service()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    record = service.get_turn(turn_id)
    if record is None or record.bridge_id != principal.bridge_id:
        raise HTTPException(status_code=404, detail="turn_not_found")
    try:
        paused = await service.pause_turn(turn_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="turn_not_found")
    return {"ok": True, "paused": bool(paused), "turn_id": record.turn_id, "session_id": record.session_key}


@router.post("/sessions/{session_id}/cancel")
async def cancel_external_session(
    session_id: str,
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    entry = _resolve_entry(session_id, principal)
    try:
        service = get_external_turn_service()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="runtime_unavailable")
    cancelled = await service.cancel_session(entry.session_key)
    return {"ok": True, "cancelled": int(cancelled), "session_id": entry.session_key}


# -- outbox (durable proactive push ledger) ---------------------------------

# 桥能写回的销账状态。其他值一律按 delivered 处理：账本出口必须是封闭集合，
# 不能让一个拼错的 status 变成"既没投递也没回收"的第三种状态。
_OUTBOX_ACK_STATUSES = frozenset({"delivered", "unreachable", "undeliverable"})


@router.get("/outbox/pending")
async def list_pending_outbox_entries(
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """Pending durable pushes owned by this bridge (pump warm-up list).

    进程重启后桥的 sessions 映射清空、pump 只等下一条入站消息才建；桥在启动时
    用本列表为有滞留推送的会话预热 pump。消息正文经各会话 SSE 流的重放投递，
    本端点只返回路由身份，不返回文本。
    """
    own_session_keys = {
        entry.session_key
        for entry in _registry().list_bridge_sessions(principal.bridge_id)
        if entry is not None
    }
    items = [
        {
            "outbox_id": str(record.get("id") or ""),
            "session_id": str(record.get("session_key") or ""),
            "external_key": str(record.get("external_key") or ""),
            "ts": str(record.get("ts") or ""),
        }
        for record in load_pending_outbound()
        if str(record.get("session_key") or "") in own_session_keys
    ]
    return {"ok": True, "items": items}


@router.post("/sessions/{session_id}/outbox/{outbox_id}/ack")
async def ack_outbox_entry(
    session_id: str,
    outbox_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    """Close one durable outbox entry (idempotent, session-scoped).

    桥在渠道 API 确认送达后调用；ack 落在 append-only 账本上，启动重放只投
    未 ack 的条目。跨会话的 outbox_id 一律拒绝（``acked=false``）。

    可选 body ``{"status": "undeliverable"}`` 让桥在投递预算耗尽时按终态销账：
    账本已不按年龄清理记录，这是永久拒投的目标唯一的重试上界。不带 body 时
    仍是 ``delivered``，旧桥的请求形状不变。
    """
    entry = _own_entry(session_id, principal)
    status = str((payload or {}).get("status") or "").strip().lower()
    if status not in _OUTBOX_ACK_STATUSES:
        status = "delivered"
    acked = ack_outbound_message(outbox_id, session_key=entry.session_key, status=status)
    return {"ok": True, "acked": bool(acked), "outbox_id": str(outbox_id or ""), "status": status}


# -- events (SSE) -----------------------------------------------------------


def _sse_chunk(event: dict[str, Any]) -> str:
    data = json.dumps(event, ensure_ascii=False)
    return f"id: {event.get('seq')}\nevent: {event.get('type')}\ndata: {data}\n\n"


@router.get("/sessions/{session_id}/events")
async def stream_external_events(
    session_id: str,
    request: Request,
    principal: ExternalApiPrincipal = Depends(require_external_api),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    entry = _resolve_entry(session_id, principal)
    hub = get_session_event_hub(entry.session_key)
    queue = hub.subscribe()
    try:
        last_seq = int(str(last_event_id or "0").strip() or 0)
    except ValueError:
        last_seq = 0
    # 服务端重启后 hub.seq 从 0 重新计数，独立进程桥缓存的却是旧进程大序号：
    # 越过本进程已发布序号的 Last-Event-ID 一律视为旧进程遗留，从头重放积压。
    if last_seq > hub.last_seq:
        last_seq = 0

    async def generate():
        try:
            for event in hub.replay(last_seq):
                yield _sse_chunk(event)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_INTERVAL_SECONDS)
                    yield _sse_chunk(event)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
