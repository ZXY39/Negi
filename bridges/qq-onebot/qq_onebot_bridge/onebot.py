"""OneBot 11 client: forward-WebSocket event receive + HTTP action calls.

Targets NapCat / LLOneBot / Lagrange style endpoints: events arrive on the
forward WebSocket, actions go through the HTTP API (``send_private_msg``,
``send_group_msg``, ``get_login_info``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
from typing import Any, Awaitable, Callable

import httpx
import websockets

logger = logging.getLogger("qq_onebot_bridge.onebot")

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _address_is_never_a_media_source(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """地址族判否：这些段永远不可能是 QQ 媒体 donor。

    回环只判 IPv6 的 ::1——示例配置的 donor 就是 IPv4 回环，拦它等于让默认部署收不到
    附件；IPv4 回环因此留作可接受目标（与私网同档，见 `url_is_safe_media_source`）。
    """
    mapped = getattr(address, "ipv4_mapped", None) or address
    if mapped.is_unspecified:
        return True
    if mapped.is_link_local:
        return True
    if mapped.is_reserved:
        return True
    if mapped.is_multicast:
        return True
    if address.version == 6 and address.is_loopback:
        return True
    return False


def _resolve_violations(host: str, port: int) -> list[str]:
    """Names the addresses that must not be fetched from an inbound URL."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return [f"unresolvable: {exc.strerror or exc}"]
    bad: list[str] = []
    for info in infos:
        raw = str(info[4][0] or "")
        try:
            address = ipaddress.ip_address(raw.split("%", 1)[0])
        except ValueError:
            bad.append(raw)
            continue
        if _address_is_never_a_media_source(address):
            bad.append(raw)
    return bad


def url_is_safe_media_source(url: str) -> bool:
    """入站 CQ 里的 URL 由消息发送者控制，抓取前先看一眼目标。

    挡住的是"永远不可能是媒体源"的地址族：链路本地（含云元数据 169.254.169.254）、
    保留段、组播、未指定地址，以及非 http(s)。回环与私网**不在这里拦**：OneBot 实现
    自己就是本地服务（示例配置的 `http://127.0.0.1:3000`），群图片/文件的下载链接
    常常由同一个本地 donor 发出，按私网一刀切会让参照桥的默认部署收不到任何附件。
    抓取前解析一次并判地址，返回给调用方的是判否；真正连线上仍由 httpx 再解析一次
    （TOCTOU 窗口存在，收紧到"连到已验证的 IP"需要换 transport，是另一档改动）。
    """
    try:
        parsed = httpx.URL(str(url))
    except Exception:
        return False
    scheme = str(parsed.scheme or "").lower()
    host = str(parsed.host or "").strip()
    if scheme not in {"http", "https"} or not host:
        return False
    port = int(parsed.port or (443 if scheme == "https" else 80))
    violations = _resolve_violations(host, port)
    if violations:
        logger.warning("inbound media URL rejected: host=%s %s", host, ", ".join(violations[:3]))
        return False
    return True


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

    async def download_bytes(self, url: str, *, max_bytes: int) -> bytes | None:
        """Stream-download ``url``; ``None`` on transport errors or when the
        body exceeds ``max_bytes`` (mirrors the /api/v1 per-attachment caps so
        oversized payloads are never submitted for a guaranteed 413)."""
        if not await asyncio.to_thread(url_is_safe_media_source, str(url)):
            return None
        try:
            async with self._http.stream("GET", str(url)) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
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
                            try:
                                await on_event(event)
                            except Exception:
                                # 一条入站消息的处理失败只丢那一条。这里的异常会穿过
                                # receive_events、穿过 gather、把整个桥进程带走——而桥
                                # 没有监督者重拉，QQ 侧从此再没有任何回复。
                                logger.exception("bridge event handler failed for event %s", str(event)[:200])
            except (OSError, websockets.WebSocketException):
                pass
            finally:
                self._ws = None
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(backoff_seconds)
