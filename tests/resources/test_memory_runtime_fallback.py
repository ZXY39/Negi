from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import g3ku.agent.rag_memory as rag_memory
from g3ku.config.schema import MemoryToolsConfig


REPO_ROOT = Path(__file__).resolve().parents[2]


def _memory_cfg(**overrides) -> MemoryToolsConfig:
    payload = MemoryToolsConfig().model_dump(mode="python")
    payload["guard"]["mode"] = "manual"
    payload["guard"]["auto_fact_confidence"] = 0.2
    payload.update(overrides)
    return MemoryToolsConfig.model_validate(payload)


def _seed_desktop_default_memory(
    workspace: Path,
    *,
    rag_applied_seq: int,
    legacy_applied_seq: int = 0,
) -> None:
    mem_dir = workspace / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "sync_state.json").write_text(
        json.dumps(
            {
                "schema_version": rag_memory.MEMORY_RUNTIME_SCHEMA_VERSION,
                "next_seq": 2,
                "rag_applied_seq": rag_applied_seq,
                "legacy_applied_seq": legacy_applied_seq,
                "last_reset_at": "2026-03-28T23:01:54.147351",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (mem_dir / "sync_journal.jsonl").write_text(
        json.dumps(
            {
                "seq": 1,
                "event_id": "desktop-pref-1",
                "record_id": "18598594c23a4ac1",
                "text": "用户要求以后所有整理文档类的结果默认放在桌面。",
                "source": "explicit_tool",
                "confidence": 1.0,
                "tags": [
                    "explicit_memory",
                    "persistent",
                    "memory_kind:preference",
                    "memory_key:document_output_default_desktop",
                ],
                "session_key": "web:ceo-7c3310045b36",
                "channel": "web",
                "chat_id": "shared",
                "event_type": "memory_write",
                "metadata": {
                    "kind": "preference",
                    "key": "document_output_default_desktop",
                    "value": "整理文档类输出默认保存到桌面",
                    "source_excerpt": "记住以后所有整理文档类的都放在桌面",
                    "write_mode": "explicit_tool",
                },
                "created_at": "2026-03-28T23:01:54.147351",
                "updated_at": "2026-03-28T23:01:54.147351",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


def test_rag_backend_uses_global_namespace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("G3KU_TASK_RUNTIME_ROLE", "worker")
    backend = rag_memory._RagMemoryBackend(tmp_path, _memory_cfg())
    try:
        assert backend.namespace_for(channel="web", chat_id="shared") == ("memory", "global")
        assert backend.namespace_for(channel="qqbot", chat_id="user-a") == ("memory", "global")
    finally:
        backend.close()


def test_memory_manager_bootstrap_full_replays_journal_on_normal_startup(monkeypatch, tmp_path: Path) -> None:
    class _RecordingBackend:
        instances: list["_RecordingBackend"] = []

        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            self.store = object()
            self.applied: list[rag_memory.MemorySyncEvent] = []
            type(self).instances.append(self)

        async def apply_sync_event(self, event: rag_memory.MemorySyncEvent) -> dict[str, object]:
            self.applied.append(event)
            return {"record_id": event.record_id}

        async def stats(self) -> dict[str, object]:
            return {
                "records": len(self.applied),
                "records_v2": len(self.applied),
                "pending": 0,
                "records_by_type": {"memory": len(self.applied)},
                "layer_distribution": {"l0": len(self.applied), "l1": len(self.applied), "l2": 0},
                "dense_enabled": False,
                "sqlite_path": "",
                "qdrant_path": "",
                "planner_calls": 0,
                "commit_calls": 0,
                "rerank_calls": 0,
                "token_in": 0,
                "token_out": 0,
                "cost_delta_pct": 0.0,
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _RecordingBackend)
    _seed_desktop_default_memory(tmp_path, rag_applied_seq=1)

    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg(bootstrap_mode="full"))
    try:
        backend = _RecordingBackend.instances[-1]
        assert len(backend.applied) == 1
        assert backend.applied[0].record_id == "18598594c23a4ac1"
        assert "默认放在桌面" in backend.applied[0].text
        assert manager._backend_state == "rag_healthy"
        assert manager._bootstrap_replay_task is None
    finally:
        manager.close()


def test_memory_manager_bootstrap_new_only_backfills_dense_when_journal_is_already_applied(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _RecordingBackend:
        instances: list["_RecordingBackend"] = []

        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            self.store = object()
            self.applied: list[rag_memory.MemorySyncEvent] = []
            self.ensure_dense_backfill_calls = 0
            type(self).instances.append(self)

        async def apply_sync_event(self, event: rag_memory.MemorySyncEvent) -> dict[str, object]:
            self.applied.append(event)
            return {"record_id": event.record_id}

        async def ensure_dense_backfill(self) -> dict[str, object]:
            self.ensure_dense_backfill_calls += 1
            return {
                "needed": True,
                "eligible": 1,
                "indexed": 1,
                "dense_points": 0,
                "sample_missing": True,
            }

        async def stats(self) -> dict[str, object]:
            return {
                "records": len(self.applied),
                "records_v2": len(self.applied),
                "pending": 0,
                "records_by_type": {"memory": len(self.applied)},
                "layer_distribution": {"l0": len(self.applied), "l1": len(self.applied), "l2": 0},
                "dense_enabled": True,
                "sqlite_path": "",
                "qdrant_path": "",
                "planner_calls": 0,
                "commit_calls": 0,
                "rerank_calls": 0,
                "token_in": 0,
                "token_out": 0,
                "cost_delta_pct": 0.0,
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _RecordingBackend)
    _seed_desktop_default_memory(tmp_path, rag_applied_seq=1)

    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg(bootstrap_mode="new_only"))
    try:
        backend = _RecordingBackend.instances[-1]
        assert backend.applied == []
        assert backend.ensure_dense_backfill_calls == 1
        assert manager._backend_state == "rag_healthy"
    finally:
        manager.close()


def test_dashscope_embeddings_split_batch_when_api_rejects_large_request() -> None:
    emb = rag_memory.DashScopeMultimodalEmbeddings(api_key="sk-test", batch_size=32)
    calls: list[int] = []

    def _fake_embed_batch(texts: list[str]) -> list[list[float]]:
        calls.append(len(texts))
        if len(texts) > 4:
            raise RuntimeError("DashScope embedding API call failed (400) at https://example.test")
        return [[float(index)] for index, _ in enumerate(texts, start=1)]

    emb._embed_batch = _fake_embed_batch  # type: ignore[method-assign]

    result = emb.embed_documents([f"text-{index}" for index in range(10)])

    assert len(result) == 10
    assert calls[0] == 10
    assert max(calls) == 10
    assert any(size <= 4 for size in calls[1:])


@pytest.mark.asyncio
async def test_memory_manager_fallback_ingest_and_retrieve_when_rag_backend_bootstrap_fails(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _FailingBackend:
        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            raise RuntimeError("backend unavailable")

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _FailingBackend)
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())

    await manager.ingest_turn(
        session_key="web:ops",
        channel="web",
        chat_id="ops",
        messages=[
            {"role": "user", "content": "remember deploy window tomorrow 10am"},
            {"role": "assistant", "content": "I will remember the deploy window"},
        ],
    )

    block = await manager.retrieve_block(
        query="deploy window",
        channel="web",
        chat_id="another",
        session_key="web:another",
    )
    stats = await manager.stats()

    assert manager.store is None
    assert "deploy" in block.lower()
    assert stats["backend_state"] == "legacy_degraded"
    assert stats["journal_seq"] == 1
    assert stats["legacy_applied_seq"] == 1
    assert "deploy window" in (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8").lower()
    assert "deploy window" in (tmp_path / "memory" / "HISTORY.md").read_text(encoding="utf-8").lower()


@pytest.mark.asyncio
async def test_retrieve_block_can_ignore_same_session_turn_memory_records(tmp_path: Path) -> None:
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager.ingest_turn(
            session_key="web:shared",
            channel="web",
            chat_id="shared",
            messages=[
                {"role": "user", "content": "remember zebra-cascade-481 deployment window"},
                {"role": "assistant", "content": "Stored zebra-cascade-481 deployment window"},
            ],
        )

        unfiltered = await manager.retrieve_block(
            query="zebra-cascade-481",
            channel="web",
            chat_id="shared",
            session_key="web:shared",
            search_context_types=["memory"],
            allowed_context_types=["memory"],
        )
        filtered = await manager.retrieve_block(
            query="zebra-cascade-481",
            channel="web",
            chat_id="shared",
            session_key="web:shared",
            search_context_types=["memory"],
            allowed_context_types=["memory"],
            exclude_same_session_turn_memory=True,
        )

        assert "zebra-cascade-481" in unfiltered.lower()
        assert "zebra-cascade-481" not in filtered.lower()
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_memory_manager_bootstrap_new_only_replays_pending_journal_inside_running_loop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _RecordingBackend:
        instances: list["_RecordingBackend"] = []

        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            self.store = object()
            self.applied: list[rag_memory.MemorySyncEvent] = []
            type(self).instances.append(self)

        async def apply_sync_event(self, event: rag_memory.MemorySyncEvent) -> dict[str, object]:
            self.applied.append(event)
            return {"record_id": event.record_id}

        async def stats(self) -> dict[str, object]:
            return {
                "records": len(self.applied),
                "records_v2": len(self.applied),
                "pending": 0,
                "records_by_type": {"memory": len(self.applied)},
                "layer_distribution": {"l0": len(self.applied), "l1": len(self.applied), "l2": 0},
                "dense_enabled": False,
                "sqlite_path": "",
                "qdrant_path": "",
                "planner_calls": 0,
                "commit_calls": 0,
                "rerank_calls": 0,
                "token_in": 0,
                "token_out": 0,
                "cost_delta_pct": 0.0,
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _RecordingBackend)
    _seed_desktop_default_memory(tmp_path, rag_applied_seq=0)

    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg(bootstrap_mode="new_only"))
    try:
        stats = await manager.stats()
        backend = _RecordingBackend.instances[-1]
        assert len(backend.applied) == 1
        assert backend.applied[0].record_id == "18598594c23a4ac1"
        assert stats["backend_state"] == "rag_healthy"
        assert stats["rag_applied_seq"] == stats["journal_seq"] == 1
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_memory_manager_replays_journal_after_rag_backend_recovers(monkeypatch, tmp_path: Path) -> None:
    attempts = {"count": 0}

    class _RecoveringBackend:
        instances: list["_RecoveringBackend"] = []

        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("bootstrap failed")
            self.store = object()
            self.applied: list[rag_memory.MemorySyncEvent] = []
            type(self).instances.append(self)

        async def apply_sync_event(self, event: rag_memory.MemorySyncEvent) -> dict[str, object]:
            self.applied.append(event)
            return {"record_id": event.record_id}

        async def stats(self) -> dict[str, object]:
            return {
                "records": len(self.applied),
                "records_v2": len(self.applied),
                "pending": 0,
                "records_by_type": {"memory": len(self.applied)},
                "layer_distribution": {"l0": len(self.applied), "l1": len(self.applied), "l2": 0},
                "dense_enabled": False,
                "sqlite_path": "",
                "qdrant_path": "",
                "planner_calls": 0,
                "commit_calls": 0,
                "rerank_calls": 0,
                "token_in": 0,
                "token_out": 0,
                "cost_delta_pct": 0.0,
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _RecoveringBackend)
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())
    manager._RAG_RETRY_BACKOFF_S = 0

    await manager.ingest_turn(
        session_key="web:shared",
        channel="web",
        chat_id="shared",
        messages=[
            {"role": "user", "content": "remember oauth callback path"},
            {"role": "assistant", "content": "Stored the oauth callback path"},
        ],
    )

    stats = await manager.stats()
    backend = _RecoveringBackend.instances[-1]

    assert len(backend.applied) == 1
    assert "oauth callback path" in backend.applied[0].text.lower()
    assert stats["rag_healthy"] is True
    assert stats["rag_applied_seq"] == stats["journal_seq"] == 1


@pytest.mark.asyncio
async def test_memory_search_fallback_returns_memory_only(monkeypatch, tmp_path: Path) -> None:
    class _FailingBackend:
        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            raise RuntimeError("backend unavailable")

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _FailingBackend)
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())

    await manager.ingest_turn(
        session_key="web:shared",
        channel="web",
        chat_id="shared",
        messages=[
            {"role": "user", "content": "remember oauth callback path"},
            {"role": "assistant", "content": "Stored oauth callback path"},
        ],
    )
    await manager.ingest_turn(
        session_key="qqbot:user-a",
        channel="qqbot",
        chat_id="user-a",
        messages=[
            {"role": "user", "content": "remember release branch naming"},
            {"role": "assistant", "content": "Stored release branch naming"},
        ],
    )

    result = await manager.search_tool_view(
        query="oauth",
        channel="qqbot",
        chat_id="user-a",
        session_key="qqbot:user-a",
        limit=5,
        context_type=None,
        include_l2=True,
    )

    assert result["grouped"]["memory"]
    assert result["grouped"]["resource"] == []
    assert result["grouped"]["skill"] == []
    assert result["view"][0]["context_type"] == "memory"


@pytest.mark.asyncio
async def test_memory_manager_explicit_write_is_searchable_and_replaces_previous_value(tmp_path: Path) -> None:
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())

    try:
        first = await manager.write_explicit_memory_items(
            session_key="web:shared",
            channel="web",
            chat_id="shared",
            items=[
                {
                    "kind": "default",
                    "key": "preferred_package_manager",
                    "value": "poetry",
                    "statement": "Default to poetry for package management.",
                    "source_excerpt": "default to poetry",
                }
            ],
        )
        second = await manager.write_explicit_memory_items(
            session_key="web:shared",
            channel="web",
            chat_id="shared",
            items=[
                {
                    "kind": "default",
                    "key": "preferred_package_manager",
                    "value": "pnpm",
                    "statement": "Default to pnpm for package management.",
                    "source_excerpt": "default to pnpm from now on",
                }
            ],
        )

        stats = await manager.stats()
        block = await manager.retrieve_block(
            query="package manager default",
            channel="web",
            chat_id="shared",
            session_key="web:shared",
        )
        new_result = await manager.search_tool_view(
            query="pnpm",
            channel="web",
            chat_id="shared",
            session_key="web:shared",
            limit=5,
            context_type="memory",
            include_l2=True,
        )
        old_result = await manager.search_tool_view(
            query="poetry",
            channel="web",
            chat_id="shared",
            session_key="web:shared",
            limit=5,
            context_type="memory",
            include_l2=True,
        )
        memory_text = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8").lower()
        history_text = (tmp_path / "memory" / "HISTORY.md").read_text(encoding="utf-8").lower()

        assert first["ok"] is True
        assert second["ok"] is True
        assert len(first["written"]) == 1
        assert len(second["written"]) == 1
        assert second["deleted"] == [{"record_id": first["written"][0]["record_id"], "key": "preferred_package_manager"}]
        assert stats["journal_seq"] == 3
        assert "pnpm" in block.lower()
        assert "poetry" not in block.lower()
        assert any("pnpm" in str(item.get("l0") or "").lower() for item in new_result["view"])
        assert not old_result["view"]
        assert "pnpm" in memory_text
        assert "poetry" not in memory_text
        assert "delete: replaced key=preferred_package_manager" in history_text
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_memory_manager_explicit_write_fallback_updates_legacy_projection_when_rag_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _FailingBackend:
        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            raise RuntimeError("backend unavailable")

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _FailingBackend)
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())

    try:
        payload = await manager.write_explicit_memory_items(
            session_key="web:shared",
            channel="web",
            chat_id="shared",
            items=[
                {
                    "kind": "preference",
                    "key": "preferred_package_manager",
                    "value": "pnpm",
                    "statement": "Prefer pnpm for package management.",
                    "source_excerpt": "please remember: prefer pnpm",
                }
            ],
        )
        stats = await manager.stats()
        block = await manager.retrieve_block(
            query="pnpm",
            channel="web",
            chat_id="shared",
            session_key="web:shared",
        )

        assert payload["ok"] is True
        assert payload["searchable"] is True
        assert stats["backend_state"] == "legacy_degraded"
        assert stats["journal_seq"] == 1
        assert stats["legacy_applied_seq"] == 1
        assert "pnpm" in block.lower()
        assert "prefer pnpm" in (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8").lower()
        assert "prefer pnpm" in (tmp_path / "memory" / "HISTORY.md").read_text(encoding="utf-8").lower()
    finally:
        manager.close()


def test_memory_manager_first_boot_clears_legacy_memory_artifacts(monkeypatch, tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("old fact", encoding="utf-8")
    (memory_dir / "HISTORY.md").write_text("old history", encoding="utf-8")
    (memory_dir / "sync_journal.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    (memory_dir / "memory.db").write_text("old db", encoding="utf-8")

    class _FailingBackend:
        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            raise RuntimeError("backend unavailable")

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _FailingBackend)
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())

    try:
        assert "old fact" not in manager.memory_file.read_text(encoding="utf-8")
        assert "old history" not in manager.history_file.read_text(encoding="utf-8")
        assert manager.journal_file.read_text(encoding="utf-8") == ""
        assert not (memory_dir / "memory.db").exists()
    finally:
        manager.close()


def test_memory_manager_schema_bump_resets_runtime_artifacts(monkeypatch, tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("old fact", encoding="utf-8")
    (memory_dir / "HISTORY.md").write_text("old history", encoding="utf-8")
    (memory_dir / "sync_journal.jsonl").write_text('{"seq":1}\n', encoding="utf-8")
    (memory_dir / "memory.db").write_text("old db", encoding="utf-8")
    (memory_dir / "checkpoints.sqlite3").write_text("old checkpoints", encoding="utf-8")
    (memory_dir / "sync_state.json").write_text(
        json.dumps(
            {
                "schema_version": "legacy-schema-v1",
                "next_seq": 5,
                "rag_applied_seq": 0,
                "legacy_applied_seq": 0,
                "last_reset_at": "2026-04-08T00:00:00",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    context_dir = memory_dir / "context_store"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "old-context.json").write_text("legacy", encoding="utf-8")
    qdrant_dir = memory_dir / "qdrant"
    qdrant_dir.mkdir(parents=True, exist_ok=True)
    (qdrant_dir / "old-vector.bin").write_text("legacy", encoding="utf-8")

    class _FailingBackend:
        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            raise RuntimeError("backend unavailable")

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _FailingBackend)
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())

    try:
        state = json.loads((memory_dir / "sync_state.json").read_text(encoding="utf-8"))
        assert state["schema_version"] == rag_memory.MEMORY_RUNTIME_SCHEMA_VERSION
        assert not (memory_dir / "memory.db").exists()
        assert not (memory_dir / "checkpoints.sqlite3").exists()
        assert not qdrant_dir.exists()
        assert list(manager.context_store_dir.iterdir()) == []
        assert manager.journal_file.read_text(encoding="utf-8").strip() == ""
        assert "old fact" not in manager.memory_file.read_text(encoding="utf-8")
        assert "old history" not in manager.history_file.read_text(encoding="utf-8")
        structured_state = memory_dir / "structured_state.json"
        assert structured_state.exists()
        structured_payload = json.loads(structured_state.read_text(encoding="utf-8"))
        assert structured_payload.get("schema_version") == rag_memory.MEMORY_RUNTIME_SCHEMA_VERSION
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_structured_memory_dual_write_generates_structured_projections_and_markdown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _FailingBackend:
        def __init__(self, workspace, cfg):
            _ = workspace, cfg
            raise RuntimeError("backend unavailable")

    monkeypatch.setattr(rag_memory, "_RagMemoryBackend", _FailingBackend)
    manager = rag_memory.MemoryManager(tmp_path, _memory_cfg())

    try:
        timestamp = "2026-04-08T12:20:00+00:00"
        statement = "State is warm."
        await manager.upsert_structured_memory_facts(
            session_key="web:shared",
            channel="web",
            chat_id="shared",
            facts=[
                {
                    "fact_id": "state-projection-v1",
                    "slot_id": "current_state",
                    "stateful_fact": True,
                    "rendered_statement": statement,
                    "state": {"value": statement},
                    "observed_at": timestamp,
                }
            ],
        )

        mem_dir = tmp_path / "memory"
        current_lines = (mem_dir / "structured_current.jsonl").read_text(encoding="utf-8").splitlines()
        history_lines = (mem_dir / "structured_history.jsonl").read_text(encoding="utf-8").splitlines()
        memory_text = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")

        assert len([line for line in current_lines if line.strip()]) == 1
        current = json.loads(current_lines[0])
        assert current["fact_id"] == "state-projection-v1"

        assert len([line for line in history_lines if line.strip()]) == 1
        history = json.loads(history_lines[0])
        assert history["op"] == "write"
        assert history["fact"]["fact_id"] == "state-projection-v1"

        assert statement in memory_text
        assert timestamp in memory_text
    finally:
        manager.close()


def test_private_state_paths_are_git_ignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", ".g3ku/config.json", "memory/MEMORY.md", "memory/HISTORY.md", "sessions/demo.jsonl"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    reported = {Path(line.strip()).as_posix() for line in result.stdout.splitlines() if line.strip()}
    assert ".g3ku/config.json" in reported
    assert "memory/MEMORY.md" in reported
    assert "memory/HISTORY.md" in reported
    assert "sessions/demo.jsonl" in reported
    content = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!memory/*.md" not in content
