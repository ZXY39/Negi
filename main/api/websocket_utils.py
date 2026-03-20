from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

_DISCONNECT_RUNTIME_MARKERS = (
    'disconnect',
    'websocket is not connected',
    'close message has been sent',
    'websocket closed',
)
_DISCONNECT_WINERRORS = {10053, 10054}
_DISCONNECT_ERRNOS = {32, 54, 104}


class WebSocketChannelClosed(Exception):
    """Raised when a websocket client has already disconnected."""


def is_disconnect_exception(exc: BaseException) -> bool:
    if isinstance(exc, WebSocketDisconnect):
        return True
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return True
    if isinstance(exc, RuntimeError):
        text = str(exc or '').strip().lower()
        return any(marker in text for marker in _DISCONNECT_RUNTIME_MARKERS)
    if not isinstance(exc, OSError):
        return False
    winerror = getattr(exc, 'winerror', None)
    errno = getattr(exc, 'errno', None)
    return winerror in _DISCONNECT_WINERRORS or errno in _DISCONNECT_ERRNOS


def _raise_if_disconnect(exc: BaseException) -> None:
    if is_disconnect_exception(exc):
        raise WebSocketChannelClosed() from exc
    raise exc


async def websocket_send_json(websocket: WebSocket, payload: dict[str, Any]) -> None:
    try:
        await websocket.send_json(payload)
    except Exception as exc:
        _raise_if_disconnect(exc)


async def websocket_receive_text(
    websocket: WebSocket,
    *,
    timeout: float | None = None,
) -> str | None:
    try:
        if timeout is None:
            return await websocket.receive_text()
        return await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
    except asyncio.TimeoutError:
        return None
    except Exception as exc:
        _raise_if_disconnect(exc)


async def websocket_receive_json(websocket: WebSocket) -> dict[str, Any]:
    try:
        payload = await websocket.receive_json()
    except Exception as exc:
        _raise_if_disconnect(exc)
    if isinstance(payload, dict):
        return payload
    return {}


async def websocket_close(websocket: WebSocket, *, code: int = 1000) -> None:
    try:
        await websocket.close(code=code)
    except Exception as exc:
        if is_disconnect_exception(exc):
            return
        raise
