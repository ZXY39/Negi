"""Auth dependency + secret overlay tests for the External Agent API."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from g3ku.runtime.api import external_auth
from g3ku.security.bootstrap import (
    apply_config_secret_entries,
    extract_config_secret_entries,
    strip_config_secret_entries,
)


def _config(*, enabled: bool, tokens: dict | None):
    entries = {
        token_id: SimpleNamespace(token=str(payload.get("token") or ""), enabled=bool(payload.get("enabled", True)), label=str(payload.get("label") or ""))
        for token_id, payload in (tokens or {}).items()
    }
    return SimpleNamespace(external_api=SimpleNamespace(enabled=enabled, tokens=entries))


def _install(monkeypatch, config):
    monkeypatch.setattr(external_auth, "get_runtime_config", lambda force=False: (config, None))


def test_disabled_api_rejects(monkeypatch):
    _install(monkeypatch, _config(enabled=False, tokens={"qq": {"token": "t"}}))
    with pytest.raises(HTTPException) as exc:
        external_auth.require_external_api(authorization="Bearer t")
    assert exc.value.status_code == 403
    assert exc.value.detail == "external_api_disabled"


def test_missing_header_rejected(monkeypatch):
    _install(monkeypatch, _config(enabled=True, tokens={"qq": {"token": "t"}}))
    with pytest.raises(HTTPException) as exc:
        external_auth.require_external_api(authorization=None)
    assert exc.value.status_code == 401


def test_non_bearer_scheme_rejected(monkeypatch):
    _install(monkeypatch, _config(enabled=True, tokens={"qq": {"token": "t"}}))
    with pytest.raises(HTTPException) as exc:
        external_auth.require_external_api(authorization="Basic dXNlcjpwYXNz")
    assert exc.value.status_code == 401


def test_wrong_token_rejected(monkeypatch):
    _install(monkeypatch, _config(enabled=True, tokens={"qq": {"token": "right"}}))
    with pytest.raises(HTTPException) as exc:
        external_auth.require_external_api(authorization="Bearer wrong")
    assert exc.value.status_code == 401


def test_valid_token_resolves_bridge_id(monkeypatch):
    _install(monkeypatch, _config(enabled=True, tokens={"qq-bot": {"token": "sek", "label": "QQ"}}))
    principal = external_auth.require_external_api(authorization="Bearer sek")
    assert principal.bridge_id == "qq-bot"
    assert principal.label == "QQ"


def test_disabled_token_entry_rejected(monkeypatch):
    _install(monkeypatch, _config(enabled=True, tokens={"qq": {"token": "sek", "enabled": False}}))
    with pytest.raises(HTTPException) as exc:
        external_auth.require_external_api(authorization="Bearer sek")
    assert exc.value.status_code == 401


def test_empty_token_entry_never_matches(monkeypatch):
    _install(monkeypatch, _config(enabled=True, tokens={"qq": {"token": ""}}))
    with pytest.raises(HTTPException) as exc:
        external_auth.require_external_api(authorization="Bearer ")
    assert exc.value.status_code == 401


def test_secret_overlay_round_trip_for_external_tokens():
    raw = {
        "externalApi": {
            "enabled": True,
            "tokens": {
                "qq-bot": {"token": "sek-ret", "label": "qq", "enabled": True},
                "feishu": {"token": "", "label": "fs", "enabled": True},
            },
        }
    }
    entries = extract_config_secret_entries(raw)
    assert entries == {"config.externalApi.tokens.qq-bot.token": "sek-ret"}

    stripped = strip_config_secret_entries(raw)
    assert stripped["externalApi"]["tokens"]["qq-bot"]["token"] == ""
    assert stripped["externalApi"]["tokens"]["qq-bot"]["label"] == "qq"
    assert "sek-ret" not in str(stripped)

    restored = apply_config_secret_entries(stripped, entries)
    assert restored["externalApi"]["tokens"]["qq-bot"]["token"] == "sek-ret"
