from __future__ import annotations

import hashlib
import json
from typing import Protocol

from g3ku.config.schema import Config
from g3ku.providers.provider_factory import build_provider_from_model_key
from g3ku.providers.base import LLMModelAttempt, LLMResponse, normalize_usage_payload
from g3ku.providers.fallback import (
    exhausted_model_chain_error,
    normalized_retry_count,
    response_requires_api_key_rotation,
    response_requires_fallback,
    sanitize_terminal_model_error,
    should_rotate_api_key_error,
    should_fallback_model_error,
)
from g3ku.utils.api_keys import iter_api_key_retry_slots

_COMPACT_HISTORY_PREFIX = '[[G3KU_COMPACT_HISTORY_V1]]'


class ChatBackend(Protocol):
    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        model_refs: list[str],
        max_tokens: int = 1200,
        temperature: float = 0.2,
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


def _compact_history_digest(messages: list[dict]) -> str:
    for message in list(messages or []):
        if str(message.get('role') or '').strip().lower() != 'assistant':
            continue
        content = str(message.get('content') or '')
        if not content.startswith(_COMPACT_HISTORY_PREFIX):
            continue
        payload = content[len(_COMPACT_HISTORY_PREFIX) :].strip()
        if not payload:
            return ''
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()
    return ''


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
        'compact_history_digest': _compact_history_digest(messages),
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


class ConfigChatBackend:
    def __init__(self, config: Config):
        self._config = config

    async def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict] | None,
        model_refs: list[str],
        max_tokens: int = 1200,
        temperature: float = 0.2,
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
        for index, ref in enumerate(refs):
            try:
                base_target = build_provider_from_model_key(self._config, ref)
            except Exception as exc:
                last_error = exc
                if should_fallback_model_error(exc) and index < len(refs) - 1:
                    continue
                if should_fallback_model_error(exc):
                    raise exhausted_model_chain_error(exc) from exc
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
                        messages=request_messages,
                        tools=tools,
                        model=target.model_id,
                        max_tokens=max(1, min(int(max_tokens), int(target.max_tokens_limit))) if target.max_tokens_limit else max(1, int(max_tokens)),
                        temperature=float(target.default_temperature) if target.default_temperature is not None else float(temperature),
                        reasoning_effort=target.default_reasoning_effort or reasoning_effort,
                        tool_choice='auto',
                        parallel_tool_calls=parallel_tool_calls,
                        prompt_cache_key=stable_prompt_cache_key,
                    )
                except Exception as exc:
                    last_error = exc
                    rotate_key = should_rotate_api_key_error(exc, retry_on=target.retry_on)
                    if rotate_key and not slot.is_last_key:
                        continue
                    if rotate_key and not slot.is_last_round:
                        continue
                    if should_fallback_model_error(exc) and index < len(refs) - 1:
                        move_to_next_model = True
                        break
                    if should_fallback_model_error(exc):
                        raise exhausted_model_chain_error(exc) from exc
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
                    return sanitize_terminal_model_error(response)
                return response
            if move_to_next_model:
                continue
        if last_error is not None:
            if should_fallback_model_error(last_error):
                raise exhausted_model_chain_error(last_error) from last_error
            raise last_error
        if last_response is None:
            raise RuntimeError('chat backend returned no response')
        last_response.attempts = list(attempts)
        return sanitize_terminal_model_error(last_response)
