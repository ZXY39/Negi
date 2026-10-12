"""链上"塞不下这一发的成员"不该被先发。

实盘形状（2026-10-12）：声明窗口虚高的成员接到超出真实窗口的请求时，网关不报错，只把
completion 预算静默裁成 `真实窗口 − 输入`，症状是一跳被裁的输出。节点道的组车道由准入
按 `required_context_window_tokens` 筛过；direct 链与前门（没有回合准入）没有这道筛，
这两条就是本文件的用例面。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import main.runtime.chat_backend as chat_backend_module
from g3ku.providers.base import LLMResponse
from g3ku.providers.provider_factory import ProviderTarget


class _OkProvider:
    def __init__(self, model_key: str, calls: list[str]) -> None:
        self.model_key = model_key
        self.calls = calls

    async def chat(self, **kwargs):
        _ = kwargs
        self.calls.append(self.model_key)
        return LLMResponse(content="ok", finish_reason="stop")


def _target(model_key: str, *, window: int | None) -> ProviderTarget:
    provider = _OkProvider(model_key, CALLS)
    parameters: dict[str, object] = {}
    if window is not None:
        parameters["context_window_tokens"] = int(window)
    return ProviderTarget(
        provider_ref=model_key,
        provider_id="custom",
        model_id=f"{model_key}-model",
        provider=provider,
        model_parameters=parameters,
        retry_on=["network", "429", "502"],
        retry_count=0,
        api_key_count=1,
        api_key_indexes=[0],
    )


CALLS: list[str] = []


def _patch(monkeypatch, targets: dict[str, ProviderTarget]) -> None:
    CALLS.clear()

    def _builder(config, model_key, *, api_key_index=None):
        _ = config, api_key_index
        return targets[str(model_key)]

    monkeypatch.setattr(chat_backend_module, "build_provider_from_model_key", _builder)


def _backend() -> chat_backend_module.ConfigChatBackend:
    return chat_backend_module.ConfigChatBackend(config=SimpleNamespace(get_model_runtime_profile=lambda ref: None))


async def _send(monkeypatch, refs: list[str], required: int) -> list[str]:
    windows = {"m_small": 390000, "m_big": 690000}
    _patch(monkeypatch, {key: _target(key, window=windows.get(key)) for key in refs})
    response = await _backend().chat(
        messages=[{"role": "user", "content": "demo"}],
        tools=None,
        model_refs=list(refs),
        required_context_window_tokens=required,
    )
    assert response.content == "ok"
    return list(CALLS)


@pytest.mark.asyncio
async def test_slot_that_cannot_hold_the_body_is_not_tried_first(monkeypatch) -> None:
    """链首 39 万、第二位 69 万，本轮要 50 万：直接打第二位。"""
    calls = await _send(monkeypatch, ["m_small", "m_big"], required=500_000)
    assert calls == ["m_big"]


@pytest.mark.asyncio
async def test_zero_required_keeps_the_existing_chain_order(monkeypatch) -> None:
    """没传体量（helper/评审车道）时链序一字不动。"""
    calls = await _send(monkeypatch, ["m_small", "m_big"], required=0)
    assert calls == ["m_small"]


@pytest.mark.asyncio
async def test_unknown_declared_window_does_not_block_the_send(monkeypatch) -> None:
    """记录里读不到窗口 ⇒ 判不出来就不拦，保持原行为。"""
    calls = await _send(monkeypatch, ["m_small"], required=500_000)
    assert calls == ["m_small"]


@pytest.mark.asyncio
async def test_when_nothing_fits_the_chain_is_walked_in_order(monkeypatch) -> None:
    """整条链都塞不下：不新增失败面，仍按链序发（宁可被裁并留下被裁读数，也不打死这一跳）。"""
    calls = await _send(monkeypatch, ["m_small", "m_big"], required=900_000)
    assert calls == ["m_small"]
