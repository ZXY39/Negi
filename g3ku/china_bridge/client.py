from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from g3ku.china_bridge.protocol import build_auth_frame, dumps


FrameHandler = Callable[[dict[str, Any]], Awaitable[None]]
StateHandler = Callable[[bool, str], Awaitable[None] | None]


class ChinaBridgeClient:
    def __init__(
        self,
        *,
        url: str,
        token: str,
        on_frame: FrameHandler,
        on_state: StateHandler | None = None,
    ):
        self._url = str(url)
        self._token = str(token or "")
        self._on_frame = on_frame
        self._on_state = on_state
        self._ws = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                async with websockets.connect(self._url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    await ws.send(dumps(build_auth_frame(self._token)))
                    raw = await ws.recv()
                    payload = json.loads(raw)
                    if str(payload.get("type") or "") != "auth_ok":
                        raise RuntimeError(f"china bridge auth failed: {payload}")
                    self._connected.set()
                    await self._notify_state(True, "connected")
                    while not self._stop.is_set():
                        raw = await ws.recv()
                        data = json.loads(raw)
                        if isinstance(data, dict):
                            await self._on_frame(data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._notify_state(False, str(exc))
                await asyncio.sleep(1.0)
            finally:
                self._connected.clear()
                self._ws = None

    async def send_frame(self, payload: dict[str, Any]) -> None:
        if not self._ws or not self._connected.is_set():
            raise RuntimeError("china bridge not connected")
        await self._ws.send(dumps(payload))

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _notify_state(self, connected: bool, reason: str) -> None:
        if self._on_state is None:
            return
        result = self._on_state(connected, reason)
        if inspect.isawaitable(result):
            await result
