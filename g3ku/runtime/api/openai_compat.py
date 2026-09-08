"""OpenAI-compatible chat gateway over the External Agent API.

Out-of-the-box surface #1 for external AI agents (see
``docs/architecture/agent-gateway.md`` — contract owner; the underlying turn /
event / auth contracts belong to ``docs/architecture/external-agent-api.md``).

Mounted under ``/api/v1`` so OpenAI SDK clients work with
``base_url=http://127.0.0.1:{port}/api/v1`` and the bootstrap-lock middleware
(423) and ``require_external_api`` Bearer auth apply unchanged.

Core semantics (all documented in agent-gateway.md):

- Conversation mapping: ``external_key = openai:{body.user|default}`` →
  persistent g3ku session. Only the LAST user message is forwarded; client
  history is ignored because the g3ku session keeps its own memory. System
  messages are prepended only on the first-ever turn of a newly created
  session.
- Turn outcomes (timeout / failed / cancelled / no_reply) are returned as
  HTTP 200 with honest assistant text plus a non-standard top-level ``g3ku``
  object. This is deliberate: 5xx/504 responses trigger OpenAI SDK auto-retry
  (``max_retries=2``), which resubmits without an idempotency key and creates
  duplicate turns. HTTP errors are reserved for request-level failures
  (400/413/503 in OpenAI ``{"error": {...}}`` shape; 401/403/423 keep the
  platform shapes).
- Streaming diffs ``reply.delta`` (latest-segment FULL text, replace-not-
  append) into OpenAI content deltas by prefix; a segment reset emits a
  separator plus the new segment. ``reply.final`` adds a tail correction only
  when it extends the last streamed segment. Raw deltas are unsanitized —
  text that outbound sanitization would strip may already be on the wire and
  cannot be retracted (accepted limitation).
- Client disconnect stops the wait but NEVER cancels the turn; the reply
  still lands in session history and is retrievable via ``/api/v1`` SSE.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Reused verbatim from the /api/v1 message path so both surfaces share one
# semantic base (cross-module private imports have precedent: external_v1 ↔
# ceo_sessions). Do not rename these helpers.
from g3ku.runtime.api.external_auth import ExternalApiPrincipal, require_external_api
from g3ku.runtime.api.external_v1 import (
    _attachment_descriptor,
    _build_external_user_message,
    _publish_ceo_catalog_best_effort,
    _store_base64_attachment,
)
from g3ku.runtime.api.external_turns import get_external_turn_service
from g3ku.runtime.external_events import (
    SSE_HEARTBEAT_INTERVAL_SECONDS,
    ExternalReplyOutcome,
    get_session_event_hub,
    wait_for_external_reply,
)
from g3ku.runtime.external_sessions import get_external_session_registry
from g3ku.runtime.session_keys import sanitize_channel_outbound_text

router = APIRouter()

MODEL_ID = "g3ku"
DEFAULT_WAIT_SECONDS = 600.0
MIN_WAIT_SECONDS = 5.0
MAX_WAIT_SECONDS = 3600.0
OPENAI_USER_KEY_PREFIX = "openai"
OPENAI_USER_KEY_MAX_CHARS = 128

_DATA_URL_PATTERN = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?;base64,(?P<data>.*)$", re.DOTALL)


def _openai_error(
    status: int,
    message: str,
    *,
    err_type: str = "invalid_request_error",
    code: str | None = None,
) -> JSONResponse:
    """OpenAI error envelope (``{"error": {...}}``) — FastAPI's HTTPException
    would render ``{"detail": ...}`` which OpenAI SDKs cannot parse."""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": str(message),
                "type": str(err_type),
                "param": None,
                "code": code,
            }
        },
    )


def _clamp_wait(payload: dict[str, Any]) -> float:
    try:
        requested = float(payload.get("wait_seconds"))
    except (TypeError, ValueError):
        requested = DEFAULT_WAIT_SECONDS
    return max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, requested))


def _resolve_external_key(payload: dict[str, Any]) -> str:
    user = str(payload.get("user") or "").strip()[:OPENAI_USER_KEY_MAX_CHARS]
    return f"{OPENAI_USER_KEY_PREFIX}:{user or 'default'}"


def _extract_user_text_and_images(message: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Last-user-message content → (text, raw image_url part dicts)."""
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    images: list[dict[str, Any]] = []
    for part in list(content or []):
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").strip().lower()
        if part_type == "text":
            text = str(part.get("text") or "")
            if text.strip():
                texts.append(text)
        elif part_type == "image_url":
            image_url = part.get("image_url")
            url = ""
            if isinstance(image_url, dict):
                url = str(image_url.get("url") or "").strip()
            elif isinstance(image_url, str):
                url = image_url.strip()
            if url:
                images.append({"url": url})
    return "\n".join(texts), images


def _attachments_from_image_parts(session_key: str, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI image_url parts → /api/v1 attachment descriptors.

    ``data:`` URLs are decoded and stored like ``data_base64`` attachments
    (same 5 MiB cap → 413); http(s) URLs pass through as url references; any
    other scheme is a request error.
    """
    from fastapi import HTTPException

    descriptors: list[dict[str, Any]] = []
    for image in images:
        url = str(image.get("url") or "").strip()
        if not url:
            continue
        if url.lower().startswith("data:"):
            match = _DATA_URL_PATTERN.match(url)
            if match is None:
                raise HTTPException(status_code=400, detail="invalid_image_data_url")
            mime_type = str(match.group("mime") or "image/png").strip() or "image/png"
            raw = str(match.group("data") or "").strip()
            try:
                base64.b64decode(raw, validate=False)
            except (binascii.Error, ValueError):
                raise HTTPException(status_code=400, detail="invalid_image_data_url")
            item = {
                "kind": "image",
                "name": f"openai-image-{uuid.uuid4().hex[:6]}.png",
                "mime_type": mime_type,
                "data_base64": raw,
            }
            descriptor = _attachment_descriptor(item)
            if descriptor is None:
                continue
            path, size = _store_base64_attachment(session_key, item)
            descriptor["path"] = path
            descriptor["size"] = size
            descriptors.append(descriptor)
            continue
        if url.lower().startswith(("http://", "https://")):
            descriptor = _attachment_descriptor({"kind": "image", "url": url})
            if descriptor is not None:
                descriptors.append(descriptor)
            continue
        raise HTTPException(status_code=400, detail="unsupported_image_url")
    return descriptors


def _usage_from_reply(usage: dict[str, Any] | None) -> dict[str, int] | None:
    if not isinstance(usage, dict) or not usage:
        return None
    prompt_tokens = int(usage.get("input_tokens") or 0)
    completion_tokens = int(usage.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _completion_payload(
    *,
    model: str,
    text: str,
    usage: dict[str, int] | None = None,
    g3ku: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(model or MODEL_ID),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": str(text)},
                "finish_reason": "stop",
            }
        ],
    }
    if usage:
        payload["usage"] = usage
    if g3ku:
        payload["g3ku"] = g3ku
    return payload


def _find_buffered_final(hub: Any, turn_id: str) -> dict[str, Any] | None:
    """Duplicate-submit pre-scan: the original reply may already sit in the
    replay buffer at seq <= after_seq (F4 in the design notes)."""
    target = str(turn_id or "").strip()
    if not target:
        return None
    for event in reversed(hub.replay(0)):
        if str(event.get("type") or "") == "reply.final" and str(event.get("turn_id") or "") == target:
            return event
    return None


def _outcome_content_and_status(
    outcome: ExternalReplyOutcome,
    *,
    submit_status: str,
    turn_id: str | None,
    session_id: str,
    receipt: str,
) -> tuple[str, str]:
    """(assistant content, g3ku.status) for every turn outcome — HTTP 200 with
    honest text; the 200 policy suppresses OpenAI SDK auto-retry storms."""
    if outcome.kind == "reply":
        return str(outcome.text or ""), "completed"
    if outcome.kind == "failed":
        return f"[g3ku] turn failed: {outcome.error or 'unknown error'}", "failed"
    if outcome.kind == "cancelled":
        return "[g3ku] turn was paused/cancelled before producing a final reply.", "cancelled"
    if outcome.kind == "no_reply":
        return "[g3ku] the turn completed without a user-visible reply.", "no_reply"
    # timeout (deadline or client disconnect)
    if submit_status == "queued":
        return receipt or "收到，将在当前任务中一并处理。", "queued_receipt"
    where = f", turn_id={turn_id}" if turn_id else ""
    return (
        f"[g3ku] still working (session={session_id}{where}). The turn continues in the "
        "background — send a follow-up request later, or retry with a larger "
        "wait_seconds / stream=true.",
        "running",
    )


async def _wait_with_stop(
    session_key: str,
    *,
    after_seq: int,
    timeout: float,
    want_turn_id: str | None,
    queued: bool,
    request: Request,
) -> ExternalReplyOutcome:
    async def _stopped() -> bool:
        try:
            return bool(await request.is_disconnected())
        except Exception:
            return False

    return await wait_for_external_reply(
        session_key,
        after_seq=after_seq,
        timeout=timeout,
        want_turn_id=want_turn_id,
        queued=queued,
        should_stop=_stopped,
    )


def _chunk_payload(*, chunk_id: str, model: str, delta: dict[str, Any], finish_reason: str | None) -> str:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": str(model or MODEL_ID),
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _stream_completion(
    *,
    request: Request,
    session_key: str,
    after_seq: int,
    timeout: float,
    want_turn_id: str | None,
    queued: bool,
    submit_status: str,
    turn_id: str | None,
    receipt: str,
    model: str,
):
    """OpenAI streaming chunks from hub events. Subscribes at generator start;
    the submit-to-subscribe gap is covered by ``replay(after_seq)`` and the
    seen-seq set dedupes the overlap."""
    hub = get_session_event_hub(session_key)
    queue = hub.subscribe()
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    seen: set[int] = set()
    finals: list[dict[str, Any]] = []
    segment_sent = ""
    any_content = False

    def _iter_events() -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            try:
                items.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                return items

    try:
        yield _chunk_payload(chunk_id=chunk_id, model=model, delta={"role": "assistant", "content": ""}, finish_reason=None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        pending = list(hub.replay(after_seq))
        finished_text: str | None = None
        while True:
            for event in pending:
                seq = int(event.get("seq") or 0)
                if seq <= after_seq or seq in seen:
                    continue
                seen.add(seq)
                event_type = str(event.get("type") or "")
                event_turn_id = str(event.get("turn_id") or "")
                if event_type == "reply.delta":
                    if want_turn_id is not None and not queued and event_turn_id != str(want_turn_id):
                        continue
                    text = str(event.get("text") or "")
                    if not text:
                        continue
                    if text.startswith(segment_sent):
                        piece = text[len(segment_sent):]
                    else:
                        piece = ("\n\n" if any_content else "") + text
                    segment_sent = text
                    if piece:
                        any_content = True
                        yield _chunk_payload(chunk_id=chunk_id, model=model, delta={"content": piece}, finish_reason=None)
                    continue
                if event_type == "reply.final":
                    if queued:
                        finals.append(dict(event))
                        continue
                    if want_turn_id is not None and event_turn_id != str(want_turn_id):
                        continue
                    finished_text = str(event.get("text") or "")
                    break
                if event_type == "turn.completed":
                    if queued:
                        if finals:
                            best = max(finals, key=lambda item: int(item.get("seq") or 0))
                            finished_text = str(best.get("text") or "")
                        else:
                            finished_text = (
                                "[g3ku] turn was paused/cancelled before producing a final reply."
                                if event.get("cancelled")
                                else "[g3ku] the turn completed without a user-visible reply."
                            )
                        break
                    if want_turn_id is not None and event_turn_id != str(want_turn_id):
                        continue
                    finished_text = (
                        "[g3ku] turn was paused/cancelled before producing a final reply."
                        if event.get("cancelled")
                        else "[g3ku] the turn completed without a user-visible reply."
                    )
                    break
                if event_type == "turn.failed":
                    if want_turn_id is not None and event_turn_id != str(want_turn_id):
                        continue
                    finished_text = f"[g3ku] turn failed: {event.get('error') or 'unknown error'}"
                    break
            if finished_text is not None:
                break
            pending = []
            remaining = deadline - loop.time()
            if remaining <= 0:
                if submit_status == "queued":
                    finished_text = receipt or "收到，将在当前任务中一并处理。"
                else:
                    where = f", turn_id={turn_id}" if turn_id else ""
                    finished_text = (
                        f"[g3ku] still working (session={session_key}{where}). The turn continues "
                        "in the background — send a follow-up request later."
                    )
                break
            # Disconnect stops the stream but never cancels the turn; the reply
            # still lands in session history (retrievable via /api/v1 SSE).
            try:
                disconnected = bool(await request.is_disconnected())
            except Exception:
                disconnected = False
            if disconnected:
                return
            try:
                event = await asyncio.wait_for(queue.get(), min(remaining, SSE_HEARTBEAT_INTERVAL_SECONDS))
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            pending = [event, *_iter_events()]
        if finished_text is not None:
            correction = ""
            if finished_text.startswith(segment_sent):
                correction = finished_text[len(segment_sent):]
            elif not segment_sent:
                correction = finished_text
            if correction:
                yield _chunk_payload(chunk_id=chunk_id, model=model, delta={"content": correction}, finish_reason=None)
        yield _chunk_payload(chunk_id=chunk_id, model=model, delta={}, finish_reason="stop")
        yield "data: [DONE]\n\n"
    finally:
        hub.unsubscribe(queue)


@router.get("/models")
async def list_models(
    principal: ExternalApiPrincipal = Depends(require_external_api),
):
    _ = principal
    return {
        "object": "list",
        "data": [
            {
                "id": MODEL_ID,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "g3ku",
            }
        ],
    }


@router.post("/chat/completions")
async def create_chat_completion(
    request: Request,
    payload: dict[str, Any] = Body(...),
    principal: ExternalApiPrincipal = Depends(require_external_api),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    from fastapi import HTTPException

    model = str(payload.get("model") or MODEL_ID).strip() or MODEL_ID
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return _openai_error(400, "messages_required")
    last_user: dict[str, Any] | None = None
    system_texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role == "system":
            text = message.get("content")
            if isinstance(text, str) and text.strip():
                system_texts.append(text.strip())
        elif role == "user":
            last_user = message
    if last_user is None:
        return _openai_error(400, "user_message_required")
    user_text, image_parts = _extract_user_text_and_images(last_user)

    external_key = _resolve_external_key(payload)
    entry, created = get_external_session_registry().resolve_or_create(
        bridge_id=principal.bridge_id,
        external_key=external_key,
        title=f"OpenAI compat · {external_key}",
    )
    if created:
        _publish_ceo_catalog_best_effort()

    text = sanitize_channel_outbound_text(user_text)
    if created and system_texts:
        # System prompt enters g3ku memory exactly once — with the first turn
        # of the newly created session; later turns rely on session memory.
        text = "\n\n".join([*system_texts, text]) if text else "\n\n".join(system_texts)
    if not text and not image_parts:
        return _openai_error(400, "message_required")
    try:
        attachments = _attachments_from_image_parts(entry.session_key, image_parts)
    except HTTPException as exc:
        if int(exc.status_code or 400) == 413:
            return _openai_error(413, str(exc.detail or "attachment_too_large"))
        return _openai_error(int(exc.status_code or 400), str(exc.detail or "invalid_request"))

    user_message = _build_external_user_message(
        text=text,
        attachments=attachments,
        sender=None,
        metadata={"source": "openai_compat", "model": model},
    )

    hub = get_session_event_hub(entry.session_key)
    after_seq = hub.last_seq  # captured BEFORE submit — race-fix ordering

    try:
        service = get_external_turn_service()
    except RuntimeError:
        return _openai_error(503, "runtime_unavailable", err_type="server_error", code="runtime_unavailable")
    result = await service.submit(
        entry=entry,
        user_message=user_message,
        idempotency_key=idempotency_key,
    )
    submit_status = str(result.get("status") or "")
    turn_id = str(result.get("turn_id") or "") or None
    receipt = str(result.get("receipt") or "")
    wait_seconds = _clamp_wait(payload)
    stream = bool(payload.get("stream"))

    want_turn_id: str | None = None
    queued = False
    wait_after_seq = after_seq
    if submit_status == "started":
        want_turn_id = turn_id
    elif submit_status == "queued":
        queued = True
    elif submit_status == "duplicate":
        original_status = str(result.get("original_status") or "")
        buffered = _find_buffered_final(hub, turn_id or "")
        if buffered is not None:
            usage = _usage_from_reply(buffered.get("usage") if isinstance(buffered.get("usage"), dict) else None)
            return JSONResponse(
                content=_completion_payload(
                    model=model,
                    text=str(buffered.get("text") or ""),
                    usage=usage,
                    g3ku={
                        "session_id": entry.session_key,
                        "turn_id": turn_id,
                        "created_session": created,
                        "status": "completed",
                        "submit_status": submit_status,
                    },
                )
            )
        queued = original_status == "queued"
        want_turn_id = turn_id if not queued else None
        wait_after_seq = 0

    if stream:
        return StreamingResponse(
            _stream_completion(
                request=request,
                session_key=entry.session_key,
                after_seq=wait_after_seq,
                timeout=wait_seconds,
                want_turn_id=want_turn_id,
                queued=queued,
                submit_status=submit_status,
                turn_id=turn_id,
                receipt=receipt,
                model=model,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    outcome = await _wait_with_stop(
        entry.session_key,
        after_seq=wait_after_seq,
        timeout=wait_seconds,
        want_turn_id=want_turn_id,
        queued=queued,
        request=request,
    )
    content, status = _outcome_content_and_status(
        outcome,
        submit_status=submit_status,
        turn_id=outcome.turn_id or turn_id,
        session_id=entry.session_key,
        receipt=receipt,
    )
    return JSONResponse(
        content=_completion_payload(
            model=model,
            text=content,
            usage=_usage_from_reply(outcome.usage),
            g3ku={
                "session_id": entry.session_key,
                "turn_id": outcome.turn_id or turn_id,
                "created_session": created,
                "status": status,
                "submit_status": submit_status,
            },
        )
    )
