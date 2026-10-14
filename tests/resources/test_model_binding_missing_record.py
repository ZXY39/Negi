from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from g3ku.config.schema import Config
from g3ku.llm_config.enums import AuthMode, Capability, ProtocolAdapter
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.llm_config.models import NormalizedProviderConfig
from g3ku.providers.chatmodels import build_chat_model


def _workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _facade(workspace):
    return LLMConfigFacade(workspace)


def _save_record(facade: LLMConfigFacade, config_id: str, *, provider_id: str, model: str) -> None:
    now = datetime.now(UTC)
    facade.repository.save(
        NormalizedProviderConfig(
            config_id=config_id,
            provider_id=provider_id,
            display_name=provider_id,
            protocol_adapter=ProtocolAdapter.OPENAI_COMPLETIONS,
            capability=Capability.CHAT,
            auth_mode=AuthMode.API_KEY,
            base_url="https://example.com/v1",
            default_model=model,
            auth={"type": "api_key", "api_key": "demo-key"},
            parameters={"context_window_tokens": 200000},
            headers={},
            extra_options={},
            template_version="test",
            created_at=now,
            updated_at=now,
        )
    )


def _config(workspace) -> Config:
    """CEO 链首位指向一条已经不存在的凭据记录。"""
    return Config.model_validate(
        {
            "agents": {"defaults": {"workspace": str(workspace)}},
            "models": {
                "catalog": [
                    {"key": "gone-model", "llm_config_id": "cfg-gone", "enabled": True},
                    {"key": "live-model", "llm_config_id": "cfg-live", "enabled": True},
                ],
                "roles": {
                    "ceo": ["gone-model", "live-model"],
                    "execution": [],
                    "inspection": [],
                    "memory": [],
                },
            },
        }
    )


def test_record_exists_only_matches_a_stored_record(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    facade = _facade(workspace)

    _save_record(facade, "cfg-live", provider_id="openai", model="demo-model")

    assert facade.record_exists("cfg-live") is True
    assert facade.record_exists("cfg-gone") is False


def test_role_head_with_missing_record_falls_to_next_chain_member(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    facade = _facade(workspace)
    config = _config(workspace)
    _save_record(facade, "cfg-live", provider_id="openai", model="demo-model")

    assert config.resolve_role_model_key("ceo") == "live-model"
    assert build_chat_model(config, role="ceo").get_default_model() == "live-model"
    # 结构视图照旧报出配置的两位：坏掉的那一位要留在链编辑里让操作员看得见、删得掉。
    assert [target.model_key for target in config.get_scope_model_chain("ceo")] == ["gone-model", "live-model"]


def test_unusable_chain_names_every_broken_binding(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    _facade(workspace)
    config = _config(workspace)

    with pytest.raises(ValueError) as excinfo:
        config.resolve_role_model_key("ceo")

    message = str(excinfo.value)
    assert "gone-model" in message
    assert "live-model" in message
    assert "凭据记录" in message


def test_model_list_reports_broken_row_instead_of_failing(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    facade = _facade(workspace)
    config = _config(workspace)
    _save_record(facade, "cfg-live", provider_id="openai", model="demo-model")

    items = {str(item["key"]): item for item in facade.list_bindings(config)}

    assert set(items) == {"gone-model", "live-model"}
    assert items["gone-model"]["record_missing"] is True
    assert items["gone-model"]["llm_config_id"] == "cfg-gone"
    assert items["gone-model"]["api_key"] == ""
    assert items["live-model"]["record_missing"] is False
    assert items["live-model"]["provider_model"] == "openai:demo-model"


def test_broken_row_detail_and_disable_stay_readable(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    facade = _facade(workspace)
    config = _config(workspace)

    assert facade.describe_binding(config, "gone-model")["record_missing"] is True

    payload = facade.set_binding_enabled(config, "gone-model", False)

    assert payload["enabled"] is False
    assert payload["record_missing"] is True


def test_broken_row_can_be_deleted_even_though_the_record_is_gone(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    facade = _facade(workspace)
    config = _config(workspace)

    facade.delete_binding(config, "gone-model")

    assert config.get_managed_model("gone-model") is None
    assert config.get_managed_model("live-model") is not None


def test_model_page_marks_the_broken_row_and_still_opens_it() -> None:
    llm_js = (Path(__file__).resolve().parents[2] / "g3ku/web/frontend/org_graph_llm.js").read_text(encoding="utf-8")

    # 模型页由 org_graph_llm.js 渲染（app 侧同名函数被它覆盖），两处都要认这个字段。
    assert "item.record_missing" in llm_js
    assert "凭据记录缺失" in llm_js
    # 坏行开详情不再打那次必然 404 的记录读取，否则「删除配置」这条修路够不到。
    assert "binding.record_missing ? null : await ApiClient.getLlmConfig" in llm_js
