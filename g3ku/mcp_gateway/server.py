"""FastMCP tool surface for the g3ku MCP gateway (stdio proxy).

Out-of-the-box surface #2 for external AI agents (contract owner:
``docs/architecture/agent-gateway.md``). The tools never raise: transport /
HTTP failures are converted into ``{"ok": False, "error": ...}`` payloads so
the calling agent always gets a readable result and can tell its user what
happened (401 invalid token, 423 project locked, 503 runtime unavailable,
connection failed, ...).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import httpx
from mcp.server.fastmcp import FastMCP

from g3ku.mcp_gateway.client import G3kuMcpClient

MIN_WAIT_SECONDS = 5
MAX_WAIT_SECONDS = 1800


async def _safe(factory: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Run a tool body; convert transport failures into readable payloads."""
    try:
        return await factory()
    except httpx.HTTPStatusError as exc:
        status_code = int(exc.response.status_code or 0)
        detail = ""
        try:
            body = exc.response.json()
            if isinstance(body, dict):
                detail = str(body.get("detail") or body.get("message") or "")
        except Exception:
            detail = ""
        return {
            "ok": False,
            "error": detail or f"http_{status_code}",
            "status_code": status_code,
        }
    except httpx.TransportError:
        return {"ok": False, "error": "connection_failed"}


def _clamp_wait(wait_seconds: int, default: int) -> int:
    try:
        value = int(wait_seconds)
    except (TypeError, ValueError):
        value = default
    return max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, value))


def build_mcp_server(client: G3kuMcpClient, *, name: str = "g3ku") -> FastMCP:
    mcp = FastMCP(
        name,
        instructions=(
            "Converse with a running g3ku agent over the External Agent API. "
            "Each conversation is a persistent g3ku session; g3ku keeps its own "
            "memory, so send only the new message (no client-side history)."
        ),
    )

    @mcp.tool(
        description=(
            "Send a message to g3ku and wait for the final reply. Returns the reply text, "
            "or status='pending' with turn_id when the agent is still working (increase "
            "wait_seconds or call g3ku_get_reply later)."
        )
    )
    async def g3ku_chat(conversation: str, message: str, wait_seconds: int = 120) -> dict[str, Any]:
        timeout = _clamp_wait(wait_seconds, 120)

        async def _body() -> dict[str, Any]:
            session_id = await client.ensure_session(conversation)
            after_seq = client.last_seq(session_id)
            async with client.event_stream(session_id, last_seq=after_seq) as events:
                result = await client.send_message(session_id, str(message or ""))
                submit_status = str(result.get("status") or "")
                turn_id = str(result.get("turn_id") or "") or None
                if submit_status == "queued":
                    outcome = await client.wait_for_reply(
                        events, queued=True, after_seq=after_seq, timeout=timeout
                    )
                elif submit_status == "duplicate":
                    outcome = await client.wait_for_reply(
                        events,
                        turn_id=turn_id,
                        queued=str(result.get("original_status") or "") == "queued",
                        after_seq=0,
                        timeout=timeout,
                    )
                else:
                    outcome = await client.wait_for_reply(
                        events, turn_id=turn_id, after_seq=after_seq, timeout=timeout
                    )
            base = {
                "ok": True,
                "conversation": conversation,
                "session_id": session_id,
                "turn_id": outcome.get("turn_id") or turn_id,
                "submit_status": submit_status,
            }
            kind = str(outcome.get("kind") or "")
            if kind == "reply":
                return {
                    **base,
                    "status": "completed",
                    "reply": str(outcome.get("text") or ""),
                    "usage": outcome.get("usage"),
                }
            if kind == "failed":
                return {**base, "status": "failed", "error": str(outcome.get("error") or "")}
            if kind in {"cancelled", "no_reply"}:
                return {**base, "status": kind}
            if submit_status == "queued":
                return {
                    **base,
                    "status": "queued_receipt",
                    "receipt": str(result.get("receipt") or ""),
                    "hint": "Message joined the running turn. Call g3ku_get_reply later to fetch the answer.",
                }
            return {
                **base,
                "status": "pending",
                "hint": "Still working. Call g3ku_get_reply later or increase wait_seconds.",
            }

        return await _safe(_body)

    @mcp.tool(
        description=(
            "Fetch the next final reply for a conversation that was not consumed yet "
            "(e.g. after g3ku_chat returned status='pending')."
        )
    )
    async def g3ku_get_reply(conversation: str, wait_seconds: int = 60) -> dict[str, Any]:
        timeout = _clamp_wait(wait_seconds, 60)

        async def _body() -> dict[str, Any]:
            session_id = await client.ensure_session(conversation)
            after_seq = client.last_seq(session_id)
            async with client.event_stream(session_id, last_seq=after_seq) as events:
                outcome = await client.wait_for_reply(
                    events, queued=True, after_seq=after_seq, timeout=timeout
                )
            if str(outcome.get("kind") or "") == "reply":
                return {
                    "ok": True,
                    "conversation": conversation,
                    "found": True,
                    "reply": str(outcome.get("text") or ""),
                    "turn_id": outcome.get("turn_id"),
                    "usage": outcome.get("usage"),
                }
            return {
                "ok": True,
                "conversation": conversation,
                "found": False,
                "status": str(outcome.get("kind") or "timeout"),
            }

        return await _safe(_body)

    @mcp.tool(description="Report whether the g3ku conversation is running and what is queued.")
    async def g3ku_session_status(conversation: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            session_id = await client.ensure_session(conversation)
            state = await client.session_state(session_id)
            return {
                "ok": True,
                "conversation": conversation,
                "session_id": session_id,
                "running": bool(state.get("running")),
                "queued_follow_ups": int(state.get("queued_follow_ups") or 0),
                "inflight_turn_id": state.get("inflight_turn_id"),
                "last_error": state.get("last_error"),
                "last_seq": client.last_seq(session_id),
            }

        return await _safe(_body)

    @mcp.tool(description="Pause the conversation's currently running g3ku turn, if any.")
    async def g3ku_pause(conversation: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            session_id = await client.ensure_session(conversation)
            state = await client.session_state(session_id)
            turn_id = str(state.get("inflight_turn_id") or "").strip()
            if not turn_id:
                return {"ok": False, "error": "no_inflight_turn", "conversation": conversation}
            result = await client.pause_turn(turn_id)
            return {
                "ok": True,
                "conversation": conversation,
                "paused": bool(result.get("paused")),
                "turn_id": turn_id,
            }

        return await _safe(_body)

    @mcp.tool(description="Cancel the conversation's running g3ku tasks.")
    async def g3ku_cancel(conversation: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            session_id = await client.ensure_session(conversation)
            result = await client.cancel_session(session_id)
            return {
                "ok": True,
                "conversation": conversation,
                "cancelled": int(result.get("cancelled") or 0),
                "session_id": session_id,
            }

        return await _safe(_body)

    @mcp.tool(description="List this gateway's g3ku conversations (sessions).")
    async def g3ku_list_conversations() -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            payload = await client.list_sessions()
            items = [
                {
                    "conversation": client.conversation_of(str(item.get("external_key") or "")),
                    "session_id": str(item.get("session_id") or ""),
                    "external_key": str(item.get("external_key") or ""),
                    "title": str(item.get("title") or ""),
                    "created_at": str(item.get("created_at") or ""),
                }
                for item in list(payload.get("items") or [])
                if isinstance(item, dict)
            ]
            return {"ok": True, "items": items}

        return await _safe(_body)

    return mcp
