"""TLS verification behavior for the Responses provider.

Covers the module-level ``_tls_verify_enabled()`` toggle (default on, opt-out
via ``G3KU_PROVIDER_TLS_VERIFY``) and that every httpx.AsyncClient construction
ships ``verify=_tls_verify_enabled()``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import g3ku.providers.responses_provider as responses_provider_module
from g3ku.providers.responses_provider import ResponsesProvider


@pytest.fixture(autouse=True)
def _reset_tls_verify_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test from a clean env var and a clean warning-once flag."""
    monkeypatch.setattr(responses_provider_module, "_tls_verify_warning_logged", False)
    monkeypatch.delenv("G3KU_PROVIDER_TLS_VERIFY", raising=False)
    yield


def test_tls_verify_enabled_defaults_to_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("G3KU_PROVIDER_TLS_VERIFY", raising=False)
    assert responses_provider_module._tls_verify_enabled() is True


@pytest.mark.parametrize(
    "raw_value",
    ["off", "false", "0", "no", "OFF", "FALSE", "No", "False", " Off ", "0  "],
)
def test_tls_verify_enabled_false_for_known_disable_values(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    monkeypatch.setenv("G3KU_PROVIDER_TLS_VERIFY", raw_value)
    assert responses_provider_module._tls_verify_enabled() is False


@pytest.mark.parametrize("raw_value", ["1", "true", "yes", "on", "enabled", "whatever"])
def test_tls_verify_enabled_true_for_unknown_values(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    monkeypatch.setenv("G3KU_PROVIDER_TLS_VERIFY", raw_value)
    assert responses_provider_module._tls_verify_enabled() is True


def test_tls_verify_warning_logged_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []

    class _FakeLogger:
        def warning(self, message: str, *args, **kwargs) -> None:
            warnings.append(message)

    monkeypatch.setattr(responses_provider_module, "logger", _FakeLogger())
    monkeypatch.setenv("G3KU_PROVIDER_TLS_VERIFY", "off")

    assert responses_provider_module._tls_verify_enabled() is False
    assert responses_provider_module._tls_verify_enabled() is False
    assert responses_provider_module._tls_verify_enabled() is False

    assert len(warnings) == 1
    assert "TLS certificate verification is DISABLED" in warnings[0]


def test_tls_verify_warning_not_logged_when_verification_stays_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []

    class _FakeLogger:
        def warning(self, message: str, *args, **kwargs) -> None:
            warnings.append(message)

    monkeypatch.setattr(responses_provider_module, "logger", _FakeLogger())

    assert responses_provider_module._tls_verify_enabled() is True
    assert warnings == []


def _FakeStream(status_code: int = 200) -> SimpleNamespace:
    class _Stream:
        async def __aenter__(self):
            return SimpleNamespace(status_code=status_code)

        async def __aexit__(self, exc_type, exc, tb):
            return None

    return _Stream()


async def _chat_with_captured_client(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]
) -> SimpleNamespace:
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        def stream(self, *args, **kwargs):
            return _FakeStream()

    async def _fake_consume_sse(response):
        return "ok", [], "stop", {}

    monkeypatch.setattr("g3ku.providers.responses_provider.httpx.AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr("g3ku.providers.responses_provider._consume_sse", _fake_consume_sse)

    provider = ResponsesProvider(api_key="test-key", api_base="https://example.com/v1")
    return await provider.chat(messages=[{"role": "user", "content": "ping"}], model="demo")


@pytest.mark.asyncio
async def test_responses_provider_client_verifies_tls_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}
    response = await _chat_with_captured_client(monkeypatch, captured)

    assert response.content == "ok"
    assert captured["verify"] is True


@pytest.mark.asyncio
async def test_responses_provider_client_disables_tls_verification_via_env(monkeypatch) -> None:
    monkeypatch.setenv("G3KU_PROVIDER_TLS_VERIFY", "OFF")

    captured: dict[str, object] = {}
    response = await _chat_with_captured_client(monkeypatch, captured)

    assert response.content == "ok"
    assert captured["verify"] is False