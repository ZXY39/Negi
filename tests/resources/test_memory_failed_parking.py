"""失败记忆停车区（memory/failed.jsonl）行为测试。

覆盖：provider 错误诊断透传、停车代替 durable discard、成功信号自动重排、
协议违规仅手动重试、手动重试 / 放弃、放弃后的幂等去重、自动重排开关。
"""

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
        "summary_max_chars": 250,
        "document_max_chars": 20000,
        "memory_file": "memory/MEMORY.md",
        "notes_dir": "memory/notes",
    }
    payload["queue"] = {
        "queue_file": "memory/queue.jsonl",
        "ops_file": "memory/ops.jsonl",
        "failed_file": "memory/failed.jsonl",
        "batch_max_chars": 50,
        "max_wait_seconds": 3,
        "review_interval_turns": 5,
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
    response_metadata: dict[str, object] | None = None,
) -> object:
    usage_payload = dict(usage or {})
    metadata = {"token_usage": usage_payload}
    metadata.update(dict(response_metadata or {}))
    return SimpleNamespace(
        content=content,
        tool_calls=list(tool_calls or []),
        usage_metadata=usage_payload,
        response_metadata=metadata,
    )


def _provider_error_response(error_text: str) -> object:
    """模拟 provider 层把 429/超时等异常转成错误响应返回（finish_reason=error、无 usage）。"""
    return _fake_response(
        content=error_text,
        response_metadata={"finish_reason": "error", "error_text": error_text, "usage": {}},
    )


def _apply_batch_response(content_text: str, minimal: str) -> object:
    return _fake_response(
        tool_calls=[
            {
                "id": "call-1",
                "name": "memory_apply_batch",
                "args": {
                    "adds": [
                        {
                            "content": content_text,
                            "minimal_memory": minimal,
                            "decision_source": "user",
                        }
                    ]
                },
            }
        ],
        usage={"input_tokens": 4, "output_tokens": 1, "cache_read_tokens": 0},
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


def _wire_runtime(module, manager, tmp_path: Path, monkeypatch, fake_model) -> None:
    monkeypatch.setattr(
        module,
        "get_runtime_config",
        lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
        raising=False,
    )
    monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/18", raising=False)
    monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)


@pytest.mark.asyncio
async def test_provider_error_response_parks_batch_with_real_error_and_no_terminal_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Remember the 429 storm case",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_429",
            )
        )
        fake_model = _FakeToolCallingModel([_provider_error_response("RateLimitError: 429 rpm exhausted")])
        _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["status"] == "parked"
        assert report["discard_reason"] == "provider_error"
        assert report["category"] == "provider_error"
        assert await manager.list_queue(limit=10) == []
        # 停车不写终态：request_id 不进入已处理集合，重入队后不会被去重误删
        assert processed == []
        assert len(failed) == 1
        record = failed[0]
        assert record["failed_id"] == report["failed_id"]
        assert record["status"] == "parked"
        assert record["category"] == "provider_error"
        assert record["request_ids"] == ["write_429"]
        # 真实 provider 错误透传，而不是误导性的 memory_apply_batch 校验错误
        assert "429 rpm exhausted" in record["last_error_text"]
        assert record["items"][0]["payload_text"] == "Remember the 429 storm case"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_agent_exception_parks_batch_as_provider_error(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Remember the exception case",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_exc",
            )
        )
        fake_model = _FakeToolCallingModel([RuntimeError("model chain exhausted: 429")])
        _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")

        assert report["status"] == "parked"
        assert report["discard_reason"] == "agent_exception"
        assert report["category"] == "provider_error"
        assert len(failed) == 1
        assert "model chain exhausted" in failed[0]["last_error_text"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_success_signal_requeues_provider_error_and_purges_after_apply(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="First memory hit by rate limit",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_first",
            )
        )
        fake_model = _FakeToolCallingModel([_provider_error_response("RateLimitError: 429 rpm exhausted")])
        _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)

        parked = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        assert parked["status"] == "parked"

        # 新记忆入队并成功应用 → 成功信号把停车记录自动重排到队尾
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Second memory succeeds",
                created_at="2026-04-18T10:01:00+08:00",
                request_id="write_second",
            )
        )
        fake_model._responses = [
            _apply_batch_response("Second memory succeeds", "second->succeeds"),
            _fake_response(content="done", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
        ]
        applied = await manager.run_due_batch_once(now_iso="2026-04-18T10:01:05+08:00")
        assert applied["status"] == "applied"
        assert applied["requeued_failed_id"] == parked["failed_id"]

        queue_rows = await manager.list_queue(limit=10)
        assert [row["request_id"] for row in queue_rows] == ["write_first"]
        assert queue_rows[0]["status"] == "pending"
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")
        assert len(failed) == 1
        assert failed[0]["status"] == "requeued"
        assert failed[0]["auto_requeue_count"] == 1

        # 重排后的批次这次成功 → 停车记录被清除
        fake_model._responses = [
            _apply_batch_response("First memory hit by rate limit", "first->recovered"),
            _fake_response(content="done", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
        ]
        reapplied = await manager.run_due_batch_once(now_iso="2026-04-18T10:02:05+08:00")
        assert reapplied["status"] == "applied"
        assert _read_jsonl(tmp_path / "memory" / "failed.jsonl") == []
        assert await manager.list_queue(limit=10) == []
        snapshot = manager.snapshot_text()
        assert "First memory hit by rate limit" in snapshot
        assert "Second memory succeeds" in snapshot
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_protocol_failure_is_not_requeued_by_success_signal(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    cfg.agent.repair_attempt_limit = 0  # 单次尝试即停车，减少 fake 响应数量
    manager = module.MemoryManager(tmp_path, cfg)
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Protocol violating memory",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_proto",
            )
        )
        fake_model = _FakeToolCallingModel(
            [_fake_response(content="no tool call at all", usage={"input_tokens": 2, "output_tokens": 1, "cache_read_tokens": 0})]
        )
        _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)

        parked = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        assert parked["status"] == "parked"
        assert parked["category"] == "protocol"
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")
        assert failed[0]["category"] == "protocol"
        assert "memory_apply_batch" in failed[0]["last_error_text"]
        # 真实 usage 被保留（与 provider 错误的 0 usage 形成对照）
        assert failed[0]["usage_total"]["input_tokens"] == 2

        # 后续批次成功 → 协议违规记录不参与自动重排
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Another memory succeeds",
                created_at="2026-04-18T10:01:00+08:00",
                request_id="write_ok",
            )
        )
        fake_model._responses = [
            _apply_batch_response("Another memory succeeds", "another->succeeds"),
            _fake_response(content="done", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
        ]
        applied = await manager.run_due_batch_once(now_iso="2026-04-18T10:01:05+08:00")
        assert applied["status"] == "applied"
        assert "requeued_failed_id" not in applied
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")
        assert len(failed) == 1
        assert failed[0]["status"] == "parked"
        assert failed[0]["auto_requeue_count"] == 0
        assert await manager.list_queue(limit=10) == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_auto_requeue_on_success_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    cfg.queue.auto_requeue_on_success = False
    manager = module.MemoryManager(tmp_path, cfg)
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Rate limited memory",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_rl",
            )
        )
        fake_model = _FakeToolCallingModel([_provider_error_response("RateLimitError: 429")])
        _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)
        parked = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        assert parked["status"] == "parked"

        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Fresh memory succeeds",
                created_at="2026-04-18T10:01:00+08:00",
                request_id="write_fresh",
            )
        )
        fake_model._responses = [
            _apply_batch_response("Fresh memory succeeds", "fresh->succeeds"),
            _fake_response(content="done", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
        ]
        applied = await manager.run_due_batch_once(now_iso="2026-04-18T10:01:05+08:00")
        assert applied["status"] == "applied"
        assert "requeued_failed_id" not in applied
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")
        assert failed[0]["status"] == "parked"
        assert await manager.list_queue(limit=10) == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_manual_retry_requeues_and_repark_keeps_history(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Memory that keeps failing",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_bad",
            )
        )
        fake_model = _FakeToolCallingModel([_provider_error_response("RateLimitError: 429 first")])
        _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)

        parked = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        failed_id = parked["failed_id"]

        retry_summary = await manager.retry_failed_record(failed_id, reason="operator")
        assert retry_summary["trigger"] == "manual"
        assert retry_summary["request_ids"] == ["write_bad"]
        queue_rows = await manager.list_queue(limit=10)
        assert [row["request_id"] for row in queue_rows] == ["write_bad"]
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")
        assert failed[0]["status"] == "requeued"
        assert failed[0]["manual_retry_count"] == 1

        # 非 parked 状态不允许再次手动重试
        with pytest.raises(ValueError):
            await manager.retry_failed_record(failed_id)
        # 不存在的记录报 KeyError（admin API 映射为 404）
        with pytest.raises(KeyError):
            await manager.retry_failed_record("failed_missing")

        # 重排后再次失败 → 同一记录二次停车，错误历史累积
        fake_model._responses = [_provider_error_response("RateLimitError: 429 second")]
        reparked = await manager.run_due_batch_once(now_iso="2026-04-18T10:02:05+08:00")
        assert reparked["status"] == "parked"
        assert reparked["failed_id"] == failed_id
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")
        assert len(failed) == 1
        assert failed[0]["status"] == "parked"
        assert failed[0]["park_count"] == 2
        assert "429 second" in failed[0]["last_error_text"]
        history_errors = [entry.get("error") for entry in failed[0]["error_history"] if entry.get("error")]
        assert any("429 first" in str(text) for text in history_errors)
        assert any("429 second" in str(text) for text in history_errors)
        assert any(entry.get("event") == "requeued" for entry in failed[0]["error_history"])
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_manual_discard_writes_terminal_record_and_dedupes_request_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Memory the operator gives up on",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_gone",
            )
        )
        fake_model = _FakeToolCallingModel([_provider_error_response("RateLimitError: 429")])
        _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)
        parked = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        failed_id = parked["failed_id"]

        summary = await manager.discard_failed_record(failed_id, reason="operator-give-up")
        assert summary["status"] == "discarded"
        assert summary["request_ids"] == ["write_gone"]

        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")
        assert len(processed) == 1
        assert processed[0]["status"] == "discarded"
        assert processed[0]["discard_reason"] == "operator_discarded"
        assert processed[0]["request_ids"] == ["write_gone"]
        assert "429" in processed[0]["error"]
        assert _read_jsonl(tmp_path / "memory" / "failed.jsonl") == []

        # 放弃后 request_id 进入已处理集合：残留/重复入队会被幂等去重
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Memory the operator gives up on",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_gone",
            )
        )
        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:05:00+08:00")
        assert report["status"] == "already_processed"
        assert await manager.list_queue(limit=10) == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_list_failed_page_returns_parked_records_newest_first(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        for index in range(2):
            await manager._append_queue_request(
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="user",
                    payload_text=f"Rate limited memory {index}",
                    created_at=f"2026-04-18T10:0{index}:00+08:00",
                    request_id=f"write_rl_{index}",
                )
            )
            fake_model = _FakeToolCallingModel([_provider_error_response(f"RateLimitError: 429 #{index}")])
            _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)
            report = await manager.run_due_batch_once(now_iso=f"2026-04-18T10:0{index}:30+08:00")
            assert report["status"] == "parked"

        page = await manager.list_failed_page(limit=10, offset=0)
        assert page["total"] == 2
        assert page["has_more"] is False
        assert [item["request_ids"][0] for item in page["items"]] == ["write_rl_1", "write_rl_0"]
    finally:
        manager.close()


async def test_parking_emits_audit_event_with_memory_source(tmp_path: Path, monkeypatch) -> None:
    """停车即向日志审计池发射 memory_batch_parked（审计只读，操作面仍在记忆板块）。"""
    module = _load_memory_agent_runtime_module()
    from g3ku import audit_events

    audit_events.configure_audit_sink(tmp_path)
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Remember the audit case",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_audit_1",
            )
        )
        fake_model = _FakeToolCallingModel([_provider_error_response("RateLimitError: 429 audit storm")])
        _wire_runtime(module, manager, tmp_path, monkeypatch, fake_model)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        assert report["status"] == "parked"

        audit_file = tmp_path / ".g3ku" / "audit.jsonl"
        assert audit_file.exists()
        records = [
            json.loads(line)
            for line in audit_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) == 1
        record = records[0]
        assert record["subsystem"] == "memory"
        assert record["level"] == "error"
        assert record["event_type"] == "memory_batch_parked"
        assert record["summary"].startswith("记忆批次停车：")
        assert record["detail"]["failed_id"] == report["failed_id"]
        assert record["detail"]["category"] == "provider_error"
        assert record["detail"]["request_count"] == 1
        assert "429" in record["detail"]["error_text"]
    finally:
        manager.close()
        audit_events.configure_audit_sink(None)
