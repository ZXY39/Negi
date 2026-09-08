"""Minimal async HTTP + SSE client for the local External Agent API.

httpx is already a core dependency, so this client has no extra requirements
and injects an optional transport for tests. It mirrors the qq-onebot bridge's
g3ku_client.py but lives in-process.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx


class ExternalApiClient:
    def __init__(self, base_url: str, token: str, *, transport: Any = None):
        self._token = str(token or "")
        self._client = httpx.AsyncClient(
            base_url=str(base_url or "").rstrip("/"),
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=5.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._token}"}
        if extra:
            headers.update(extra)
        return headers

    async def ensure_session(self, external_key: str, *, title: str | None = None) -> str:
        response = await self._client.post(
            "/sessions",
            json={"external_key": external_key, "title": title},
            headers=self._headers(),
        )
        response.raise_for_status()
        return str(response.json().get("session_id") or "")

    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        idempotency_key: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if attachments:
            payload["attachments"] = list(attachments)
        response = await self._client.post(
            f"/sessions/{session_id}/messages",
            json=payload,
            headers=self._headers({"Idempotency-Key": idempotency_key}),
        )
        response.raise_for_status()
        return response.json()

    async def stream_events(self, session_id: str, *, last_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
        headers = self._headers({"Accept": "text/event-stream"})
        if last_seq > 0:
            headers["Last-Event-ID"] = str(last_seq)
        async with self._client.stream("GET", f"/sessions/{session_id}/events", headers=headers) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue