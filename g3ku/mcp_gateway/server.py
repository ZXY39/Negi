"""FastMCP tool surface for the Negi MCP gateway (stdio proxy).

Out-of-the-box surface #2 for external AI agents (contract owner:
``docs/architecture/agent-gateway.md``). The tools never raise: transport /
HTTP failures are converted into ``{"ok": False, "error": ...}`` payloads so
the calling agent always gets a readable result and can tell its user what
happened (401 invalid token, 423 project locked, 503 runtime unavailable,
connection failed, ...).
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from g3ku.mcp_gateway.client import G3kuMcpClient
from g3ku.mcp_gateway.local_store import (
    read_wake_state,
    spill_reply,
    write_wake_state,
)

MIN_WAIT_SECONDS = 5
MAX_WAIT_SECONDS = 1800
DEFAULT_LIST_LIMIT = 50
WAKE_POLL_INTERVAL_SECONDS = 2.0

# 附件的上限沿用运行时已有的闸门，MCP 侧不新立一套数字；等值关系由
# tests/resources/test_mcp_chat_attachments.py 钉住（网关不许 import web 路由层）。
ATTACHMENT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
ATTACHMENT_FILE_MAX_BYTES = 20 * 1024 * 1024
ATTACHMENTS_PER_MESSAGE_MAX = 4
_URL_PREFIXES = ("http://", "https://")
_SCHEME_PREFIX = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _attachment_problem(
    index: int, source: str, error: str, hint: str, **extra: Any
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": error,
        "attachment_index": index,
        "attachment": source,
        "hint": hint,
        **extra,
    }


def _resolve_attachments(
    items: list[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    """Turn caller-supplied paths/URLs into ``/api/v1`` attachment descriptors.

    The gateway does the reading, so the runtime only ever receives files it
    stored itself and the calling model never has to emit base64.
    """
    raw = [str(item or "").strip() for item in list(items or [])]
    if len(raw) > ATTACHMENTS_PER_MESSAGE_MAX:
        return [], _attachment_problem(
            ATTACHMENTS_PER_MESSAGE_MAX,
            "",
            "too_many_attachments",
            f"Send at most {ATTACHMENTS_PER_MESSAGE_MAX} attachments per message; "
            "this one carried none of them, so nothing was submitted. Split them over "
            "several messages, or name the files in the text instead.",
            attachments_requested=len(raw),
            max_attachments=ATTACHMENTS_PER_MESSAGE_MAX,
        ), []

    descriptors: list[dict[str, Any]] = []
    echoes: list[str] = []
    for index, source in enumerate(raw):
        if not source:
            return [], _attachment_problem(
                index, source, "attachment_empty",
                "Remove the empty entry; attachments are file paths or http(s) URLs.",
            ), []
        if source.lower().startswith(_URL_PREFIXES):
            # 名字由服务端从 URL 尾段推，这里不多送一个字段。
            descriptors.append({"url": source})
            echoes.append(source)
            continue
        if source.lower().startswith("data:"):
            return [], _attachment_problem(
                index, source, "unsupported_attachment_scheme",
                "A base64 payload does not travel through this argument: save the bytes "
                "to a file on the machine running `negi mcp serve` and pass that path.",
            ), []
        scheme = _SCHEME_PREFIX.match(source)
        if scheme:
            return [], _attachment_problem(
                index, source, "unsupported_attachment_scheme",
                f"'{scheme.group(0)}' is not accepted here: attachments are local file "
                "paths on the machine running `negi mcp serve`, or http(s) URLs.",
            ), []

        path = Path(source).expanduser()
        try:
            size = path.stat().st_size
        except OSError as exc:
            return [], _attachment_problem(
                index, source, "attachment_not_found",
                f"The gateway could not stat this path ({exc.strerror or exc}). Pass a "
                "file that exists on the machine running `negi mcp serve` — not on the "
                "caller's own machine.",
            ), []
        if not path.is_file():
            return [], _attachment_problem(
                index, source, "attachment_is_a_directory",
                "Point at one file inside the directory, not the directory itself.",
            ), []
        mime_type = str(mimetypes.guess_type(path.name)[0] or "").strip()
        max_bytes = (
            ATTACHMENT_IMAGE_MAX_BYTES
            if mime_type.startswith("image/")
            else ATTACHMENT_FILE_MAX_BYTES
        )
        if size > max_bytes:
            return [], _attachment_problem(
                index, source, "attachment_too_large",
                "Compress or split this file; nothing was submitted.",
                attachment_bytes=size,
                max_bytes=max_bytes,
            ), []
        try:
            payload = path.read_bytes()
        except OSError as exc:
            return [], _attachment_problem(
                index, source, "attachment_unreadable",
                f"The gateway could not read this file ({exc.strerror or exc}).",
            ), []
        descriptors.append({
            "data_base64": base64.b64encode(payload).decode("ascii"),
            "name": path.name,
            "mime_type": mime_type,
        })
        echoes.append(f"{path.name} ({size} bytes)")

    return descriptors, None, echoes



def _queue_visibility(result: dict[str, Any]) -> dict[str, Any]:
    """把"你排在谁后面"从服务端回执搬到调用方眼前。没撞车时这两个键不出现。"""
    extras: dict[str, Any] = {}
    running = str(result.get("running_turn_id") or "").strip()
    if running:
        extras["running_turn_id"] = running
    if result.get("queued_position") is not None:
        extras["queued_position"] = int(result.get("queued_position") or 0)
    return extras


def _progress_hook(ctx: Any) -> Any:
    """把等待期间的事件转成 MCP progress 通知。

    客户端不支持通知时 `report_progress` 会抛，这里只让它静默失效——等待语义与
    最终回复绝不能因为"没地方汇报进度"而变化。
    """
    if ctx is None:
        return None

    async def _emit(event: dict[str, Any]) -> None:
        if str(event.get("type") or "") != "progress":
            return
        await ctx.report_progress(
            progress=float(event.get("seq") or 0),
            message=f"{event.get('kind')}: {str(event.get('text') or '')[:120]}",
        )

    return _emit


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
            "memory, so send only the new message (no client-side history). "
            "Conversation addressing is the same for every tool here: pass the bare "
            "short name you were given (e.g. 'c1'), or a real session_id taken from "
            "negi_list_conversations. A name containing ':' is always addressed as a "
            "full session key, so the gateway's own external_key prefix ('mcp:') is NOT "
            "part of a conversation name — 'mcp:c1' matches no session and the tool "
            "returns {ok:false, error:\"http_404\"}."
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
            "Sending while a turn is already running is safe and does NOT restart it: the message "
            "is queued onto that session and its answer arrives after the running turn settles, in "
            "submission order, so independent questions can be submitted back-to-back instead of "
            "awaited one by one. While blocked, progress notifications carry what Negi is doing. "
            "A deep verification turn routinely runs for many minutes — do not assume a slow "
            "response means a stalled one; check negi_session_status for activity and silence. "
            "task_ids are the machine-readable handles of the tasks this turn dispatched "
            "(use them with the web runtime to follow up); reply is prose for a human. "
            "`attachments` sends files with the message: local file paths on the machine "
            "running `negi mcp serve`, or http(s) URLs — never base64, and not paths from "
            "your own machine. Up to 4 per message, images up to 5 MB, other files up to "
            "20 MB; what got sent is echoed in `attachments`. A rejected attachment "
            "submits nothing at all — fix the argument and resend."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
    )
    async def negi_chat(
        conversation: str,
        message: str,
        wait_seconds: int = 120,
        attachments: list[str] = [],
        ctx: Context = None,
    ) -> dict[str, Any]:
        submit_only = _raw_wait(wait_seconds, 120) == 0
        timeout = _clamp_wait(wait_seconds, 120)
        on_event = _progress_hook(ctx)
        resolved, attachment_problem, sent = _resolve_attachments(attachments)
        if attachment_problem is not None:
            return attachment_problem
        sent_payload = {"attachments": sent} if sent else {}

        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
            after_seq = client.last_seq(session_id)
            if submit_only:
                result = await client.send_message(
                    session_id, str(message or ""), attachments=resolved
                )
                extras = _queue_visibility(result)
                hint = "Submitted without waiting. Call negi_get_reply to fetch the final reply."
                if extras.get("running_turn_id"):
                    hint = (
                        "Queued behind a running turn — this did NOT restart it. Stop that turn "
                        "with negi_pause/negi_cancel; its id is running_turn_id. Fetch the answer "
                        "later with negi_get_reply."
                    )
                return {
                    "ok": True,
                    "conversation": conversation,
                    "session_id": session_id,
                    "turn_id": str(result.get("turn_id") or "") or None,
                    "submit_status": str(result.get("status") or ""),
                    "status": "submitted",
                    "task_ids": [],
                    "receipt": str(result.get("receipt") or ""),
                    "hint": hint,
                    **extras,
                    **sent_payload,
                }
            async with client.event_stream(session_id, last_seq=after_seq) as events:
                result = await client.send_message(
                    session_id, str(message or ""), attachments=resolved
                )
                submit_status = str(result.get("status") or "")
                turn_id = str(result.get("turn_id") or "") or None
                if submit_status == "queued":
                    outcome = await client.wait_for_reply(
                        events,
                        queued=True,
                        after_seq=after_seq,
                        timeout=timeout,
                        session_id=session_id,
                        on_event=on_event,
                    )
                elif submit_status == "duplicate":
                    outcome = await client.wait_for_reply(
                        events,
                        turn_id=turn_id,
                        queued=str(result.get("original_status") or "") == "queued",
                        after_seq=0,
                        timeout=timeout,
                        session_id=session_id,
                        on_event=on_event,
                    )
                else:
                    outcome = await client.wait_for_reply(
                        events,
                        turn_id=turn_id,
                        after_seq=after_seq,
                        timeout=timeout,
                        session_id=session_id,
                        on_event=on_event,
                    )
            base = {
                "ok": True,
                "conversation": conversation,
                "session_id": session_id,
                "turn_id": outcome.get("turn_id") or turn_id,
                "submit_status": submit_status,
                "task_ids": list(outcome.get("task_ids") or []),
                "transcript_turn_id": outcome.get("transcript_turn_id"),
                **sent_payload,
            }
            kind = str(outcome.get("kind") or "")
            if kind == "reply":
                return {
                    **base,
                    "status": "completed",
                    "usage": outcome.get("usage"),
                    **spill_reply(
                        session_id,
                        str(base.get("transcript_turn_id") or base.get("turn_id") or ""),
                        str(outcome.get("text") or ""),
                    ),
                }
            if kind == "failed":
                return {**base, "status": "failed", "error": str(outcome.get("error") or "")}
            if kind in {"cancelled", "no_reply"}:
                return {**base, "status": kind, **await _absence_diagnosis(session_id, outcome)}
            if submit_status == "queued":
                return {
                    **base,
                    "status": "queued_receipt",
                    "receipt": str(result.get("receipt") or ""),
                    "hint": "Message joined the running turn. Call negi_get_reply later to fetch the answer.",
                    **_queue_visibility(result),
                }
            return {
                **base,
                "status": "pending",
                "hint": "Still working. Call negi_get_reply later, or pass wait_seconds=0 to stop blocking.",
                **await _absence_diagnosis(session_id, outcome),
            }

        return await _safe(_body)

    async def _absence_diagnosis(session_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """取不到正文时"为什么取不到"的那组读数，`negi_chat` 与 `negi_get_reply` 共用。

        以前只回 `{found:false, status:"timeout"}`，调用方分不清"这轮没交正文""等短了
        东西还在""流断了""已被别人取走"，实测的应对动作是重派——把几十分钟的活再烧一遍。
        判据全取自既有读口（状态端点 + 持久账本条数），不新增状态。
        """
        kind = str(outcome.get("kind") or "timeout")
        try:
            state = await client.session_state(session_id)
        except Exception:
            state = {}
        unconsumed = int(state.get("unconsumed_replies") or 0)
        running = bool(state.get("running"))
        stop_reason = str(state.get("stop_reason") or "")
        if kind == "cancelled":
            reason = "turn_cancelled"
        elif kind == "no_reply":
            reason = "no_final_written"
        elif str(outcome.get("error") or "") == "stream_closed":
            reason = "stream_closed"
        elif unconsumed:
            reason = "waited_out_reply_pending"
        elif running:
            reason = "still_running"
        else:
            # 既没在跑、账本里也没存货：这轮要么没交正文，要么正文已被某个消费者取走——
            # 接口分不开这两种，就照"分不开"说，不冒充成 `already_consumed`。
            reason = "nothing_pending"
        diagnosis: dict[str, Any] = {
            "reason": reason,
            "unconsumed_replies": unconsumed,
            "session_running": running,
        }
        if stop_reason:
            diagnosis["stop_reason"] = stop_reason
        if reason == "waited_out_reply_pending":
            diagnosis["where_to_look"] = "negi_wait_wake 取未销账终稿（超阈值的会落盘并给路径）"
        elif reason in {"no_final_written", "nothing_pending"}:
            diagnosis["where_to_look"] = "看 stop_reason 判这轮是交付还是早退；要重做就再发一条，别当已丢"
        return diagnosis

    async def _absence_readout(conversation: str, session_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "conversation": conversation,
            "found": False,
            "status": str(outcome.get("kind") or "timeout"),
            **await _absence_diagnosis(session_id, outcome),
        }

    @mcp.tool(
        description=(
            "Fetch the next final reply for a conversation that was not consumed yet "
            "(e.g. after negi_chat returned status='pending' or status='submitted'). "
            "Carries the same task_ids handle as negi_chat. Pass the conversation exactly "
            "as you passed it to negi_chat. Give `turn_id` (the id negi_chat returned) to wait "
            "for THAT turn instead of whatever answer comes next; the reply always reports "
            "`turn_id` and `answered_seq` so a reply can be matched to its question."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
        ),
    )
    async def negi_get_reply(
        conversation: str, wait_seconds: int = 60, turn_id: str = "", ctx: Context = None
    ) -> dict[str, Any]:
        timeout = _clamp_wait(wait_seconds, 60)
        want_turn_id = str(turn_id or "").strip()
        on_event = _progress_hook(ctx)

        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
            after_seq = client.last_seq(session_id)
            async with client.event_stream(session_id, last_seq=after_seq) as events:
                outcome = await client.wait_for_reply(
                    events,
                    turn_id=want_turn_id or None,
                    queued=not bool(want_turn_id),
                    after_seq=after_seq,
                    timeout=timeout,
                    session_id=session_id,
                    on_event=on_event,
                )
            if str(outcome.get("kind") or "") == "reply":
                fetched = {
                    "ok": True,
                    "conversation": conversation,
                    "found": True,
                    "turn_id": outcome.get("turn_id"),
                    "answered_seq": int(outcome.get("seq") or 0),
                    "usage": outcome.get("usage"),
                    "task_ids": list(outcome.get("task_ids") or []),
                    "transcript_turn_id": outcome.get("transcript_turn_id"),
                }
                return {
                    **fetched,
                    **spill_reply(
                        session_id,
                        str(fetched.get("transcript_turn_id") or fetched.get("turn_id") or ""),
                        str(outcome.get("text") or ""),
                    ),
                }
            return await _absence_readout(conversation, session_id, outcome)

        return await _safe(_body)

    @mcp.tool(
        description=(
            "Re-read one finished turn's stored reply text, from the session transcript "
            "(durable: it survives this client's own context compaction, unlike whatever "
            "summary is left in memory). Pass `transcript_turn_id` — the id the transcript row "
            "carries, returned by negi_chat/negi_get_reply alongside `turn_id`. The two ids are "
            "different namespaces on purpose: `turn_id` addresses pause/resume of the external "
            "turn, `transcript_turn_id` addresses the stored line. Returns turn_not_found when "
            "the given id is not a stored transcript line for this conversation."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    async def negi_get_turn(conversation: str, turn_id: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
            wanted = str(turn_id or "").strip()
            if not wanted:
                return {"ok": False, "error": "turn_id_required", "conversation": conversation}
            try:
                payload = await client.get_turn(session_id, wanted)
            except httpx.HTTPStatusError as exc:
                if int(getattr(exc.response, "status_code", 0) or 0) != 404:
                    raise
                return {"ok": False, "error": "turn_not_found", "conversation": conversation, "turn_id": wanted}
            return {
                "ok": True,
                "conversation": conversation,
                "session_id": session_id,
                "turn_id": wanted,
                "reply": str(payload.get("text") or ""),
                "created_at": str(payload.get("created_at") or ""),
                "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else {},
                "task_ids": list(payload.get("task_ids") or []),
                "silent_reply": bool(payload.get("silent_reply")),
            }

        return await _safe(_body)

    @mcp.tool(
        description=(
            "Answer \"what happened while I wasn't listening\" in one call: tasks this "
            "conversation dispatched that have since reached a terminal state, plus final "
            "replies still unconsumed in the durable ledger. It reads committed state (the task "
            "table and the outbound ledger), not a live stream — so a task that finished 40 "
            "minutes ago with nobody connected is still reported. Replies surfaced here are "
            "acked, so each one arrives once; if the ack itself fails the entry carries "
            "`ack_failed` and may appear again. Long replies are spilled to a file like "
            "everywhere else. `wait_seconds=0` looks once and returns; larger values re-check "
            "every 2 seconds until something wakes it. The per-conversation cursor is kept by "
            "the gateway — you don't pass `since`. Nothing already reported at the cursor "
            "second comes back a second time (deduped by `task_id`/`outbox_id`), so an idle "
            "conversation answers `woke:false` instead of re-announcing the same terminal row."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
        ),
    )
    async def negi_wait_wake(conversation: str, wait_seconds: int = 120) -> dict[str, Any]:
        budget = max(0, _raw_wait(wait_seconds, 120))

        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
            since, reported = read_wake_state(session_id)
            seen = set(reported)
            deadline = time.monotonic() + budget
            payload: dict[str, Any] = {}
            tasks: list[dict[str, Any]] = []
            woken: list[dict[str, Any]] = []
            while True:
                payload = await client.wake(session_id, since)
                # 服务端下界是包含式的（终态行只存到秒，严格大于会吞掉同秒内更晚落库的那条），
                # 所以"已经报过"必须按身份在这里摘掉——否则同一秒那条终态会被永久重报。
                tasks = [item for item in list(payload.get("tasks") or []) if str(item.get("task_id") or "") not in seen]
                fresh_replies = [
                    item
                    for item in list(payload.get("replies") or [])
                    if str(item.get("outbox_id") or "") not in seen
                ]
                if tasks or fresh_replies or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(WAKE_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
            for item in fresh_replies:
                outbox_id = str(item.get("outbox_id") or "")
                entry = {
                    "outbox_id": outbox_id,
                    "ts": str(item.get("ts") or ""),
                    **spill_reply(session_id, outbox_id, str(item.get("text") or "")),
                }
                woken.append(entry)
                if outbox_id:
                    try:
                        await client.ack_reply(session_id, outbox_id)
                    except Exception:
                        # 销账失败就说出来：这条记录还在账本里，下次唤醒会再出现一次。
                        entry["ack_failed"] = True
            woke = bool(tasks or woken)
            next_since = str(payload.get("next_since") or "") or since
            if woke:
                # 游标推进到新的一秒就丢掉旧的去重集；还在同一秒内则把报过的身份累加进去。
                carry = reported if next_since == since else []
                identities = carry + [str(item.get("task_id") or "") for item in tasks]
                identities += [str(item.get("outbox_id") or "") for item in woken]
                write_wake_state(session_id, next_since, [i for i in identities if i])
            reason = ""
            if tasks:
                reason = "task_terminal"
            elif woken:
                reason = "reply"
            return {
                "ok": True,
                "conversation": conversation,
                "session_id": session_id,
                "woke": woke,
                "reason": reason or ("empty" if budget == 0 else "timeout"),
                "since": since,
                "next_since": next_since,
                "tasks": tasks,
                "tasks_may_be_truncated": bool(payload.get("tasks_truncated")),
                "replies": woken,
                "replies_pending": int(payload.get("replies_pending") or 0),
                "wait_budget_seconds": budget,
            }

        return await _safe(_body)

    @mcp.tool(
        description=(
            "Report whether the Negi conversation is running and what is queued. Activity is "
            "expressed as time, not as a counter: `started_at`/`elapsed_seconds` (the in-flight "
            "turn), `last_activity_at`/`silence_seconds` (the newest session event — silence of "
            "40 seconds and silence of 12 minutes are different decisions), plus "
            "`last_activity_kind`/`last_activity_text` for what it is doing right now. "
            "Also carries `latest_model_text` (the newest model output, outbound-sanitized and "
            "truncated — `latest_model_text_total_chars` says how long it really was), "
            "`recent_tool_calls` (tool names used, newest first) and `stages_recent` (last three "
            "stages: status, tool-round usage/budget, round count and a goal prefix — no summaries, "
            "no key_refs). A missing `stages_recent` means the session is not resident, not that "
            "it has no stages. "
            "`last_seq` is the gateway's SSE event cursor, NOT a progress percentage or a step "
            "count — do not read a slow-rising `last_seq` as a stall. Task-side activity is a "
            "separate clock: `tasks_running`/`tasks` plus `task_activity_at`/`task_silence_seconds` "
            "cover nodes this conversation dispatched that are still running, and session-side "
            "silence during that window is expected rather than a stall."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
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
                "started_at": str(state.get("started_at") or ""),
                "elapsed_seconds": state.get("elapsed_seconds"),
                "last_activity_at": str(state.get("last_activity_at") or ""),
                "silence_seconds": state.get("silence_seconds"),
                "last_activity_kind": str(state.get("last_activity_kind") or ""),
                "last_activity_text": str(state.get("last_activity_text") or ""),
                "last_event_seq": int(state.get("last_event_seq") or 0),
                # 在做什么：最新一段模型文本（已过出站消毒、截断后仍报总字数）与最近
                # 调用过的工具名；阶段只给状态/计数与 goal 前缀，不给摘要与 key_refs。
                "latest_model_text": str(state.get("latest_model_text") or ""),
                "latest_model_text_total_chars": int(state.get("latest_model_text_total_chars") or 0),
                "recent_tool_calls": list(state.get("recent_tool_calls") or []),
                "stages_recent": list(state.get("stages_recent") or []) if "stages_recent" in state else None,
                # 跨阶段整数读数：只给最新 3 条 goal 会被读成"就 3 个阶段"，累计轮数
                # 更是没有任何一口能给出（实测 114 轮那种活）。账本读不到时为 None。
                "stage_totals": state.get("stage_totals"),
                # 结束方式：回合 completed 而阶段仍 active 时，这是唯一分辨交付/早退的读数。
                "stop_reason": str(state.get("stop_reason") or ""),
                "unconsumed_replies": int(state.get("unconsumed_replies") or 0),
                "turn_left_unconsumed_reply": bool(state.get("turn_left_unconsumed_reply")),
                "last_error": state.get("last_error"),
                # 任务侧活动：派发回合早就收尾了，节点还在跑，这段时间会话侧本来就没有
                # 新事件——只看 silence_seconds 会把"在跑几十分钟的活"读成"卡死了"。
                "tasks_running": state.get("tasks_running"),
                "tasks": list(state.get("tasks") or []),
                "task_activity_at": str(state.get("task_activity_at") or ""),
                "task_silence_seconds": state.get("task_silence_seconds"),
                "last_seq": client.last_seq(session_id),
            }

        return await _safe(_body)

    @mcp.tool(
        description=(
            "Pause the conversation's currently running Negi turn, if any. This stops the agent "
            "at its next stage boundary; it does not touch tasks the turn already dispatched."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
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
            "Cancel the conversation's in-flight work: the running turn AND the tasks this "
            "session dispatched that have not settled. Read the result, don't infer from `ok`: "
            "`cancelled_turns`/`cancelled_tasks` + `task_ids` say what actually stopped, "
            "`skipped:[{target,reason,how_to_clear}]` names anything this call could not settle "
            "(and how to clear it), and `tasks_still_running` re-counts unfinished work. "
            "Cancellation is request-style under a separated worker: a node settles at its next "
            "checkpoint, so `tasks_still_running` may still be positive in the immediately "
            "following reads — that is not a failure. A task sitting in paused-never-resumed is "
            "cancellable here (in_progress+paused is settled as canceled); if the runtime refuses, "
            "the refusal reason is carried verbatim in `skipped`."
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        ),
    )
    async def negi_cancel(conversation: str) -> dict[str, Any]:
        async def _body() -> dict[str, Any]:
            session_id = await client.resolve_session(conversation)
            result = await client.cancel_session(session_id)
            skipped = list(result.get("skipped") or [])
            payload = {
                "ok": True,
                "conversation": conversation,
                "session_id": session_id,
                "cancelled": int(result.get("cancelled") or 0),
                "cancelled_turns": int(result.get("cancelled_turns") or 0),
                "cancelled_tasks": int(result.get("cancelled_tasks") or 0),
                "task_ids": list(result.get("task_ids") or []),
                "skipped": skipped,
                "tasks_still_running": int(result.get("tasks_still_running") or 0),
            }
            if str(result.get("hint") or "").strip():
                payload["hint"] = str(result.get("hint") or "")
            return payload

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
        ),
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
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
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
        ),
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
                "transcript_removed": bool(result.get("transcript_removed")),
            }

        return await _safe(_body)

    return mcp
