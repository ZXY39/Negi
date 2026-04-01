from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

from g3ku.config.schema import Config
from g3ku.providers.provider_factory import build_provider_from_model_key
from g3ku.providers.base import LLMModelAttempt, LLMResponse, normalize_usage_payload
from g3ku.providers.fallback import (
    RETRYABLE_MODEL_CHAIN_MAX_ROUNDS,
    exhausted_model_chain_error,
    normalized_retry_count,
    response_requires_api_key_rotation,
    response_requires_retry,
    response_requires_fallback,
    sanitize_terminal_model_error,
    should_rotate_api_key_error,
    should_fallback_model_error,
    should_retry_model_chain_error,
)
from g3ku.utils.api_keys import iter_api_key_retry_slots

_STAGE_COMPACT_PREFIX = '[G3KU_STAGE_COMPACT_V1]'
_STAGE_EXTERNALIZED_PREFIX = '[G3KU_STAGE_EXTERNALIZED_V1]'


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
    ) -> LLMResponse: ...


def _json_compact(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _message_content_signature(message: dict) -> str:
    content = message.get('content')
    if isinstance(content, str):
        return content
    return _json_compact(content)


def _tool_signature(tools: list[dict] | None) -> list[dict[str, object]]:
    signatures: list[dict[str, object]] = []
    for item in list(tools or []):
        if not isinstance(item, dict):
            continue
        function = item.get('function') if item.get('type') == 'function' else item
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
        'tool_signatures': _tool_signature(tools),
        'provider_model': str(provider_model or '').strip(),
        'stage_context_digest': _stage_context_digest(messages),
    }
    return hashlib.sha256(_json_compact(payload).encode('utf-8')).hexdigest()


def build_session_prompt_cache_key(*, session_key: str, provider_model: str, scope: str = 'chat') -> str:
    payload = {
        'scope': str(scope or '').strip() or 'chat',
        'session_key': str(session_key or '').strip(),
        'provider_model': str(provider_model or '').strip(),
    }
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
    ) -> LLMResponse:
        refs = [str(item or '').strip() for item in list(model_refs or []) if str(item or '').strip()]
        if not refs:
            raise ValueError('model_refs must not be empty')
        last_error: Exception | None = None
        last_response: LLMResponse | None = None
        attempts: list[LLMModelAttempt] = []
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
                retry_count = normalized_retry_count(getattr(base_target, "retry_count", 0))
                move_to_next_model = False
                stable_prompt_cache_key = str(prompt_cache_key or build_stable_prompt_cache_key(messages, tools, base_target.model_id))
                for slot in iter_api_key_retry_slots(api_key_count=getattr(base_target, "api_key_count", 0), retry_count=retry_count):
                    target = base_target
                    request_messages = list(messages or [])
                    request_message_count, request_message_chars = _message_stats(request_messages)
                    try:
                        target = base_target if slot.attempt_number == 1 else build_provider_from_model_key(
                            self._config,
                            ref,
                            api_key_index=slot.key_index,
                        )
                        response = await target.provider.chat(
                            **{
                                **{
                                    'messages': request_messages,
                                    'tools': tools,
                                    'model': target.model_id,
                                    'tool_choice': tool_choice if tool_choice is not None else 'auto',
                                    'parallel_tool_calls': parallel_tool_calls,
                                    'prompt_cache_key': stable_prompt_cache_key,
                                },
                                **_resolve_model_request_parameters(
                                    target,
                                    max_tokens=max_tokens,
                                    temperature=temperature,
                                    reasoning_effort=reasoning_effort,
                                ),
                            },
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
