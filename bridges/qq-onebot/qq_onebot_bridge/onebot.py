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
from typing import Any, Awaitable, Callable, Iterable

import httpx
import websockets

logger = logging.getLogger("qq_onebot_bridge.onebot")

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _address_is_never_a_media_source(address: ipaddress._BaseAddress) -> bool:
    """永远不可能是媒体源的地址族。

    回环先排除：它和私网同档，归"点名的 socket 才放行"那一档管（donor 常被写成
    `localhost`/`[::1]`，把它当"永不是 donor"直接拒会让这类部署收不到附件）。
    Python 的 `is_reserved` 把 `::1` 也算进保留段，所以这里必须先看回环。
    """
    mapped = getattr(address, "ipv4_mapped", None) or address
    if mapped.is_loopback:
        return False
    return bool(
        mapped.is_unspecified
        or mapped.is_link_local
        or mapped.is_reserved
        or mapped.is_multicast
    )


def _address_is_local(address: ipaddress._BaseAddress) -> bool:
    mapped = getattr(address, "ipv4_mapped", None) or address
    return bool(mapped.is_private or mapped.is_loopback)


def _url_authority(value: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of a URL or a bare `host[:port]` entry; None when unparseable."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    has_scheme = "://" in text
    try:
        parsed = httpx.URL(text if has_scheme else f"//{text}")
    except Exception:
        # `http://fe80::1/a.png` 这种不加分隔符的 IPv6 写法会被解析成"带端口"，
        # httpx 直接抛 InvalidURL。判否即可，不必替发消息的人把 URL 修好。
        return None
    host = str(parsed.host or "").strip()
    if not host:
        return None
    scheme = str(parsed.scheme or "").lower()
    default_port = 443 if scheme == "https" else 80
    try:
        port = int(parsed.port or default_port)
    except ValueError:
        return None
    return ("" if not has_scheme else scheme), host, port


def _resolved_endpoints(host: str, port: int) -> set[tuple[str, int]]:
    """解析后的 socket 端点。主机名不进集合：`localhost:3000` 与 `127.0.0.1:3000`
    打的是同一个 donor socket，按名字比会让 donor 换了写法就误杀。"""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        infos = []
    endpoints = {(str(info[4][0]).split("%", 1)[0], int(port)) for info in infos if info[4]}
    return endpoints or {(host, int(port))}


class MediaSourcePolicy:
    """入站 CQ 里的 URL 由消息发送者写，抓之前先看一眼目标。

    放行三档，其余全拒：
    1. 公网地址——QQ 自己的 CDN 与任意外部图床，这是正常形状。
    2. **自家 donor**：`onebot.http_url` 那一台。NapCat 一类实现本身就是本机服务，群图片/
       文件的下载链接常由它发出；这是操作员已经表达过的意图，不该再逼他抄一遍白名单。
    3. `behavior.allowed_media_hosts` 点名的 `host[:port]`。

    拒掉的形状里最要紧的是"没被点名的私网/回环目标"——同一台机器上 `127.0.0.1:18790`
    的管理面对回环没有鉴权（`/api/tasks`、`/api/llm/configs/<id>` 无 token 直接 200），
    放任意私网过去等于把这份读口借给群里任何会发消息的人。链路本地（含云元数据
    `169.254.169.254`）、保留段、组播、未指定地址连 donor 都不可能是，任何配置下都拒。

    比对按解析出来的 socket（IP:port），不按主机名：donor 写作 `127.0.0.1:3000` 时
    `localhost:3000` 会多出一枚 `::1:3000` 端点而不算命中 donor——那种部署要在
    `allowed_media_hosts` 里点名，或把 donor 与媒体链接写成同一个族。

    判完再交给 httpx 连接，它会再解析一次（DNS TOCTOU 窗口留着；要钉死得换成"连已验证
    的 IP"的 transport，那是另一档改动）。
    """

    def __init__(self, *, donor_http_url: str = "", allowed_hosts: Iterable[str] = ()):
        self._allowed: set[tuple[str, int]] = set()
        for entry in [donor_http_url, *list(allowed_hosts or [])]:
            authority = _url_authority(entry)
            if authority is None:
                continue
            self._allowed |= _resolved_endpoints(authority[1], authority[2])

    def allows(self, url: str) -> tuple[bool, str]:
        authority = _url_authority(url)
        if authority is None:
            return False, "not a parseable URL"
        scheme, host, port = authority
        if scheme not in {"http", "https"}:
            return False, f"scheme {scheme or 'none'} is not fetchable"
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except OSError as exc:
            return False, f"host does not resolve ({exc.strerror or exc})"
        addresses: list[ipaddress._BaseAddress] = []
        for info in infos:
            raw = str(info[4][0] or "").split("%", 1)[0]
            try:
                addresses.append(ipaddress.ip_address(raw))
            except ValueError:
                return False, f"host returned a non-IP address {raw!r}"
        if not addresses:
            return False, "host does not resolve"
        for address in addresses:
            if _address_is_never_a_media_source(address):
                return False, f"address family {address} can never serve media"
        if not any(_address_is_local(address) for address in addresses):
            return True, ""
        endpoints = {(str(address), port) for address in addresses}
        if endpoints and endpoints <= self._allowed:
            return True, ""
        return False, (
            f"private/loopback target {host}:{port} is not the configured donor; "
            "add it to behavior.allowed_media_hosts if it serves media"
        )


def _address_is_local(address: ipaddress._BaseAddress) -> bool:
    mapped = getattr(address, "ipv4_mapped", None) or address
    return bool(mapped.is_private or mapped.is_loopback)


class OnebotClient:
    def __init__(self, ws_url: str, http_url: str, *, media_policy: MediaSourcePolicy | None = None):
        self._ws_url = str(ws_url)
        self._http_url = str(http_url or "").rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._http_url, timeout=30.0)
        self._media_policy = media_policy or MediaSourcePolicy(donor_http_url=self._http_url)
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
        allowed, reason = await asyncio.to_thread(self._media_policy.allows, str(url))
        if not allowed:
            authority = _url_authority(str(url))
            logger.warning(
                "inbound media URL rejected: target=%s reason=%s",
                f"{authority[1]}:{authority[2]}" if authority else str(url)[:80],
                reason,
            )
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
