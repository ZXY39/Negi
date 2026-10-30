"""Apikey 读面的两道门：默认只回掩码，明文要经项目解锁口令换一次 5 分钟免密窗口。

这里的断言同时钉住三条容易退化的边界：
- 掩码读面必须带 `api_key_redacted` / `api_key_count`，编辑面才分得清"没有 key"与"key 被遮住"；
- `include_secrets=true` 不再是免口令旁路；
- 创建/更新响应不再把明文 key 回给调用方（否则一次 no-op PUT 就能把密钥读出来）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from g3ku.llm_config.enums import AuthMode, Capability, ProtocolAdapter
from g3ku.llm_config.export import MASKED_SECRET_VALUE
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.llm_config.models import NormalizedProviderConfig
from main.api import admin_rest

PASSWORD = "owner-password"
REAL_KEY = "sk-live-1,sk-live-2"


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _record(config_id: str = "cfg-reveal") -> NormalizedProviderConfig:
    now = datetime.now(UTC)
    return NormalizedProviderConfig(
        config_id=config_id,
        provider_id="openai",
        display_name="reveal test",
        protocol_adapter=ProtocolAdapter.OPENAI_COMPLETIONS,
        capability=Capability.CHAT,
        auth_mode=AuthMode.API_KEY,
        base_url="https://example.invalid/v1",
        default_model="gpt-test",
        template_version="1",
        parameters={"context_window_tokens": 200000},
        created_at=now,
        updated_at=now,
        auth={"type": "api_key", "api_key": REAL_KEY},
    )


def _client(monkeypatch: pytest.MonkeyPatch, facade: LLMConfigFacade) -> TestClient:
    monkeypatch.setattr(admin_rest, "_llm_facade", lambda: facade)
    admin_rest._LLM_SECRET_REVEAL_GRANT.reset()
    app = FastAPI()
    app.include_router(admin_rest.router, prefix="/api")
    return TestClient(app)


@pytest.fixture()
def reveal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    facade = LLMConfigFacade(_workspace(tmp_path))
    facade.security.setup_initial_realm(password=PASSWORD)
    facade.repository.save(_record(), last_probe_status=None)
    client = _client(monkeypatch, facade)
    yield client, facade
    admin_rest._LLM_SECRET_REVEAL_GRANT.reset()


def test_default_read_returns_mask_and_key_metadata(reveal_env) -> None:
    client, _facade = reveal_env

    payload = client.get("/api/llm/configs/cfg-reveal").json()["item"]

    assert payload["auth"]["api_key"] == MASKED_SECRET_VALUE
    assert payload["api_key_redacted"] is True
    assert payload["api_key_count"] == 2
    # 掩码读面继续清空请求头：那里也可能藏着密钥。
    assert payload["headers"] == {}


def test_include_secrets_is_not_a_passwordless_bypass(reveal_env) -> None:
    client, _facade = reveal_env

    response = client.get("/api/llm/configs/cfg-reveal", params={"include_secrets": "true"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "api_key_password_required"


def test_reveal_requires_password_when_an_envelope_exists(reveal_env) -> None:
    client, _facade = reveal_env

    response = client.post("/api/llm/configs/cfg-reveal/reveal", json={})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "api_key_password_required"


def test_reveal_rejects_a_wrong_password_without_opening_the_window(reveal_env) -> None:
    client, _facade = reveal_env

    response = client.post("/api/llm/configs/cfg-reveal/reveal", json={"password": "wrong"})

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "api_key_password_invalid"
    again = client.get("/api/llm/configs/cfg-reveal", params={"include_secrets": "true"})
    assert again.status_code == 403


def test_reveal_with_password_opens_a_five_minute_passwordless_window(reveal_env) -> None:
    client, _facade = reveal_env

    response = client.post("/api/llm/configs/cfg-reveal/reveal", json={"password": PASSWORD})

    assert response.status_code == 200
    body = response.json()
    assert body["item"]["auth"]["api_key"] == REAL_KEY
    assert body["item"]["api_key_redacted"] is False
    assert body["reveal_ttl_seconds"] == 300
    assert 0 < body["reveal_expires_in_seconds"] <= 300

    passwordless = client.post("/api/llm/configs/cfg-reveal/reveal", json={})
    assert passwordless.status_code == 200
    assert passwordless.json()["item"]["auth"]["api_key"] == REAL_KEY

    unredacted_read = client.get("/api/llm/configs/cfg-reveal", params={"include_secrets": "true"})
    assert unredacted_read.status_code == 200
    assert unredacted_read.json()["item"]["auth"]["api_key"] == REAL_KEY


def test_reveal_window_closes_without_asking_again(reveal_env, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _facade = reveal_env
    clock = {"now": 1000.0}
    monkeypatch.setattr(admin_rest.time, "monotonic", lambda: clock["now"])
    admin_rest._LLM_SECRET_REVEAL_GRANT.reset()

    assert client.post("/api/llm/configs/cfg-reveal/reveal", json={"password": PASSWORD}).status_code == 200
    clock["now"] += 300.5

    expired = client.post("/api/llm/configs/cfg-reveal/reveal", json={})
    assert expired.status_code == 403
    assert expired.json()["detail"]["code"] == "api_key_password_required"


def test_project_without_an_envelope_reveals_only_while_unlocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    facade = LLMConfigFacade(_workspace(tmp_path))
    facade.repository.save(_record("cfg-master-key-only"), last_probe_status=None)
    client = _client(monkeypatch, facade)

    locked = client.post("/api/llm/configs/cfg-master-key-only/reveal", json={})
    assert locked.status_code == 403

    # 只用主密钥启动的部署没有可核对的口令：解锁态里免密查看不新增能力边界。
    facade.security.activate_with_master_key(master_key="test-master-key")
    allowed = client.post("/api/llm/configs/cfg-master-key-only/reveal", json={})
    assert allowed.status_code == 200
    assert allowed.json()["item"]["auth"]["api_key"] == REAL_KEY


def test_write_responses_never_carry_the_plaintext_key(tmp_path: Path) -> None:
    facade = LLMConfigFacade(_workspace(tmp_path))
    facade.security.setup_initial_realm(password=PASSWORD)

    created = facade.create_config_record(
        {
            "provider_id": "openai",
            "capability": "chat",
            "auth_mode": "api_key",
            "display_name": "demo",
            "api_key": "sk-created-secret",
            "base_url": "https://example.invalid/v1",
            "default_model": "gpt-test",
            "parameters": {"context_window_tokens": 200000},
            "extra_headers": {},
            "extra_options": {},
        }
    )
    assert created["auth"]["api_key"] == MASKED_SECRET_VALUE
    assert created["api_key_redacted"] is True
    assert created["api_key_count"] == 1

    updated = facade.update_config_record(
        created["config_id"],
        {"api_key": MASKED_SECRET_VALUE, "parameters": {"context_window_tokens": 200000}},
    )
    assert updated["auth"]["api_key"] == MASKED_SECRET_VALUE

    plain = facade.get_config_record(created["config_id"], include_secrets=True)
    assert plain["auth"]["api_key"] == "sk-created-secret"
