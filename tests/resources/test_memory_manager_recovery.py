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
        "document_max_chars": 10000,
        "memory_file": "memory/MEMORY.md",
        "notes_dir": "memory/notes",
    }
    payload["queue"] = {
        "queue_file": "memory/queue.jsonl",
        "ops_file": "memory/ops.jsonl",
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


def test_memory_runtime_loads_memory_agent_and_assessor_prompts_from_main_prompts(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        system_prompt = manager._memory_agent_system_prompt()
        assessor_prompt = manager._memory_assessor_system_prompt()
    finally:
        manager.close()

    assert "记忆" in system_prompt
    assert "角色" in system_prompt
    assert "职责" in system_prompt
    assert "可用工具" in system_prompt
    assert "处理规则" in system_prompt
    assert "条件+要求" in system_prompt
    assert "优先使用 rewrite" in system_prompt
    assert "不得使用 adds" in system_prompt
    assert "noop_reason" in system_prompt
    assert "250" in system_prompt
    assert "null" in assessor_prompt.lower()


@pytest.mark.asyncio
async def test_v2_run_due_batch_once_applies_write_batch_with_memory_apply_batch(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Prefer concise answers",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_1",
            )
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_apply_batch",
                            "args": {
                                "adds": [
                                    {
                                        "content": "Prefer concise answers",
                                        "decision_source": "user",
                                    }
                                ]
                            },
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
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/18", raising=False)
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        snapshot_text = manager.snapshot_text()

        assert report["status"] == "applied"
        assert "id:" in snapshot_text
        assert "2026/4/18-user：" in snapshot_text
        assert "Prefer concise answers" in snapshot_text
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_run_due_batch_once_accepts_explicit_noop_reason_without_changing_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    markdown_memory = importlib.import_module("g3ku.agent.markdown_memory")
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            markdown_memory.format_memory_entry(
                markdown_memory.MemoryEntry(
                    memory_id="Ab12Z9",
                    date_text="2026/4/18",
                    source="user",
                    summary="Prefer concise answers",
                )
            ),
            encoding="utf-8",
        )
        before_snapshot = manager.snapshot_text()
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Prefer concise answers",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_1",
            )
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_apply_batch",
                            "args": {
                                "noop_reason": "duplicate_of:Ab12Z9",
                            },
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

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert manager.snapshot_text() == before_snapshot
        assert len(processed) == 1
        assert processed[0]["request_ids"] == ["write_1"]
        assert processed[0]["noop_reason"] == "duplicate_of:Ab12Z9"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_memory_runtime_reloads_model_chain_between_repair_attempts_after_runtime_refresh(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())

    def _app_config_for_memory_chain(chain: list[str]) -> object:
        return SimpleNamespace(
            get_role_model_keys=lambda role: list(chain) if str(role) == "memory" else ["memory-old"],
            get_role_max_iterations=lambda role: 6 if str(role) == "memory" else 1,
        )

    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Prefer concise answers",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_1",
            )
        )

        runtime_states = iter(
            [
                (_app_config_for_memory_chain(["memory-old"]), 1, False),
                (_app_config_for_memory_chain(["memory-new"]), 2, True),
            ]
        )
        seen_model_chains: list[list[str]] = []

        def _get_runtime_config(force: bool = False):
            _ = force
            try:
                return next(runtime_states)
            except StopIteration:
                return (_app_config_for_memory_chain(["memory-new"]), 2, False)

        def _build_chat_model(config, **kwargs):
            _ = kwargs
            model_chain = list(config.get_role_model_keys("memory"))
            seen_model_chains.append(model_chain)
            if model_chain == ["memory-old"]:
                return _FakeToolCallingModel(
                    [
                        _fake_response(
                            tool_calls=[
                                {
                                    "id": "call-old-1",
                                    "name": "memory_apply_batch",
                                    "args": {
                                        "rewrites": [
                                            {
                                                "id": "MissingMemoryId",
                                                "content": "Prefer concise answers",
                                            }
                                        ]
                                    },
                                }
                            ],
                            usage={"input_tokens": 4, "output_tokens": 1, "cache_read_tokens": 0},
                        ),
                        _fake_response(
                            content="retry",
                            usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
                        ),
                    ]
                )
            if model_chain == ["memory-new"]:
                return _FakeToolCallingModel(
                    [
                        _fake_response(
                            tool_calls=[
                                {
                                    "id": "call-new-1",
                                    "name": "memory_apply_batch",
                                    "args": {
                                        "adds": [
                                            {
                                                "content": "Prefer concise answers",
                                                "decision_source": "user",
                                            }
                                        ]
                                    },
                                }
                            ],
                            usage={"input_tokens": 4, "output_tokens": 1, "cache_read_tokens": 0},
                        ),
                        _fake_response(
                            content="done",
                            usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
                        ),
                    ]
                )
            raise AssertionError(f"unexpected memory model chain: {model_chain}")

        monkeypatch.setattr(module, "get_runtime_config", _get_runtime_config, raising=False)
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/18", raising=False)
        monkeypatch.setattr(module, "build_chat_model", _build_chat_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert report["attempt_count"] == 2
        assert "Prefer concise answers" in manager.snapshot_text()
        assert len(processed) == 1
        assert processed[0]["model_chain"] == ["memory-new"]
        assert seen_model_chains == [["memory-old"], ["memory-new"]]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_run_due_batch_once_rewrite_preserves_id_and_source_but_refreshes_date(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            "---\nid:Ab12Z9\n2026/4/17-user：\nPrefer concise answers\n",
            encoding="utf-8",
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Update memory",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_1",
            )
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_apply_batch",
                            "args": {
                                "rewrites": [
                                    {
                                        "id": "Ab12Z9",
                                        "content": "Prefer concise English headings",
                                    }
                                ]
                            },
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
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/18", raising=False)
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)

        await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        snapshot_text = manager.snapshot_text()

        assert "id:Ab12Z9" in snapshot_text
        assert "2026/4/18-user：" in snapshot_text
        assert "Prefer concise English headings" in snapshot_text
        assert "2026/4/17-user：" not in snapshot_text
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_run_due_batch_once_delete_batch_removes_memory_by_id(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            (
                "---\nid:Ab12Z9\n2026/4/17-user：\nPrefer concise answers\n"
                "---\nid:Cd34Ef\n2026/4/17-self：\nReport total elapsed time\n"
            ),
            encoding="utf-8",
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="user",
                payload_text="Ab12Z9",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="delete_1",
            )
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_apply_batch",
                            "args": {"deletes": ["Ab12Z9"]},
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

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        snapshot_text = manager.snapshot_text()

        assert report["status"] == "applied"
        assert "id:Ab12Z9" not in snapshot_text
        assert "id:Cd34Ef" in snapshot_text
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_run_due_batch_once_drops_assess_batch_when_assessor_returns_null(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="assess",
                decision_source="self",
                payload_text="window payload",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="assess_1",
                session_key="web:shared",
            )
        )
        assessor_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "assess-call-1",
                            "name": "memory_assessment_result",
                            "args": {"content": "null"},
                        }
                    ],
                    usage={"input_tokens": 2, "output_tokens": 1, "cache_read_tokens": 0},
                )
            ]
        )
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: assessor_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["status"] == "discarded"
        assert report["discard_reason"] == "assessed_null"
        assert await manager.list_queue(limit=10) == []
        assert len(processed) == 1
        assert processed[0]["status"] == "discarded"
        assert processed[0]["discard_reason"] == "assessed_null"
        assert processed[0]["request_ids"] == ["assess_1"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_run_due_batch_once_records_rejected_assess_batch_in_processed_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="assess",
                decision_source="self",
                payload_text="window payload",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="assess_1",
                session_key="web:shared",
            )
        )
        assessor_model = _FakeToolCallingModel(
            [
                _fake_response(
                    content="plain text reply without tool call",
                    usage={"input_tokens": 2, "output_tokens": 1, "cache_read_tokens": 0},
                )
            ]
        )
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: assessor_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["status"] == "discarded"
        assert report["discard_reason"] == "rejected"
        assert await manager.list_queue(limit=10) == []
        assert len(processed) == 1
        assert processed[0]["status"] == "discarded"
        assert processed[0]["discard_reason"] == "rejected"
        assert processed[0]["request_ids"] == ["assess_1"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_run_due_batch_once_records_precheck_failed_batch_in_processed_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="assess",
                decision_source="self",
                payload_text="",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="assess_1",
                session_key="web:shared",
            )
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
            lambda *args, **kwargs: pytest.fail("precheck-failed batch should not invoke memory model"),
            raising=False,
        )

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["status"] == "discarded"
        assert report["discard_reason"] == "precheck_failed"
        assert await manager.list_queue(limit=10) == []
        assert len(processed) == 1
        assert processed[0]["status"] == "discarded"
        assert processed[0]["discard_reason"] == "precheck_failed"
        assert processed[0]["request_ids"] == ["assess_1"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_run_due_batch_once_processes_assess_batch_into_memory_write(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="assess",
                decision_source="self",
                payload_text="window payload",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="assess_1",
                session_key="web:shared",
            )
        )
        assessor_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "assess-call-1",
                            "name": "memory_assessment_result",
                            "args": {"content": "User prefers concise answers"},
                        }
                    ],
                    usage={"input_tokens": 2, "output_tokens": 1, "cache_read_tokens": 0},
                    response_metadata={
                        "provider_request_id": "provider-assess-1",
                        "provider_request_meta": {"provider": "responses", "endpoint": "/responses"},
                        "provider_request_body": {"model": "gpt-5.2", "input": [{"role": "user", "content": "assess"}]},
                    },
                )
            ]
        )
        processor_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "process-call-1",
                            "name": "memory_apply_batch",
                            "args": {
                                "adds": [
                                    {
                                        "content": "User prefers concise answers",
                                        "decision_source": "self",
                                    }
                                ]
                            },
                        }
                    ],
                    usage={"input_tokens": 4, "output_tokens": 1, "cache_read_tokens": 0},
                    response_metadata={
                        "provider_request_id": "provider-agent-1",
                        "provider_request_meta": {"provider": "responses", "endpoint": "/responses"},
                        "provider_request_body": {"model": "gpt-5.2", "input": [{"role": "user", "content": "apply"}]},
                    },
                ),
                _fake_response(content="done", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
            ]
        )
        models = iter([assessor_model, processor_model])
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/18", raising=False)
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: next(models), raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        snapshot_text = manager.snapshot_text()
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["status"] == "applied"
        assert "User prefers concise answers" in snapshot_text
        assert "id:" in snapshot_text
        assert processed[0]["source_op"] == "assess"
        assert processed[0]["op"] == "write"
        assert processed[0]["status"] == "applied"
        assert processed[0]["provider_request_ids"] == ["provider-assess-1", "provider-agent-1"]
        assert len(processed[0]["request_artifact_paths"]) == 2
        for artifact_path in processed[0]["request_artifact_paths"]:
            artifact = json.loads(Path(artifact_path).read_text(encoding="utf-8"))
            assert artifact["queue_request_ids"] == ["assess_1"]
            assert str(artifact["provider_request_id"]).startswith("provider-")
    finally:
        manager.close()


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
                            "name": "memory_apply_batch",
                            "args": {
                                "adds": [
                                    {
                                        "content": "Report total elapsed time",
                                        "decision_source": "self",
                                    }
                                ]
                            },
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
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/17", raising=False)
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


@pytest.mark.asyncio
async def test_run_due_batch_once_skips_when_worker_lease_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Prefer concise answers",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_1",
            )
        )
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(module, "_try_acquire_file_lock", lambda *args, **kwargs: None, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        queue_items = await manager.list_queue(limit=10)

        assert report == {"ok": True, "status": "worker_lease_unavailable", "processed": 0}
        assert len(queue_items) == 1
        assert queue_items[0]["request_id"] == "write_1"
        assert queue_items[0]["status"] == "pending"
        assert _read_jsonl(tmp_path / "memory" / "ops.jsonl") == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_drops_request_already_recorded_in_processed_log(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Prefer concise answers",
                created_at="2026-04-18T10:00:00+08:00",
                request_id="write_1",
                status="processing",
                processing_started_at="2026-04-18T10:00:01+08:00",
            )
        )
        (tmp_path / "memory" / "ops.jsonl").write_text(
            json.dumps(
                {
                    "batch_id": "write_done_1",
                    "op": "write",
                    "source_op": "write",
                    "processed_at": "2026-04-18T10:00:04+08:00",
                    "request_ids": ["write_1"],
                    "request_count": 1,
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
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
            lambda *args, **kwargs: pytest.fail("already-processed queue item should not invoke memory model"),
            raising=False,
        )

        report = await manager.run_due_batch_once(now_iso="2026-04-18T10:00:05+08:00")
        queue_items = await manager.list_queue(limit=10)
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["status"] == "already_processed"
        assert report["request_ids"] == ["write_1"]
        assert queue_items == []
        assert len(processed) == 1
        assert processed[0]["request_ids"] == ["write_1"]
    finally:
        manager.close()
