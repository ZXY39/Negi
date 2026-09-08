"""HTTP+SSE client for the g3ku External Agent API (MCP gateway side).

独立实现：不 import ``g3ku.qq_official`` 或 ``bridges/``——网关是独立 stdio
进程，与其他桥零耦合，只复用 ``/api/v1`` 契约（docs/architecture/
external-agent-api.md）。形态参考 bridges/qq-onebot 的 ``g3ku_client.py``
（integration-manual 的 copy-as-is 模板），另加有界等待 ``wait_for_reply``。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:18790/api/v1"


class G3kuMcpClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        token: str = "",
        *,
        conversation_prefix: str = "mcp",
        transport: Any = None,
        timeout: float = 30.0,
    ):
        self.base_url = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self.token = str(token or "")
        self.conversation_prefix = str(conversation_prefix or "mcp").strip() or "mcp"
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=5.0),
        )
        self._session_ids: dict[str, str] = {}
        self._last_seq: dict[str, int] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra:
            headers.update(extra)
        return headers

    def external_key(self, conversation: str) -> str:
        return f"{self.conversation_prefix}:{str(conversation or 'default').strip() or 'default'}"

    def conversation_of(self, external_key: str) -> str:
        raw = str(external_key or "").strip()
        prefix = f"{self.conversation_prefix}:"
        return raw[len(prefix):] if raw.startswith(prefix) else raw

    def last_seq(self, session_id: str) -> int:
        return int(self._last_seq.get(str(session_id or "")) or 0)

    async def ensure_session(self, conversation: str, *, title: str | None = None) -> str:
        cached = self._session_ids.get(str(conversation or ""))
        if cached:
            return cached
        response = await self._client.post(
            "/sessions",
            json={"external_key": self.external_key(conversation), "title": title},
            headers=self._headers(),
        )
        response.raise_for_status()
        session_id = str(response.json().get("session_id") or "")
        self._session_ids[str(conversation or "")] = session_id
        return session_id

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = self._headers()
        key = str(idempotency_key or "").strip()
        if key:
            headers["Idempotency-Key"] = key
        response = await self._client.post(
            f"/sessions/{session_id}/messages",
            json={"text": text},
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    async def session_state(self, session_id: str) -> dict[str, Any]:
        response = await self._client.get(f"/sessions/{session_id}/state", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def pause_turn(self, turn_id: str) -> dict[str, Any]:
        response = await self._client.post(f"/turns/{turn_id}/pause", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def cancel_session(self, session_id: str) -> dict[str, Any]:
        response = await self._client.post(f"/sessions/{session_id}/cancel", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def list_sessions(self) -> dict[str, Any]:
        response = await self._client.get("/sessions", headers=self._headers())
        response.raise_for_status()
        return response.json()

    @asynccontextmanager
    async def event_stream(self, session_id: str, *, last_seq: int = 0) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        """Open the session SSE stream; yields an async iterator of parsed
        events and tracks the per-session replay cursor. SSE frames are
        ``id: {seq}\\nevent: {type}\\ndata: {json}``; the 15s heartbeat comment
        keeps the httpx read timeout (30s) from firing."""
        headers = self._headers({"Accept": "text/event-stream"})
        cursor = int(last_seq or 0) or self.last_seq(session_id)
        if cursor > 0:
            headers["Last-Event-ID"] = str(cursor)
        async with self._client.stream(
            "GET", f"/sessions/{session_id}/events", headers=headers
        ) as response:
            response.raise_for_status()

            async def _events() -> AsyncIterator[dict[str, Any]]:
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    seq = int(event.get("seq") or 0)
                    if seq:
                        self._last_seq[session_id] = max(self.last_seq(session_id), seq)
                    yield event

            yield _events()

    async def wait_for_reply(
        self,
        events: AsyncIterator[dict[str, Any]],
        *,
        turn_id: str | None = None,
        queued: bool = False,
        after_seq: int = 0,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Client-side mirror of ``wait_for_external_reply`` (same rules):

        - started: first ``reply.final`` matching turn_id with seq > after_seq;
        - queued: collect finals until the first terminal, return max-seq final
          (the running turn's own final for the PREDECESSOR message arrives
          before the drain batch that answers the queued message);
        - ``turn.failed`` → failed; deadline / closed stream → timeout.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        threshold = int(after_seq or 0)
        seen: set[int] = set()
        finals: list[dict[str, Any]] = []
        iterator = events.__aiter__()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"kind": "timeout"}
            try:
                event = await asyncio.wait_for(iterator.__anext__(), min(remaining, 20.0))
            except asyncio.TimeoutError:
                continue
            except StopAsyncIteration:
                return {"kind": "timeout", "error": "stream_closed"}
            seq = int(event.get("seq") or 0)
            if seq <= threshold or seq in seen:
                continue
            seen.add(seq)
            event_type = str(event.get("type") or "")
            event_turn_id = str(event.get("turn_id") or "")
            if event_type == "reply.final":
                if queued:
                    finals.append(dict(event))
                    continue
                if turn_id is None or event_turn_id == str(turn_id):
                    return {
                        "kind": "reply",
                        "text": str(event.get("text") or ""),
                        "turn_id": event_turn_id or None,
                        "usage": event.get("usage") if isinstance(event.get("usage"), dict) else None,
                    }
                continue
            if event_type == "turn.completed":
                if queued:
                    if finals:
                        best = max(finals, key=lambda item: int(item.get("seq") or 0))
                        return {
                            "kind": "reply",
                            "text": str(best.get("text") or ""),
                            "turn_id": str(best.get("turn_id") or "") or None,
                            "usage": best.get("usage") if isinstance(best.get("usage"), dict) else None,
                        }
                    return {"kind": "cancelled" if event.get("cancelled") else "no_reply"}
                if turn_id is not None and event_turn_id != str(turn_id):
                    continue
                return {"kind": "cancelled" if event.get("cancelled") else "no_reply"}
            if event_type == "turn.failed":
                if turn_id is not None and event_turn_id != str(turn_id):
                    continue
                return {"kind": "failed", "error": str(event.get("error") or "unknown error")}
