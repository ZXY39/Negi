from __future__ import annotations

from datetime import UTC, datetime
import json

from g3ku.llm_config.enums import AuthMode, Capability, ProtocolAdapter
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.llm_config.models import NormalizedProviderConfig, StoredConfigSummary
from g3ku.llm_config.repositories import EncryptedConfigRepository


def _config(config_id: str = "cfg-bom") -> NormalizedProviderConfig:
    now = datetime.now(UTC)
    return NormalizedProviderConfig(
        config_id=config_id,
        provider_id="openai",
        display_name="OpenAI",
        protocol_adapter=ProtocolAdapter.OPENAI_RESPONSES,
        capability=Capability.CHAT,
        auth_mode=AuthMode.API_KEY,
        base_url="https://example.com/v1",
        default_model="gpt-5.4",
        auth={"type": "api_key", "api_key": ""},
        parameters={"api_mode": "openai-responses"},
        headers={},
        extra_options={},
        template_version="test",
        created_at=now,
        updated_at=now,
    )


def test_repository_reads_index_with_utf8_bom(tmp_path) -> None:
    storage_root = tmp_path / "llm-config"
    storage_root.mkdir(parents=True, exist_ok=True)
    index_path = storage_root / "index.json"
    payload = {
        "version": 1,
        "configs": [
            StoredConfigSummary(
                config_id="cfg-bom",
                provider_id="openai",
                display_name="OpenAI",
                capability=Capability.CHAT,
                default_model="gpt-5.4",
                last_probe_status="success",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            ).model_dump(mode="json")
        ],
    }
    index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8-sig")

    repo = EncryptedConfigRepository(storage_root, secret_store=None)

    entries = repo.list_summaries()

    assert len(entries) == 1
    assert entries[0].config_id == "cfg-bom"


def test_repository_reads_record_with_utf8_bom(tmp_path) -> None:
    storage_root = tmp_path / "llm-config"
    records_root = storage_root / "records"
    records_root.mkdir(parents=True, exist_ok=True)
    record_path = records_root / "cfg-bom.json"
    record_path.write_text(_config().model_dump_json(indent=2), encoding="utf-8-sig")

    repo = EncryptedConfigRepository(storage_root, secret_store=None)

    loaded = repo.get("cfg-bom")

    assert loaded.config_id == "cfg-bom"
    assert loaded.base_url == "https://example.com/v1"


def test_facade_reads_memory_binding_with_utf8_bom(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    store_root = workspace / ".g3ku" / "llm-config"
    store_root.mkdir(parents=True, exist_ok=True)
    memory_binding_path = store_root / "memory_binding.json"
    memory_binding_path.write_text(
        json.dumps(
            {
                "embedding_config_id": "emb-1",
                "rerank_config_id": "rer-1",
            },
            indent=2,
        ),
        encoding="utf-8-sig",
    )

    facade = LLMConfigFacade(workspace)

    payload, loaded = facade._read_memory_binding_payload()

    assert loaded is True
    assert payload == {
        "embedding_config_id": "emb-1",
        "rerank_config_id": "rer-1",
    }
