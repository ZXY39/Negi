"""OneBot 11 client: forward-WebSocket event receive + HTTP action calls.

Targets NapCat / LLOneBot / Lagrange style endpoints: events arrive on the
forward WebSocket, actions go through the HTTP API (``send_private_msg``,
``send_group_msg``, ``get_login_info``).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import httpx
import websockets

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class OnebotClient:
    def __init__(self, ws_url: str, http_url: str):
        self._ws_url = str(ws_url)
        self._http_url = str(http_url or "").rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._http_url, timeout=30.0)
        self._ws: Any | None = None

    async def close(self) -> None:
        await self._http.aclose()
        if self._ws is not None:
            await self._ws.close()

    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        response = await self._http.post("/", json={"action": action, "params": params})
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("retcode") not in (0, None):
            raise RuntimeError(f"onebot action failed: {payload}")
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else {}

    async def send_private_msg(self, user_id: int | str, text: str) -> None:
        await self.call_action("send_private_msg", user_id=int(user_id), message=text)

    async def send_group_msg(self, group_id: int | str, text: str) -> None:
        await self.call_action("send_group_msg", group_id=int(group_id), message=text)

    async def get_login_user_id(self) -> int:
        data = await self.call_action("get_login_info")
        return int(data.get("user_id") or 0)

    async def download_bytes(self, url: str) -> bytes | None:
        try:
            response = await self._http.get(str(url))
            response.raise_for_status()
            return response.content
        except httpx.HTTPError:
            return None

    async def receive_events(
        self,
        *,
        on_event: EventCallback,
        backoff_seconds: float = 3.0,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Consume the forward WebSocket forever, reconnecting after drops."""
        while stop is None or not stop.is_set():
            try:
                async with websockets.connect(self._ws_url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    async for raw in ws:
                        try:
                            event = json.loads(str(raw))
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            await on_event(event)
            except (OSError, websockets.WebSocketException):
                pass
            finally:
                self._ws = None
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(backoff_seconds)
