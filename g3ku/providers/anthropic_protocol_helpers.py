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


def anthropic_messages_suffix(api_base: str) -> str:
    """Suffix to append to a base URL to reach the Messages endpoint.

    官方 base 含 /v1（api.anthropic.com/v1）→ /messages；百炼 base 不含 /v1
    （dashscope.aliyuncs.com/apps/anthropic）→ /v1/messages。base 已含 /messages 时返回 ""。
    provider 与连接探测共用，保证两处拼出的 URL 一致。
    """
    base = str(api_base or "").rstrip("/")
    if base.endswith("/messages"):
        return ""
    if base.endswith("/v1"):
        return "/messages"
    return "/v1/messages"


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

    图片：data URL → base64 source；http(s) URL → url source（百炼支持 url，官方 Anthropic
    亦支持 url source）；已是 Anthropic source 形状的原样透传。受 imageMultimodalEnabled
    闸门控制（默认关，像素不进请求）。
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
        if item_type in {"image_url", "input_image", "image"}:
            source = item.get("source")
            if isinstance(source, dict) and source.get("type"):
                blocks.append({"type": "image", "source": source})
                continue
            image_value = item.get("image_url")
            url = image_value.get("url") if isinstance(image_value, dict) else (image_value or item.get("url"))
            if isinstance(url, str) and url:
                if url.startswith("data:"):
                    header, _, data = url.partition(",")
                    mime = header[5:].split(";")[0] or "image/png"
                    if data:
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
                else:
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
            continue
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


def _replayable_thinking_blocks(msg: dict[str, Any], model: str | None) -> list[dict[str, Any]]:
    """Replay stored thinking blocks for the target model (signature is model-bound).

    只回给签发模型（g3ku_reasoning_model 戳匹配）；无戳或跨模型一律不回放（fail-closed，
    同 Responses reasoning_items）。缺签名的 thinking 降级成 text（pi-ai 同形）：保住内容
    但丢原生思考连续性。redacted_thinking 原样回。
    """
    out: list[dict[str, Any]] = []
    for raw in list(msg.get("thinking_blocks") or []):
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("g3ku_reasoning_model") or "").strip()
        if not source or (model and source != str(model).strip()):
            continue
        btype = raw.get("type")
        if btype == "redacted_thinking":
            data = raw.get("data")
            if data:
                out.append({"type": "redacted_thinking", "data": data})
            continue
        if btype == "thinking":
            thinking = str(raw.get("thinking") or "")
            signature = str(raw.get("signature") or "")
            if signature.strip():
                out.append({"type": "thinking", "thinking": thinking, "signature": signature})
            elif thinking.strip():
                out.append({"type": "text", "text": thinking})
    return out


def _assistant_blocks(msg: dict[str, Any], model: str | None = None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    # thinking 块必须在 assistant 消息最前（Anthropic 顺序合同：thinking 先于 text/tool_use）。
    blocks.extend(_replayable_thinking_blocks(msg, model))
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        blocks.extend(_text_blocks_from_content(content))
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


def _convert_messages(
    messages: list[dict[str, Any]], *, model: str | None = None, cache_control: bool = False
) -> tuple[str, list[dict[str, Any]]]:
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
            push("assistant", _assistant_blocks(msg, model))
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
    # 滚动缓存断点：末条 user 消息末块（system/tools 的固定断点在 provider 侧打）。
    if cache_control and converted and converted[-1]["role"] == "user" and converted[-1]["content"]:
        converted[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
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


def _convert_tools(tools: list[dict[str, Any]] | None, *, cache_control: bool = False) -> list[dict[str, Any]]:
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
    # 固定缓存断点：末项工具（与 system 尾、末条 user 一起构成稳定前缀，≤4 断点）。
    if cache_control and converted:
        converted[-1]["cache_control"] = {"type": "ephemeral"}
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
    response_model = ""

    async for event in _iter_sse(response):
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message") or {}
            raw_usage.update(message.get("usage") or {})
            response_model = str(message.get("model") or "").strip()
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
    # 思考签名模型绑定：打戳供回放侧只回给签发模型（同 Responses reasoning_items）。
    if response_model:
        for block in thinking_blocks:
            block["g3ku_reasoning_model"] = response_model
    return "".join(content_parts), tool_calls, finish_reason, usage, thinking_blocks


__all__ = [
    "_convert_messages",
    "_convert_tools",
    "_consume_sse",
    "_friendly_error",
    "_map_stop_reason",
    "_normalize_tool_call_id",
]
