"""记忆批次里"确定性写故障"的车道合同：可修的要吃修复轮，不可修的不能混进自动重入队那一档。

实盘形状：一条 `minimal_memory` 带换行的 add 让渲染器抛 ValueError，渲染那行不在
`validate_memory_document` 的保护里，于是错误绕过修复轮被当成 provider 瞬时错误停车，
而那一档正是"队列下次成功就自动重排"的那一档 - 同一个载荷重演必然得到同一个错误。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _cfg(*, document_max: int = 20000, summary_max: int = 300):
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.document.document_max_chars = document_max
    cfg.document.summary_max_chars = summary_max
    cfg.queue.max_wait_seconds = 0
    return cfg


def _manager(tmp_path: Path, **kwargs):
    return _module().MemoryManager(tmp_path, _cfg(**kwargs))


def _add(manager, *, body: str, minimal: str, now_iso: str = "2026-04-25T00:00:00+08:00") -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body, minimal_memory=minimal, source="self", from_user=False, now_iso=now_iso
    )
    return str(row["memory_id"])


def _paint(manager) -> None:
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:30+08:00")


def _session(manager, *, op: str = "write"):
    mod = _module()
    return mod._MemoryToolSession(
        snapshot_text=manager.snapshot_text(),
        notes_dir=manager.notes_dir,
        index_dir=manager.index_dir,
        batch_op=op,
    )


def _batch(manager, *, request_id: str = "write_1"):
    mod = _module()
    return mod.MemoryBatch(
        op="write",
        items=[
            mod.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="把这条长期规则记住",
                created_at="2026-04-25T00:00:32+08:00",
                request_id=request_id,
            )
        ],
    )


def _app_config(tmp_path: Path) -> object:
    schema = importlib.import_module("g3ku.config.schema")
    return schema.Config.model_validate(
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
                "roleIterations": {"ceo": 40, "execution": 16, "inspection": 16, "memory": 6},
                "roleConcurrency": {"ceo": None, "execution": None, "inspection": None, "memory": 1},
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
                    "memory": ["memory-primary"],
                },
            },
            "providers": {"openai": {"apiKey": "", "apiBase": None, "extraHeaders": None}},
        }
    )


def _stage_attempt(manager, monkeypatch, *, payload=None, raises=None):
    """Stand in for one model round: either it hands back a staged batch or it faults."""
    mod = _module()

    async def _attempt(**_kwargs):
        if raises is not None:
            raise raises
        session = _session(manager)
        session.applied_batch = dict(payload or {})
        session.apply_batch_count = 1
        return mod._MemoryAttemptResult(
            session=session,
            usage={"input_tokens": 5, "output_tokens": 7, "cache_read_tokens": 0},
            final_text="",
            request_artifacts=[{"path": "memory/requests/round-1.json"}],
        )

    monkeypatch.setattr(manager, "_run_memory_agent_attempt", _attempt)


def _use_runtime_config(manager_module, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        manager_module,
        "get_runtime_config",
        lambda force=False: (_app_config(tmp_path), 1, False),
        raising=False,
    )


def _valid_add(manager) -> dict:
    return {
        "adds": [
            {
                "content": "回信给外部渠道时先给结论再给依据",
                "minimal_memory": "回信->结论前置",
                "decision_source": "self",
                "note_ref": "",
            }
        ]
    }


def test_multiline_minimal_memory_is_rejected_at_the_argument_door(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        session = _session(manager)
        result = session.apply_batch(
            adds=[
                {
                    "content": "Report total elapsed time",
                    "minimal_memory": "tasks->report-elapsed-time\n第二条关键词",
                    "decision_source": "self",
                }
            ]
        )
        assert "single line" in json.dumps(result, ensure_ascii=False)
        assert session.applied_batch is None, "the batch never stages, so the model fixes it inside the round"

        rewrite_result = session.apply_batch(
            rewrites=[{"id": "abcdef", "content": "新正文", "minimal_memory": "一行\n两行"}]
        )
        assert "single line" in json.dumps(rewrite_result, ensure_ascii=False)
    finally:
        manager.close()


def test_a_summary_shape_fault_names_the_summary_field(tmp_path: Path) -> None:
    """打回文案要点名错的字段：写"content"会让模型去改正文，摘要照旧非法。"""
    mod = _module()
    manager = _manager(tmp_path)
    try:
        _add(manager, body="先核对上游口径再落笔", minimal="口径->先核对")
        _paint(manager)
        session = _session(manager)
        session.applied_batch = {
            "adds": [
                {
                    "content": "整理日报按三档归类",
                    "minimal_memory": "日报->三档\n多余的一行",
                    "decision_source": "self",
                    "note_ref": "",
                }
            ]
        }
        session.apply_batch_count = 1

        with pytest.raises(mod._MemoryAgentValidationError) as caught:
            manager._validate_candidate_state(batch=_batch(manager), before_text=manager.snapshot_text(), session=session)

        assert caught.value.capacity_overflow is False
        assert "minimal_memory must be one line" in str(caught.value)
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_a_batch_that_cannot_even_start_parks_as_agent_fault(tmp_path: Path, monkeypatch) -> None:
    """读不出常驻块就没跑过任何一轮模型：这类确定性故障不许进 provider 那一档。

    provider_error 是唯一会被"下一次成功"自动重排的档，重排回的仍是同一个读不出的库。
    """
    mod = _module()
    manager = _manager(tmp_path)
    try:
        _use_runtime_config(mod, tmp_path, monkeypatch)
        _add(manager, body="承诺时间要给依据", minimal="承诺->给依据")
        _paint(manager)

        def _unreadable() -> str:
            raise ValueError("memory summary must be one non-empty line")

        monkeypatch.setattr(manager, "snapshot_text", _unreadable)
        await manager._append_queue_request(_batch(manager).items[0])

        report = await manager.run_due_batch_once(now_iso="2026-04-25T00:00:40+08:00")

        assert report["status"] == "parked"
        record = manager._read_failed_records()[0]
        assert record["category"] == "agent_fault", "确定性故障不是瞬时错误"
        assert int(record["llm_attempt_count"]) == 0, "这一趟一轮模型都没跑，读数就该是 0"
        assert "non-empty line" in str(record["last_error_text"])
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_a_write_build_fault_parks_as_agent_fault_with_the_rounds_that_ran(
    tmp_path: Path,
    monkeypatch,
) -> None:
    mod = _module()
    manager = _manager(tmp_path)
    try:
        _use_runtime_config(mod, tmp_path, monkeypatch)
        _stage_attempt(manager, monkeypatch, payload=_valid_add(manager))

        def _boom(**_kwargs):
            raise ValueError("boom while turning the output into a document")

        monkeypatch.setattr(manager, "_validate_candidate_state", _boom)
        await manager._append_queue_request(_batch(manager).items[0])

        report = await manager.run_due_batch_once(now_iso="2026-04-25T00:00:40+08:00")

        assert report["status"] == "parked"
        records = manager._read_failed_records()
        assert len(records) == 1
        record = records[0]
        assert record["category"] == "agent_fault"
        assert record["discard_reason"] == "agent_fault"
        assert "boom" in str(record["last_error_text"]), "停车要带得上真实错误文本"
        assert int(record["llm_attempt_count"]) == 1, "这一档不能再硬写 0 次往返"
        assert int((record.get("usage_total") or {}).get("input_tokens") or 0) == 5
        assert record["request_artifact_paths"] == ["memory/requests/round-1.json"], "请求体路径不能丢"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_a_model_call_fault_keeps_the_provider_lane_and_the_round_it_reached(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """非 Provider 包装的调用故障仍是瞬时的：留在会自动重排的那一档，但带上真实读数。"""
    mod = _module()
    manager = _manager(tmp_path)
    try:
        _use_runtime_config(mod, tmp_path, monkeypatch)
        _stage_attempt(manager, monkeypatch, raises=RuntimeError("socket closed mid-stream"))
        await manager._append_queue_request(_batch(manager).items[0])

        report = await manager.run_due_batch_once(now_iso="2026-04-25T00:00:40+08:00")

        assert report["status"] == "parked"
        record = manager._read_failed_records()[0]
        assert record["category"] == "provider_error"
        assert int(record["llm_attempt_count"]) == 1
        assert "socket closed mid-stream" in str(record["last_error_text"])
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_the_success_signal_requeues_a_provider_park_but_never_an_agent_fault(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """确定性故障不许被"下一次成功"重演：重排只认 provider_error 那一档。"""
    mod = _module()
    manager = _manager(tmp_path)
    try:
        _use_runtime_config(mod, tmp_path, monkeypatch)

        _stage_attempt(manager, monkeypatch, payload=_valid_add(manager))

        def _boom(**_kwargs):
            raise ValueError("deterministic write fault")

        monkeypatch.setattr(manager, "_validate_candidate_state", _boom)
        await manager._append_queue_request(
            mod.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="第一条：确定性故障",
                created_at="2026-04-25T00:00:32+08:00",
                request_id="write_fault",
            )
        )
        await manager.run_due_batch_once(now_iso="2026-04-25T00:00:40+08:00")

        _stage_attempt(manager, monkeypatch, raises=RuntimeError("rate limited"))
        await manager._append_queue_request(
            mod.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="第二条：瞬时故障",
                created_at="2026-04-25T00:00:33+08:00",
                request_id="write_transient",
            )
        )
        await manager.run_due_batch_once(now_iso="2026-04-25T00:00:41+08:00")

        records = {str(row.get("request_ids", [""])[0]): row for row in manager._read_failed_records()}
        assert records["write_fault"]["category"] == "agent_fault"
        assert records["write_transient"]["category"] == "provider_error"

        requeued = manager._requeue_next_parked_on_success(now_iso="2026-04-25T00:00:42+08:00")

        assert requeued is not None
        assert requeued["request_ids"] == ["write_transient"], "只有瞬时那一档会被自动重排"
        after = {str(row.get("request_ids", [""])[0]): row for row in manager._read_failed_records()}
        assert after["write_fault"]["status"] == "parked"
    finally:
        manager.close()
