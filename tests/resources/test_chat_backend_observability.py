from __future__ import annotations

from types import SimpleNamespace

import pytest

import g3ku.providers.fallback as fallback_module
import main.runtime.chat_backend as chat_backend_module
from g3ku.prompt_trace import render_model_chain_trace
from g3ku.providers.base import LLMResponse
from g3ku.providers.provider_factory import ProviderTarget


class _AlwaysFailProvider:
    async def chat(self, **kwargs):
        _ = kwargs
        raise RuntimeError('HTTP 502: Upstream request failed')


class _RetryThenSuccessProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError('HTTP 502: Upstream request failed')
        return LLMResponse(content='ok', finish_reason='stop')


class _SuccessProvider:
    async def chat(self, **kwargs):
        _ = kwargs
        return LLMResponse(content='ok', finish_reason='stop')


def _capture_logger(target_module, sink: list[str], monkeypatch) -> None:
    def _record(template, *args, **kwargs):
        _ = kwargs
        try:
            rendered = str(template).format(*args)
        except Exception:
            rendered = str(template)
        sink.append(rendered)

    monkeypatch.setattr(target_module, 'logger', SimpleNamespace(info=_record, warning=_record, error=_record))


def test_render_model_chain_trace_uses_ansi_color() -> None:
    rendered = render_model_chain_trace(
        title='FALLBACK',
        severity='fallback',
        lines=['model_ref: primary', 'next_model_ref: secondary'],
    )

    assert '[' in rendered
    assert 'MODEL CHAIN: FALLBACK' in rendered
    assert 'next_model_ref: secondary' in rendered


@pytest.mark.asyncio
async def test_config_chat_backend_logs_colored_fallback_events(monkeypatch) -> None:
    logs: list[str] = []
    _capture_logger(chat_backend_module, logs, monkeypatch)

    targets = {
        'primary': ProviderTarget(
            provider_ref='primary',
            provider_id='responses',
            model_id='gpt-primary',
            provider=_AlwaysFailProvider(),
            retry_on=['5xx'],
            retry_count=0,
        ),
        'secondary': ProviderTarget(
            provider_ref='secondary',
            provider_id='responses',
            model_id='gpt-secondary',
            provider=_SuccessProvider(),
            retry_on=['5xx'],
            retry_count=0,
        ),
    }
    monkeypatch.setattr(chat_backend_module, 'build_provider_from_model_key', lambda config, ref: targets[ref])

    backend = chat_backend_module.ConfigChatBackend(config=SimpleNamespace())
    response = await backend.chat(
        messages=[
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': '{"task_id":"task-1","node_id":"node-1","goal":"demo task"}'},
        ],
        tools=None,
        model_refs=['primary', 'secondary'],
    )

    assert response.content == 'ok'
    joined = '\n'.join(logs)
    assert '[' in joined
    assert 'MODEL CHAIN: FALLBACK' in joined
    assert 'task_id=task-1 node_id=node-1' in joined
    assert 'next_model_ref: secondary' in joined


@pytest.mark.asyncio
async def test_fallback_provider_logs_colored_retry_events(monkeypatch) -> None:
    logs: list[str] = []
    _capture_logger(fallback_module, logs, monkeypatch)

    provider = _RetryThenSuccessProvider()
    monkeypatch.setattr(
        'g3ku.providers.provider_factory.build_provider_from_model_key',
        lambda config, model_key: ProviderTarget(
            provider_ref=model_key,
            provider_id='responses',
            model_id='gpt-primary',
            provider=provider,
            retry_on=['5xx'],
            retry_count=1,
        ),
    )

    fallback = fallback_module.FallbackProvider(
        config=SimpleNamespace(),
        model_chain=['primary'],
        default_model_ref='primary',
    )
    response = await fallback.chat(messages=[{'role': 'user', 'content': 'demo'}], model='primary')

    assert response.content == 'ok'
    joined = '\n'.join(logs)
    assert '[' in joined
    assert 'MODEL CHAIN: RETRY' in joined
    assert 'model_ref: primary' in joined
