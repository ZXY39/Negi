from __future__ import annotations

import importlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

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
        self.bound_tools = []
        self.calls: list[list[object]] = []

    def bind_tools(self, tools):
        self.bound_tools = list(tools or [])
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages or []))
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


def test_user_priority_classify_memory_payload_promotes_explicit_user_preference(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = manager.classify_memory_payload(
            decision_source="self",
            payload_text="用户说：请记住，以后默认用中文回复。",
            trigger_source="autonomous_review:turn-1",
        )

        assert result.enqueue is True
        assert result.decision_source == "user"
        assert result.memory_kind == "user_preference"
        assert result.replace_mode == "replace_existing"
    finally:
        manager.close()


def test_user_priority_classify_memory_payload_promotes_stable_project_identity(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = manager.classify_memory_payload(
            decision_source="self",
            payload_text="I work on project G3KU.",
            trigger_source="autonomous_review:turn-project-identity",
        )

        assert result.enqueue is True
        assert result.decision_source == "user"
        assert result.memory_kind == "user_identity"
        assert result.replace_mode == "replace_existing"
    finally:
        manager.close()


def test_user_priority_classify_memory_payload_does_not_upgrade_task_local_diff_instruction(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = manager.classify_memory_payload(
            decision_source="self",
            payload_text="Please use the attached diff for this change.",
            trigger_source="autonomous_review:turn-2",
        )

        assert result.enqueue is True
        assert result.decision_source == "self"
        assert result.memory_kind == "self_general"
        assert result.replace_mode == "append_or_merge"
    finally:
        manager.close()


def test_user_priority_classify_memory_payload_does_not_upgrade_one_turn_test_deferral(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = manager.classify_memory_payload(
            decision_source="self",
            payload_text="Don't run tests yet; I'll do that later.",
            trigger_source="autonomous_review:turn-3",
        )

        assert result.enqueue is True
        assert result.decision_source == "self"
        assert result.memory_kind == "self_general"
        assert result.replace_mode == "append_or_merge"
    finally:
        manager.close()


def test_user_priority_classify_memory_payload_does_not_upgrade_current_debugging_status(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = manager.classify_memory_payload(
            decision_source="self",
            payload_text="I'm debugging a flaky test right now.",
            trigger_source="autonomous_review:turn-debugging",
        )

        assert result.enqueue is True
        assert result.decision_source == "self"
        assert result.memory_kind == "self_general"
        assert result.replace_mode == "append_or_merge"
    finally:
        manager.close()


def test_user_priority_classify_memory_payload_does_not_upgrade_today_work_status(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = manager.classify_memory_payload(
            decision_source="self",
            payload_text="I am working on fixing this crash today.",
            trigger_source="autonomous_review:turn-working",
        )

        assert result.enqueue is True
        assert result.decision_source == "self"
        assert result.memory_kind == "self_general"
        assert result.replace_mode == "append_or_merge"
    finally:
        manager.close()


def test_user_priority_classify_memory_payload_does_not_upgrade_this_command_repo_root_instruction(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = manager.classify_memory_payload(
            decision_source="self",
            payload_text="Please use the repo root for this command.",
            trigger_source="autonomous_review:turn-command",
        )

        assert result.enqueue is True
        assert result.decision_source == "self"
        assert result.memory_kind == "self_general"
        assert result.replace_mode == "append_or_merge"
    finally:
        manager.close()


def test_user_priority_classify_memory_payload_does_not_upgrade_this_one_bash_instruction(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = manager.classify_memory_payload(
            decision_source="self",
            payload_text="Please use bash for this one.",
            trigger_source="autonomous_review:turn-bash",
        )

        assert result.enqueue is True
        assert result.decision_source == "self"
        assert result.memory_kind == "self_general"
        assert result.replace_mode == "append_or_merge"
    finally:
        manager.close()


def test_user_priority_prioritize_existing_entries_keeps_user_before_self(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    markdown = _load_markdown_memory_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        entries = [
            markdown.MemoryEntry(
                date_text="2026/4/17",
                source="self",
                summary="Report total elapsed time",
            ),
            markdown.MemoryEntry(
                date_text="2026/4/17",
                source="user",
                summary="Prefer Chinese replies by default",
            ),
            markdown.MemoryEntry(
                date_text="2026/4/17",
                source="self",
                summary="Remember session boundary flush",
            ),
        ]

        prioritized = manager.prioritize_existing_entries(entries)

        assert [item.summary for item in prioritized][:2] == [
            "Prefer Chinese replies by default",
            "Report total elapsed time",
        ]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_self_pruning_enqueue_write_request_ignores_session_boundary_flush(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = await manager.enqueue_write_request(
            session_key="session-1",
            decision_source="self",
            payload_text="session boundary flush",
            trigger_source="session_boundary_flush",
        )

        assert result["ok"] is True
        assert result["status"] == "ignored"
        assert result["reason"] == "self_memory_ignored"
        assert await manager.list_queue(limit=10) == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_self_pruning_enqueue_write_request_keeps_generalized_processing_rule(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = await manager.enqueue_write_request(
            session_key="session-1",
            decision_source="self",
            payload_text="When processing CSV imports, always preserve column order.",
            trigger_source="autonomous_review:turn-csv",
        )
        queue_items = await manager.list_queue(limit=10)

        assert result["ok"] is True
        assert result["status"] == "queued"
        assert len(queue_items) == 1
        assert queue_items[0]["decision_source"] == "self"
        assert queue_items[0]["payload_text"] == "When processing CSV imports, always preserve column order."
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_autonomous_review_enqueue_promotes_direct_user_request_to_user_priority(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        result = await manager.enqueue_autonomous_review(
            session_key="session-1",
            channel="web",
            chat_id="chat-1",
            user_messages=["记住：以后默认用中文回复。"],
            assistant_text="好的，我会默认用中文回复。",
            turn_id="turn-direct-memory",
        )
        queue_items = await manager.list_queue(limit=10)

        assert result["ok"] is True
        assert result["status"] == "queued"
        assert len(queue_items) == 1
        assert queue_items[0]["decision_source"] == "user"
        assert queue_items[0]["trigger_source"] == "autonomous_review:turn-direct-memory"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_autonomous_review_does_not_promote_task_local_test_deferral_to_user_priority(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        decision = manager.should_enqueue_autonomous_review(
            session_key="session-1",
            turn_id="turn-no-upgrade",
            user_messages=["Don't run tests yet; I'll do that later."],
            assistant_text="Understood, I'll wait for you to run them.",
        )

        assert decision.decision_source == "self"
        assert decision.reason != "direct_user_memory"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_autonomous_review_does_not_promote_current_debugging_status_to_user_priority(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        decision = manager.should_enqueue_autonomous_review(
            session_key="session-1",
            turn_id="turn-debug-status",
            user_messages=["I'm debugging a flaky test right now."],
            assistant_text="Okay, keep me posted once you have a stable repro.",
        )

        assert decision.decision_source == "self"
        assert decision.reason != "direct_user_memory"
    finally:
        manager.close()


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


@pytest.mark.asyncio
async def test_run_due_batch_once_blocks_processing_batch_when_memory_role_missing(
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
                payload_text="创建文件默认格式要求",
                created_at="2026-04-17T10:00:00+08:00",
                request_id="write_1",
            )
        )
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=[]), 1, False),
            raising=False,
        )

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:04+08:00")
        queue_items = await manager.list_queue(limit=10)

        assert report["ok"] is False
        assert report["status"] == "blocked"
        assert report["error"] == "memory role not configured"
        assert manager.snapshot_text() == ""
        assert len(queue_items) == 1
        assert queue_items[0]["status"] == "processing"
        assert queue_items[0]["last_error_text"] == "memory role not configured"
        assert _read_jsonl(tmp_path / "memory" / "ops.jsonl") == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_records_successful_processed_batch_with_usage_and_model_chain(
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
                payload_text="创建文件默认格式要求，在 ref:note_policy",
                created_at="2026-04-17T10:00:00+08:00",
                request_id="write_1",
            )
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_write_note",
                            "args": {"ref": "note_policy", "content": "创建文件默认格式要求"},
                        },
                        {
                            "id": "call-2",
                            "name": "memory_write_document",
                            "args": {
                                "content": "---\n2026/4/17-user：\n创建文件默认格式要求，见 ref:note_policy\n"
                            },
                        },
                    ],
                    usage={"input_tokens": 8, "output_tokens": 3, "cache_read_tokens": 2},
                ),
                _fake_response(
                    content="done",
                    usage={"input_tokens": 2, "output_tokens": 1, "cache_read_tokens": 0},
                ),
            ]
        )
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:04+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert report["attempt_count"] == 1
        assert "2026/4/17-user：" in manager.snapshot_text()
        assert "ref:note_policy" in manager.snapshot_text()
        assert (tmp_path / "memory" / "notes" / "note_policy.md").exists()
        assert (tmp_path / "memory" / "queue.jsonl").read_text(encoding="utf-8").strip() == ""
        assert len(processed) == 1
        assert processed[0]["op"] == "write"
        assert processed[0]["model_chain"] == ["memory-primary"]
        assert processed[0]["request_ids"] == ["write_1"]
        assert processed[0]["usage"] == {
            "input_tokens": 10,
            "output_tokens": 4,
            "cache_read_tokens": 2,
        }
        assert processed[0]["note_refs_written"] == ["note_policy"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_keeps_processing_batch_on_provider_error_without_processed_record(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="完成任务必须说明任务总耗时",
                created_at="2026-04-17T10:00:00+08:00",
                request_id="write_1",
            )
        )
        fake_model = _FakeToolCallingModel([RuntimeError("provider exploded")])
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:04+08:00")
        queue_items = await manager.list_queue(limit=10)

        assert report["ok"] is False
        assert report["status"] == "error"
        assert manager.snapshot_text() == ""
        assert len(queue_items) == 1
        assert queue_items[0]["status"] == "processing"
        assert "provider exploded" in str(queue_items[0]["last_error_text"])
        assert _read_jsonl(tmp_path / "memory" / "ops.jsonl") == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_applies_delete_batch_via_memory_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            "---\n2026/4/17-self：\n完成任务必须说明任务总耗时\n",
            encoding="utf-8",
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_write_document",
                            "args": {"content": ""},
                        }
                    ],
                    usage={"input_tokens": 4, "output_tokens": 1, "cache_read_tokens": 0},
                ),
                _fake_response(content="done", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
            ]
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="完成任务必须说明任务总耗时",
                created_at="2026-04-17T10:00:05+08:00",
                request_id="delete_1",
            )
        )
        monkeypatch.setattr(
            module,
            "get_runtime_config",
            lambda force=False: (_app_config(tmp_path, memory_chain=["memory-primary"]), 2, False),
            raising=False,
        )
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:09+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["op"] == "delete"
        assert manager.snapshot_text() == ""
        assert len(processed) == 1
        assert processed[0]["op"] == "delete"
        assert processed[0]["request_ids"] == ["delete_1"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_delete_only_removes_visible_snapshot_items(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            (
                "---\n2026/4/17-self：\nReport total elapsed time\n\n"
                "---\n2026/4/17-user：\nPrefer Chinese replies\n"
            ),
            encoding="utf-8",
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="Report total elapsed time",
                created_at="2026-04-17T10:00:05+08:00",
                request_id="delete_1",
            )
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_write_document",
                            "args": {
                                "content": "---\n2026/4/17-user：\nPrefer Chinese replies\n"
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

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:09+08:00")

        assert report["ok"] is True
        assert report["op"] == "delete"
        assert "Report total elapsed time" not in manager.snapshot_text()
        assert "Prefer Chinese replies" in manager.snapshot_text()
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_repairs_delete_batch_that_keeps_non_snapshot_block(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            "---\n2026/4/17-self：\nReport total elapsed time\n",
            encoding="utf-8",
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="Report total elapsed time",
                created_at="2026-04-17T10:00:05+08:00",
                request_id="delete_1",
            )
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_write_document",
                            "args": {
                                "content": "---\n2026/4/17-self：\nInvented replacement block\n"
                            },
                        }
                    ],
                    usage={"input_tokens": 4, "output_tokens": 1, "cache_read_tokens": 0},
                ),
                _fake_response(content="retry", usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0}),
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-2",
                            "name": "memory_write_document",
                            "args": {"content": ""},
                        }
                    ],
                    usage={"input_tokens": 5, "output_tokens": 1, "cache_read_tokens": 0},
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

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:09+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["op"] == "delete"
        assert report["attempt_count"] == 2
        assert manager.snapshot_text() == ""
        assert len(processed) == 1
        assert processed[0]["op"] == "delete"
        assert processed[0]["request_ids"] == ["delete_1"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_cleans_up_orphan_notes_after_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            "---\n2026/4/17-user：\nDetailed workflow, see ref:orphan_note\n",
            encoding="utf-8",
        )
        orphan_note = tmp_path / "memory" / "notes" / "orphan_note.md"
        orphan_note.parent.mkdir(parents=True, exist_ok=True)
        orphan_note.write_text("Old note body\n", encoding="utf-8")
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Prefer concise answers",
                created_at="2026-04-17T10:00:05+08:00",
                request_id="write_1",
            )
        )
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_write_document",
                            "args": {
                                "content": "---\n2026/4/17-user：\nPrefer concise answers\n"
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

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:09+08:00")

        assert report["ok"] is True
        assert manager.snapshot_text() == "---\n2026/4/17-user：\nPrefer concise answers"
        assert not orphan_note.exists()
    finally:
        manager.close()
