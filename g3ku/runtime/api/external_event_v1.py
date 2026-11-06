"""Authenticated inbound endpoint for agent-created external event subscriptions."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Body, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from loguru import logger

from g3ku.config.live_runtime import get_runtime_config
from g3ku.runtime.external_event_service import get_external_event_service

router = APIRouter()

_EVENT_BODY_MAX_BYTES = 256 * 1024
_IDEMPOTENCY_KEY_MAX_CHARS = 128
_EVENT_TYPE_MAX_CHARS = 128
_OCCURRED_AT_MAX_CHARS = 64


def _extract_bearer_token(authorization: str | None) -> str:
    raw = str(authorization or "").strip()
    if not raw.lower().startswith("bearer "):
        return ""
    return raw[7:].strip()


def _require_external_api_enabled() -> None:
    config = get_runtime_config(force=False)[0]
    external_api = getattr(config, "external_api", None)
    if external_api is None or not bool(getattr(external_api, "enabled", False)):
        raise HTTPException(status_code=403, detail="external_api_disabled")


@router.post("/event-subscriptions/{subscription_id}/events")
async def post_external_event(
    subscription_id: str,
    payload: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None, alias="Authorization"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Response:
    _require_external_api_enabled()
    service = get_external_event_service()
    if service is None:
        raise HTTPException(status_code=503, detail="event_service_unavailable")

    token = _extract_bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="invalid_api_token")
    subscription, reason = service.manager.authenticate(subscription_id, token)
    if reason == "not_found":
        raise HTTPException(status_code=404, detail="subscription_not_found")
    if reason == "disabled":
        raise HTTPException(status_code=403, detail="subscription_disabled")
    if reason != "ok" or subscription is None:
        raise HTTPException(status_code=401, detail="invalid_api_token")

    key = str(idempotency_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="idempotency_key_required")
    if len(key) > _IDEMPOTENCY_KEY_MAX_CHARS:
        raise HTTPException(status_code=400, detail="idempotency_key_too_long")

    event_type = str(payload.get("type") or "").strip()
    if len(event_type) > _EVENT_TYPE_MAX_CHARS:
        raise HTTPException(status_code=400, detail="event_type_too_long")
    occurred_at = str(payload.get("occurred_at") or "").strip()
    if len(occurred_at) > _OCCURRED_AT_MAX_CHARS:
        raise HTTPException(status_code=400, detail="occurred_at_too_long")
    text = str(payload.get("text") or "")
    raw_data = payload.get("data")
    if raw_data is not None and not isinstance(raw_data, dict):
        raise HTTPException(status_code=400, detail="event_data_must_be_object")
    data = dict(raw_data or {})
    if not text.strip() and not data:
        raise HTTPException(status_code=400, detail="event_content_required")

    try:
        encoded_body = json.dumps(
            {"text": text, "data": data},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="event_content_not_serializable") from None
    if len(encoded_body) > _EVENT_BODY_MAX_BYTES:
        raise HTTPException(status_code=413, detail="event_body_too_large")

    try:
        record, inserted = service.accept(
            subscription=subscription,
            idempotency_key=key,
            event_type=event_type,
            occurred_at=occurred_at,
            text=text,
            data=data,
        )
    except ValueError as exc:
        logger.warning("external event rejected: subscription={} reason={}", subscription_id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from None

    return JSONResponse(
        status_code=202 if inserted else 200,
        content={
            "ok": True,
            "event_id": str(record.get("event_id") or ""),
            "status": str(record.get("status") or ""),
            "duplicate": not inserted,
        },
    )


__all__ = ["router"]
