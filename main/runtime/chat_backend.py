from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

from g3ku.config.schema import Config
from g3ku.json_schema_utils import normalize_openai_tool_definitions
from g3ku.providers.provider_factory import build_provider_from_model_key
from g3ku.providers.base import LLMModelAttempt, LLMResponse, normalize_usage_payload
from g3ku.providers.fallback import (
    DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
    RETRYABLE_MODEL_CHAIN_MAX_ROUNDS,
    exhausted_model_chain_error,
    normalize_request_timeout_seconds,
    normalized_retry_count,
    response_requires_api_key_rotation,
    response_requires_retry,
    response_requires_fallback,
    sanitize_terminal_model_error,
    should_rotate_api_key_error,
    should_fallback_model_error,
    should_retry_model_chain_error,
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
_MODEL_CHAIN_HARD_TIMEOUT_SAFETY_SECONDS = 15.0
_MISSING = object()


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
    if temperature is not None:
        resolved['temperature'] = float(temperature)
    elif configured.get('temperature') is not None:
        resolved['temperature'] = float(configured['temperature'])
    explicit_reasoning = str(reasoning_effort or '').strip()
    if explicit_reasoning:
        resolved['reasoning_effort'] = explicit_reasoning
    else:
        configured_reasoning = str(configured.get('reasoning_effort') or '').strip()
        if configured_reasoning:
            resolved['reasoning_effort'] = configured_reasoning
    return resolved


class ConfigChatBackend:
    def __init__(self, config: Config):
        self._config = config
        self._model_attempt_timeout_seconds: float | None = DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS

    def _normalized_model_attempt_timeout_seconds(self) -> float | None:
        return normalize_request_timeout_seconds(getattr(self, "_model_attempt_timeout_seconds", None))

    def _estimated_attempt_count_for_model_ref(self, model_ref: str) -> int:
        normalized_ref = str(model_ref or "").strip()
        if not normalized_ref:
            return 1
        try:
            target = build_provider_from_model_key(self._config, normalized_ref)
        except Exception:
            return 1
        configured_api_key_indexes = getattr(target, "api_key_indexes", None)
        if configured_api_key_indexes is None:
            enabled_key_count = max(1, int(getattr(target, "api_key_count", 0) or 0))
        else:
            enabled_key_count = len([int(item) for item in configured_api_key_indexes])
            if enabled_key_count <= 0:
                enabled_key_count = 1
        retry_count = normalized_retry_count(getattr(target, "retry_count", 0))
        return max(1, enabled_key_count) * max(1, retry_count + 1)

    def recommended_model_response_timeout_seconds(self, *, model_refs: list[str] | None = None) -> float | None:
        attempt_timeout_seconds = self._normalized_model_attempt_timeout_seconds()
        refs = [
            str(item or "").strip()
            for item in list(model_refs or [])
            if str(item or "").strip()
        ]
        if attempt_timeout_seconds is None or not refs:
            return None
        attempts_per_chain_round = sum(
            self._estimated_attempt_count_for_model_ref(ref)
            for ref in refs
        )
        if attempts_per_chain_round <= 0:
            return None
        total_attempt_budget_seconds = (
            float(attempt_timeout_seconds)
            * float(attempts_per_chain_round)
            * float(RETRYABLE_MODEL_CHAIN_MAX_ROUNDS)
        )
        return float(total_attempt_budget_seconds + _MODEL_CHAIN_HARD_TIMEOUT_SAFETY_SECONDS)

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
    ) -> LLMResponse:
        refs = [str(item or '').strip() for item in list(model_refs or []) if str(item or '').strip()]
        if not refs:
            raise ValueError('model_refs must not be empty')
        last_error: Exception | None = None
        last_response: LLMResponse | None = None
        attempts: list[LLMModelAttempt] = []
        held_turn_lease = node_turn_lease
        try:
            for chain_round_index in range(RETRYABLE_MODEL_CHAIN_MAX_ROUNDS):
                round_last_error: Exception | None = None
                retry_full_chain = False
                for index, ref in enumerate(refs):
                    try:
                        base_target = build_provider_from_model_key(self._config, ref)
                    except Exception as exc:
                        last_error = round_last_error = exc
                        if should_fallback_model_error(exc) and index < len(refs) - 1:
                            continue
                        if should_fallback_model_error(exc):
                            exhausted = exhausted_model_chain_error(exc)
                            if should_retry_model_chain_error(exhausted) and chain_round_index < RETRYABLE_MODEL_CHAIN_MAX_ROUNDS - 1:
                                retry_full_chain = True
                                break
                            raise exhausted from exc
                        raise
                    configured_api_key_indexes = getattr(base_target, "api_key_indexes", None)
                    if configured_api_key_indexes is None:
                        api_key_indexes = list(range(max(1, int(getattr(base_target, "api_key_count", 0) or 0))))
                    else:
                        api_key_indexes = [int(item) for item in configured_api_key_indexes]
                    if int(getattr(base_target, "api_key_count", 0) or 0) > 0 and not api_key_indexes:
                        raise RuntimeError(f"All configured API keys are disabled for model {ref}")
                    retry_count = normalized_retry_count(getattr(base_target, "retry_count", 0))
                    move_to_next_model = False
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
                    for slot in iter_api_key_retry_slots(api_key_count=getattr(base_target, "api_key_count", 0), retry_count=retry_count, key_indexes=api_key_indexes):
                        target = base_target
                        request_message_count, request_message_chars = _message_stats(request_messages)
                        permit_lease: ModelKeyPermitLease | None = None
                        use_held_turn_permit = bool(
                            held_turn_lease is not None
                            and held_turn_lease.initial_model_permit is not None
                            and str(ref or '').strip() == str(held_turn_lease.model_ref or '').strip()
                            and int(slot.key_index) == int(held_turn_lease.key_index)
                            and int(slot.attempt_number) == 1
                        )
                        try:
                            selected_api_key_index = int(held_turn_lease.key_index) if use_held_turn_permit and held_turn_lease is not None else int(slot.key_index)
                            target = build_provider_from_model_key(
                                self._config,
                                ref,
                                api_key_index=selected_api_key_index,
                            )
                            attempt_timeout_seconds = self._normalized_model_attempt_timeout_seconds()
                            if use_held_turn_permit and held_turn_lease is not None:
                                permit_lease = held_turn_lease.initial_model_permit
                                held_turn_lease.initial_model_permit = None
                            elif model_concurrency_controller is not None:
                                permit_lease = await model_concurrency_controller.acquire_specific(
                                    model_ref=target.provider_ref,
                                    key_index=selected_api_key_index,
                                )
                            provider_kwargs = {
                                **dict(preview_payload or {}),
                                'messages': request_messages,
                                'tools': request_tools,
                                'model': target.model_id,
                                'request_timeout_seconds': None if bool(getattr(target.provider, 'manages_request_timeout_internally', False)) else attempt_timeout_seconds,
                            }
                            outer_attempt_timeout_seconds = None if bool(getattr(target.provider, 'manages_request_timeout_internally', False)) else attempt_timeout_seconds
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
                            last_error = round_last_error = exc
                            rotate_key = should_rotate_api_key_error(exc, retry_on=target.retry_on)
                            if rotate_key and not slot.is_last_key:
                                continue
                            if rotate_key and not slot.is_last_round:
                                continue
                            if should_fallback_model_error(exc) and index < len(refs) - 1:
                                move_to_next_model = True
                                break
                            if should_fallback_model_error(exc):
                                exhausted = exhausted_model_chain_error(exc, retry_on=target.retry_on)
                                if should_retry_model_chain_error(exhausted) and chain_round_index < RETRYABLE_MODEL_CHAIN_MAX_ROUNDS - 1:
                                    retry_full_chain = True
                                    break
                                raise exhausted from exc
                            raise
                        finally:
                            if permit_lease is not None and model_concurrency_controller is not None:
                                model_concurrency_controller.release(permit_lease)
                        response.usage = normalize_usage_payload(response.usage)
                        response.request_message_count = request_message_count
                        response.request_message_chars = request_message_chars
                        response_attempts = list(response.attempts or [])
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
                        last_response = response
                        rotate_key_response = response_requires_api_key_rotation(response, retry_on=target.retry_on)
                        retryable_response = response_requires_retry(response, retry_on=target.retry_on)
                        fallback_response = response_requires_fallback(response)
                        if rotate_key_response:
                            if not slot.is_last_key:
                                continue
                            if not slot.is_last_round:
                                continue
                        if fallback_response and index < len(refs) - 1:
                            move_to_next_model = True
                            break
                        if fallback_response:
                            last_response = sanitize_terminal_model_error(response)
                            if retryable_response and chain_round_index < RETRYABLE_MODEL_CHAIN_MAX_ROUNDS - 1:
                                retry_full_chain = True
                                break
                            return last_response
                        return response
                    if retry_full_chain:
                        break
                    if move_to_next_model:
                        continue
                if retry_full_chain:
                    continue
                if round_last_error is not None and should_retry_model_chain_error(round_last_error) and chain_round_index < RETRYABLE_MODEL_CHAIN_MAX_ROUNDS - 1:
                    continue
                break
            if last_error is not None:
                if should_fallback_model_error(last_error):
                    raise exhausted_model_chain_error(last_error) from last_error
                raise last_error
            if last_response is None:
                raise RuntimeError('chat backend returned no response')
            last_response.attempts = list(attempts)
            return sanitize_terminal_model_error(last_response)
        finally:
            if held_turn_lease is not None and held_turn_lease.initial_model_permit is not None and model_concurrency_controller is not None:
                model_concurrency_controller.release(held_turn_lease.initial_model_permit)
                held_turn_lease.initial_model_permit = None
