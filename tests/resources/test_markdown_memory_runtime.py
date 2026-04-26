from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
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
        memory_id="Ab12Z9",
        date_text="2026/4/17",
        source="self",
        summary="完成任务必须说明任务总耗时",
        note_ref="",
    )

    assert module.format_memory_entry(entry) == (
        "---\n"
        "id:Ab12Z9\n"
        "2026/4/17-self：\n"
        "完成任务必须说明任务总耗时\n"
    )


def test_parse_memory_document_round_trips_user_and_self_entries() -> None:
    module = _load_markdown_memory_module()
    text = (
        "---\n"
        "id:Ab12Z9\n"
        "2026/4/17-self：\n"
        "完成任务必须说明任务总耗时\n\n"
        "---\n"
        "id:Cd34Ef\n"
        "2026/4/17-user：\n"
        "创建文件默认格式要求，见 ref:note_a1b2\n"
    )

    items = module.parse_memory_document(text)

    assert [item.source for item in items] == ["self", "user"]
    assert [item.memory_id for item in items] == ["Ab12Z9", "Cd34Ef"]
    assert items[0].summary == "完成任务必须说明任务总耗时"
    assert items[1].note_ref == "note_a1b2"


def test_validate_memory_document_rejects_summary_over_250_chars() -> None:
    module = _load_markdown_memory_module()
    text = (
        "---\n"
        "id:Ab12Z9\n"
        "2026/4/17-self：\n"
        f"{'x' * 251}\n"
    )

    with pytest.raises(ValueError, match="summary line exceeds 250 chars"):
        module.validate_memory_document(text, summary_max_chars=250, document_max_chars=20000)


def test_validate_memory_document_rejects_document_over_20000_chars() -> None:
    module = _load_markdown_memory_module()
    body = (
        "---\n"
        "2026/4/17-self：\n"
        "短句\n"
    )
    oversized = body + ("x" * 20001)

    with pytest.raises(ValueError, match="memory document exceeds 20000 chars"):
        module.validate_memory_document(oversized, summary_max_chars=250, document_max_chars=20000)


def test_v2_memory_entry_requires_id_line_and_formats_four_line_block() -> None:
    module = _load_markdown_memory_module()
    entry = module.MemoryEntry(
        memory_id="Ab12Z9",
        date_text="2026/4/18",
        source="user",
        summary="Prefer concise answers",
        note_ref="",
    )

    assert module.format_memory_entry(entry) == (
        "---\n"
        "id:Ab12Z9\n"
        "2026/4/18-user：\n"
        "Prefer concise answers\n"
    )


def test_v2_parse_memory_document_reads_id_line() -> None:
    module = _load_markdown_memory_module()
    text = (
        "---\n"
        "id:Ab12Z9\n"
        "2026/4/18-user：\n"
        "Prefer concise answers\n"
    )

    items = module.parse_memory_document(text)

    assert len(items) == 1
    assert items[0].memory_id == "Ab12Z9"
    assert items[0].source == "user"
    assert items[0].summary == "Prefer concise answers"


def test_v2_validate_memory_document_rejects_legacy_blocks_without_id() -> None:
    module = _load_markdown_memory_module()
    text = (
        "---\n"
        "2026/4/18-user：\n"
        "Prefer concise answers\n"
    )

    with pytest.raises(ValueError, match="memory document contains invalid blocks"):
        module.validate_memory_document(text, summary_max_chars=250, document_max_chars=20000)


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


def test_memory_repository_generates_six_char_base62_ids(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)
    db_path = tmp_path / "memory" / "memory_state.sqlite3"

    try:
        row = manager._memory_repo.create_memory(
            memory_body="Prefer concise answers",
            minimal_memory="answers->concise",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
    finally:
        manager.close()

    assert len(row["memory_id"]) == 6
    assert row["memory_id"].isalnum()
    assert row["memory_body"] == "Prefer concise answers"
    assert row["minimal_memory"] == "answers->concise"
    assert row["refresh_count"] == 0
    assert row["passed_count"] == 0
    assert row["is_compressed"] is False
    assert row["from_user"] is True
    assert db_path.exists()

    with sqlite3.connect(str(db_path)) as conn:
        persisted = conn.execute(
            """
            SELECT memory_id, memory_body, minimal_memory, source, from_user, refresh_count, passed_count, is_compressed
            FROM memories
            WHERE memory_id = ?
            """,
            (row["memory_id"],),
        ).fetchone()

    assert persisted is not None
    assert persisted[0] == row["memory_id"]
    assert persisted[1] == "Prefer concise answers"
    assert persisted[2] == "answers->concise"
    assert persisted[3] == "user"
    assert persisted[4] == 1
    assert persisted[5] == 0
    assert persisted[6] == 0
    assert persisted[7] == 0


def test_memory_repository_rejects_source_from_user_mismatch(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    repo = module._MemorySqliteRepository(tmp_path / "memory" / "memory_state.sqlite3")

    with pytest.raises(ValueError, match="from_user must match source"):
        repo.create_memory(
            memory_body="Prefer concise answers",
            minimal_memory="answers->concise",
            source="self",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )


def test_memory_repository_closes_sqlite_connections_explicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_memory_agent_runtime_module()
    original_connect = sqlite3.connect
    close_calls = {"count": 0}

    class _TrackedConnection:
        def __init__(self, conn: sqlite3.Connection):
            self._conn = conn

        @property
        def row_factory(self):
            return self._conn.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            self._conn.row_factory = value

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._conn.__exit__(exc_type, exc, tb)

        def close(self) -> None:
            close_calls["count"] += 1
            self._conn.close()

        def commit(self) -> None:
            self._conn.commit()

        def execute(self, *args, **kwargs):
            return self._conn.execute(*args, **kwargs)

    def _tracked_connect(*args, **kwargs):
        return _TrackedConnection(original_connect(*args, **kwargs))

    monkeypatch.setattr(module.sqlite3, "connect", _tracked_connect, raising=False)

    repo = module._MemorySqliteRepository(tmp_path / "memory" / "memory_state.sqlite3")
    repo.create_memory(
        memory_body="Prefer concise answers",
        minimal_memory="answers->concise",
        source="user",
        from_user=True,
        now_iso="2026-04-25T00:00:00+08:00",
    )

    assert close_calls["count"] >= 2


def test_memory_snapshot_rebuilds_from_sqlite_rows(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        first = manager._memory_repo.create_memory(
            memory_body="Prefer concise answers",
            minimal_memory="answers->concise",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        second = manager._memory_repo.create_memory(
            memory_body="Use checklist replies ref:note_policy",
            minimal_memory="replies->checklist",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:05:00+08:00",
        )
        db_path = tmp_path / "memory" / "memory_state.sqlite3"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE memories SET is_compressed = 1 WHERE memory_id = ?",
                (second["memory_id"],),
            )
            conn.commit()

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        snapshot = manager.snapshot_text()
        assert f"id:{first['memory_id']}" in snapshot
        assert "2026/4/25-user" in snapshot
        assert "Prefer concise answers" in snapshot
        assert "answers->concise" not in snapshot
        assert f"id:{second['memory_id']}" in snapshot
        assert "2026/4/25-self" in snapshot
        assert "replies->checklist ref:note_policy" in snapshot
        assert "Use checklist replies ref:note_policy" not in snapshot
        rebuilt_entries = module.parse_memory_document(snapshot)
        rebuilt_second = next(entry for entry in rebuilt_entries if entry.memory_id == second["memory_id"])
        assert rebuilt_second.summary == "replies->checklist ref:note_policy"
        assert rebuilt_second.note_ref == "note_policy"
    finally:
        manager.close()


def test_memory_snapshot_rebuild_normalizes_noteid_refs_from_compressed_minimal_memory(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        compressed = manager._memory_repo.create_memory(
            memory_body="Use checklist replies",
            minimal_memory="replies->checklist 见noteid:note_policy",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:05:00+08:00",
        )
        db_path = tmp_path / "memory" / "memory_state.sqlite3"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE memories SET is_compressed = 1 WHERE memory_id = ?",
                (compressed["memory_id"],),
            )
            conn.commit()

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        snapshot = manager.snapshot_text()
        assert "见noteid:note_policy" not in snapshot
        assert "replies->checklist ref:note_policy" in snapshot
        rebuilt_entries = module.parse_memory_document(snapshot)
        rebuilt = next(entry for entry in rebuilt_entries if entry.memory_id == compressed["memory_id"])
        assert rebuilt.summary == "replies->checklist ref:note_policy"
        assert rebuilt.note_ref == "note_policy"
    finally:
        manager.close()


def test_memory_snapshot_rebuild_reinjects_minimal_memory_note_ref_for_uncompressed_rows(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        row = manager._memory_repo.create_memory(
            memory_body="Use checklist replies",
            minimal_memory="replies->checklist ref:note_policy",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:05:00+08:00",
        )

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        snapshot = manager.snapshot_text()
        assert "Use checklist replies ref:note_policy" in snapshot
        rebuilt_entries = module.parse_memory_document(snapshot)
        rebuilt = next(entry for entry in rebuilt_entries if entry.memory_id == row["memory_id"])
        assert rebuilt.summary == "Use checklist replies ref:note_policy"
        assert rebuilt.note_ref == "note_policy"
    finally:
        manager.close()


def test_memory_snapshot_rebuild_prefers_minimal_memory_note_ref_when_memory_body_conflicts(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        row = manager._memory_repo.create_memory(
            memory_body="Use checklist replies ref:old_note",
            minimal_memory="replies->checklist ref:new_note",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:05:00+08:00",
        )

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        snapshot = manager.snapshot_text()
        assert "Use checklist replies ref:new_note" in snapshot
        assert "old_note" not in snapshot
        rebuilt_entries = module.parse_memory_document(snapshot)
        rebuilt = next(entry for entry in rebuilt_entries if entry.memory_id == row["memory_id"])
        assert rebuilt.summary == "Use checklist replies ref:new_note"
        assert rebuilt.note_ref == "new_note"
    finally:
        manager.close()


def test_memory_snapshot_rebuild_uses_active_document_max_chars(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    cfg.document.document_max_chars = 40
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        manager._memory_repo.create_memory(
            memory_body="Prefer concise answers",
            minimal_memory="answers->concise",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )

        with pytest.raises(ValueError, match="memory document exceeds 40 chars"):
            manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")
    finally:
        manager.close()


def test_memory_batch_updates_refresh_and_passed_counts(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        used = manager._memory_repo.create_memory(
            memory_body="A",
            minimal_memory="a->do",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        untouched = manager._memory_repo.create_memory(
            memory_body="B",
            minimal_memory="b->do",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        rewritten = manager._memory_repo.create_memory(
            memory_body="C",
            minimal_memory="c->do",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        validated = module._MemoryValidatedWrite(
            document_text="",
            note_writes={},
            note_refs_written=[],
            memory_chars_after=0,
            document_preview="",
            adds=[{"content": "D", "minimal_memory": "d->do", "decision_source": "self"}],
            rewrites=[{"id": rewritten["memory_id"], "content": "C updated", "minimal_memory": "c->updated"}],
            deletes=[],
            note_upserts={},
            inspired_memory_ids=[used["memory_id"]],
            noop_reason="",
        )
        manager._commit_validated_write(validated)

        rows = {row["memory_id"]: row for row in manager._memory_repo.list_memories()}

        assert len(rows) == 4
        assert rows[used["memory_id"]]["refresh_count"] == 1
        assert rows[used["memory_id"]]["passed_count"] == 0
        assert rows[rewritten["memory_id"]]["refresh_count"] == 1
        assert rows[rewritten["memory_id"]]["passed_count"] == 0
        assert rows[untouched["memory_id"]]["passed_count"] == 1
        assert rows[rewritten["memory_id"]]["from_user"] is True
        assert any(
            row["memory_body"] == "D" and row["minimal_memory"] == "d->do" and row["from_user"] is False
            for row in rows.values()
        )
    finally:
        manager.close()


def test_memory_commit_keeps_snapshot_after_sqlite_rebuild(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())

    try:
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Prefer concise answers",
                    created_at="2026-04-25T00:00:00+08:00",
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)

        apply_result = session.apply_batch(
            adds=[{"content": "Prefer concise answers", "minimal_memory": "answers->concise", "decision_source": "self"}]
        )
        assert apply_result == {"ok": True, "status": "batch_staged"}

        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        manager._commit_validated_write(validated)

        committed_snapshot = manager.snapshot_text()

        assert "Prefer concise answers" in committed_snapshot
        assert "id:" in committed_snapshot

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        assert manager.snapshot_text() == committed_snapshot
    finally:
        manager.close()


def test_memory_compression_orders_by_passed_desc_refresh_asc_and_preserves_from_user_rows(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    cfg.document.compress_trigger_chars = 120
    cfg.document.compress_target_chars = 80
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        protected = manager._memory_repo.create_memory(
            memory_body="U" * 70,
            minimal_memory="user->protected",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        cold = manager._memory_repo.create_memory(
            memory_body="C" * 70,
            minimal_memory="cold->compressed",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        hotter = manager._memory_repo.create_memory(
            memory_body="H" * 70,
            minimal_memory="hotter->compressed",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )

        with manager._memory_repo._connect() as conn:
            conn.execute(
                "UPDATE memories SET passed_count = 100, refresh_count = 0 WHERE memory_id = ?",
                (protected["memory_id"],),
            )
            conn.execute(
                "UPDATE memories SET passed_count = 100, refresh_count = 0 WHERE memory_id = ?",
                (cold["memory_id"],),
            )
            conn.execute(
                "UPDATE memories SET passed_count = 100, refresh_count = 10 WHERE memory_id = ?",
                (hotter["memory_id"],),
            )
            conn.commit()

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")
        manager._compress_memory_snapshot_if_needed(now_iso="2026-04-25T00:00:00+08:00")

        rows = {row["memory_id"]: row for row in manager._memory_repo.list_memories()}

        assert protected["memory_id"] in rows
        assert rows[protected["memory_id"]]["is_compressed"] is True
        assert cold["memory_id"] not in rows or rows[cold["memory_id"]]["is_compressed"] is True
        assert len(manager.snapshot_text()) <= 80
    finally:
        manager.close()


def test_processed_history_records_refresh_and_compression_metadata(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        validated = module._MemoryValidatedWrite(
            document_text="",
            note_writes={},
            note_refs_written=[],
            memory_chars_after=0,
            document_preview="",
            adds=[{"content": "Use headings", "minimal_memory": "answers->headings", "decision_source": "self"}],
            rewrites=[],
            deletes=[],
            note_upserts={},
            inspired_memory_ids=[],
            noop_reason="",
        )
        manager._commit_validated_write(validated)
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use headings",
                    created_at=manager._now_iso(),
                    request_id="write_1",
                )
            ],
        )

        payload = manager._append_terminal_history(
            batch=batch,
            status="applied",
            op="write",
            processed_at=manager._now_iso(),
            validated=validated,
        )

        assert payload["source_op"] == "write"
        assert payload["inspired_memory_ids"] == []
        assert "memory_chars_after" in payload
        assert payload["compression_triggered"] is False
        assert payload["compressed_memory_ids"] == []
        assert payload["deleted_by_compression_ids"] == []
    finally:
        manager.close()


def test_memory_commit_after_empty_snapshot_clears_stale_sqlite_rows(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())

    try:
        stale = manager._memory_repo.create_memory(
            memory_body="Stale memory",
            minimal_memory="stale->old",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )

        assert manager.snapshot_text() == ""

        validated = module._MemoryValidatedWrite(
            document_text="",
            note_writes={},
            note_refs_written=[],
            memory_chars_after=0,
            document_preview="",
            adds=[{"content": "Fresh memory", "minimal_memory": "fresh->new", "decision_source": "self"}],
            rewrites=[],
            deletes=[],
            note_upserts={},
            inspired_memory_ids=[],
            noop_reason="",
        )
        manager._commit_validated_write(validated)

        rows = {row["memory_id"]: row for row in manager._memory_repo.list_memories()}
        snapshot = manager.snapshot_text()

        assert stale["memory_id"] not in rows
        assert len(rows) == 1
        assert "Fresh memory" in snapshot
        assert "Stale memory" not in snapshot

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        assert "Fresh memory" in manager.snapshot_text()
        assert "Stale memory" not in manager.snapshot_text()
    finally:
        manager.close()


def test_import_legacy_apply_populates_sqlite_and_preserves_notes(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(
        json.dumps(
            [
                {
                    "date_text": "2026/4/18",
                    "source": "user",
                    "summary": "Prefer concise answers ref:note_policy",
                    "note_ref": "note_policy",
                    "note_text": "Use concise bullets by default.",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    try:
        report = manager.import_legacy(legacy_path, apply=True)
        note_path = tmp_path / "memory" / "notes" / "note_policy.md"
        rows = manager._memory_repo.list_memories()

        assert report["status"] == "applied"
        assert len(rows) == 1
        assert "Prefer concise answers ref:note_policy" in manager.snapshot_text()
        assert note_path.exists()
        assert note_path.read_text(encoding="utf-8") == "Use concise bullets by default.\n"

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        assert "Prefer concise answers ref:note_policy" in manager.snapshot_text()
        assert note_path.exists()
    finally:
        manager.close()


def test_memory_commit_replays_effective_deduped_adds(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())

    try:
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use headings",
                    created_at="2026-04-25T00:00:00+08:00",
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        apply_result = session.apply_batch(
            adds=[
                {"content": "Use headings", "minimal_memory": "answers->headings", "decision_source": "self"},
                {"content": "Use headings", "minimal_memory": "answers->headings", "decision_source": "self"},
            ]
        )

        assert apply_result == {"ok": True, "status": "batch_staged"}

        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        manager._commit_validated_write(validated)

        assert len(manager._memory_repo.list_memories()) == 1
        assert module.MemoryManager._normalize_document_text(manager.snapshot_text()) == validated.document_text
    finally:
        manager.close()


def test_memory_commit_replays_effective_deduped_rewrites(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())

    try:
        first = manager._memory_repo.create_memory(
            memory_body="A",
            minimal_memory="a->do",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        second = manager._memory_repo.create_memory(
            memory_body="B",
            minimal_memory="b->do",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Converge duplicates",
                    created_at="2026-04-25T00:00:00+08:00",
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        apply_result = session.apply_batch(
            rewrites=[
                {"id": first["memory_id"], "content": "Unified", "minimal_memory": "same->value"},
                {"id": second["memory_id"], "content": "Unified", "minimal_memory": "same->value"},
            ]
        )

        assert apply_result == {"ok": True, "status": "batch_staged"}

        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        manager._commit_validated_write(validated)

        rows = manager._memory_repo.list_memories()

        assert len(rows) == 1
        assert rows[0]["memory_id"] in {first["memory_id"], second["memory_id"]}
        assert module.MemoryManager._normalize_document_text(manager.snapshot_text()) == validated.document_text
    finally:
        manager.close()


def test_memory_sync_preserves_metadata_for_matching_ids_on_non_empty_divergence(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())

    try:
        row = manager._memory_repo.create_memory(
            memory_body="Prefer concise answers",
            minimal_memory="answers->concise",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        db_path = tmp_path / "memory" / "memory_state.sqlite3"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                """
                UPDATE memories
                SET refresh_count = 7, passed_count = 9, is_compressed = 1
                WHERE memory_id = ?
                """,
                (row["memory_id"],),
            )
            conn.commit()
        (tmp_path / "memory" / "MEMORY.md").write_text(
            f"---\nid:{row['memory_id']}\n2026/4/25-user：\nUpdated snapshot text\n",
            encoding="utf-8",
        )

        manager._sync_sqlite_from_snapshot_if_needed_locked(now_iso="2026-04-25T01:00:00+08:00")

        rows = manager._memory_repo.list_memories()

        assert len(rows) == 1
        assert rows[0]["memory_id"] == row["memory_id"]
        assert rows[0]["memory_body"] == "Updated snapshot text"
        assert rows[0]["minimal_memory"] == "Updated snapshot text"
        assert rows[0]["refresh_count"] == 7
        assert rows[0]["passed_count"] == 9
        assert rows[0]["is_compressed"] is True
        assert rows[0]["from_user"] is True
    finally:
        manager.close()


def test_v2_memory_agent_tools_expose_read_note_and_apply_batch_only(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        session = module._MemoryToolSession(snapshot_text="", notes_dir=tmp_path / "memory" / "notes")
        tool_names = [tool.name for tool in manager._memory_agent_tools(session)]
    finally:
        manager.close()

    assert tool_names == ["memory_read_note", "memory_apply_batch"]


def test_memory_agent_apply_batch_tool_preserves_inspired_ids_in_active_runtime_path(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        existing = manager._memory_repo.create_memory(
            memory_body="Prefer concise answers",
            minimal_memory="answers->concise",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use headings",
                    created_at=manager._now_iso(),
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        apply_tool = next(tool for tool in manager._memory_agent_tools(session) if tool.name == "memory_apply_batch")

        assert "inspired_memory_ids" in apply_tool.args
        result = apply_tool.invoke(
            {
                "adds": [{"content": "Use headings", "minimal_memory": "answers->headings", "decision_source": "self"}],
                "inspired_memory_ids": [existing["memory_id"]],
            }
        )

        assert result["ok"] is True
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        assert validated.inspired_memory_ids == [existing["memory_id"]]
    finally:
        manager.close()


def test_memory_agent_apply_batch_accepts_inspired_ids_present_in_snapshot_without_sqlite_row(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            "---\nid:Ab12Z9\n2026/4/25-user：\nPrefer concise answers\n",
            encoding="utf-8",
        )
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use headings",
                    created_at=manager._now_iso(),
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        apply_tool = next(tool for tool in manager._memory_agent_tools(session) if tool.name == "memory_apply_batch")

        result = apply_tool.invoke(
            {
                "adds": [{"content": "Use headings", "minimal_memory": "answers->headings", "decision_source": "self"}],
                "inspired_memory_ids": ["Ab12Z9"],
            }
        )

        assert result["ok"] is True
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        assert validated.inspired_memory_ids == ["Ab12Z9"]
    finally:
        manager.close()


def test_memory_agent_apply_batch_rejects_non_list_inspired_ids_at_staging(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    session = module._MemoryToolSession(snapshot_text="", notes_dir=tmp_path / "memory" / "notes")

    result = session.apply_batch(
        adds=[{"content": "Use headings", "minimal_memory": "answers->headings", "decision_source": "self"}],
        inspired_memory_ids="Ab12Z9",
    )

    assert result["ok"] is False
    assert result["errors"]["inspired_memory_ids"] == "inspired_memory_ids must be a list"


def test_memory_agent_apply_batch_requires_minimal_memory_and_tracks_inspired_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        existing = manager._memory_repo.create_memory(
            memory_body="Prefer concise answers",
            minimal_memory="answers->concise",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use headings",
                    created_at=manager._now_iso(),
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        result = session.apply_batch(
            adds=[{"content": "Use headings", "minimal_memory": "answers->headings", "decision_source": "self"}],
            inspired_memory_ids=[existing["memory_id"]],
        )

        assert result["ok"] is True
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        assert validated.adds[0]["minimal_memory"] == "answers->headings"
        assert validated.inspired_memory_ids == [existing["memory_id"]]
    finally:
        manager.close()


def test_memory_agent_apply_batch_rejects_non_list_inspired_ids_at_validation(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use headings",
                    created_at=manager._now_iso(),
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        session.apply_batch_count = 1
        session.applied_batch = {
            "adds": [{"content": "Use headings", "minimal_memory": "answers->headings", "decision_source": "self"}],
            "rewrites": [],
            "deletes": [],
            "note_upserts": {},
            "inspired_memory_ids": "Ab12Z9",
            "noop_reason": "",
        }

        with pytest.raises(ValueError, match="inspired_memory_ids must be a list"):
            manager._build_validated_write_from_apply_batch(
                before_text=manager.snapshot_text(),
                session=session,
                batch=batch,
            )
    finally:
        manager.close()


def test_memory_agent_apply_batch_rejects_minimal_memory_without_condition_arrow_shape(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use headings",
                    created_at=manager._now_iso(),
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        result = session.apply_batch(
            adds=[{"content": "Use headings", "minimal_memory": "headings", "decision_source": "self"}],
        )

        assert result["ok"] is True
        with pytest.raises(ValueError, match="minimal_memory must use 条件->要求关键词"):
            manager._build_validated_write_from_apply_batch(
                before_text=manager.snapshot_text(),
                session=session,
                batch=batch,
            )
    finally:
        manager.close()


@pytest.mark.parametrize("mode", ["add", "rewrite"])
def test_memory_agent_apply_batch_prefers_minimal_memory_note_ref_over_stale_content_ref(
    tmp_path: Path,
    mode: str,
) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        before_text = ""
        if mode == "rewrite":
            before_text = "---\nid:Ab12Z9\n2026/4/25-self：\nUse checklist replies ref:old_note\n"
            (tmp_path / "memory" / "MEMORY.md").write_text(before_text, encoding="utf-8")

        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use checklist replies",
                    created_at=manager._now_iso(),
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        apply_kwargs: dict[str, object] = {
            "note_upserts": {"new_note": "Use checklist replies with the current checklist note."},
        }
        if mode == "add":
            apply_kwargs["adds"] = [
                {
                    "content": "Use checklist replies ref:old_note",
                    "minimal_memory": "replies->checklist 见noteid:new_note",
                    "decision_source": "self",
                }
            ]
        else:
            apply_kwargs["rewrites"] = [
                {
                    "id": "Ab12Z9",
                    "content": "Use checklist replies ref:old_note",
                    "minimal_memory": "replies->checklist 见noteid:new_note",
                }
            ]

        result = session.apply_batch(**apply_kwargs)

        assert result["ok"] is True
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        assert "ref:new_note" in validated.document_text
        assert "old_note" not in validated.document_text
        parsed_entries = module.parse_memory_document(validated.document_text)
        assert len(parsed_entries) == 1
        assert parsed_entries[0].note_ref == "new_note"
    finally:
        manager.close()


@pytest.mark.parametrize("note_ref_token", ["ref:note_policy", "见noteid:note_policy"])
def test_memory_agent_apply_batch_requires_note_for_note_ref_present_only_in_minimal_memory(
    tmp_path: Path,
    note_ref_token: str,
) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="self",
                    payload_text="Use headings",
                    created_at=manager._now_iso(),
                    request_id="write_1",
                )
            ],
        )
        session = module._MemoryToolSession(snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir)
        result = session.apply_batch(
            adds=[
                {
                    "content": "Use headings",
                    "minimal_memory": f"answers->headings {note_ref_token}",
                    "decision_source": "self",
                }
            ],
        )

        assert result["ok"] is True
        with pytest.raises(ValueError, match="missing note refs: note_policy"):
            manager._build_validated_write_from_apply_batch(
                before_text=manager.snapshot_text(),
                session=session,
                batch=batch,
            )
    finally:
        manager.close()


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
        result = await manager.enqueue_session_boundary_flush(
            session_key="session-1",
            channel="web",
            chat_id="chat-1",
            trigger_source="session_boundary_flush",
        )

        assert result["ok"] is True
        assert result["status"] == "ignored"
        assert result["reason"] == "session_boundary_flush_disabled"
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
        assert result["status"] == "buffered"
        assert queue_items == []
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
async def test_v2_record_turn_for_review_queues_write_after_five_turns(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        for index in range(4):
            result = await manager.record_turn_for_review(
                session_key="web:shared",
                turn_id=f"turn-{index + 1}",
                user_messages=[f"user {index + 1}"],
                assistant_text=f"assistant {index + 1}",
                compression_summary={"status": ""},
                canonical_summary={"stages": []},
            )
            assert result["status"] == "buffered"
            assert await manager.list_queue(limit=10) == []

        fifth = await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-5",
            user_messages=["user 5"],
            assistant_text="assistant 5",
            compression_summary={"status": ""},
            canonical_summary={"stages": []},
        )
        queue_items = await manager.list_queue(limit=10)

        assert fifth["status"] == "queued"
        assert len(queue_items) == 1
        assert queue_items[0]["op"] == "write"
        assert queue_items[0]["session_key"] == "web:shared"

        state = manager._read_review_state()
        assert state["sessions"]["web:shared"]["pending_turns"] == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_flush_review_window_queues_write_before_threshold_on_compression(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["user 1"],
            assistant_text="assistant 1",
            compression_summary={"status": ""},
            canonical_summary={"stages": []},
        )
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-2",
            user_messages=["user 2"],
            assistant_text="assistant 2",
            compression_summary={"status": ""},
            canonical_summary={"stages": []},
        )

        result = await manager.flush_review_window(
            session_key="web:shared",
            trigger_source="token_compression",
        )
        queue_items = await manager.list_queue(limit=10)

        assert result["status"] == "queued"
        assert len(queue_items) == 1
        assert queue_items[0]["op"] == "write"
        assert queue_items[0]["trigger_source"] == "token_compression"

        state = manager._read_review_state()
        assert state["sessions"]["web:shared"]["pending_turns"] == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_flush_review_window_ignores_non_compression_trigger(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["user 1"],
            assistant_text="assistant 1",
            compression_summary={"status": "running", "text": "should not persist"},
            canonical_summary={"stages": []},
        )

        result = await manager.flush_review_window(
            session_key="web:shared",
            trigger_source="pre_compression_flush",
        )
        queue_items = await manager.list_queue(limit=10)

        assert result["status"] == "ignored"
        assert result["reason"] == "unsupported_trigger_source"
        assert queue_items == []
        state = manager._read_review_state()
        assert len(state["sessions"]["web:shared"]["pending_turns"]) == 1
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_review_turn_payload_keeps_only_user_assistant_and_stage_summary(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        canonical_summary = {
            "active_stage_id": "frontdoor-stage-2",
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_goal": "collect candidate pool",
                    "completed_stage_summary": "verified current ranking methodology",
                    "rounds": [
                        {
                            "tools": [
                                {
                                    "tool_name": "web_fetch",
                                    "output_text": "HUGE_TOOL_OUTPUT_SHOULD_NOT_APPEAR",
                                }
                            ]
                        }
                    ],
                    "contract_revision": "exp:should-not-appear",
                }
            ],
        }
        result = await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["user wants a durable preference"],
            assistant_text="assistant confirmed the preference",
            compression_summary={"status": "running", "text": "TOKEN_COMPACTION_SHOULD_NOT_APPEAR"},
            canonical_summary=canonical_summary,
        )

        assert result["status"] == "buffered"
        state = manager._read_review_state()
        payload_text = state["sessions"]["web:shared"]["pending_turns"][0]["payload_text"]
        assert "user wants a durable preference" in payload_text
        assert "assistant confirmed the preference" in payload_text
        assert "collect candidate pool" in payload_text
        assert "verified current ranking methodology" in payload_text
        assert "HUGE_TOOL_OUTPUT_SHOULD_NOT_APPEAR" not in payload_text
        assert "TOKEN_COMPACTION_SHOULD_NOT_APPEAR" not in payload_text
        assert "exp:should-not-appear" not in payload_text
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_review_window_keeps_stage_summary_deltas_inside_one_window(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["first turn"],
            assistant_text="assistant one",
            compression_summary={"status": ""},
            canonical_summary={
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-alpha",
                        "stage_goal": "stage alpha goal",
                        "completed_stage_summary": "stage alpha completed",
                    }
                ]
            },
        )
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-2",
            user_messages=["second turn"],
            assistant_text="assistant two",
            compression_summary={"status": ""},
            canonical_summary={
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-alpha",
                        "stage_goal": "stage alpha goal",
                        "completed_stage_summary": "stage alpha completed",
                    },
                    {
                        "stage_id": "frontdoor-stage-beta",
                        "stage_goal": "stage beta goal",
                        "completed_stage_summary": "stage beta completed",
                    },
                ]
            },
        )

        result = await manager.flush_review_window(
            session_key="web:shared",
            trigger_source="token_compression",
        )
        queue_items = await manager.list_queue(limit=10)

        assert result["status"] == "queued"
        payload_text = queue_items[0]["payload_text"]
        assert payload_text.count("frontdoor-stage-alpha") == 1
        assert payload_text.count("stage alpha completed") == 1
        assert payload_text.count("frontdoor-stage-beta") == 1
        assert payload_text.count("stage beta completed") == 1
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_review_window_stage_cursor_survives_flush_for_next_window(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["first window"],
            assistant_text="assistant one",
            compression_summary={"status": ""},
            canonical_summary={
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-alpha",
                        "stage_goal": "stage alpha goal",
                        "completed_stage_summary": "stage alpha completed",
                    }
                ]
            },
        )
        first_flush = await manager.flush_review_window(
            session_key="web:shared",
            trigger_source="token_compression",
        )
        assert first_flush["status"] == "queued"

        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-2",
            user_messages=["second window"],
            assistant_text="assistant two",
            compression_summary={"status": ""},
            canonical_summary={
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-alpha",
                        "stage_goal": "stage alpha goal",
                        "completed_stage_summary": "stage alpha completed",
                    },
                    {
                        "stage_id": "frontdoor-stage-beta",
                        "stage_goal": "stage beta goal",
                        "completed_stage_summary": "stage beta completed",
                    },
                ]
            },
        )
        second_flush = await manager.flush_review_window(
            session_key="web:shared",
            trigger_source="token_compression",
        )
        queue_items = await manager.list_queue(limit=10)

        assert second_flush["status"] == "queued"
        assert len(queue_items) == 2
        second_payload = queue_items[1]["payload_text"]
        assert "frontdoor-stage-alpha" not in second_payload
        assert "stage alpha completed" not in second_payload
        assert "frontdoor-stage-beta" in second_payload
        assert "stage beta completed" in second_payload
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_review_window_records_stage_when_summary_version_changes(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["stage started"],
            assistant_text="assistant one",
            compression_summary={"status": ""},
            canonical_summary={
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-alpha",
                        "stage_goal": "stage alpha goal",
                    }
                ]
            },
        )
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-2",
            user_messages=["stage completed"],
            assistant_text="assistant two",
            compression_summary={"status": ""},
            canonical_summary={
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-alpha",
                        "stage_goal": "stage alpha goal",
                        "completed_stage_summary": "stage alpha completed later",
                    }
                ]
            },
        )

        result = await manager.flush_review_window(
            session_key="web:shared",
            trigger_source="token_compression",
        )
        queue_items = await manager.list_queue(limit=10)

        assert result["status"] == "queued"
        payload_text = queue_items[0]["payload_text"]
        assert payload_text.count("frontdoor-stage-alpha") == 2
        assert "stage alpha completed later" in payload_text
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
                            "name": "memory_apply_batch",
                            "args": {
                                "adds": [
                                    {
                                        "content": "创建文件默认格式要求，见 ref:note_policy",
                                        "minimal_memory": "files->default-format",
                                        "decision_source": "user",
                                    }
                                ],
                                "note_upserts": {
                                    "note_policy": "创建文件默认格式要求"
                                },
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
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/17", raising=False)
        monkeypatch.setattr(module, "build_chat_model", lambda config, **kwargs: fake_model, raising=False)

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:04+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert report["attempt_count"] == 1
        assert "id:" in manager.snapshot_text()
        assert "2026/4/17-user：" in manager.snapshot_text()
        assert "ref:note_policy" in manager.snapshot_text()
        assert (tmp_path / "memory" / "notes" / "note_policy.md").exists()
        assert (tmp_path / "memory" / "queue.jsonl").read_text(encoding="utf-8").strip() == ""
        assert len(processed) == 1
        assert processed[0]["op"] == "write"
        assert processed[0]["write_mode"] == "add"
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
async def test_run_due_batch_once_records_rewrite_write_mode_for_processed_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            "---\nid:Ab12Z9\n2026/4/16-user：\n创建 Markdown 文档时，默认使用 [G3KU]-时间-前缀命名。\n",
            encoding="utf-8",
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="创建 Markdown 文档时，改为使用时间-内容格式命名，不要默认添加 [G3KU]-时间-前缀。",
                created_at="2026-04-17T10:00:00+08:00",
                request_id="write_rewrite_1",
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
                                        "content": "创建 Markdown 文档时，要使用时间-内容格式命名，不要默认添加 [G3KU]-时间-前缀。",
                                        "minimal_memory": "markdown-docs->time-content-naming",
                                    }
                                ]
                            },
                        }
                    ],
                    usage={"input_tokens": 6, "output_tokens": 2, "cache_read_tokens": 0},
                ),
                _fake_response(
                    content="done",
                    usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
                ),
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

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:04+08:00")
        processed = _read_jsonl(tmp_path / "memory" / "ops.jsonl")

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert len(processed) == 1
        assert processed[0]["op"] == "write"
        assert processed[0]["write_mode"] == "rewrite"
        assert processed[0]["request_ids"] == ["write_rewrite_1"]
        assert "id:Ab12Z9" in manager.snapshot_text()
        assert "时间-内容格式命名" in manager.snapshot_text()
        assert "[G3KU]-时间-" in manager.snapshot_text()
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_records_change_preview_for_processed_rewrite_batch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    markdown_module = _load_markdown_memory_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            markdown_module.format_memory_entry(
                markdown_module.MemoryEntry(
                    memory_id="Ab12Z9",
                    date_text="2026/4/16",
                    source="user",
                    summary="Use [G3KU]-time-prefix for Markdown files.",
                    note_ref="",
                )
            ),
            encoding="utf-8",
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="Change Markdown naming to time-content format without the [G3KU] prefix.",
                created_at="2026-04-17T10:00:00+08:00",
                request_id="write_rewrite_preview_1",
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
                                        "content": "Use time-content naming for Markdown files.",
                                        "minimal_memory": "markdown-docs->time-content-naming",
                                    }
                                ]
                            },
                        }
                    ],
                    usage={"input_tokens": 6, "output_tokens": 2, "cache_read_tokens": 0},
                ),
                _fake_response(
                    content="done",
                    usage={"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
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
        assert len(processed) == 1
        assert processed[0]["change_preview"] == "修改 Ab12Z9：Use time-content naming for Markdown files."
        assert processed[0]["document_preview"] != processed[0]["change_preview"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_list_processed_page_prunes_batches_older_than_seven_days(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        monkeypatch.setattr(module.MemoryManager, "_now_iso", staticmethod(lambda: "2026-04-27T10:00:00+08:00"))
        old_payload = {
            "batch_id": "old_batch",
            "op": "write",
            "source_op": "write",
            "status": "applied",
            "processed_at": "2026-04-19T09:59:59+08:00",
            "request_ids": ["old_request"],
            "request_count": 1,
            "decision_sources": ["user"],
            "payload_texts": ["old payload"],
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
            "model_chain": ["memory-primary"],
            "attempt_count": 1,
        }
        boundary_payload = {
            "batch_id": "boundary_batch",
            "op": "write",
            "source_op": "write",
            "status": "applied",
            "processed_at": "2026-04-20T10:00:00+08:00",
            "request_ids": ["boundary_request"],
            "request_count": 1,
            "decision_sources": ["user"],
            "payload_texts": ["boundary payload"],
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
            "model_chain": ["memory-primary"],
            "attempt_count": 1,
        }
        (tmp_path / "memory" / "ops.jsonl").write_text(
            "\n".join(
                [
                    json.dumps(old_payload, ensure_ascii=False),
                    json.dumps(boundary_payload, ensure_ascii=False),
                ]
            ) + "\n",
            encoding="utf-8",
        )

        payload = await manager.list_processed_page(limit=20, offset=0)

        assert [item["batch_id"] for item in payload["items"]] == ["boundary_batch"]
        assert [item["batch_id"] for item in _read_jsonl(tmp_path / "memory" / "ops.jsonl")] == ["boundary_batch"]
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
            "---\nid:Ab12Z9\n2026/4/17-self：\n完成任务必须说明任务总耗时\n",
            encoding="utf-8",
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
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="Ab12Z9",
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
                "---\nid:Ab12Z9\n2026/4/17-self：\nReport total elapsed time\n"
                "---\nid:Cd34Ef\n2026/4/17-user：\nPrefer Chinese replies\n"
            ),
            encoding="utf-8",
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="Ab12Z9",
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
            "---\nid:Ab12Z9\n2026/4/17-self：\nReport total elapsed time\n",
            encoding="utf-8",
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="Ab12Z9",
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
                            "name": "memory_apply_batch",
                            "args": {
                                "adds": [
                                    {
                                        "content": "Invented replacement block",
                                        "minimal_memory": "repair->invented-replacement",
                                        "decision_source": "self",
                                    }
                                ]
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
                            "name": "memory_apply_batch",
                            "args": {"deletes": ["Ab12Z9"]},
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
            "---\nid:Ab12Z9\n2026/4/17-user：\nDetailed workflow, see ref:orphan_note\n",
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
                            "name": "memory_apply_batch",
                            "args": {
                                "adds": [
                                    {
                                        "content": "Prefer concise answers",
                                        "minimal_memory": "answers->concise",
                                        "decision_source": "user",
                                    }
                                ],
                                "deletes": ["Ab12Z9"],
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

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:09+08:00")

        assert report["ok"] is True
        assert "Prefer concise answers" in manager.snapshot_text()
        assert "id:" in manager.snapshot_text()
        assert not orphan_note.exists()
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_preserves_note_ref_present_only_in_minimal_memory(
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
                payload_text="Use checklist replies",
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
                            "name": "memory_apply_batch",
                            "args": {
                                "adds": [
                                    {
                                        "content": "Use checklist replies",
                                        "minimal_memory": "replies->checklist 见noteid:note_policy",
                                        "decision_source": "self",
                                    }
                                ],
                                "note_upserts": {
                                    "note_policy": "Use checklist replies with headings and checklist formatting."
                                },
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

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:09+08:00")
        note_path = tmp_path / "memory" / "notes" / "note_policy.md"

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert note_path.exists()
        assert "Use checklist replies ref:note_policy" in manager.snapshot_text()

        manager.close()
        reopened = module.MemoryManager(tmp_path, _memory_cfg())
        try:
            assert note_path.exists()
            assert "Use checklist replies ref:note_policy" in reopened.snapshot_text()
        finally:
            reopened.close()
        manager = None
    finally:
        if manager is not None:
            manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["add", "rewrite"])
async def test_run_due_batch_once_prefers_minimal_memory_note_ref_over_stale_content_ref(
    tmp_path: Path,
    monkeypatch,
    mode: str,
) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        old_note_path = tmp_path / "memory" / "notes" / "old_note.md"
        old_note_path.parent.mkdir(parents=True, exist_ok=True)
        old_note_path.write_text("Old checklist note\n", encoding="utf-8")
        if mode == "rewrite":
            (tmp_path / "memory" / "MEMORY.md").write_text(
                "---\nid:Ab12Z9\n2026/4/17-self：\nUse checklist replies ref:old_note\n",
                encoding="utf-8",
            )

        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="Use checklist replies",
                created_at="2026-04-17T10:00:05+08:00",
                request_id="write_1",
            )
        )
        args: dict[str, object] = {
            "note_upserts": {
                "new_note": "Use checklist replies with the current checklist note."
            }
        }
        if mode == "add":
            args["adds"] = [
                {
                    "content": "Use checklist replies ref:old_note",
                    "minimal_memory": "replies->checklist 见noteid:new_note",
                    "decision_source": "self",
                }
            ]
        else:
            args["rewrites"] = [
                {
                    "id": "Ab12Z9",
                    "content": "Use checklist replies ref:old_note",
                    "minimal_memory": "replies->checklist 见noteid:new_note",
                }
            ]
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_apply_batch",
                            "args": args,
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

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:09+08:00")
        new_note_path = tmp_path / "memory" / "notes" / "new_note.md"

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert "ref:new_note" in manager.snapshot_text()
        assert "old_note" not in manager.snapshot_text()
        assert new_note_path.exists()
        assert not old_note_path.exists()
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_accepts_inspired_ids_from_current_snapshot(tmp_path: Path, monkeypatch) -> None:
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
                decision_source="self",
                payload_text="Use headings",
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
                            "name": "memory_apply_batch",
                            "args": {
                                "adds": [
                                    {
                                        "content": "Use headings",
                                        "minimal_memory": "answers->headings",
                                        "decision_source": "self",
                                    }
                                ],
                                "inspired_memory_ids": ["Ab12Z9"],
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

        report = await manager.run_due_batch_once(now_iso="2026-04-17T10:00:09+08:00")

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert "Prefer concise answers" in manager.snapshot_text()
        assert "Use headings" in manager.snapshot_text()
    finally:
        manager.close()
