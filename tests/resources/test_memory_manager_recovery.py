from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_memory_agent_runtime_module():
    assert importlib.util.find_spec("g3ku.agent.memory_agent_runtime") is not None
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


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


def _app_config(tmp_path: Path, *, memory_chain: list[str] | None) -> object:
    from g3ku.config.schema import Config

    return Config.model_validate(
        {
            "agents": {
                "defaults": {
                    "workspace": str(tmp_path),
                    "runtime": "langgraph",
                    "maxTokens": 1,
                    "temperature": 0.1,
                    "maxToolIterations": 1,
                    "memoryWindow": 1,
                    "reasoningEffort": "low",
                },
                "roleIterations": {
                    "ceo": 40,
                    "execution": 16,
                    "inspection": 16,
                    "memory": 6,
                },
                "roleConcurrency": {
                    "ceo": None,
                    "execution": None,
                    "inspection": None,
                    "memory": 1,
                },
                "multiAgent": {"orchestratorModelKey": None},
            },
            "models": {
                "catalog": [
                    {
                        "key": "memory-primary",
                        "providerModel": "openai:gpt-4.1",
                        "apiKey": "demo-key",
                        "apiBase": None,
                        "extraHeaders": None,
                        "enabled": True,
                        "maxTokens": 1,
                        "temperature": 0.1,
                        "reasoningEffort": "low",
                        "retryOn": [],
                        "description": "",
                    }
                ],
                "roles": {
                    "ceo": ["memory-primary"],
                    "execution": ["memory-primary"],
                    "inspection": ["memory-primary"],
                    "memory": list(memory_chain or []),
                },
            },
            "providers": {
                "openai": {"apiKey": "", "apiBase": None, "extraHeaders": None},
            },
        }
    )


def _fake_response(
    *,
    content: str = "",
    tool_calls: list[dict[str, object]] | None = None,
    usage: dict[str, int] | None = None,
) -> object:
    usage_payload = dict(usage or {})
    return SimpleNamespace(
        content=content,
        tool_calls=list(tool_calls or []),
        usage_metadata=usage_payload,
        response_metadata={"token_usage": usage_payload},
    )


class _FakeToolCallingModel:
    def __init__(self, responses: list[object]):
        self._responses = list(responses)

    def bind_tools(self, tools):
        _ = tools
        return self

    async def ainvoke(self, messages):
        _ = messages
        if not self._responses:
            raise AssertionError("unexpected extra memory agent call")
        next_item = self._responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_processing_head_waits_until_retry_after(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        manager._write_queue_requests(
            [
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Report total elapsed time",
                    created_at="2026-04-17T10:00:00+08:00",
                    request_id="write_1",
                    status="processing",
                    processing_started_at="2026-04-17T10:00:04+08:00",
                    last_error_text="provider exploded",
                    last_error_at="2026-04-17T10:00:04+08:00",
                    retry_after="2026-04-17T10:00:34+08:00",
                )
            ]
        )

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:20+08:00")
        queue_items = await manager.list_queue(limit=10)

        assert report == {"ok": True, "status": "idle", "processed": 0}
        assert len(queue_items) == 1
        assert queue_items[0]["status"] == "processing"
        assert queue_items[0]["processing_started_at"] == "2026-04-17T10:00:04+08:00"
        assert queue_items[0]["retry_after"] == "2026-04-17T10:00:34+08:00"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_processing_head_retries_after_retry_after(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        manager._write_queue_requests(
            [
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Report total elapsed time",
                    created_at="2026-04-17T10:00:00+08:00",
                    request_id="write_1",
                    status="processing",
                    processing_started_at="2026-04-17T10:00:04+08:00",
                    last_error_text="provider exploded",
                    last_error_at="2026-04-17T10:00:04+08:00",
                    retry_after="2026-04-17T10:00:05+08:00",
                )
            ]
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_write_document",
                            "args": {"content": "---\n2026/4/17-self：\nReport total elapsed time\n"},
                        }
                    ],
                    usage={"input_tokens": 4, "output_tokens": 1, "cache_read_tokens": 0},
                ),
                _fake_response(content="done", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
            ]
        )
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:40+08:00")
        queue_items = await manager.list_queue(limit=10)
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert report["request_ids"] == ["write_1"]
        assert queue_items == []
        assert len(processed) == 1
        assert processed[0]["request_ids"] == ["write_1"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_processing_started_at_survives_restart_retry(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        manager._write_queue_requests(
            [
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Report total elapsed time",
                    created_at="2026-04-17T10:00:00+08:00",
                    request_id="write_1",
                    status="processing",
                    processing_started_at="2026-04-17T10:00:04+08:00",
                    last_error_text="provider exploded",
                    last_error_at="2026-04-17T10:00:04+08:00",
                    retry_after="2026-04-17T10:00:05+08:00",
                )
            ]
        )
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(
            module,
            "build_chat_model",
            lambda config, **kwargs: _FakeToolCallingModel([RuntimeError("provider exploded again")]),
            raising=False,
        )

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:40+08:00")
        queue_items = await manager.list_queue(limit=10)

        assert report["ok"] is False
        assert report["status"] == "error"
        assert len(queue_items) == 1
        assert queue_items[0]["status"] == "processing"
        assert queue_items[0]["processing_started_at"] == "2026-04-17T10:00:04+08:00"
        assert "provider exploded again" in str(queue_items[0]["last_error_text"])
        assert queue_items[0]["last_error_at"] == "2026-04-17T10:00:40+08:00"
        assert queue_items[0]["retry_after"] != ""
        assert _read_jsonl(tmp_path / "memory" / "ops.jsonl") == []
    finally:
        manager.close()
