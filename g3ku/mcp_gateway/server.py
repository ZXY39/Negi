"""FastMCP tool surface for the Negi MCP gateway (stdio proxy).

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
DEFAULT_LIST_LIMIT = 50


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
    return max(MIN_WAIT_SECONDS, min(MAX_WAIT_SECONDS, _raw_wait(wait_seconds, default)))


def _raw_wait(wait_seconds: int, default: int) -> int:
    try:
        return int(wait_seconds)
    except (TypeError, ValueError):
        return int(default)


def build_mcp_server(client: G3kuMcpClient, *, name: str = "negi") -> FastMCP:
    mcp = FastMCP(
        name,
        instructions=(
            "Converse with a running Negi agent over the External Agent API. "
            "Each conversation is a persistent Negi session; Negi keeps its own "
            "memory, so send only the new message (no client-side history)."
        ),
    )

    @mcp.tool(
        description=(
            "Send a message to Negi. `conversation` is either a short name in this gateway's own "
            "namespace or a full session key taken from negi_list_conversations (keys containing "
            "':' address that session directly, without creating one). wait_seconds>0 blocks until "
            "the turn answers and returns status='completed' with reply/usage/task_ids; "
            "wait_seconds=0 submits and returns immediately with status='submitted' plus the "
            "turn_id and any task_ids already bound to the turn — call negi_get_reply later. "
            "task_ids are the machine-readable handles of the tasks this turn dispatched "
            "(use them with the web runtime to follow up); reply is prose for a human."
        )
    )
    async def negi_chat(conversation: str, message: str, wait_seconds: int = 120) -> dict[str, Any]:
        submit_only = _raw_wait(wait_seconds, 120) == 0
        timeout = _clamp_wait(wait_seconds, 120)

        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
            after_seq = client.last_seq(session_id)
            if submit_only:
                result = await client.send_message(session_id, str(message or ""))
                return {
                    "ok": True,
                    "conversation": conversation,
                    "session_id": session_id,
                    "turn_id": str(result.get("turn_id") or "") or None,
                    "submit_status": str(result.get("status") or ""),
                    "status": "submitted",
                    "task_ids": [],
                    "receipt": str(result.get("receipt") or ""),
                    "hint": "Submitted without waiting. Call negi_get_reply to fetch the final reply.",
                }
            async with client.event_stream(session_id, last_seq=after_seq) as events:
                result = await client.send_message(session_id, str(message or ""))
                submit_status = str(result.get("status") or "")
                turn_id = str(result.get("turn_id") or "") or None
                if submit_status == "queued":
                    outcome = await client.wait_for_reply(
                        events,
                        queued=True,
                        after_seq=after_seq,
                        timeout=timeout,
                        session_id=session_id,
                    )
                elif submit_status == "duplicate":
                    outcome = await client.wait_for_reply(
                        events,
                        turn_id=turn_id,
                        queued=str(result.get("original_status") or "") == "queued",
                        after_seq=0,
                        timeout=timeout,
                        session_id=session_id,
                    )
                else:
                    outcome = await client.wait_for_reply(
                        events,
                        turn_id=turn_id,
                        after_seq=after_seq,
                        timeout=timeout,
                        session_id=session_id,
                    )
            base = {
                "ok": True,
                "conversation": conversation,
                "session_id": session_id,
                "turn_id": outcome.get("turn_id") or turn_id,
                "submit_status": submit_status,
                "task_ids": list(outcome.get("task_ids") or []),
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
                    "hint": "Message joined the running turn. Call negi_get_reply later to fetch the answer.",
                }
            return {
                **base,
                "status": "pending",
                "hint": "Still working. Call negi_get_reply later, or pass wait_seconds=0 to stop blocking.",
            }

        return await _safe(_body)

    @mcp.tool(
        description=(
            "Fetch the next final reply for a conversation that was not consumed yet "
            "(e.g. after negi_chat returned status='pending' or status='submitted'). "
            "Carries the same task_ids handle as negi_chat."
        )
    )
    async def negi_get_reply(conversation: str, wait_seconds: int = 60) -> dict[str, Any]:
        timeout = _clamp_wait(wait_seconds, 60)

        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
            after_seq = client.last_seq(session_id)
            async with client.event_stream(session_id, last_seq=after_seq) as events:
                outcome = await client.wait_for_reply(
                    events, queued=True, after_seq=after_seq, timeout=timeout, session_id=session_id
                )
            if str(outcome.get("kind") or "") == "reply":
                return {
                    "ok": True,
                    "conversation": conversation,
                    "found": True,
                    "reply": str(outcome.get("text") or ""),
                    "turn_id": outcome.get("turn_id"),
                    "usage": outcome.get("usage"),
                    "task_ids": list(outcome.get("task_ids") or []),
                }
            return {
                "ok": True,
                "conversation": conversation,
                "found": False,
                "status": str(outcome.get("kind") or "timeout"),
            }

        return await _safe(_body)

    @mcp.tool(description="Report whether the Negi conversation is running and what is queued.")
    async def negi_session_status(conversation: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
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

    @mcp.tool(
        description=(
            "Pause the conversation's currently running Negi turn, if any. This stops the agent "
            "at its next stage boundary; it does not touch tasks the turn already dispatched."
        )
    )
    async def negi_pause(conversation: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
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

    @mcp.tool(
        description=(
            "Cancel the conversation's in-flight work: the running turn and the tasks dispatched "
            "from this session. Returns the cancelled task count. Paused-but-never-resumed tasks "
            "belong to the web runtime's task commands, not this call."
        )
    )
    async def negi_cancel(conversation: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
            result = await client.cancel_session(session_id)
            return {
                "ok": True,
                "conversation": conversation,
                "cancelled": int(result.get("cancelled") or 0),
                "session_id": session_id,
            }

        return await _safe(_body)

    @mcp.tool(
        description=(
            "List Negi conversations. scope='all' covers every session the runtime knows — web CEO "
            "chats, channel sessions (QQ and others) and other bridges' sessions — and is the only "
            "scope that carries preview_text, message_count, updated_at, is_running, status and the "
            "transcript file path; requires the token's cross-session scope, otherwise this tool "
            "degrades to scope='own' and says so in both `scope` and `requested_scope`. scope='own' "
            "lists only this gateway's own namespace, and those rows carry preview and activity too. "
            "Pass any returned session_id back as `conversation` to talk to that session. Filter with "
            "`query` (substring of conversation/title/preview/session_id) and optionally `bridge_id`; "
            "pagination is `offset` + returned `next_offset`. Replies to channel sessions go out as "
            "proactive messages, so the platform's message window and quota can refuse the reply even "
            "though the turn answered."
        )
    )
    async def negi_list_conversations(
        scope: str = "all",
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
        query: str = "",
        bridge_id: str = "",
    ) -> dict[str, Any]:
        def _normalize(raw: Any) -> dict[str, Any] | None:
            if not isinstance(raw, dict):
                return None
            session_id = str(raw.get("session_id") or "").strip()
            if not session_id:
                return None
            conversation = client.conversation_of(str(raw.get("external_key") or "")) or session_id
            item = {
                "conversation": conversation,
                "session_id": session_id,
                "title": str(raw.get("title") or ""),
                "preview_text": str(raw.get("preview_text") or ""),
                "message_count": int(raw.get("message_count") or 0),
                "updated_at": str(raw.get("updated_at") or ""),
                "created_at": str(raw.get("created_at") or ""),
                "is_running": bool(raw.get("is_running")),
                "status": str(raw.get("status") or ""),
                "session_family": str(raw.get("session_family") or ""),
                "session_origin": str(raw.get("session_origin") or ""),
                "can_message": bool(raw.get("can_message", True)),
                "transcript_path": str(raw.get("transcript_path") or ""),
            }
            # 上游没报这个读数时整键省略：把它折成 False 等于替调用方下一句假话
            # （"这个会话没有转录"），而真实情况只是"服务端没说"。
            if "has_transcript" in raw:
                item["has_transcript"] = bool(raw.get("has_transcript"))
            return item

        def _matches(item: dict[str, Any], *, needle: str, bridge: str) -> bool:
            if bridge:
                account = str(item.get("session_id") or "").split(":")
                if len(account) < 3 or account[1] != bridge:
                    return False
            if not needle:
                return True
            haystack = " ".join(
                [
                    str(item.get("conversation") or ""),
                    str(item.get("session_id") or ""),
                    str(item.get("title") or ""),
                    str(item.get("preview_text") or ""),
                ]
            ).lower()
            return needle in haystack

        async def _body() -> dict[str, Any]:
            wanted = str(scope or "").strip().lower() or "all"
            try:
                cap = max(1, int(limit or 0))
            except (TypeError, ValueError):
                cap = DEFAULT_LIST_LIMIT
            try:
                start = max(0, int(offset or 0))
            except (TypeError, ValueError):
                start = 0
            needle = str(query or "").strip().lower()
            bridge = str(bridge_id or "").strip()
            effective = wanted
            payload: dict[str, Any]
            if wanted == "all":
                try:
                    payload = await client.list_sessions(scope="all")
                except httpx.HTTPStatusError as exc:
                    if int(getattr(exc.response, "status_code", 0) or 0) != 403:
                        raise
                    # 这个 token 没有跨桥作用域：退回本桥命名空间并如实标注两个字段，
                    # 不静默把"全量"降级成"自己那几个"。
                    effective = "own"
                    payload = await client.list_sessions()
            else:
                payload = await client.list_sessions()
            items = [item for item in (_normalize(entry) for entry in list(payload.get("items") or [])) if item]
            if needle or bridge:
                items = [item for item in items if _matches(item, needle=needle, bridge=bridge)]
            page = items[start : start + cap]
            return {
                "ok": True,
                "scope": effective,
                "requested_scope": wanted,
                "matched": len(items),
                "count": len(page),
                "offset": start,
                "next_offset": start + len(page) if start + len(page) < len(items) else None,
                "truncated": bool(items[start + cap :]),
                "items": page,
            }

        return await _safe(_body)

    @mcp.tool(
        description=(
            "Delete a conversation this gateway created, together with its transcript and side "
            "artifacts. Only short names in this gateway's own namespace are accepted; full session "
            "keys (anything containing ':') are refused so one bridge cannot wipe another's "
            "sessions. The conversation reappears as a fresh session on the next message."
        )
    )
    async def negi_delete_conversation(conversation: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            raw = str(conversation or "").strip()
            if not raw:
                return {"ok": False, "error": "conversation_required"}
            if ":" in raw:
                # 全键寻址在这里是越权道：别的桥的会话不该由本网关删。
                return {"ok": False, "error": "own_namespace_only", "conversation": raw}
            session_id = await client.ensure_session(raw)
            result = await client.delete_session(session_id, purge=True)
            client.forget_session(raw)
            return {
                "ok": True,
                "conversation": raw,
                "session_id": session_id,
                "deleted": bool(result.get("deleted")),
                "registry_forgotten": bool(result.get("registry_forgotten")),
            }

        return await _safe(_body)

    return mcp
