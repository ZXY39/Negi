"""OpenAI Codex Responses Provider."""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
from typing import Any, AsyncGenerator

import httpx
from loguru import logger
from oauth_cli_kit import get_token as get_codex_token

from g3ku.providers.base import LLMProvider, LLMResponse, ToolCallRequest, normalize_usage_payload

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "g3ku"


class CodexStreamError(RuntimeError):
    def __init__(self, message: str, *, partial_content: str = "") -> None:
        super().__init__(message)
        self.partial_content = str(partial_content or "")


class OpenAICodexProvider(LLMProvider):
    """Use Codex OAuth to call the Responses API."""

    def __init__(self, default_model: str = "openai-codex/gpt-5.1-codex"):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
        prompt_cache_key: str | None = None,
    ) -> LLMResponse:
        model = model or self.default_model
        system_prompt, input_items = _convert_messages(messages)

        token = await asyncio.to_thread(get_codex_token)
        headers = _build_headers(token.account_id, token.access)

        body: dict[str, Any] = {
            "model": _strip_model_prefix(model),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": str(prompt_cache_key or _prompt_cache_key(messages)),
        }

        if tools:
            body["tools"] = _convert_tools(tools)
            body["tool_choice"] = tool_choice if tool_choice is not None else "auto"
            body["parallel_tool_calls"] = (
                bool(parallel_tool_calls) if parallel_tool_calls is not None else True
            )

        url = DEFAULT_CODEX_URL
        self._trace_request_payload(
            provider="openai_codex",
            endpoint=url,
            body=body,
        )

        try:
            try:
                content, tool_calls, finish_reason, usage = await _request_codex(url, headers, body, verify=True)
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                logger.warning("SSL certificate verification failed for Codex API; retrying with verify=False")
                content, tool_calls, finish_reason, usage = await _request_codex(url, headers, body, verify=False)
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=usage,
            )
        except Exception as e:
            partial_content = str(getattr(e, "partial_content", "") or "").strip()
            if partial_content:
                logger.warning("Codex stream failed after partial content; returning partial content for JSON recovery")
                return LLMResponse(
                    content=partial_content,
                    finish_reason="error",
                )
            return LLMResponse(
                content=f"Error calling Codex: {str(e)}",
                finish_reason="error",
            )

    def get_default_model(self) -> str:
        return self.default_model


def _strip_model_prefix(model: str) -> str:
    if model.startswith("openai-codex/") or model.startswith("openai_codex/"):
        return model.split("/", 1)[1]
    return model


def _build_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "g3ku (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
) -> tuple[str, list[ToolCallRequest], str, dict[str, int]]:
    async with httpx.AsyncClient(timeout=60.0, verify=verify) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(_friendly_error(response.status_code, text.decode("utf-8", "ignore")))
            return await _consume_sse(response)


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling schema to Codex flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or "",
            "parameters": params if isinstance(params, dict) else {},
        })
    return converted


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    messages = _sanitize_tool_call_history(messages)
    system_prompt = ""
    input_items: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(_convert_user_message(content))
            continue

        if role == "assistant":
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

    return system_prompt, input_items


def _sanitize_tool_call_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop incomplete tool-call turns that would break Responses API replay.

    LangGraph checkpoints can occasionally retain an assistant tool call without the
    matching tool output when a prior run was interrupted or failed mid-turn. The
    Responses API rejects such history. Preserve assistant text, but strip dangling
    tool calls so subsequent turns can continue.
    """
    completed_call_ids: set[str] = set()
    declared_call_ids: set[str] = set()

    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            for tool_call in msg.get("tool_calls", []) or []:
                call_id, _ = _split_tool_call_id(tool_call.get("id"))
                if call_id:
                    declared_call_ids.add(call_id)
            continue
        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
            if call_id:
                completed_call_ids.add(call_id)

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
            if updated.get("content") or updated.get("reasoning_content") or updated.get("thinking_blocks"):
                sanitized.append(updated)
            continue

        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
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


async def _consume_sse(response: httpx.Response) -> tuple[str, list[ToolCallRequest], str, dict[str, int]]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    finish_reason = "stop"
    usage: dict[str, int] = {}

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
            content += event.get("delta") or ""
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
            response_payload = event.get("response") or {}
            status = response_payload.get("status")
            finish_reason = _map_finish_reason(status)
            usage = normalize_usage_payload(response_payload.get("usage") or event.get("usage"))
        elif event_type in {"error", "response.failed"}:
            raise CodexStreamError("Codex response failed", partial_content=content)

    return content, tool_calls, finish_reason, usage


_FINISH_REASON_MAP = {"completed": "stop", "incomplete": "length", "failed": "error", "cancelled": "error"}


def _map_finish_reason(status: str | None) -> str:
    return _FINISH_REASON_MAP.get(status or "completed", "stop")


def _friendly_error(status_code: int, raw: str) -> str:
    if status_code == 429:
        return "ChatGPT usage quota exceeded or rate limit triggered. Please try again later."
    detail = _summarize_error_payload(raw)
    if status_code >= 500:
        if detail:
            return f"Upstream service temporarily unavailable (HTTP {status_code}: {detail}). Please retry shortly."
        return f"Upstream service temporarily unavailable (HTTP {status_code}). Please retry shortly."
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

