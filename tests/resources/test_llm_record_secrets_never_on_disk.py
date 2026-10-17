"""凭据记录落盘必须先去钥匙：明文 api_key 不许进 `records/*.json`。

覆盖层的存在理由就是"密钥不落明文盘"。正常创建/更新车道都先
`_sanitize_record_for_storage` 再 `_store_record_secrets`；迁移与内联绑定归一那两条道
曾经把整份记录（含 `_hydrate_record_secrets` 贴回来的明文钥匙）直接交给 `save()`，
等于每次跑迁移就往盘上重印一遍密钥，而且从没进过覆盖层。
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

LIVE_RECORD_WRITE_FILES = (
    "g3ku/config/loader.py",
    "g3ku/llm_config/facade.py",
    "g3ku/llm_config/migration.py",
)


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


def test_legacy_parameter_cleanup_does_not_reprint_the_key(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    facade = LLMConfigFacade(workspace)
    # 4096 是旧版默认 max_tokens，正是这条清理道要改的形状。
    facade.repository.save(_record("cfg-legacy", api_key="super-secret-key", parameters={"max_tokens": 4096}), last_probe_status=None)

    assert _cleanup_repository_legacy_default_parameters(facade) is True

    stored = _stored_raw(facade, "cfg-legacy")
    assert str((stored.get("auth") or {}).get("api_key") or "") == ""
    assert stored.get("headers") in (None, {})
    assert "max_tokens" not in (stored.get("parameters") or {})


def test_inline_binding_migration_does_not_write_a_plaintext_key(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
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

    assert _normalize_inline_model_bindings(config) is True

    facade = LLMConfigFacade(workspace)
    stored = _stored_raw(facade, config.models.catalog[0].llm_config_id)
    assert str((stored.get("auth") or {}).get("api_key") or "") == ""


def test_every_live_record_write_sanitizes_before_saving(tmp_path: Path) -> None:
    """`repository.save(` 的每一个活车道调用都必须先把钥匙摘掉。

    扫描而非逐条断言：这条不变量的失效方式正是"新加一处保存，忘了套清洗"。
    """
    root = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    for relative in LIVE_RECORD_WRITE_FILES:
        source = (root / relative).read_text(encoding="utf-8")
        for index in range(len(source)):
            if not source.startswith("repository.save(", index):
                continue
            depth = 0
            end = index + len("repository.save")
            while end < len(source):
                char = source[end]
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            statement = source[index : end + 1]
            if "_sanitize_record_for_storage" not in statement:
                line_no = source[:index].count("\n") + 1
                offenders.append(f"{relative}:{line_no}")

    assert offenders == []
