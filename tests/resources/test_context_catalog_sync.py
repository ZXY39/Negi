from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import g3ku.runtime.context.catalog as catalog_module
from g3ku.runtime.context.catalog import ContextCatalogIndexer


class _MemoryManager:
    def __init__(self) -> None:
        self.config = SimpleNamespace(catalog_summary=SimpleNamespace(model_key=""))
        self.records: dict[str, object] = {}

    @staticmethod
    def _stable_text_hash(text: str) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

    async def list_context_records(self, *, namespace_prefix=None, limit: int = 200000):
        _ = namespace_prefix, limit
        return list(self.records.values())

    async def put_context_record(self, *, namespace, record) -> None:
        _ = namespace
        self.records[record.record_id] = record

    async def delete_context_record(self, *, namespace, record_id: str) -> None:
        _ = namespace
        self.records.pop(record_id, None)


@pytest.mark.asyncio
async def test_context_catalog_indexer_rebuilds_skill_record_when_only_metadata_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def _skip_model_summary(*args, **kwargs):
        _ = args, kwargs
        return "", ""

    monkeypatch.setattr(catalog_module, "summarize_layered_model_first", _skip_model_summary)

    skill_file = tmp_path / "demo_skill" / "SKILL.md"
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    skill_file.write_text("# Demo Skill\n\nStable body.\n", encoding="utf-8")

    service = SimpleNamespace(
        current_skill=SimpleNamespace(
            skill_id="demo_skill",
            display_name="Old skill title",
            description="Old skill summary",
            skill_doc_path=str(skill_file),
        ),
        list_skill_resources=lambda: [service.current_skill],
        list_tool_resources=lambda: [],
    )
    memory_manager = _MemoryManager()
    indexer = ContextCatalogIndexer(memory_manager=memory_manager, service=service)

    first = await indexer.sync(skill_ids={"demo_skill"})
    service.current_skill = SimpleNamespace(
        skill_id="demo_skill",
        display_name="New skill title",
        description="New skill summary",
        skill_doc_path=str(skill_file),
    )
    second = await indexer.sync(skill_ids={"demo_skill"})

    record = memory_manager.records["skill:demo_skill"]
    assert first == {"created": 1, "updated": 0, "removed": 0}
    assert second == {"created": 0, "updated": 1, "removed": 0}
    assert record.l0 == "New skill title"
    assert "New skill summary" in record.l1


@pytest.mark.asyncio
async def test_context_catalog_indexer_rebuilds_tool_record_when_only_metadata_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _skip_model_summary(*args, **kwargs):
        _ = args, kwargs
        return "", ""

    monkeypatch.setattr(catalog_module, "summarize_layered_model_first", _skip_model_summary)

    tool_body = "# Demo Tool\n\nStable toolskill body.\n"
    service = SimpleNamespace(
        current_family=SimpleNamespace(
            tool_id="demo_tool",
            display_name="Old tool title",
            description="Old tool summary",
        ),
        current_toolskill={
            "tool_id": "demo_tool",
            "description": "Old tool summary",
            "content": tool_body,
            "path": "",
        },
        list_skill_resources=lambda: [],
        list_tool_resources=lambda: [service.current_family],
        get_tool_toolskill=lambda _tool_id: dict(service.current_toolskill),
    )
    memory_manager = _MemoryManager()
    indexer = ContextCatalogIndexer(memory_manager=memory_manager, service=service)

    first = await indexer.sync(tool_ids={"demo_tool"})
    service.current_family = SimpleNamespace(
        tool_id="demo_tool",
        display_name="New tool title",
        description="New tool summary",
    )
    service.current_toolskill = {
        "tool_id": "demo_tool",
        "description": "New tool summary",
        "content": tool_body,
        "path": "",
    }
    second = await indexer.sync(tool_ids={"demo_tool"})

    record = memory_manager.records["tool:demo_tool"]
    assert first == {"created": 1, "updated": 0, "removed": 0}
    assert second == {"created": 0, "updated": 1, "removed": 0}
    assert record.l0 == "New tool title"
    assert "New tool summary" in record.l1
