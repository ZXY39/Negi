from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest


def _load_markdown_memory_module():
    assert importlib.util.find_spec("g3ku.agent.markdown_memory") is not None
    return importlib.import_module("g3ku.agent.markdown_memory")


def _load_memory_agent_runtime_module():
    assert importlib.util.find_spec("g3ku.agent.memory_agent_runtime") is not None
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def test_format_memory_entry_uses_minimal_block_shape() -> None:
    module = _load_markdown_memory_module()
    entry = module.MemoryEntry(
        date_text="2026/4/17",
        source="self",
        summary="完成任务必须说明任务总耗时",
        note_ref="",
    )

    assert module.format_memory_entry(entry) == (
        "---\n"
        "2026/4/17-self：\n"
        "完成任务必须说明任务总耗时\n"
    )


def test_parse_memory_document_round_trips_user_and_self_entries() -> None:
    module = _load_markdown_memory_module()
    text = (
        "---\n"
        "2026/4/17-self：\n"
        "完成任务必须说明任务总耗时\n\n"
        "---\n"
        "2026/4/17-user：\n"
        "创建文件默认格式要求，见 ref:note_a1b2\n"
    )

    items = module.parse_memory_document(text)

    assert [item.source for item in items] == ["self", "user"]
    assert items[0].summary == "完成任务必须说明任务总耗时"
    assert items[1].note_ref == "note_a1b2"


def test_validate_memory_document_rejects_summary_over_100_chars() -> None:
    module = _load_markdown_memory_module()
    text = (
        "---\n"
        "2026/4/17-self：\n"
        f"{'x' * 101}\n"
    )

    with pytest.raises(ValueError, match="summary line exceeds 100 chars"):
        module.validate_memory_document(text, summary_max_chars=100, document_max_chars=10000)


def test_validate_memory_document_rejects_document_over_10000_chars() -> None:
    module = _load_markdown_memory_module()
    body = (
        "---\n"
        "2026/4/17-self：\n"
        "短句\n"
    )
    oversized = body + ("x" * 10001)

    with pytest.raises(ValueError, match="memory document exceeds 10000 chars"):
        module.validate_memory_document(oversized, summary_max_chars=100, document_max_chars=10000)


def _memory_cfg():
    from g3ku.config.schema import MemoryToolsConfig

    payload = MemoryToolsConfig().model_dump(mode="python")
    payload["document"] = {
        "summary_max_chars": 100,
        "document_max_chars": 10000,
        "memory_file": "memory/MEMORY.md",
        "notes_dir": "memory/notes",
    }
    payload["queue"] = {
        "queue_file": "memory/queue.jsonl",
        "ops_file": "memory/ops.jsonl",
        "batch_max_chars": 50,
        "max_wait_seconds": 3,
        "review_interval_turns": 10,
    }
    return MemoryToolsConfig.model_validate(payload)


@pytest.mark.asyncio
async def test_collect_due_batch_stops_at_mixed_operation_boundary(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="A" * 10,
                created_at="2026-04-17T10:00:00+08:00",
            )
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="B" * 10,
                created_at="2026-04-17T10:00:01+08:00",
            )
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="目标记忆",
                created_at="2026-04-17T10:00:02+08:00",
            )
        )

        batch = await manager.collect_due_batch(now_iso="2026-04-17T10:00:04+08:00")

        assert batch is not None
        assert batch.op == "write"
        assert [item.op for item in batch.items] == ["write", "write"]
        assert all(item.payload_text != "目标记忆" for item in batch.items)
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_collect_due_batch_respects_char_limit_before_wait_limit(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="A" * 30,
                created_at="2026-04-17T10:00:00+08:00",
            )
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="B" * 30,
                created_at="2026-04-17T10:00:01+08:00",
            )
        )

        batch = await manager.collect_due_batch(now_iso="2026-04-17T10:00:01+08:00")

        assert batch is not None
        assert sum(len(item.payload_text) for item in batch.items) == 30
        assert len(batch.items) == 1
    finally:
        manager.close()
