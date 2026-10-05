"""Shared protocol helpers for OpenAI Responses-style endpoints.

These helpers convert chat-shaped message history into Responses API input
items, consume Responses SSE streams, and normalize provider errors. They are
protocol-level utilities shared by the Responses provider, the request-preview
path, and historically the Codex provider.
"""

from __future__ import annotations

import hashlib
import html
import inspect
import json
import re
from typing import Any, AsyncGenerator

import httpx
from loguru import logger

from g3ku.json_schema_utils import normalize_responses_tool_definitions
from g3ku.providers.base import ToolCallRequest, normalize_usage_payload
from g3ku.runtime.tool_history import analyze_tool_call_history, extract_call_id


class CodexStreamError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        partial_content: str = "",
        error_body: str = "",
        error_status: int | None = None,
        error_code: str = "",
    ) -> None:
        super().__init__(message)
        self.partial_content = str(partial_content or "")
        self.error_body = str(error_body or "")
        # 流内 `response.failed` 事件没有 HTTP 状态，只有 `error.code` 这类结构化标识。
        # 把它翻成状态码带在异常上，链层判"可重试与否"就不必去猜供应商那句英文散文。
        self.error_status = error_status
        self.error_code = str(error_code or "")


# 上游在流内事件里给出的限流/配额类结构化 code，一律按 HTTP 429 记账。
RATE_LIMIT_EVENT_CODES = frozenset({
    "rate_limit_exceeded",
    "rate_limit",
    "insufficient_quota",
    "usage_limit_reached",
    "too_many_requests",
})


def _codex_failure_event_status(body: Any) -> tuple[int | None, str]:
    """从失败事件体里取结构化 code，映射成 HTTP 语义状态；取不到就返回 (None, '')."""
    if not isinstance(body, dict):
        return None, ""
    codes = [str(body.get(key) or "").strip().lower() for key in ("code", "type", "reason")]
    codes = [code for code in codes if code]
    for code in codes:
        if code in RATE_LIMIT_EVENT_CODES:
            return 429, code
    return None, (codes[0] if codes else "")


# 心跳与节点错误栏只带这一段的长度，超出的完整错误体落到 worker 日志。
CODEX_FAILURE_DETAIL_LIMIT = 600


def _failure_body_from_event(event: dict[str, Any]) -> Any:
    response = event.get("response") if isinstance(event.get("response"), dict) else {}
    for candidate in (event.get("error"), response.get("error"), response.get("incomplete_details")):
        if candidate:
            return candidate if isinstance(candidate, dict) else {"message": str(candidate)}
    if response:
        projection = {
            key: response.get(key)
            for key in ("id", "status", "model", "error", "incomplete_details")
            if response.get(key) is not None
        }
        if projection:
            return projection
    return {"message": str(event.get("message") or event.get("type") or "")}


def _codex_failure_summary(event: dict[str, Any]) -> tuple[str, str]:
    """Return (bounded one-line reason, full error body, HTTP 语义状态, 结构化 code)."""
    body = _failure_body_from_event(event)
    try:
        full_body = json.dumps(body, ensure_ascii=False, default=str)
    except Exception:
        full_body = str(body)
    parts: list[str] = []
    if isinstance(body, dict):
        parts = [
            str(body.get(key) or "").strip()
            for key in ("message", "type", "code", "reason")
            if str(body.get(key) or "").strip()
        ]
    summary = " | ".join(parts) if parts else full_body
    if len(summary) > CODEX_FAILURE_DETAIL_LIMIT:
        summary = (
            summary[:CODEX_FAILURE_DETAIL_LIMIT]
            + f"...(截断，原文 {len(summary)} 字，完整错误体见 worker 日志)"
        )
    error_status, error_code = _codex_failure_event_status(body)
    return summary, full_body, error_status, error_code


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert shared tool schemas into Responses/Codex flat function format."""
    return normalize_responses_tool_definitions(tools)


def _system_message_text(content: Any) -> str:
    """Extract plain text from a system message's content for position-preserving mapping."""
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


def _system_message_item(system_text: str) -> dict[str, Any]:
    """A mid-history system message kept at its original index in `input`."""
    return {
        "type": "message",
        "role": "system",
        "content": [{"type": "input_text", "text": system_text}],
    }


def _convert_messages(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    messages = _sanitize_tool_call_history(messages)
    system_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    leading_system = True

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            # 位置即语义：只有开头连续的 system 并进前导 `[SYSTEM]` 块，其后的 system 留在
            # 原位发成独立项。把它们抽到 input[0] 既摧毁原位压缩设计，又让活状态块每次改动
            # 都重写请求最前面——缓存边界正好落在改动点上（实测单跳按新输入重计费 10 万
            # token 级）。openai_chat_provider 原样透传 mid-history system，这里对齐同一
            # 不变量；role=system 与位置无关，是 Responses 输入项的合法角色。
            system_text = _system_message_text(content)
            if not str(system_text or "").strip():
                continue
            if leading_system:
                system_parts.append(system_text)
            else:
                input_items.append(_system_message_item(system_text))
            continue

        leading_system = False

        if role == "user":
            input_items.append(_convert_user_message(content))
            continue

        if role == "assistant":
            # 思考项必须排在它所属的 assistant 正文之前，顺序本身就是这条契约。
            input_items.extend(_replayable_reasoning_items(msg.get("reasoning_items"), model))
            # Handle text first.
            if isinstance(content, str) and content:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                        "status": "completed",
                        "id": f"msg_{idx}",
                    }
                )
            # Then handle tool calls.
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = _split_tool_call_id(tool_call.get("id"))
                call_id = call_id or f"call_{idx}"
                item_id = item_id or f"fc_{idx}"
                input_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
            output_payload = _convert_multimodal_content(content)
            if not output_payload:
                output_payload = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_payload,
                }
            )
            continue

    return "\n\n".join(system_parts), input_items


def _sanitize_tool_call_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop incomplete tool-call turns that would break Responses API replay.

    Interrupted or failed turns can occasionally retain an assistant tool call without the
    matching tool output when a prior run was interrupted or failed mid-turn. The
    Responses API rejects such history. Preserve assistant text, but strip dangling
    tool calls so subsequent turns can continue.
    """
    analysis = analyze_tool_call_history(messages)
    completed_call_ids = set(analysis.completed_call_ids)
    declared_call_ids = set(analysis.declared_call_ids)

    sanitized: list[dict[str, Any]] = []
    dropped_assistant_call_ids: list[str] = []

    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            original_tool_calls = list(msg.get("tool_calls", []) or [])
            if not original_tool_calls:
                sanitized.append(msg)
                continue

            kept_tool_calls: list[dict[str, Any]] = []
            for tool_call in original_tool_calls:
                call_id, _ = _split_tool_call_id(tool_call.get("id"))
                if call_id and call_id in completed_call_ids:
                    kept_tool_calls.append(tool_call)
                else:
                    dropped_assistant_call_ids.append(call_id or "<missing>")

            if kept_tool_calls:
                updated = dict(msg)
                updated["tool_calls"] = kept_tool_calls
                sanitized.append(updated)
                continue

            updated = dict(msg)
            updated.pop("tool_calls", None)
            if (
                updated.get("content")
                or updated.get("reasoning_content")
                or updated.get("thinking_blocks")
                or updated.get("reasoning_items")
            ):
                sanitized.append(updated)
            continue

        if role == "tool":
            call_id = extract_call_id(msg.get("tool_call_id"))
            if call_id and call_id in declared_call_ids:
                sanitized.append(msg)
            else:
                logger.warning(
                    "Dropping orphan tool result without matching assistant tool call before Responses API request: {}",
                    call_id or "<missing>",
                )
            continue

        sanitized.append(msg)

    if dropped_assistant_call_ids:
        logger.warning(
            "Dropping {} dangling assistant tool call(s) without matching tool output before Responses API request: {}",
            len(dropped_assistant_call_ids),
            ", ".join(dropped_assistant_call_ids),
        )

    return sanitized


def _convert_user_message(content: Any) -> dict[str, Any]:
    converted = _convert_multimodal_content(content)
    if converted:
        return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def _convert_multimodal_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return []

    converted: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"text", "input_text", "output_text"}:
            text = item.get("text", item.get("content", ""))
            if isinstance(text, str) and text:
                converted.append({"type": "input_text", "text": text})
            continue

        if item_type in {"image_url", "input_image"}:
            image_value = item.get("image_url")
            if isinstance(image_value, dict):
                image_url = image_value.get("url")
            else:
                image_url = image_value or item.get("url")
            if isinstance(image_url, str) and image_url:
                converted.append({"type": "input_image", "image_url": image_url, "detail": "auto"})
            continue

        if item_type in {"file", "input_file"}:
            file_value = item.get("file") if isinstance(item.get("file"), dict) else item
            if not isinstance(file_value, dict):
                continue

            filename = file_value.get("filename") or item.get("filename")
            file_data = file_value.get("file_data") or file_value.get("data")
            file_id = file_value.get("file_id") or item.get("file_id")
            if isinstance(file_data, str) and file_data:
                if not file_data.startswith("data:"):
                    mime_type = str(file_value.get("mime_type") or item.get("mime_type") or "application/octet-stream")
                    file_data = f"data:{mime_type};base64,{file_data}"
                block: dict[str, Any] = {"type": "input_file", "file_data": file_data}
                if isinstance(filename, str) and filename:
                    block["filename"] = filename
                converted.append(block)
            elif isinstance(file_id, str) and file_id:
                block = {"type": "input_file", "file_id": file_id}
                if isinstance(filename, str) and filename:
                    block["filename"] = filename
                converted.append(block)

    return converted


def _split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None


def _prompt_cache_key(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                data_lines = [l[5:].strip() for l in buffer if l.startswith("data:")]
                buffer = []
                if not data_lines:
                    continue
                data = "\n".join(data_lines).strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except Exception:
                    continue
            continue
        buffer.append(line)


async def _consume_sse(
    response: httpx.Response,
    *,
    on_text_delta: Any = None,
) -> tuple[str, list[ToolCallRequest], str, dict[str, int], list[dict[str, Any]]]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    finish_reason = "stop"
    usage: dict[str, int] = {}
    reasoning_items: list[dict[str, Any]] = []

    async for event in _iter_sse(response):
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                tool_call_buffers[call_id] = {
                    "id": item.get("id") or "fc_0",
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "",
                }
        elif event_type == "response.output_text.delta":
            delta_text = str(event.get("delta") or "")
            content += delta_text
            if delta_text and callable(on_text_delta):
                callback_result = on_text_delta(delta_text)
                if inspect.isawaitable(callback_result):
                    await callback_result
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] = event.get("arguments") or ""
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "reasoning":
                stored = next(
                    (entry for entry in reasoning_items if entry.get("id") == item.get("id")),
                    None,
                )
                if stored is None:
                    reasoning_items.append(dict(item))
                elif item.get("encrypted_content") and not stored.get("encrypted_content"):
                    # 密文只在带 include 的那一拍出现，早到的空壳条目就地补齐。
                    stored["encrypted_content"] = item["encrypted_content"]
                continue
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                buf = tool_call_buffers.get(call_id) or {}
                args_raw = buf.get("arguments") or item.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {"raw": args_raw}
                tool_calls.append(
                    ToolCallRequest(
                        id=f"{call_id}|{buf.get('id') or item.get('id') or 'fc_0'}",
                        name=buf.get("name") or item.get("name"),
                        arguments=args,
                    )
                )
        elif event_type == "response.completed":
            # 终止事件是这一条协议车道唯一的"说完了"凭据：usage 只在这里出现，
            # finish_reason 也只在这里被覆盖。把它同时记进流式诊断，未收到终止事件
            # 的回包才可能被上层当成传输故障（而不是伪装成一次正常 stop）。
            note_terminal = getattr(response, "note_terminal_event", None)
            if callable(note_terminal):
                note_terminal()
            response_payload = event.get("response") or {}
            status = response_payload.get("status")
            finish_reason = _map_finish_reason(status)
            usage = normalize_usage_payload(response_payload.get("usage") or event.get("usage"))
            response_model = str(response_payload.get("model") or "").strip()
            for output_item in list(response_payload.get("output") or []):
                if not isinstance(output_item, dict) or output_item.get("type") != "reasoning":
                    continue
                stored = next(
                    (entry for entry in reasoning_items if entry.get("id") == output_item.get("id")),
                    None,
                )
                if stored is None:
                    reasoning_items.append(dict(output_item))
                else:
                    # 终止事件带的是权威完整项（含 summary / encrypted_content），逐键补齐早到的分片。
                    for key, value in output_item.items():
                        if value not in (None, "", [], {}):
                            stored[key] = value
            for stored in reasoning_items:
                # 密文绑模型：戳在终止事件这一步才打得起来，没有它就没有重放资格。
                if response_model:
                    stored["g3ku_reasoning_model"] = response_model
        elif event_type in {"error", "response.failed"}:
            summary, full_body, error_status, error_code = _codex_failure_summary(event)
            raise CodexStreamError(
                summary or "provider stream failed without an error body",
                partial_content=content,
                error_body=full_body,
                error_status=error_status,
                error_code=error_code,
            )

    return content, tool_calls, finish_reason, usage, reasoning_items


def _replayable_reasoning_items(raw_items: Any, model: str | None) -> list[dict[str, Any]]:
    """把行上存的加密 reasoning 项还原成可回放的 input 项。

    只回给产生它的那个模型：密文由签发模型的服务端密钥加密，换个模型解不开，会被当成畸形项。
    戳（`g3ku_reasoning_model`）是我们的记账字段，回放前摘掉。
    """
    items: list[dict[str, Any]] = []
    for raw in list(raw_items or []):
        if not isinstance(raw, dict):
            continue
        source_model = str(raw.get("g3ku_reasoning_model") or "").strip()
        if not source_model:
            continue
        if model and source_model != str(model).strip():
            continue
        item = {key: value for key, value in raw.items() if key != "g3ku_reasoning_model"}
        if item.get("type") != "reasoning":
            continue
        if not (item.get("encrypted_content") or item.get("summary")):
            continue
        items.append(item)
    return items


_FINISH_REASON_MAP = {"completed": "stop", "incomplete": "length", "failed": "error", "cancelled": "error"}


def _map_finish_reason(status: str | None) -> str:
    return _FINISH_REASON_MAP.get(status or "completed", "stop")


def _friendly_error(status_code: int, raw: str) -> str:
    """把上游响应体压成一行可读文本，但**不改写语义**：状态码原样带上，供应商说什么就是什么。

    这里过去对 429/5xx 各写了一句我们自己的解释文案（429 那句还写着 ChatGPT/Codex），
    结果是：判定层拿我们自己的散文当关键词来源，展示层把实际供应商名字写错。
    """
    detail = _summarize_error_payload(raw)
    if detail:
        return f"HTTP {status_code}: {detail}"
    return f"HTTP {status_code}"


def _summarize_error_payload(raw: str) -> str:
    payload = str(raw or "").strip()
    if not payload:
        return ""

    try:
        parsed = json.loads(payload)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "code", "type"):
                value = str(error.get(key) or "").strip()
                if value:
                    return _truncate_error_text(value)
        for key in ("message", "detail", "error"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_error_text(value)

    lowered = payload.lower()
    if "<!doctype html" in lowered or "<html" in lowered:
        title_match = re.search(r"<title>(.*?)</title>", payload, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            title = html.unescape(title_match.group(1))
            title = re.sub(r"\s+", " ", title).strip(" :-")
            if title:
                return _truncate_error_text(title)
        if "bad gateway" in lowered:
            return "Bad gateway"
        return "HTML error page returned by upstream gateway"

    return _truncate_error_text(re.sub(r"\s+", " ", payload))


def _truncate_error_text(text: str, limit: int = 180) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
