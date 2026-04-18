from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_catalog_store_module_exports_catalog_types_and_store_helpers() -> None:
    from g3ku.agent.catalog_store import (
        ContextRecordV2,
        DashScopeTextReranker,
        G3kuHybridStore,
    )

    record = ContextRecordV2(
        record_id="tool:demo",
        context_type="resource",
        uri="g3ku://resource/tool/demo",
    )

    assert record.record_id == "tool:demo"
    assert DashScopeTextReranker.__name__ == "DashScopeTextReranker"
    assert hasattr(G3kuHybridStore, "purge_process_local_dense_backends")


def test_memory_catalog_bridge_uses_catalog_store_manager_not_legacy_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import g3ku.agent.memory_catalog_bridge as bridge_module

    calls: list[object] = []
    fake_store = object()
    config = SimpleNamespace()

    class FakeCatalogStoreManager:
        def __init__(self, workspace: Path, config: object) -> None:
            calls.append(("init", workspace, config))
            self.store = fake_store

        async def sync_catalog(self, service: object) -> dict[str, object]:
            calls.append(("sync_catalog", service))
            return {"ok": True}

        async def ensure_catalog_bootstrap(self, service: object) -> dict[str, object]:
            calls.append(("ensure_catalog_bootstrap", service))
            return {"ok": True}

        async def semantic_search_context_records(self, **kwargs: object) -> list[object]:
            calls.append(("semantic_search_context_records", kwargs))
            return []

        async def list_context_records(self, **kwargs: object) -> list[object]:
            calls.append(("list_context_records", kwargs))
            return []

        async def put_context_record(self, **kwargs: object) -> None:
            calls.append(("put_context_record", kwargs))

        async def delete_context_record(self, **kwargs: object) -> None:
            calls.append(("delete_context_record", kwargs))

        def close(self) -> None:
            calls.append(("close", None))

    def _legacy_boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("legacy memory manager should not be used by MemoryCatalogBridge")

    monkeypatch.setattr(bridge_module, "CatalogStoreManager", FakeCatalogStoreManager, raising=False)
    monkeypatch.setattr(bridge_module, "LegacyCatalogManager", _legacy_boom, raising=False)

    bridge = bridge_module.MemoryCatalogBridge(tmp_path, config)

    assert calls == [("init", tmp_path, config)]
    assert bridge.store is fake_store

    bridge.close()

    assert calls[-1] == ("close", None)


@pytest.mark.asyncio
async def test_context_catalog_indexer_builds_records_with_catalog_store_context_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import g3ku.runtime.context.catalog as catalog_module
    from g3ku.runtime.context.catalog import ContextCatalogIndexer

    async def _skip_model_summary(*args: object, **kwargs: object) -> tuple[str, str]:
        _ = args, kwargs
        return "", ""

    monkeypatch.setattr(catalog_module, "summarize_layered_model_first", _skip_model_summary)

    class LegacyContextRecordV2:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("legacy ContextRecordV2 should not be used by ContextCatalogIndexer")

    class CatalogContextRecordV2:
        def __init__(self, **kwargs: object) -> None:
            self.__dict__.update(kwargs)

    monkeypatch.setitem(sys.modules, "g3ku.agent.rag_memory", SimpleNamespace(ContextRecordV2=LegacyContextRecordV2))
    monkeypatch.setitem(
        sys.modules,
        "g3ku.agent.catalog_store",
        SimpleNamespace(ContextRecordV2=CatalogContextRecordV2),
    )

    skill_file = tmp_path / "demo_skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text("# Demo Skill\n\nStable body.\n", encoding="utf-8")

    service = SimpleNamespace(
        current_skill=SimpleNamespace(
            skill_id="demo_skill",
            display_name="Demo skill title",
            description="Demo skill summary",
            skill_doc_path=str(skill_file),
        ),
        list_skill_resources=lambda: [service.current_skill],
        list_tool_resources=lambda: [],
    )

    class _MemoryManager:
        def __init__(self) -> None:
            self.config = SimpleNamespace(catalog_summary=SimpleNamespace(model_key=""))
            self.records: dict[str, object] = {}

        @staticmethod
        def _stable_text_hash(text: str) -> str:
            return text

        async def list_context_records(self, *, namespace_prefix=None, limit: int = 200000):
            _ = namespace_prefix, limit
            return list(self.records.values())

        async def put_context_record(self, *, namespace, record) -> None:
            _ = namespace
            self.records[record.record_id] = record

        async def delete_context_record(self, *, namespace, record_id: str) -> None:
            _ = namespace
            self.records.pop(record_id, None)

    memory_manager = _MemoryManager()
    indexer = ContextCatalogIndexer(memory_manager=memory_manager, service=service)

    result = await indexer.sync(skill_ids={"demo_skill"})

    assert result == {"created": 1, "updated": 0, "removed": 0}
    record = memory_manager.records["skill:demo_skill"]
    assert type(record).__name__ == "CatalogContextRecordV2"
    assert record.record_id == "skill:demo_skill"


def test_runtime_bootstrap_bridge_purges_dense_backends_via_catalog_store_module(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import g3ku.runtime.bootstrap_bridge as bootstrap_module

    calls: list[tuple[Path, str]] = []

    class LegacyHybridStore:
        @classmethod
        def purge_process_local_dense_backends(cls, **kwargs: object) -> int:
            raise AssertionError("legacy G3kuHybridStore should not be used during dense backend purge")

    class CatalogHybridStore:
        @classmethod
        def purge_process_local_dense_backends(
            cls,
            *,
            qdrant_path: Path,
            qdrant_collection: str,
        ) -> int:
            calls.append((qdrant_path, qdrant_collection))
            return 1

    monkeypatch.setitem(sys.modules, "g3ku.agent.rag_memory", SimpleNamespace(G3kuHybridStore=LegacyHybridStore))
    monkeypatch.setitem(sys.modules, "g3ku.agent.catalog_store", SimpleNamespace(G3kuHybridStore=CatalogHybridStore))

    loop = SimpleNamespace(workspace=tmp_path)
    cfg = SimpleNamespace(store=SimpleNamespace(qdrant_path="memory/qdrant", qdrant_collection="catalog"))

    bootstrap_module.RuntimeBootstrapBridge(loop)._purge_stale_dense_backends(cfg)

    assert calls == [(tmp_path / "memory" / "qdrant", "catalog")]


def test_frontdoor_catalog_selection_imports_reranker_from_catalog_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "g3ku.runtime.context.frontdoor_catalog_selection"
    sys.modules.pop(module_name, None)

    class LegacyReranker:
        pass

    class CatalogReranker:
        pass

    monkeypatch.setitem(sys.modules, "g3ku.agent.rag_memory", SimpleNamespace(DashScopeTextReranker=LegacyReranker))
    monkeypatch.setitem(
        sys.modules,
        "g3ku.agent.catalog_store",
        SimpleNamespace(DashScopeTextReranker=CatalogReranker),
    )

    selection_module = importlib.import_module(module_name)

    assert selection_module.DashScopeTextReranker is CatalogReranker
