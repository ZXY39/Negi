from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from loguru import logger

from g3ku.config.schema import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_REASONING_EFFORT,
    Config,
    normalize_reasoning_effort,
)
from g3ku.json_schema_utils import normalize_openai_tool_definitions
from g3ku.prompt_trace import render_model_chain_trace
from g3ku.providers.provider_factory import build_provider_from_model_key
from g3ku.providers.base import LLMModelAttempt, LLMResponse, normalize_usage_payload
from g3ku.providers.fallback import (
    DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
    DEFAULT_RETRYABLE_MODEL_ROUNDS,
    current_runtime_config_revision,
    exception_chain_display_text,
    exhausted_model_chain_error,
    is_request_shape_error,
    is_retryable_model_error,
    model_retry_backoff_seconds,
    normalize_request_timeout_seconds,
    normalized_retry_count,
    response_requires_retry,
    response_requires_fallback,
    retryable_chain_config_changed_error,
    sanitize_terminal_model_error,
    should_fallback_model_error,
    wait_for_model_attempt,
)
from g3ku.runtime.stage_prompt_compaction import (
    STAGE_COMPACT_PREFIX as _STAGE_COMPACT_PREFIX,
    STAGE_EXTERNALIZED_PREFIX as _STAGE_EXTERNALIZED_PREFIX,
)
from g3ku.utils.api_keys import iter_api_key_retry_slots
from main.runtime.send_token_preflight import estimate_runtime_provider_request_preview_tokens
from main.runtime.model_key_concurrency import ModelKeyConcurrencyController, ModelKeyPermitLease
from main.runtime.node_turn_controller import NodeTurnLease
_MISSING = object()
# 前端折叠态只做视觉截断、点击展开需要全文，因此这里仅做防超大 payload 的宽上限。
_MODEL_RETRY_STATUS_ERROR_CHAR_LIMIT = 4096


def _model_retry_status_error_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= _MODEL_RETRY_STATUS_ERROR_CHAR_LIMIT:
        return text
    return f"{text[:_MODEL_RETRY_STATUS_ERROR_CHAR_LIMIT - 3].rstrip()}..."


class ChatBackend(Protocol):
    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        model_refs: list[str],
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        parallel_tool_calls: bool | None = None,
        prompt_cache_key: str | None = None,
        node_turn_lease: NodeTurnLease | None = None,
        model_concurrency_controller: ModelKeyConcurrencyController | None = None,
        on_text_delta: Any = None,
        model_refs_resolver: Any = None,
        single_request_timeout_seconds: float | None = None,
        on_model_retry_status: Any = None,
    ) -> LLMResponse: ...


@dataclass(frozen=True, slots=True)
class SendModelContextWindowInfo:
    model_key: str
    provider_id: str
    provider_model: str
    resolved_model: str
    context_window_tokens: int
    resolution_error: str = ""


def resolve_send_model_context_window_info(
    *,
    config: Config,
    model_refs: list[str] | None,
) -> SendModelContextWindowInfo:
    refs = [
        str(item or "").strip()
        for item in list(model_refs or [])
        if str(item or "").strip()
    ]
    model_key = refs[0] if refs else ""
    if not model_key:
        return SendModelContextWindowInfo(
            model_key="",
            provider_id="",
            provider_model="",
            resolved_model="",
            context_window_tokens=0,
            resolution_error="model_refs_empty",
        )
    try:
        target = build_provider_from_model_key(config, model_key)
    except Exception as exc:
        return SendModelContextWindowInfo(
            model_key=model_key,
            provider_id="",
            provider_model="",
            resolved_model="",
            context_window_tokens=0,
            resolution_error=str(exc or exc.__class__.__name__).strip() or exc.__class__.__name__,
        )
    raw = dict(getattr(target, "model_parameters", {}) or {}).get("context_window_tokens")
    try:
        context_window_tokens = int(raw or 0)
    except (TypeError, ValueError):
        context_window_tokens = 0
    provider_id = str(getattr(target, "provider_id", "") or "").strip()
    resolved_model = str(getattr(target, "model_id", "") or "").strip()
    provider_model = f"{provider_id}:{resolved_model}" if provider_id and resolved_model else model_key
    resolution_error = "" if context_window_tokens > 0 else "context_window_tokens_missing"
    return SendModelContextWindowInfo(
        model_key=model_key,
        provider_id=provider_id,
        provider_model=provider_model,
        resolved_model=resolved_model,
        context_window_tokens=max(0, int(context_window_tokens or 0)),
        resolution_error=resolution_error,
    )


def resolve_send_model_context_window_tokens(
    *,
    config: Config,
    model_refs: list[str] | None,
) -> int:
    """
    Runtime-authoritative context-window resolver for node/runtime sends.

    This deliberately goes through the managed config path (`resolve_chat_target` via
    `build_provider_from_model_key`) rather than role defaults or hard-coded heuristics.
    """

    info = resolve_send_model_context_window_info(config=config, model_refs=model_refs)
    return int(info.context_window_tokens or 0)


def build_send_provider_request_preview(
    *,
    config: Config,
    messages: list[dict],
    tools: list[dict] | None,
    model_refs: list[str],
    tool_choice: str | dict[str, Any] | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    parallel_tool_calls: bool | None = None,
    prompt_cache_key: str | None = None,
) -> dict[str, Any]:
    """
    Shared provider-request preview surface for node/runtime sends.

    This mirrors the arguments that eventually reach the provider `chat(...)` method, but it is
    side-effect free and suitable for token estimation / preflight decisions.
    """

    refs = [str(item or "").strip() for item in list(model_refs or []) if str(item or "").strip()]
    if not refs:
        raise ValueError("model_refs must not be empty")
    target = build_provider_from_model_key(config, refs[0])
    request_messages = sanitize_provider_messages(messages)
    normalized_tools = normalize_openai_tool_definitions(tools)
    stable_prompt_cache_key = str(
        prompt_cache_key
        or build_stable_prompt_cache_key(
            request_messages,
            normalized_tools,
            str(getattr(target, "model_id", "") or ""),
        )
    ).strip()
    return {
        "messages": request_messages,
        "tools": normalized_tools or None,
        "model": str(getattr(target, "model_id", "") or ""),
        "tool_choice": tool_choice if tool_choice is not None else "auto",
        "parallel_tool_calls": parallel_tool_calls,
        "prompt_cache_key": stable_prompt_cache_key or None,
        **_resolve_model_request_parameters(
            target,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        ),
    }


def estimate_send_provider_request_preview_tokens(*, preview_payload: dict[str, Any] | None) -> int:
    """
    Token-estimation helper for send-side request previews.
    """

    payload = dict(preview_payload or {})
    if not payload:
        return 0
    return estimate_runtime_provider_request_preview_tokens(
        provider_request_body=payload,
        request_messages=[
            dict(item)
            for item in list(payload.get("messages") or [])
            if isinstance(item, dict)
        ],
        tool_schemas=[
            dict(item)
            for item in list(payload.get("tools") or [])
            if isinstance(item, dict)
        ],
    )


def _json_compact(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _message_content_signature(message: dict) -> str:
    content = message.get('content')
    if isinstance(content, str):
        return content
    return _json_compact(content)


def _dynamic_appendix_hash(messages: list[dict[str, Any]] | None) -> str:
    normalized = sanitize_provider_messages(messages)
    if not normalized:
        return ''
    return hashlib.sha256(_json_compact(normalized).encode('utf-8')).hexdigest()


def _request_messages_hash(messages: list[dict[str, Any]] | None) -> str:
    normalized = sanitize_provider_messages(messages)
    if not normalized:
        return ''
    return hashlib.sha256(_json_compact(normalized).encode('utf-8')).hexdigest()


def _normalize_provider_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in list(tool_calls or []):
        if not isinstance(item, dict):
            continue
        function = item.get('function') if isinstance(item.get('function'), dict) else {}
        name = str(function.get('name') or item.get('name') or '').strip()
        arguments = _MISSING
        for container in (function, item):
            if not isinstance(container, dict):
                continue
            if 'arguments' in container:
                arguments = container.get('arguments')
                break
            if 'args' in container:
                arguments = container.get('args')
                break
        if arguments is _MISSING or arguments is None or not isinstance(arguments, dict | str):
            arguments = {}
        call_id = str(item.get('id') or '').strip()
        payload: dict[str, Any] = {
            'type': 'function',
            'function': {
                'name': name,
                'arguments': arguments,
            },
        }
        if call_id:
            payload['id'] = call_id
        normalized.append(payload)
    return normalized


def sanitize_provider_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for item in list(messages or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get('role') or '').strip().lower()
        if role not in {'system', 'user', 'assistant', 'tool'}:
            continue
        payload: dict[str, Any] = {'role': role}
        content = item.get('content')
        if role in {'system', 'user'}:
            payload['content'] = content
            name = str(item.get('name') or '').strip()
            if name:
                payload['name'] = name
            sanitized.append(payload)
            continue
        if role == 'assistant':
            payload['content'] = content
            tool_calls = _normalize_provider_tool_calls(item.get('tool_calls'))
            if tool_calls:
                payload['tool_calls'] = tool_calls
            function_call = item.get('function_call')
            if isinstance(function_call, dict):
                function_name = str(function_call.get('name') or '').strip()
                if function_name:
                    payload['function_call'] = {
                        'name': function_name,
                        'arguments': function_call.get('arguments'),
                    }
            name = str(item.get('name') or '').strip()
            if name:
                payload['name'] = name
            sanitized.append(payload)
            continue
        payload['content'] = content
        tool_call_id = str(item.get('tool_call_id') or '').strip()
        if tool_call_id:
            payload['tool_call_id'] = tool_call_id
        name = str(item.get('name') or '').strip()
        if name:
            payload['name'] = name
        sanitized.append(payload)
    return sanitized


def _tool_signature(tools: list[dict] | None) -> list[dict[str, object]]:
    signatures: list[dict[str, object]] = []
    for item in normalize_openai_tool_definitions(tools):
        function = item.get('function') if isinstance(item.get('function'), dict) else {}
        if not isinstance(function, dict):
            continue
        signatures.append(
            {
                'name': str(function.get('name') or '').strip(),
                'description': str(function.get('description') or '').strip(),
                'parameters': function.get('parameters') if isinstance(function.get('parameters'), dict) else {},
            }
        )
    signatures.sort(key=lambda value: (str(value.get('name') or ''), _json_compact(value.get('parameters') or {})))
    return signatures


def _tool_signature_hash(tools: list[dict] | None) -> str:
    tool_signatures = _tool_signature(tools)
    if not tool_signatures:
        return ''
    return hashlib.sha256(_json_compact(tool_signatures).encode('utf-8')).hexdigest()


def _normalize_model_attempts(attempts: list[LLMModelAttempt] | None) -> list[LLMModelAttempt]:
    normalized_attempts: list[LLMModelAttempt] = []
    for attempt in list(attempts or []):
        normalized_attempts.append(
            LLMModelAttempt(
                model_key=str(getattr(attempt, 'model_key', '') or '').strip(),
                provider_id=str(getattr(attempt, 'provider_id', '') or '').strip(),
                provider_model=str(getattr(attempt, 'provider_model', '') or '').strip(),
                usage=normalize_usage_payload(getattr(attempt, 'usage', None)),
                finish_reason=str(getattr(attempt, 'finish_reason', 'stop') or 'stop'),
            )
        )
    return normalized_attempts


def build_actual_request_diagnostics(
    *,
    request_messages: list[dict[str, Any]] | None,
    tool_schemas: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    normalized_request_messages = sanitize_provider_messages(request_messages)
    return {
        'actual_request_hash': _request_messages_hash(normalized_request_messages),
        'actual_request_message_count': len(normalized_request_messages),
        'actual_tool_schema_hash': _tool_signature_hash(tool_schemas),
    }


def _stage_context_digest(messages: list[dict]) -> str:
    found = False
    digest = hashlib.sha256()
    for message in list(messages or []):
        if str(message.get('role') or '').strip().lower() != 'assistant':
            continue
        content = str(message.get('content') or '')
        if not (
            content.startswith(_STAGE_COMPACT_PREFIX)
            or content.startswith(_STAGE_EXTERNALIZED_PREFIX)
        ):
            continue
        found = True
        digest.update(content.encode('utf-8'))
    return digest.hexdigest() if found else ''


def build_stable_prompt_cache_key(messages: list[dict], tools: list[dict] | None, provider_model: str) -> str:
    _ = tools
    system_prompt = ''
    bootstrap_user = ''
    for message in list(messages or []):
        role = str(message.get('role') or '').strip().lower()
        if role == 'system' and not system_prompt:
            system_prompt = _message_content_signature(message)
            continue
        if role == 'user' and not bootstrap_user:
            bootstrap_user = _message_content_signature(message)
            break
    payload = {
        'system': system_prompt,
        'bootstrap_user': bootstrap_user,
        'provider_model': str(provider_model or '').strip(),
        'stage_context_digest': _stage_context_digest(messages),
    }
    return hashlib.sha256(_json_compact(payload).encode('utf-8')).hexdigest()


def build_prompt_cache_diagnostics(
    *,
    stable_messages: list[dict] | None,
    dynamic_appendix_messages: list[dict] | None = None,
    tool_schemas: list[dict] | None,
    provider_model: str,
    scope: str,
    prompt_cache_key: str | None = None,
    overlay_text: str | None = None,
    overlay_section_count: int | None = None,
    cache_family_revision: str | None = None,
    stable_prefix_hash: str | None = None,
    dynamic_appendix_hash: str | None = None,
    prompt_lane: str | None = None,
    prefix_invalidation_reason: str | None = None,
    actual_request_messages: list[dict] | None = None,
    actual_tool_schemas: list[dict] | None = None,
) -> dict[str, object]:
    normalized_messages = list(stable_messages or [])
    normalized_tools = list(tool_schemas or []) or None
    normalized_overlay = str(overlay_text or '').strip()
    normalized_overlay_sections = [
        section.strip()
        for section in normalized_overlay.split('\n\n')
        if section.strip()
    ]
    normalized_dynamic_messages = sanitize_provider_messages(dynamic_appendix_messages)
    if not normalized_dynamic_messages and normalized_overlay:
        normalized_dynamic_messages = [
            {
                'role': 'assistant',
                'content': normalized_overlay,
            }
        ]
    tool_signatures = _tool_signature(normalized_tools)
    resolved_stable_prefix_hash = str(stable_prefix_hash or '').strip() or build_stable_prompt_cache_key(
        normalized_messages,
        normalized_tools,
        str(provider_model or '').strip(),
    )
    resolved_dynamic_appendix_hash = (
        str(dynamic_appendix_hash or '').strip()
        or _dynamic_appendix_hash(normalized_dynamic_messages)
    )
    normalized_actual_request_messages = sanitize_provider_messages(
        actual_request_messages
        if actual_request_messages is not None
        else [*normalized_messages, *normalized_dynamic_messages]
    )
    normalized_actual_tool_schemas = list(actual_tool_schemas or normalized_tools or []) or None
    tool_signature_hash = _tool_signature_hash(normalized_tools)
    actual_request_diagnostics = build_actual_request_diagnostics(
        request_messages=normalized_actual_request_messages,
        tool_schemas=normalized_actual_tool_schemas,
    )
    return {
        'scope': str(scope or '').strip(),
        'prompt_lane': str(prompt_lane or scope or '').strip(),
        'provider_model': str(provider_model or '').strip(),
        'cache_family_revision': str(cache_family_revision or '').strip(),
        'prefix_invalidation_reason': str(prefix_invalidation_reason or '').strip(),
        'stable_prompt_signature': resolved_stable_prefix_hash,
        'stable_prefix_hash': resolved_stable_prefix_hash,
        'dynamic_appendix_hash': resolved_dynamic_appendix_hash,
        'stable_prefix_message_count': len(normalized_messages),
        'dynamic_appendix_message_count': len(normalized_dynamic_messages),
        'tool_signature_count': len(tool_signatures),
        'tool_signature_hash': tool_signature_hash,
        'overlay_present': bool(normalized_overlay),
        'overlay_section_count': max(
            len(normalized_overlay_sections),
            max(0, int(overlay_section_count or 0)),
        ),
        'overlay_text_hash': (
            hashlib.sha256(normalized_overlay.encode('utf-8')).hexdigest()
            if normalized_overlay
            else ''
        ),
        'prompt_cache_key_hash': (
            hashlib.sha256(str(prompt_cache_key or '').encode('utf-8')).hexdigest()
            if str(prompt_cache_key or '').strip()
            else ''
        ),
        **actual_request_diagnostics,
    }


def build_session_prompt_cache_key(
    *,
    session_key: str,
    provider_model: str,
    scope: str = 'chat',
    stable_messages: list[dict] | None = None,
    tool_schemas: list[dict] | None = None,
    cache_family_revision: str | None = None,
) -> str:
    payload = {
        'scope': str(scope or '').strip() or 'chat',
        'session_key': str(session_key or '').strip(),
        'provider_model': str(provider_model or '').strip(),
        'cache_family_revision': str(cache_family_revision or '').strip(),
    }
    if stable_messages is not None or tool_schemas is not None:
        payload['stable_prompt_signature'] = build_stable_prompt_cache_key(
            list(stable_messages or []),
            list(tool_schemas or []) or None,
            str(provider_model or '').strip(),
        )
    return hashlib.sha256(_json_compact(payload).encode('utf-8')).hexdigest()


def _message_stats(messages: list[dict]) -> tuple[int, int]:
    message_list = list(messages or [])
    try:
        payload = json.dumps(message_list, ensure_ascii=False, default=str)
    except Exception:
        payload = str(message_list)
    return len(message_list), len(payload)


def _resolve_model_request_parameters(
    target,
    *,
    max_tokens: int | None,
    temperature: float | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    configured = dict(getattr(target, 'model_parameters', {}) or {})
    if configured.get('max_tokens') is None and getattr(target, 'max_tokens_limit', None) is not None:
        configured['max_tokens'] = getattr(target, 'max_tokens_limit', None)
    if configured.get('temperature') is None and getattr(target, 'default_temperature', None) is not None:
        configured['temperature'] = getattr(target, 'default_temperature', None)
    if not str(configured.get('reasoning_effort') or '').strip() and getattr(target, 'default_reasoning_effort', None) is not None:
        configured['reasoning_effort'] = getattr(target, 'default_reasoning_effort', None)
    resolved: dict[str, Any] = {}
    if max_tokens is not None:
        resolved['max_tokens'] = max(1, int(max_tokens))
    elif configured.get('max_tokens') is not None:
        resolved['max_tokens'] = max(1, int(configured['max_tokens']))
    else:
        resolved['max_tokens'] = max(1, int(DEFAULT_MAX_OUTPUT_TOKENS))
    if temperature is not None:
        resolved['temperature'] = float(temperature)
    elif configured.get('temperature') is not None:
        resolved['temperature'] = float(configured['temperature'])
    explicit_reasoning = normalize_reasoning_effort(reasoning_effort) if str(reasoning_effort or '').strip() else ''
    if explicit_reasoning:
        resolved['reasoning_effort'] = explicit_reasoning
    else:
        configured_reasoning = str(configured.get('reasoning_effort') or '').strip()
        if configured_reasoning:
            resolved['reasoning_effort'] = normalize_reasoning_effort(configured_reasoning)
        else:
            resolved['reasoning_effort'] = DEFAULT_REASONING_EFFORT
    if str(resolved.get('reasoning_effort') or '').strip().lower() == 'none':
        resolved.pop('reasoning_effort', None)
    return resolved


def _build_provider_target_compat(config: Config, model_ref: str, *, api_key_index: int | None = None):
    if api_key_index is None:
        return build_provider_from_model_key(config, model_ref)
    try:
        parameters = inspect.signature(build_provider_from_model_key).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "api_key_index" in parameters:
        return build_provider_from_model_key(config, model_ref, api_key_index=api_key_index)
    return build_provider_from_model_key(config, model_ref)


def _model_chain_request_context_lines(messages: list[dict[str, Any]] | None) -> list[str]:
    for message in list(messages or []):
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        text = str(content or "").strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        task_id = str(payload.get("task_id") or "").strip()
        node_id = str(payload.get("node_id") or "").strip()
        goal = str(payload.get("goal") or "").strip()
        context_lines: list[str] = []
        if task_id or node_id:
            pair = " ".join(
                part
                for part in [
                    f"task_id={task_id}" if task_id else "",
                    f"node_id={node_id}" if node_id else "",
                ]
                if part
            ).strip()
            if pair:
                context_lines.append(pair)
        if goal:
            context_lines.append(f"goal: {goal}")
        return context_lines
    return []


def _log_model_chain_fallback(
    *,
    messages: list[dict[str, Any]] | None,
    model_ref: str,
    next_model_ref: str,
    reason: Any,
) -> None:
    lines = [
        *_model_chain_request_context_lines(messages),
        f"model_ref: {str(model_ref or '').strip()}",
        f"next_model_ref: {str(next_model_ref or '').strip()}",
        f"reason: {str(reason or '').strip()}",
    ]
    logger.warning(
        render_model_chain_trace(
            title="FALLBACK",
            severity="fallback",
            lines=lines,
        )
    )


class ConfigChatBackend:
    def __init__(self, config: Config):
        self._config = config
        self._model_attempt_timeout_seconds: float | None = DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS

    def _normalized_model_attempt_timeout_seconds(self) -> float | None:
        return normalize_request_timeout_seconds(getattr(self, "_model_attempt_timeout_seconds", None))

    def recommended_model_response_timeout_seconds(self, *, model_refs: list[str] | None = None) -> float | None:
        """Response-time limit for a single (one-round) provider request.

        No longer accumulated as "attempt timeout x attempts x chain rounds":
        retryable chain retries are unbounded and paced by backoff, so the only
        hard cap left is how long one provider request may take (10 minutes by
        default).
        """
        _ = model_refs
        return self._normalized_model_attempt_timeout_seconds()

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        model_refs: list[str],
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        parallel_tool_calls: bool | None = None,
        prompt_cache_key: str | None = None,
        node_turn_lease: NodeTurnLease | None = None,
        model_concurrency_controller: ModelKeyConcurrencyController | None = None,
        on_text_delta: Any = None,
        model_refs_resolver: Any = None,
        single_request_timeout_seconds: float | None = None,
        on_model_retry_status: Any = None,
    ) -> LLMResponse:
        refs = [str(item or '').strip() for item in list(model_refs or []) if str(item or '').strip()]
        if not refs:
            raise ValueError('model_refs must not be empty')
        request_attempt_timeout_seconds = normalize_request_timeout_seconds(
            single_request_timeout_seconds
            if single_request_timeout_seconds is not None
            else self._model_attempt_timeout_seconds
        )

        def _resolved_model_refs() -> list[str]:
            if not callable(model_refs_resolver):
                return []
            try:
                candidate = model_refs_resolver()
            except Exception:
                return []
            return [str(item or '').strip() for item in list(candidate or []) if str(item or '').strip()]

        last_error: Exception | None = None
        last_response: LLMResponse | None = None
        attempts: list[LLMModelAttempt] = []
        held_turn_lease = node_turn_lease
        start_revision = current_runtime_config_revision()
        retry_status_emitted = False
        # provider 实际请求次数（含 key 轮换/跨模型 fallback/可重试退避的每一发请求）。
        # 前端重试 toast 的 retry_count 用它，避免轮换/单模型轮次的真实请求被少计、
        # 低于实际打到 provider 的次数。
        provider_request_count = 0
        # 上一发请求的模型 ref：用于区分"同模型重发（轮换/可重试轮转）"与"跨模型 fallback"。
        # 只在同模型重发与退避重试时发 toast；跨模型 fallback 属链路正常工作，只计数不发。
        last_request_model_ref = ""
        # 退避重试已发过 status 后，抑制同模型下一发请求在咽喉点的重复发射
        # （退避那次已带"下次重试时间"，再发一发 delay=0 会把它盖掉）。
        suppress_next_request_retry_emission = False
        # 已耗尽预算/已试过的模型 ref：模型前进边界的链刷新后据此跳过，不回头重试。
        tried_model_refs: set[str] = set()

        async def _emit_model_retry_status(status: dict[str, Any]) -> None:
            nonlocal retry_status_emitted
            if not callable(on_model_retry_status):
                return
            if str(status.get("state") or "").strip() == "retrying":
                retry_status_emitted = True
            try:
                result = on_model_retry_status(dict(status))
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.debug("Model retry status callback failed")

        try:
            # 发送前先做一次活解析（与旧首轮行为一致）：拿到调用前刚发生的链变更。
            fresh_refs = _resolved_model_refs()
            if fresh_refs and fresh_refs != refs:
                logger.info(
                    "Model chain refreshed before first model: {} -> {}",
                    ", ".join(refs),
                    ", ".join(fresh_refs),
                )
                refs = fresh_refs
            model_index = 0
            while True:
                while model_index < len(refs) and refs[model_index] in tried_model_refs:
                    model_index += 1
                if model_index >= len(refs):
                    break
                ref = refs[model_index]
                tried_model_refs.add(ref)
                try:
                    base_target = build_provider_from_model_key(self._config, ref)
                except Exception as exc:
                    last_error = exc
                    if should_fallback_model_error(exc) and model_index < len(refs) - 1:
                        _log_model_chain_fallback(
                            messages=messages,
                            model_ref=ref,
                            next_model_ref=refs[model_index + 1],
                            reason=exc,
                        )
                        model_index += 1
                        continue
                    if should_fallback_model_error(exc):
                        profile = self._config.get_model_runtime_profile(ref)
                        raise exhausted_model_chain_error(
                            exc,
                            retry_on=list(profile.retry_on) if profile is not None else None,
                        ) from exc
                    raise
                configured_api_key_indexes = getattr(base_target, "api_key_indexes", None)
                if configured_api_key_indexes is None:
                    api_key_indexes = list(range(max(1, int(getattr(base_target, "api_key_count", 0) or 0))))
                else:
                    api_key_indexes = [int(item) for item in configured_api_key_indexes]
                if int(getattr(base_target, "api_key_count", 0) or 0) > 0 and not api_key_indexes:
                    raise RuntimeError(f"All configured API keys are disabled for model {ref}")
                # 本模型的可重试轮数预算：绑定 retry_count 即配置页「重试次数」，
                # 0/未设置用默认 DEFAULT_RETRYABLE_MODEL_ROUNDS。一轮 = 完整轮过该模型所有 key。
                budget_rounds = normalized_retry_count(getattr(base_target, "retry_count", 0)) or DEFAULT_RETRYABLE_MODEL_ROUNDS
                model_retry_on = list(getattr(base_target, "retry_on", []) or [])
                preview_payload = build_send_provider_request_preview(
                    config=self._config,
                    messages=messages,
                    tools=tools,
                    model_refs=[ref],
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    parallel_tool_calls=parallel_tool_calls,
                    prompt_cache_key=prompt_cache_key,
                )
                request_messages = sanitize_provider_messages(preview_payload.get("messages"))
                request_tools = list(preview_payload.get("tools") or []) or None
                stable_prompt_cache_key = str(preview_payload.get("prompt_cache_key") or "").strip()
                rounds_used = 0
                model_last_error: Exception | None = None
                model_last_response: LLMResponse | None = None
                model_last_failure_reason: Any = None
                advance_to_next_model = False
                while True:  # 本模型轮循环：一轮 = 完整轮过该模型所有 key
                    round_retryable_failed = False
                    # 单轮 = 每个 key 各试一次（retry_count=0 → 单趟 key 遍历）；
                    # 可重试错误的重跑由外层轮预算驱动，不走 slot 轮次。
                    for slot in iter_api_key_retry_slots(api_key_count=getattr(base_target, "api_key_count", 0), retry_count=0, key_indexes=api_key_indexes):
                        target = base_target
                        request_message_count, request_message_chars = _message_stats(request_messages)
                        permit_lease: ModelKeyPermitLease | None = None
                        attempt_visible_text_streamed = False
                        use_held_turn_permit = bool(
                            held_turn_lease is not None
                            and held_turn_lease.initial_model_permit is not None
                            and str(ref or '').strip() == str(held_turn_lease.model_ref or '').strip()
                            and int(slot.key_index) == int(held_turn_lease.key_index)
                            and int(slot.attempt_number) == 1
                        )
                        try:
                            selected_api_key_index = int(held_turn_lease.key_index) if use_held_turn_permit and held_turn_lease is not None else int(slot.key_index)
                            target = _build_provider_target_compat(
                                self._config,
                                ref,
                                api_key_index=selected_api_key_index,
                            )
                            attempt_timeout_seconds = request_attempt_timeout_seconds
                            if use_held_turn_permit and held_turn_lease is not None:
                                permit_lease = held_turn_lease.initial_model_permit
                                held_turn_lease.initial_model_permit = None
                            elif model_concurrency_controller is not None:
                                permit_lease = await model_concurrency_controller.acquire_specific(
                                    model_ref=target.provider_ref,
                                    key_index=selected_api_key_index,
                                )

                            def _provider_text_delta_callback(text: Any) -> Any:
                                nonlocal attempt_visible_text_streamed
                                normalized_text = str(text or "")
                                if not normalized_text:
                                    return None
                                attempt_visible_text_streamed = True
                                if on_text_delta is None:
                                    return None
                                return on_text_delta(normalized_text)

                            provider_kwargs = {
                                **dict(preview_payload or {}),
                                'messages': request_messages,
                                'tools': request_tools,
                                'model': target.model_id,
                                'request_timeout_seconds': attempt_timeout_seconds,
                            }
                            if on_text_delta is not None and bool(getattr(target.provider, 'supports_streaming', False)):
                                provider_kwargs['on_text_delta'] = _provider_text_delta_callback
                            outer_attempt_timeout_seconds = None if bool(getattr(target.provider, 'manages_request_timeout_internally', False)) else attempt_timeout_seconds
                            # 咽喉点统计真实请求次数：除第一发外的每次请求都是一次重试。
                            # 同模型重发（轮换/可重试轮转）在此发 retrying status；跨模型
                            # fallback 只计数不发（属链路正常工作）；退避重试已在退避分支
                            # 发过、此处按 suppress 跳过。
                            current_model_ref = str(ref or '').strip()
                            if provider_request_count >= 1:
                                if suppress_next_request_retry_emission:
                                    suppress_next_request_retry_emission = False
                                elif current_model_ref == last_request_model_ref:
                                    await _emit_model_retry_status(
                                        {
                                            "state": "retrying",
                                            "retry_count": provider_request_count,
                                            "chain_round": rounds_used + 1,
                                            "error_message": _model_retry_status_error_text(
                                                str(model_last_failure_reason or "")
                                            ),
                                            "model_refs": list(refs),
                                            "delay_seconds": 0.0,
                                            "last_retry_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                            "next_retry_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                        }
                                    )
                            provider_request_count += 1
                            last_request_model_ref = current_model_ref
                            response = await wait_for_model_attempt(
                                target.provider.chat(
                                    **provider_kwargs,
                                ),
                                timeout_seconds=outer_attempt_timeout_seconds,
                                model_ref=str(getattr(target, "provider_ref", ref) or ref),
                                provider_id=str(getattr(target, "provider_id", "") or ""),
                                provider_model=str(getattr(target, "model_id", "") or ""),
                                key_index=selected_api_key_index,
                            )
                        except Exception as exc:
                            last_error = model_last_error = exc
                            model_last_response = None
                            model_last_failure_reason = exception_chain_display_text(exc)
                            if attempt_visible_text_streamed:
                                raise
                            if is_request_shape_error(exc):
                                # 请求形状错误（结构化 400/422）：换 key 修不了畸形
                                # payload，直接切下一模型，不消耗重试轮。
                                advance_to_next_model = True
                                break
                            if not should_fallback_model_error(exc):
                                raise  # 内部运行时错误 / API key 配置错误直接上抛
                            if is_retryable_model_error(exc, retry_on=target.retry_on):
                                round_retryable_failed = True
                                continue  # 轮内继续轮过该模型下一个 key
                            continue  # 非可重试：每个 key 各试一次，轮完切下一模型
                        finally:
                            if permit_lease is not None and model_concurrency_controller is not None:
                                model_concurrency_controller.release(permit_lease)
                        response.usage = normalize_usage_payload(response.usage)
                        response.request_message_count = request_message_count
                        response.request_message_chars = request_message_chars
                        response_attempts = _normalize_model_attempts(response.attempts)
                        if not response_attempts:
                            response_attempts = [
                                LLMModelAttempt(
                                    model_key=target.provider_ref,
                                    provider_id=target.provider_id,
                                    provider_model=target.model_id,
                                    usage=dict(response.usage or {}),
                                    finish_reason=str(response.finish_reason or 'stop'),
                                )
                            ]
                        attempts.extend(response_attempts)
                        response.attempts = list(attempts)
                        response.visible_text_streamed = bool(
                            getattr(response, 'visible_text_streamed', False) or attempt_visible_text_streamed
                        )
                        last_response = response
                        retryable_response = response_requires_retry(response, retry_on=target.retry_on)
                        fallback_response = response_requires_fallback(response)
                        if not fallback_response:
                            return response  # 成功终态（或内部错误响应按原样返回）
                        if response.visible_text_streamed:
                            return response  # 已出现可见流式文本：不做透明重试/回退
                        model_last_response = response
                        model_last_error = None
                        model_last_failure_reason = str(response.error_text or response.content or response.finish_reason or "")
                        if is_request_shape_error(response):
                            advance_to_next_model = True
                            break
                        if retryable_response:
                            round_retryable_failed = True
                        continue  # 轮内继续轮过该模型下一个 key
                    if advance_to_next_model:
                        break
                    rounds_used += 1
                    if round_retryable_failed and rounds_used < budget_rounds:
                        if current_runtime_config_revision() != start_revision:
                            raise retryable_chain_config_changed_error() from model_last_error
                        delay_seconds = model_retry_backoff_seconds(rounds_used)
                        logger.warning(
                            "Retryable model failure for {} (round {}/{}); retrying in {:.1f}s: {}",
                            ref,
                            rounds_used,
                            budget_rounds,
                            delay_seconds,
                            model_last_failure_reason,
                        )
                        # retry_count 用 provider 实际请求次数（含轮换/跨模型），避免少报；
                        # 并 suppress 新一轮第一发在咽喉点的重复发射。
                        suppress_next_request_retry_emission = True
                        await _emit_model_retry_status(
                            {
                                "state": "retrying",
                                "retry_count": provider_request_count,
                                "chain_round": rounds_used,
                                "error_message": _model_retry_status_error_text(str(model_last_failure_reason or "")),
                                "model_refs": list(refs),
                                "delay_seconds": float(delay_seconds or 0.0),
                                # 绝对时刻（本地带偏移），供前端 toast 显示"最新重试时间/下次重试时间"。
                                "last_retry_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "next_retry_at": (
                                    datetime.now().astimezone() + timedelta(seconds=float(delay_seconds or 0.0))
                                ).isoformat(timespec="seconds"),
                            }
                        )
                        await asyncio.sleep(delay_seconds)
                        continue  # 同模型重跑一轮
                    break  # 预算耗尽或本轮无可重试失败 → 本模型耗尽
                # 本模型耗尽：在模型前进边界活刷新链（运行中新增的 fallback 模型
                # 在此可见），跳过已试模型后决定前进还是落终态。
                model_index += 1
                fresh_refs = _resolved_model_refs()
                if fresh_refs and fresh_refs != refs:
                    logger.info(
                        "Model chain refreshed at model boundary: {} -> {}",
                        ", ".join(refs),
                        ", ".join(fresh_refs),
                    )
                    refs = fresh_refs
                    model_index = 0
                while model_index < len(refs) and refs[model_index] in tried_model_refs:
                    model_index += 1
                if model_index < len(refs):
                    _log_model_chain_fallback(
                        messages=messages,
                        model_ref=ref,
                        next_model_ref=refs[model_index],
                        reason=(
                            model_last_failure_reason
                            if model_last_failure_reason is not None
                            else (model_last_error or "")
                        ),
                    )
                    continue
                # 全链（含刷新新增模型）都已耗尽：落终态。
                if model_last_error is not None:
                    if should_fallback_model_error(model_last_error):
                        raise exhausted_model_chain_error(model_last_error, retry_on=model_retry_on) from model_last_error
                    raise model_last_error
                if model_last_response is not None:
                    model_last_response.attempts = list(attempts)
                    return sanitize_terminal_model_error(model_last_response)
                raise RuntimeError('chat backend returned no response')
            if last_error is not None:
                if should_fallback_model_error(last_error):
                    raise exhausted_model_chain_error(last_error) from last_error
                raise last_error
            if last_response is None:
                raise RuntimeError('chat backend returned no response')
            last_response.attempts = list(attempts)
            return sanitize_terminal_model_error(last_response)
        finally:
            if retry_status_emitted:
                await _emit_model_retry_status({"state": "cleared"})
            if held_turn_lease is not None and held_turn_lease.initial_model_permit is not None and model_concurrency_controller is not None:
                model_concurrency_controller.release(held_turn_lease.initial_model_permit)
                held_turn_lease.initial_model_permit = None
