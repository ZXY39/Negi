"""NO_PROXY normalization keeps HTTP client construction alive."""

from __future__ import annotations

import asyncio
import os

import httpx
import httpx2

from g3ku.providers.openai_chat_provider import OpenAIChatProvider
from g3ku.utils.net_env import normalize_no_proxy_env


def test_strips_brackets_from_ipv6_entries() -> None:
    environ = {"NO_PROXY": "localhost,.localhost,127.0.0.1,127.0.0.0/8,[::1],::1"}

    assert normalize_no_proxy_env(environ) is True
    assert environ["NO_PROXY"] == "localhost,.localhost,127.0.0.1,127.0.0.0/8,::1,::1"


def test_normalizes_bracketed_subnet_and_port_entries() -> None:
    environ = {"no_proxy": " [2001:db8::1]/32 , [2001:db8::2]:8080 "}

    assert normalize_no_proxy_env(environ) is True
    assert environ["no_proxy"] == "2001:db8::1/32,2001:db8::2"


def test_leaves_other_entries_untouched() -> None:
    environ = {"NO_PROXY": "localhost,example.com,::1"}

    assert normalize_no_proxy_env(environ) is False
    assert environ["NO_PROXY"] == "localhost,example.com,::1"


def test_http_clients_construct_after_normalization(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,[::1]")

    assert normalize_no_proxy_env() is True
    with httpx.Client():
        pass
    with httpx2.Client():
        pass


def test_provider_construction_normalizes_environment(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "localhost,[::1]")
    monkeypatch.setenv("no_proxy", "localhost,[::1]")

    provider = OpenAIChatProvider(api_key="test-key", api_base="https://example.invalid/v1")

    assert os.environ["NO_PROXY"] == "localhost,::1"
    asyncio.run(provider._client.close())
