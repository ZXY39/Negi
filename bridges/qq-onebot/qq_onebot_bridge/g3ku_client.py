"""Async client for the G3KU External Agent API (/api/v1).

HTTP for commands, SSE (httpx streaming) for turn/outbound events. One
persistent event stream per session; reconnection resumes via Last-Event-ID.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import httpx

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class G3kuClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0):
        self._base_url = str(base_url or "").rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        self._session_ids: dict[str, str] = {}
        self._last_seq: dict[str, int] = {}

    async def close(self) -> None:
        await self._http.aclose()

    # -- sessions -----------------------------------------------------------

    async def ensure_session(self, external_key: str, *, title: str | None = None) -> str:
        cached = self._session_ids.get(external_key)
        if cached:
            return cached
        response = await self._http.post(
            "/api/v1/sessions", json={"external_key": external_key, "title": title}
        )
        response.raise_for_status()
        session_id = str(response.json()["session_id"])
        self._session_ids[external_key] = session_id
        return session_id

    def external_key_for(self, session_id: str) -> str | None:
        for external_key, known in self._session_ids.items():
            if known == session_id:
                return external_key
        return None

    # -- turns --------------------------------------------------------------

    async def send_message(
        self,
        session_id: str,
        *,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        sender: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text}
        if attachments:
            payload["attachments"] = attachments
        if sender:
            payload["sender"] = sender
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = await self._http.post(
            f"/api/v1/sessions/{session_id}/messages", json=payload, headers=headers
        )
        response.raise_for_status()
        body = response.json()
        turn_id = body.get("turn_id")
        if turn_id and body.get("status") == "started":
            self._note_turn(session_id, str(turn_id))
        return body

    async def pause_turn(self, turn_id: str) -> bool | None:
        """True = paused, False = no active turn, None = turn unknown."""
        response = await self._http.post(f"/api/v1/turns/{turn_id}/pause")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return bool(response.json().get("paused"))

    async def cancel_session(self, session_id: str) -> int:
        response = await self._http.post(f"/api/v1/sessions/{session_id}/cancel")
        response.raise_for_status()
        return int(response.json().get("cancelled") or 0)

    # -- events -------------------------------------------------------------

    def _note_turn(self, session_id: str, turn_id: str) -> None:
        self._active_turns = getattr(self, "_active_turns", {})
        self._active_turns[session_id] = turn_id

    def active_turn_id(self, session_id: str) -> str | None:
        return dict(getattr(self, "_active_turns", {})).get(session_id)

    def forget_turn(self, session_id: str) -> None:
        getattr(self, "_active_turns", {}).pop(session_id, None)

    def last_seq(self, session_id: str) -> int:
        return self._last_seq.get(session_id, 0)

    async def stream_events(
        self,
        session_id: str,
        *,
        on_event: EventCallback,
        backoff_seconds: float = 3.0,
        stop: asyncio.Event | None = None,
    ) -> None:
        """Consume one session's SSE stream forever, resuming after drops."""
        while stop is None or not stop.is_set():
            headers: dict[str, str] = {}
            last_seq = self._last_seq.get(session_id, 0)
            if last_seq:
                headers["Last-Event-ID"] = str(last_seq)
            try:
                async with self._http.stream(
                    "GET",
                    f"/api/v1/sessions/{session_id}/events",
                    headers=headers,
                    timeout=None,
                ) as response:
                    response.raise_for_status()
                    data_buffer: list[str] = []
                    async for line in response.aiter_lines():
                        if line.startswith("id:"):
                            try:
                                self._last_seq[session_id] = int(line[3:].strip())
                            except ValueError:
                                pass
                        elif line.startswith("data:"):
                            data_buffer.append(line[5:].strip())
                        elif line == "" and data_buffer:
                            raw = "\n".join(data_buffer)
                            data_buffer = []
                            try:
                                event = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(event, dict):
                                await on_event(event)
            except (httpx.HTTPError, asyncio.TimeoutError):
                pass
            if stop is not None and stop.is_set():
                return
            await asyncio.sleep(backoff_seconds)
