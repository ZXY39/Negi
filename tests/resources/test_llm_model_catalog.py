from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.llm_config.models import ProviderConfigDraft
from g3ku.llm_config.repositories import EncryptedConfigRepository
from g3ku.llm_config.service import ConfigService


def _build_service(tmp_path: Path, transport: httpx.BaseTransport | None = None) -> ConfigService:
    return ConfigService(EncryptedConfigRepository(tmp_path / "llm-config", None), transport=transport)


def _build_draft(api_key: str = "key-1") -> ProviderConfigDraft:
    return ProviderConfigDraft(
        provider_id="openai",
        api_key=api_key,
        base_url="https://example.com/v1",
        default_model="custom-model",
        parameters={"context_window_tokens": 32000},
    )


@pytest.mark.asyncio
async def test_list_draft_models_returns_sorted_unique_model_ids(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers.get("authorization") == "Bearer key-1"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "zeta-model"},
                    {"id": "alpha-model"},
                    "beta-model",
                    {"id": "alpha-model"},
                ],
            },
        )

    service = _build_service(tmp_path, transport=httpx.MockTransport(handler))

    result = await service.list_draft_models(_build_draft())

    assert result.success is True
    assert result.models == ["alpha-model", "beta-model", "zeta-model"]
    assert result.resolved_base_url == "https://example.com/v1"


@pytest.mark.asyncio
async def test_list_draft_models_switches_to_next_api_key_on_auth_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == "Bearer bad-key":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"data": [{"id": "model-a"}]})

    service = _build_service(tmp_path, transport=httpx.MockTransport(handler))

    result = await service.list_draft_models(_build_draft("bad-key,good-key"))

    assert result.success is True
    assert result.models == ["model-a"]


@pytest.mark.asyncio
async def test_list_draft_models_reports_validation_errors_without_request(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP request should be issued when validation fails")

    service = _build_service(tmp_path, transport=httpx.MockTransport(handler))

    result = await service.list_draft_models(_build_draft(api_key=""))

    assert result.success is False
    errors = result.diagnostics.get("errors") or []
    assert any(item.get("field") == "api_key" for item in errors)


@pytest.mark.asyncio
async def test_list_draft_models_fails_on_non_json_response(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, text="<html>welcome</html>", headers={"content-type": "text/html"})

    service = _build_service(tmp_path, transport=httpx.MockTransport(handler))

    result = await service.list_draft_models(_build_draft())

    assert result.success is False
    assert "non-JSON" in result.message


@pytest.mark.asyncio
async def test_list_draft_models_fails_when_catalog_is_empty(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        return httpx.Response(200, json={"data": []})

    service = _build_service(tmp_path, transport=httpx.MockTransport(handler))

    result = await service.list_draft_models(_build_draft())

    assert result.success is False
    assert result.models == []


def test_llm_draft_models_route_returns_result(monkeypatch) -> None:
    admin_rest = importlib.import_module("main.api.admin_rest")

    class _StubFacade:
        async def list_draft_models(self, payload: dict):
            _ = payload
            return {
                "success": True,
                "provider_id": "openai",
                "resolved_base_url": "https://example.com/v1",
                "models": ["m-1"],
                "message": "ok",
            }

    monkeypatch.setattr(admin_rest.ModelManager, "load_facade", classmethod(lambda cls: _StubFacade()))

    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    client = TestClient(app)

    response = client.post("/api/llm/drafts/models", json={"provider_id": "openai"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["result"]["models"] == ["m-1"]
