"""Shared protocol helpers for Anthropic Messages-style endpoints (/v1/messages).

These convert chat-shaped message history into Anthropic Messages input, consume
Anthropic SSE streams, and normalize provider errors. They mirror the Responses
helpers (`responses_protocol_helpers.py`) so the third protocol lane stays the same
shape as the second.

Anthropic contract differences handled here (see work/anthropic-protocol-plan.md):
- system rides in a top-level ``system`` field, not a message role.
- tool results are ``tool_result`` blocks inside a ``user`` message, must immediately
  follow the ``tool_use`` that declared them, and consecutive same-role messages merge.
- tools use ``input_schema`` (root object), not ``function.parameters``.
- thinking blocks carry a model-bound ``signature`` (P1; P0 sends thinking disabled).
"""

from __future__ import annotations

import inspect
import re
from typing import Any

import json_repair
from loguru import logger

from g3ku.json_schema_utils import normalize_openai_tool_definition
from g3ku.providers.base import ToolCallRequest, normalize_usage_payload
from g3ku.providers.responses_protocol_helpers import _friendly_error, _iter_sse
from g3ku.runtime.tool_history import analyze_tool_call_history, extract_call_id

# Anthropic tool_use ids must match this pattern (pi-ai normalizeToolCallId).
_TOOL_ID_INVALID = re.compile(r"[^a-zA-Z0-9_-]")
_TOOL_ID_MAX = 64


def _normalize_tool_call_id(value: Any) -> str:
    raw = extract_call_id(value)
    if not raw:
        return ""
    return _TOOL_ID_INVALID.sub("_", raw)[:_TOOL_ID_MAX]


def _system_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text", block.get("content", ""))
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _text_blocks_from_content(content: Any) -> list[dict[str, Any]]:
    """Convert user/tool string-or-list content into Anthropic text/image blocks.

    P0 handles text; image blocks are skipped (the imageMultimodalEnabled gate keeps
    pixels out of the request by default). P1 adds URL->base64 image sources.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"text", "input_text", "output_text"}:
            text = item.get("text", item.get("content", ""))
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            continue
        # image_url / input_image: P1 (base64 source). Skipped in P0.
    return blocks


def _tool_arguments_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json_repair.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _sanitize_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop dangling assistant tool calls and orphan tool results before conversion.

    Same resilience philosophy as the Responses lane: an interrupted turn can leave a
    tool call without its result (or vice versa); Anthropic rejects both. Preserve
    assistant text, strip the unmatched calls/results so the turn can continue.
    """
    analysis = analyze_tool_call_history(messages)
    completed = set(analysis.completed_call_ids)
    declared = set(analysis.declared_call_ids)

    sanitized: list[dict[str, Any]] = []
    dropped_calls: list[str] = []
    for msg in messages:
        role = str(msg.get("role") or "").strip().lower()
        if role == "assistant":
            calls = list(msg.get("tool_calls") or [])
            if not calls:
                sanitized.append(msg)
                continue
            kept = []
            for call in calls:
                call_id = extract_call_id(call.get("id"))
                if call_id and call_id in completed:
                    kept.append(call)
                else:
                    dropped_calls.append(call_id or "<missing>")
            updated = dict(msg)
            if kept:
                updated["tool_calls"] = kept
                sanitized.append(updated)
                continue
            updated.pop("tool_calls", None)
            if updated.get("content") or updated.get("reasoning_content") or updated.get("reasoning_items") or updated.get("thinking_blocks"):
                sanitized.append(updated)
            continue
        if role == "tool":
            call_id = extract_call_id(msg.get("tool_call_id"))
            if call_id and call_id in declared:
                sanitized.append(msg)
            else:
                logger.warning("Dropping orphan tool result without matching assistant tool call before Anthropic request: {}", call_id or "<missing>")
            continue
        sanitized.append(msg)

    if dropped_calls:
        logger.warning("Dropping {} dangling assistant tool call(s) without matching tool output before Anthropic request: {}", len(dropped_calls), ", ".join(dropped_calls))
    return sanitized


def _assistant_blocks(msg: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        blocks.extend(_text_blocks_from_content(content))
    # P1: replay thinking_blocks (with model-bound signature) here, before text/tool_use.
    for call in list(msg.get("tool_calls") or []):
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or "").strip()
        call_id = _normalize_tool_call_id(call.get("id"))
        if not name or not call_id:
            continue
        blocks.append(
            {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": _tool_arguments_dict(function.get("arguments", call.get("arguments"))),
            }
        )
    return blocks


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Convert chat-shaped history into (system_text, anthropic_messages).

    Enforces Anthropic's structural contracts: top-level system, alternating
    user/assistant roles (consecutive same-role merged), tool_result blocks inside a
    user message immediately after their tool_use and ordered first in that message.
    """
    messages = _sanitize_tool_history(messages)
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    leading_system = True

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})

    for msg in messages:
        role = str(msg.get("role") or "").strip().lower()
        content = msg.get("content")
        if role == "system":
            text = _system_text(content)
            if not text.strip():
                continue
            if leading_system:
                system_parts.append(text)
            else:
                # Defensive: sanitize already folds mid-history system into user.
                push("user", [{"type": "text", "text": text}])
            continue
        leading_system = False
        if role == "user":
            push("user", _text_blocks_from_content(content))
            continue
        if role == "assistant":
            push("assistant", _assistant_blocks(msg))
            continue
        if role == "tool":
            call_id = _normalize_tool_call_id(msg.get("tool_call_id"))
            if not call_id:
                continue
            result_blocks = _text_blocks_from_content(content)
            if not result_blocks:
                result_blocks = [{"type": "text", "text": "(empty)"}]
            push("user", [{"type": "tool_result", "tool_use_id": call_id, "content": result_blocks}])
            continue

    # tool_result blocks must come first in their user message (Anthropic contract).
    for msg in converted:
        if msg["role"] != "user":
            continue
        results = [b for b in msg["content"] if b.get("type") == "tool_result"]
        if results:
            rest = [b for b in msg["content"] if b.get("type") != "tool_result"]
            msg["content"] = results + rest

    _enforce_tool_adjacency(converted)
    return "\n\n".join(system_parts), converted


def _enforce_tool_adjacency(converted: list[dict[str, Any]]) -> None:
    """Drop tool_result blocks whose tool_use is not in the immediately preceding assistant.

    Anthropic requires each tool_result to immediately follow its tool_use. Negi keeps
    tool groups atomic so this normally holds; a mid-inserted block between a call and
    its result would violate it. Fail-safe (drop the offending result) rather than 400,
    matching the Responses lane's drop philosophy. Mutates ``converted`` in place.
    """
    for index in range(len(converted) - 1, -1, -1):
        msg = converted[index]
        if msg["role"] != "user":
            continue
        results = [b for b in msg["content"] if b.get("type") == "tool_result"]
        if not results:
            continue
        declared: set[str] = set()
        if index > 0 and converted[index - 1]["role"] == "assistant":
            declared = {b.get("id") for b in converted[index - 1]["content"] if b.get("type") == "tool_use"}
        kept = []
        for block in msg["content"]:
            if block.get("type") == "tool_result" and block.get("tool_use_id") not in declared:
                logger.warning("Dropping Anthropic tool_result not adjacent to its tool_use: {}", block.get("tool_use_id"))
                continue
            kept.append(block)
        msg["content"] = kept
    # Remove messages emptied by the pass; merge any newly-adjacent same-role messages.
    converted[:] = [m for m in converted if m["content"]]
    merged: list[dict[str, Any]] = []
    for msg in converted:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"].extend(msg["content"])
        else:
            merged.append(msg)
    converted[:] = merged


def _convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert shared tool schemas into Anthropic ``input_schema`` form.

    Reuses the OpenAI normalizer (which already strips root anyOf/oneOf/allOf via
    sanitize_provider_parameters_schema), then reshapes to name/description/input_schema
    with a root object — Anthropic rejects root-level combinators.
    """
    converted: list[dict[str, Any]] = []
    for tool in list(tools or []):
        normalized = normalize_openai_tool_definition(tool)
        if not normalized:
            continue
        function = normalized.get("function") or {}
        parameters = function.get("parameters") if isinstance(function.get("parameters"), dict) else {}
        input_schema: dict[str, Any] = {"type": "object"}
        if isinstance(parameters.get("properties"), dict):
            input_schema["properties"] = parameters["properties"]
        if isinstance(parameters.get("required"), list):
            input_schema["required"] = parameters["required"]
        if "additionalProperties" in parameters:
            input_schema["additionalProperties"] = parameters["additionalProperties"]
        if isinstance(parameters.get("$defs"), dict):
            input_schema["$defs"] = parameters["$defs"]
        converted.append(
            {
                "name": str(function.get("name") or "").strip(),
                "description": str(function.get("description") or "").strip(),
                "input_schema": input_schema,
            }
        )
    return converted


_STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "error",
}


def _map_stop_reason(reason: Any) -> str:
    return _STOP_REASON_MAP.get(str(reason or "").strip(), "stop")


async def _consume_sse(
    response: Any,
    *,
    on_text_delta: Any = None,
    on_reasoning_delta: Any = None,
) -> tuple[str, list[ToolCallRequest], str, dict[str, int], list[dict[str, Any]]]:
    """Consume an Anthropic SSE stream into (content, tool_calls, finish_reason, usage, thinking_blocks).

    ``response`` is the diagnostics-wrapped httpx response (aiter_lines +
    note_terminal_event). The terminal credential is ``message_stop``; without it the
    caller marks the stream incomplete (same判据 as Responses' response.completed).
    """
    content_parts: list[str] = []
    tool_calls: list[ToolCallRequest] = []
    thinking_blocks: list[dict[str, Any]] = []
    open_blocks: dict[int, dict[str, Any]] = {}
    finish_reason = "stop"
    usage: dict[str, int] = {}
    raw_usage: dict[str, Any] = {}

    async for event in _iter_sse(response):
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message") or {}
            raw_usage.update(message.get("usage") or {})
        elif event_type == "content_block_start":
            index = event.get("index")
            block = event.get("content_block") or {}
            block_type = block.get("type")
            if not isinstance(index, int):
                continue
            if block_type == "text":
                open_blocks[index] = {"kind": "text", "text": block.get("text") or ""}
            elif block_type == "thinking":
                open_blocks[index] = {"kind": "thinking", "thinking": block.get("thinking") or "", "signature": block.get("signature") or ""}
            elif block_type == "redacted_thinking":
                open_blocks[index] = {"kind": "redacted_thinking", "data": block.get("data") or ""}
            elif block_type == "tool_use":
                open_blocks[index] = {"kind": "tool_use", "id": block.get("id") or "", "name": block.get("name") or "", "json": ""}
        elif event_type == "content_block_delta":
            index = event.get("index")
            delta = event.get("delta") or {}
            open_block = open_blocks.get(index) if isinstance(index, int) else None
            if open_block is None:
                continue
            delta_type = delta.get("type")
            if delta_type == "text_delta" and open_block["kind"] == "text":
                text = str(delta.get("text") or "")
                open_block["text"] += text
                if text and callable(on_text_delta):
                    result = on_text_delta(text)
                    if inspect.isawaitable(result):
                        await result
            elif delta_type == "thinking_delta" and open_block["kind"] == "thinking":
                text = str(delta.get("thinking") or "")
                open_block["thinking"] += text
                if text and callable(on_reasoning_delta):
                    result = on_reasoning_delta(text)
                    if inspect.isawaitable(result):
                        await result
            elif delta_type == "signature_delta" and open_block["kind"] == "thinking":
                open_block["signature"] += str(delta.get("signature") or "")
            elif delta_type == "input_json_delta" and open_block["kind"] == "tool_use":
                open_block["json"] += str(delta.get("partial_json") or "")
        elif event_type == "content_block_stop":
            index = event.get("index")
            open_block = open_blocks.pop(index, None) if isinstance(index, int) else None
            if open_block is None:
                continue
            kind = open_block["kind"]
            if kind == "text":
                if open_block["text"]:
                    content_parts.append(open_block["text"])
            elif kind == "thinking":
                thinking_blocks.append({"type": "thinking", "thinking": open_block["thinking"], "signature": open_block["signature"]})
            elif kind == "redacted_thinking":
                thinking_blocks.append({"type": "redacted_thinking", "data": open_block["data"]})
            elif kind == "tool_use":
                args_raw = open_block["json"] or "{}"
                tool_calls.append(
                    ToolCallRequest(
                        id=open_block["id"],
                        name=open_block["name"],
                        arguments=_tool_arguments_dict(args_raw),
                        raw_arguments=args_raw,
                    )
                )
        elif event_type == "message_delta":
            delta = event.get("delta") or {}
            if delta.get("stop_reason") is not None:
                finish_reason = _map_stop_reason(delta.get("stop_reason"))
            if isinstance(event.get("usage"), dict):
                raw_usage.update(event["usage"])
        elif event_type == "message_stop":
            note_terminal = getattr(response, "note_terminal_event", None)
            if callable(note_terminal):
                note_terminal()
            usage = normalize_usage_payload(raw_usage)
        elif event_type == "error":
            error = event.get("error") or {}
            message = str(error.get("message") or "Anthropic stream error")
            raise RuntimeError(f"Anthropic stream error: {message}")

    if not usage:
        usage = normalize_usage_payload(raw_usage)
    return "".join(content_parts), tool_calls, finish_reason, usage, thinking_blocks


__all__ = [
    "_convert_messages",
    "_convert_tools",
    "_consume_sse",
    "_friendly_error",
    "_map_stop_reason",
    "_normalize_tool_call_id",
]
