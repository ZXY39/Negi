from __future__ import annotations

import threading
import time
import types
import importlib
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.llm_config.enums import ProbeStatus
from g3ku.llm_config.models import ProbeResult, ProviderConfigDraft
from g3ku.llm_config.repositories import EncryptedConfigRepository
from g3ku.llm_config.service import ConfigService


def _build_service(tmp_path: Path) -> ConfigService:
    return ConfigService(EncryptedConfigRepository(tmp_path / "llm-config", None))


def _build_draft(api_key: str) -> ProviderConfigDraft:
    return ProviderConfigDraft(
        provider_id="custom",
        api_key=api_key,
        base_url="https://example.com/v1",
        default_model="custom-model",
        parameters={
            "timeout_s": 8,
            "temperature": 0.2,
            "max_tokens": 64,
            "api_mode": "custom-direct",
        },
    )


@pytest.mark.asyncio
async def test_probe_max_concurrency_draft_derives_limits_and_zeroes_failed_keys(tmp_path: Path, monkeypatch) -> None:
    service = _build_service(tmp_path)
    thresholds = {
        "bad-key": 0,
        "key-2": 3,
        "key-3": 5,
    }
    lock = threading.Lock()
    active_by_key: dict[str, int] = {}

    def _fake_probe_config(config, *, transport=None):
        _ = transport
        key = str(config.auth.get("api_key", "") or "").strip()
        with lock:
            active_by_key[key] = int(active_by_key.get(key, 0)) + 1
            current = active_by_key[key]
        time.sleep(0.02)
        success = current <= int(thresholds.get(key, 0))
        with lock:
            active_by_key[key] = max(0, int(active_by_key.get(key, 0)) - 1)
        return ProbeResult(
            status=ProbeStatus.SUCCESS if success else ProbeStatus.AUTH_ERROR if key == "bad-key" else ProbeStatus.INVALID_RESPONSE,
            success=success,
            provider_id=config.provider_id,
            protocol_adapter=config.protocol_adapter,
            capability=config.capability,
            resolved_base_url=config.base_url,
            checked_model=config.default_model,
            message="ok" if success else "failed",
            diagnostics={},
        )

    monkeypatch.setattr("g3ku.llm_config.service.probe_config", _fake_probe_config)
    monkeypatch.setattr("g3ku.llm_config.service.probe_config_for_concurrency", _fake_probe_config)

    result = await service.probe_max_concurrency_draft(_build_draft("bad-key,key-2,key-3"))

    assert result.suggested_limits == [0, 3, 5]
    assert [item.suggested_limit for item in result.per_key_results] == [0, 3, 5]
    assert result.per_key_results[0].connection_probe.success is False
    assert result.per_key_results[1].connection_probe.success is True
    assert result.per_key_results[2].connection_probe.success is True


@pytest.mark.asyncio
async def test_probe_max_concurrency_draft_limits_parallel_key_probes_to_five(tmp_path: Path, monkeypatch) -> None:
    service = _build_service(tmp_path)
    thresholds = {f"key-{index}": 1 for index in range(6)}
    lock = threading.Lock()
    active_by_key: dict[str, int] = {}
    max_parallel_keys = 0

    def _fake_probe_config(config, *, transport=None):
        nonlocal max_parallel_keys
        _ = transport
        key = str(config.auth.get("api_key", "") or "").strip()
        with lock:
            active_by_key[key] = int(active_by_key.get(key, 0)) + 1
            current = active_by_key[key]
            running_keys = sum(1 for count in active_by_key.values() if count > 0)
            max_parallel_keys = max(max_parallel_keys, running_keys)
        time.sleep(0.03)
        success = current <= int(thresholds.get(key, 0))
        with lock:
            active_by_key[key] = max(0, int(active_by_key.get(key, 0)) - 1)
        return ProbeResult(
            status=ProbeStatus.SUCCESS if success else ProbeStatus.INVALID_RESPONSE,
            success=success,
            provider_id=config.provider_id,
            protocol_adapter=config.protocol_adapter,
            capability=config.capability,
            resolved_base_url=config.base_url,
            checked_model=config.default_model,
            message="ok" if success else "failed",
            diagnostics={},
        )

    monkeypatch.setattr("g3ku.llm_config.service.probe_config", _fake_probe_config)
    monkeypatch.setattr("g3ku.llm_config.service.probe_config_for_concurrency", _fake_probe_config)

    result = await service.probe_max_concurrency_draft(_build_draft(",".join(thresholds.keys())))

    assert result.suggested_limits == [1, 1, 1, 1, 1, 1]
    assert max_parallel_keys <= 5


@pytest.mark.asyncio
async def test_probe_max_concurrency_draft_uses_standard_probe_for_connection_and_concurrency_probe_for_levels(tmp_path: Path, monkeypatch) -> None:
    service = _build_service(tmp_path)
    calls = {"connection": 0, "concurrency": 0}

    def _fake_probe_config(config, *, transport=None):
        _ = transport
        calls["connection"] += 1
        return ProbeResult(
            status=ProbeStatus.SUCCESS,
            success=True,
            provider_id=config.provider_id,
            protocol_adapter=config.protocol_adapter,
            capability=config.capability,
            resolved_base_url=config.base_url,
            checked_model=config.default_model,
            message="ok",
            diagnostics={},
        )

    def _fake_probe_config_for_concurrency(config, *, transport=None):
        _ = transport
        calls["concurrency"] += 1
        return ProbeResult(
            status=ProbeStatus.SUCCESS,
            success=True,
            provider_id=config.provider_id,
            protocol_adapter=config.protocol_adapter,
            capability=config.capability,
            resolved_base_url=config.base_url,
            checked_model=config.default_model,
            message="ok",
            diagnostics={},
        )

    monkeypatch.setattr("g3ku.llm_config.service.probe_config", _fake_probe_config)
    monkeypatch.setattr("g3ku.llm_config.service.probe_config_for_concurrency", _fake_probe_config_for_concurrency)

    result = await service.probe_max_concurrency_draft(_build_draft("key-1,key-2"))

    assert result.suggested_limits == [32, 32]
    assert calls["connection"] == 2
    assert calls["concurrency"] > 2


@pytest.mark.asyncio
async def test_probe_concurrency_level_caps_high_level_request_fanout(tmp_path: Path, monkeypatch) -> None:
    service = _build_service(tmp_path)
    normalized = service.validate_draft(_build_draft("key-1")).normalized_preview
    assert normalized is not None

    lock = threading.Lock()
    active = 0
    max_active = 0

    async def _fake_probe_single_config_async(config):
        nonlocal active, max_active
        _ = config
        with lock:
            active += 1
            max_active = max(max_active, active)
        await __import__("asyncio").sleep(0.02)
        with lock:
            active -= 1
        return ProbeResult(
            status=ProbeStatus.SUCCESS,
            success=True,
            provider_id=normalized.provider_id,
            protocol_adapter=normalized.protocol_adapter,
            capability=normalized.capability,
            resolved_base_url=normalized.base_url,
            checked_model=normalized.default_model,
            message="ok",
            diagnostics={},
        )

    monkeypatch.setattr(service, "_probe_single_config_async", _fake_probe_single_config_async)

    ok = await service._probe_concurrency_level(normalized, 16)

    assert ok is True
    assert max_active <= 8


def test_probe_max_concurrency_route_returns_result(monkeypatch) -> None:
    captured: dict[str, object] = {}

    admin_rest = importlib.import_module("main.api.admin_rest")

    class _StubFacade:
        async def probe_max_concurrency_draft(self, payload: dict):
            captured["payload"] = dict(payload)
            return {
                "success": True,
                "message": "ok",
                "suggested_limits": [3, 5],
                "per_key_results": [],
            }

    monkeypatch.setattr(admin_rest.ModelManager, "load_facade", classmethod(lambda cls: _StubFacade()))

    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    client = TestClient(app)

    response = client.post("/api/llm/drafts/probe-max-concurrency", json={"provider_id": "custom"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["result"]["suggested_limits"] == [3, 5]
    assert captured["payload"] == {"provider_id": "custom"}
