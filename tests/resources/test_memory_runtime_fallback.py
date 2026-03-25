from __future__ import annotations

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


def test_rag_backend_uses_global_namespace(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("G3KU_TASK_RUNTIME_ROLE", "worker")
    backend = rag_memory._RagMemoryBackend(tmp_path, _memory_cfg())
    try:
        assert backend.namespace_for(channel="web", chat_id="shared") == ("memory", "global")
        assert backend.namespace_for(channel="qqbot", chat_id="user-a") == ("memory", "global")
    finally:
        backend.close()


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
