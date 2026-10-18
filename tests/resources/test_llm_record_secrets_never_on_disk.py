"""凭据记录落盘唯一的出口：密钥先有去处，才许把清洗过的版本写盘。

覆盖层的存在理由是"密钥不落明文盘"。但反过来做顺序错了会造成另一种丢法：项目锁定时
覆盖层写不进，若仍然照例清洗，密钥就既不在盘上也没进覆盖层——模型直接变成
"API key is required"。所以判据是一句话：**收得下才洗**，并且这个判断只有一个作者
（`LLMConfigFacade._persist_record`），其余车道一律走它。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from g3ku.config.loader import _normalize_inline_model_bindings
from g3ku.config.schema import Config
from g3ku.llm_config.enums import AuthMode, Capability, ProtocolAdapter
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.llm_config.migration import _cleanup_repository_legacy_default_parameters
from g3ku.llm_config.models import NormalizedProviderConfig

RECORD_WRITE_FILES = (
    "g3ku/config/loader.py",
    "g3ku/llm_config/facade.py",
    "g3ku/llm_config/migration.py",
)

LEGACY_PARAMETERS = {"max_tokens": 4096}


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _record(config_id: str, *, api_key: str, parameters: dict) -> NormalizedProviderConfig:
    now = datetime.now(UTC)
    return NormalizedProviderConfig(
        config_id=config_id,
        provider_id="openai",
        display_name="openai",
        protocol_adapter=ProtocolAdapter.OPENAI_COMPLETIONS,
        capability=Capability.CHAT,
        auth_mode=AuthMode.API_KEY,
        base_url="https://example.com/v1",
        default_model="demo-model",
        auth={"type": "api_key", "api_key": api_key},
        parameters=parameters,
        headers={"X-Trace": "secret-token"},
        extra_options={},
        template_version="test",
        created_at=now,
        updated_at=now,
    )


def _stored_raw(facade: LLMConfigFacade, config_id: str) -> dict:
    path = facade.repository.records_root / f"{config_id}.json"
    return json.loads(path.read_text(encoding="utf-8-sig"))


def test_unlocked_store_moves_the_key_off_disk(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    facade = LLMConfigFacade(workspace)
    monkeypatch.setattr(facade.security, "is_unlocked", lambda: True)
    stored: dict[str, object] = {}
    monkeypatch.setattr(
        facade.security,
        "set_overlay_values",
        lambda updates: stored.update(updates),
    )
    facade.repository.save(_record("cfg-live", api_key="super-secret-key", parameters=LEGACY_PARAMETERS), last_probe_status=None)

    assert _cleanup_repository_legacy_default_parameters(facade) is True

    stored_raw = _stored_raw(facade, "cfg-live")
    assert str((stored_raw.get("auth") or {}).get("api_key") or "") == ""
    assert stored_raw.get("headers") in (None, {})
    assert "super-secret-key" in json.dumps(list(stored.values()), ensure_ascii=False)


def test_locked_project_keeps_the_key_it_cannot_encrypt(tmp_path: Path, monkeypatch) -> None:
    """锁定时覆盖层收不下钥匙，此时清洗等于把密钥丢两次。

    实盘形状：管理页PUT 一颗模型的上下文窗口，内联绑定在保存时被归一成记录，
    若那一步洗掉明文又没进覆盖层，下一次读取直接报 `api_key: API key is required`。
    """
    workspace = _workspace(tmp_path)
    facade = LLMConfigFacade(workspace)
    monkeypatch.setattr(facade.security, "is_unlocked", lambda: False)
    facade.repository.save(_record("cfg-locked", api_key="super-secret-key", parameters=LEGACY_PARAMETERS), last_probe_status=None)

    assert _cleanup_repository_legacy_default_parameters(facade) is True

    stored_raw = _stored_raw(facade, "cfg-locked")
    assert str((stored_raw.get("auth") or {}).get("api_key") or "") == "super-secret-key"


def test_inline_binding_migration_does_not_write_a_plaintext_key(tmp_path: Path, monkeypatch) -> None:
    workspace = _workspace(tmp_path)
    monkeypatch.chdir(workspace)
    config = Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(workspace)}},
            "providers": {"openai": {"apiKey": "super-secret-key", "apiBase": "https://example.com/v1"}},
            "models": {
                "catalog": [
                    {
                        "key": "inline-model",
                        "provider_model": "openai:demo-model",
                        "provider": "openai",
                        "api_key": "super-secret-key",
                        "context_window_tokens": 200000,
                    }
                ],
                "roles": {"ceo": ["inline-model"], "execution": [], "inspection": [], "memory": []},
            },
        }
    )
    facade = LLMConfigFacade(workspace)
    monkeypatch.setattr(facade.security, "is_unlocked", lambda: True)
    monkeypatch.setattr(facade.security, "set_overlay_values", lambda updates: None)

    assert _normalize_inline_model_bindings(config) is True

    stored_raw = _stored_raw(LLMConfigFacade(workspace), config.models.catalog[0].llm_config_id)
    assert str((stored_raw.get("auth") or {}).get("api_key") or "") == ""


def test_record_writes_all_go_through_one_door() -> None:
    """`repository.save(` 只能存在于 facade 的那一个出口里。

    扫文件而不是逐条断言：这条不变量的失效方式正是"新加一处保存，绕过了出口"。
    """
    root = Path(__file__).resolve().parents[2]
    counted = {}
    for relative in RECORD_WRITE_FILES:
        source = (root / relative).read_text(encoding="utf-8")
        counted[relative] = source.count("repository.save(")

    assert counted == {
        "g3ku/config/loader.py": 0,
        "g3ku/llm_config/facade.py": 1,
        "g3ku/llm_config/migration.py": 0,
    }
