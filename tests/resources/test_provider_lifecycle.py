import os
import sys
from types import SimpleNamespace

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from g3ku.providers import fallback as fb
from g3ku.providers.base import LLMResponse
from g3ku.providers.openai_chat_provider import OpenAIChatProvider


class RecordingProvider:
    def __init__(self, behavior):
        self.behavior = behavior
        self.closed = False
        self.manages_request_timeout_internally = True

    async def chat(self, **kwargs):
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior

    async def aclose(self):
        self.closed = True


def make_target(behavior):
    return SimpleNamespace(
        provider=RecordingProvider(behavior),
        model_id="m",
        provider_ref="m",
        provider_id="p",
        retry_on=[],
        api_key_count=0,
        api_key_indexes=None,
        model_parameters={},
        max_tokens_limit=None,
        default_temperature=None,
        default_reasoning_effort=None,
        retry_count=0,
    )


@pytest.fixture
def factory(monkeypatch):
    created = []

    def build(config, model_key, **kwargs):
        target = make_target(LLMResponse(content="ok", finish_reason="stop"))
        created.append(target)
        return target

    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", build)
    monkeypatch.setattr(fb, "current_runtime_config_revision", lambda: 0)
    return created


def make_fallback():
    return fb.FallbackProvider(
        config=SimpleNamespace(), model_chain=["m"], default_model_ref="m", role=""
    )


async def test_success_closes_every_built_target(factory):
    resp = await make_fallback().chat(messages=[{"role": "user", "content": "hi"}])
    assert resp.content == "ok"
    assert factory, "factory must have been used"
    assert all(t.provider.closed for t in factory)


async def test_non_fallback_error_still_closes(monkeypatch, factory):
    def build(config, model_key, **kwargs):
        target = make_target(RuntimeError("boom"))
        factory.append(target)
        return target

    monkeypatch.setattr("g3ku.providers.provider_factory.build_provider_from_model_key", build)
    with pytest.raises(RuntimeError):
        await make_fallback().chat(messages=[{"role": "user", "content": "hi"}])
    assert factory and all(t.provider.closed for t in factory)


async def test_openai_provider_aclose_closes_sdk_client():
    provider = OpenAIChatProvider(api_key="k", api_base="http://127.0.0.1:1/v1")
    assert not provider._client.is_closed()
    await provider.aclose()
    assert provider._client.is_closed()
