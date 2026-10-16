from __future__ import annotations

import importlib
import importlib.util
import json
import re
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
        memory_id="Ab12Z9Xq",
        date_text="2026/4/17",
        source="self",
        summary="完成任务必须说明任务总耗时",
        note_ref="",
    )

    assert module.format_memory_entry(entry) == "260417:完成任务必须说明任务总耗时\n"

    linked = module.format_memory_entry(
        module.MemoryEntry(
            memory_id="Ab12Z9Xq",
            date_text="2026/4/17",
            source="user",
            summary="创建文件默认格式要求",
            note_ref="n12345",
        )
    )

    assert linked == "260417:创建文件默认格式要求 (note: n12345)\n"


def test_parse_memory_document_round_trips_every_line() -> None:
    module = _load_markdown_memory_module()
    text = (
        "260417:完成任务必须说明任务总耗时\n"
        "260417:创建文件默认格式要求 (note: n12345)\n"
    )

    items = module.parse_memory_document(text)

    assert [item.summary for item in items] == ["完成任务必须说明任务总耗时", "创建文件默认格式要求"]
    assert [item.date_text for item in items] == ["2026/4/17", "2026/4/17"]
    assert items[0].note_ref == ""
    assert items[1].note_ref == "n12345"
    assert module.format_memory_entry(items[1]) in text


def test_parse_memory_document_reads_a_ref_token_as_prose() -> None:
    """`ref:` inside memory text is prose, not a relationship: nothing parses it any more."""
    module = _load_markdown_memory_module()
    text = "260417:创建文件默认格式要求，见 ref:note_a1b2\n"

    items = module.parse_memory_document(text)

    assert items[0].note_ref == ""
    assert items[0].summary == "创建文件默认格式要求，见 ref:note_a1b2"


def test_validate_memory_document_rejects_summary_over_250_chars() -> None:
    module = _load_markdown_memory_module()
    text = f"260417:{'x' * 251}\n"

    with pytest.raises(ValueError, match="summary line exceeds 250 chars"):
        module.validate_memory_document(text, summary_max_chars=250, document_max_chars=20000)


def test_validate_memory_document_rejects_document_over_20000_chars() -> None:
    module = _load_markdown_memory_module()
    oversized = "260417:短句\n" + ("x" * 20001)

    with pytest.raises(ValueError, match="memory document exceeds 20000 chars"):
        module.validate_memory_document(oversized, summary_max_chars=250, document_max_chars=20000)


def test_validate_memory_document_rejects_the_pre_line_block_shape() -> None:
    """An unmigrated store fails here; `negi memory reformat` is the only way past it."""
    module = _load_markdown_memory_module()
    text = (
        "---\n"
        "id:Ab12Z9\n"
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


def _with_confirm_call(tool_calls: list[dict[str, object]] | None) -> list[dict[str, object]]:
    """A mutating batch is scripted together with the confirm call the model makes after reading its preview."""

    calls = list(tool_calls or [])
    mutating = any(
        str(call.get("name") or "") == "memory_apply_batch"
        and isinstance(call.get("args"), dict)
        and any((call.get("args") or {}).get(key) for key in ("adds", "rewrites", "deletes"))
        for call in calls
    )
    confirmed = any(str(call.get("name") or "") == "memory_apply_confirm" for call in calls)
    if mutating and not confirmed:
        calls.append({"id": f"call-confirm-{len(calls)}", "name": "memory_apply_confirm", "args": {}})
    return calls


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
        tool_calls=_with_confirm_call(tool_calls),
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

    async def chat(self, messages, tools=None, **kwargs):
        _ = tools, kwargs
        return await self.ainvoke(messages)

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


def test_memory_repository_generates_eight_char_base62_ids(tmp_path: Path) -> None:
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

    assert len(row["memory_id"]) == 8
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
            memory_body="Use checklist replies",
            minimal_memory="replies->checklist",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:05:00+08:00",
            note_ref="note_policy",
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
        handle = module.note_handle("note_policy")
        # No id reaches the reader: the block is addressed by line number instead.
        assert first["memory_id"] not in snapshot
        assert second["memory_id"] not in snapshot
        assert "260425:Prefer concise answers\n" in snapshot
        assert "answers->concise" not in snapshot
        # The column owns the note; what the reader sees is the derived handle riding on the
        # same line, so the stored sentence stays exactly what was written.
        assert f"260425:replies->checklist (note: {handle})" in snapshot
        assert "ref:note_policy" not in snapshot
        assert "note_policy" not in snapshot
        rebuilt = module.parse_memory_document(snapshot)
        assert [item.summary for item in rebuilt] == ["Prefer concise answers", "replies->checklist"]
        assert rebuilt[0].date_text == "2026/4/25"
        assert rebuilt[1].note_ref == handle
    finally:
        manager.close()


def test_backfill_note_refs_reads_legacy_markers_once_and_is_idempotent(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        row = manager._memory_repo.create_memory(
            memory_body="Use checklist replies ref:note_policy",
            minimal_memory="replies->checklist 见noteid:note_policy",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:05:00+08:00",
        )
        db_path = tmp_path / "memory" / "memory_state.sqlite3"

        assert manager._memory_repo.backfill_note_refs() == 1

        with sqlite3.connect(str(db_path)) as conn:
            stored = conn.execute(
                "SELECT note_ref, memory_body, minimal_memory FROM memories WHERE memory_id = ?",
                (row["memory_id"],),
            ).fetchone()
        assert stored[0] == "note_policy"
        assert "ref:" not in stored[1] and "见noteid:" not in stored[1]
        assert "ref:" not in stored[2] and "见noteid:" not in stored[2]
        assert stored[2] == "replies->checklist"

        assert manager._memory_repo.backfill_note_refs() == 0
    finally:
        manager.close()


def test_memory_snapshot_rebuild_renders_column_ownership(tmp_path: Path) -> None:
    """Ownership comes from the column; a marker inside either text field renders nothing."""
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        linked = manager._memory_repo.create_memory(
            memory_body="Use checklist replies",
            minimal_memory="replies->checklist",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:05:00+08:00",
            note_ref="note_policy",
        )
        manager._memory_repo.create_memory(
            memory_body="Use headings ref:old_note",
            minimal_memory="answers->headings",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:06:00+08:00",
        )
        db_path = tmp_path / "memory" / "memory_state.sqlite3"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "UPDATE memories SET is_compressed = 1 WHERE memory_id = ?",
                (linked["memory_id"],),
            )
            conn.commit()

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")
        snapshot = manager.snapshot_text()
        handle = module.note_handle("note_policy")
        rebuilt = module.parse_memory_document(snapshot)

        assert rebuilt[0].summary == "replies->checklist"
        assert rebuilt[0].note_ref == handle
        assert f"(note: {handle})" in snapshot
        # Nothing is derived from text any more: ` ref:old_note` stays prose.
        assert rebuilt[1].note_ref == ""
        assert "ref:old_note" in rebuilt[1].summary
    finally:
        manager.close()


def test_memory_snapshot_rebuild_uses_active_document_max_chars(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    cfg.document.document_max_chars = 20
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        manager._memory_repo.create_memory(
            memory_body="Prefer concise answers",
            minimal_memory="answers->concise",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )

        with pytest.raises(ValueError, match="memory document exceeds 20 chars"):
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

        session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        manager._commit_validated_write(validated)

        committed_snapshot = manager.snapshot_text()

        assert re.fullmatch(r"\d{6}:Prefer concise answers", committed_snapshot.strip())
        assert "id:" not in committed_snapshot

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        assert manager.snapshot_text() == committed_snapshot
    finally:
        manager.close()


def test_trigger_queues_a_consolidation_batch_at_the_head_instead_of_deleting(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    cfg.document.compress_trigger_chars = 120
    cfg.document.compress_target_chars = 80
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        user_stated = manager._memory_repo.create_memory(
            memory_body="U" * 70,
            minimal_memory="user->stated",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        cold = manager._memory_repo.create_memory(
            memory_body="C" * 70,
            minimal_memory="cold->compressed",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:01+08:00",
        )
        hotter = manager._memory_repo.create_memory(
            memory_body="H" * 70,
            minimal_memory="hotter->compressed",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:02+08:00",
        )

        with manager._memory_repo._connect() as conn:
            conn.execute(
                "UPDATE memories SET passed_count = 100, refresh_count = 0 WHERE memory_id = ?",
                (user_stated["memory_id"],),
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
        manager._write_queue_requests(
            [
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="user",
                    payload_text="a request that arrived first",
                    created_at="2026-04-25T00:00:00+08:00",
                    request_id="write_first",
                )
            ]
        )

        result = manager._compress_memory_snapshot_if_needed(now_iso="2026-04-25T00:00:00+08:00")

        queue_rows = manager._read_queue_requests()
        assert [row.op for row in queue_rows] == ["consolidate", "write"]
        assert result["consolidation_requested"] is True
        assert result["deleted_by_compression_ids"] == []
        # 第 1 级先把冷行降成摘要，降完仍高于 target 才排到第 2 级归并。
        assert result["compressed_memory_ids"]

        # Nothing was lost: the ladder only repaints the block, every body stays in the store.
        rows = {row["memory_id"]: row for row in manager._memory_repo.list_memories()}
        assert {user_stated["memory_id"], cold["memory_id"], hotter["memory_id"]} == set(rows)
        assert {row["memory_body"] for row in rows.values()} == {"U" * 70, "C" * 70, "H" * 70}

        payload = queue_rows[0].payload_text
        # 用户亲口的行现在与普通行同权进候选：退场只是把正文搬进分类 note，
        # 行不删、`memory_body` 原样留着，随时读得回来。
        assert "U" * 70 in payload
        assert "C" * 70 in payload
        # 只有压缩道已经降成摘要的行才会被端上桌：H 靠 refresh_count 排在水池之外，
        # 本轮既没降级也没被请去归并。
        assert "H" * 70 not in payload
        assert payload.index("U" * 70) < payload.index("C" * 70)
        assert set(result["consolidation_memory_ids"]) == set(result["compressed_memory_ids"])

        again = manager._compress_memory_snapshot_if_needed(now_iso="2026-04-25T00:00:01+08:00")
        assert again["consolidation_requested"] is False
        # 一轮只放 70% 的水池：上次没轮到降的那一行这次降了下来，压完已低于 target，
        # 于是既不重排归并，也不再经过排队那道口。
        assert again["compressed_memory_ids"]
        assert len(manager._read_queue_requests()) == 2
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
            adds=[{"content": "Use headings in every answer", "minimal_memory": "answers->headings", "decision_source": "self"}],
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


def test_parse_legacy_change_preview_splits_every_segment_type() -> None:
    module = _load_memory_agent_runtime_module()
    preview = (
        "新增：全新的记忆甲；"
        "修改 Ab12Z9：改写后的记忆乙；"
        "修改：无 id 的改写丙；"
        "删除 Zz9999；"
        "更新 note ref:note_g3ku_persona：note 正文丁；"
        "更新 note ref:note_empty"
    )
    changes = module.MemoryManager._parse_legacy_change_preview(preview)

    assert [c["type"] for c in changes] == ["add", "rewrite", "rewrite", "delete", "note_upsert", "note_upsert"]
    assert changes[0]["content"] == "全新的记忆甲"
    assert changes[1]["memory_id"] == "Ab12Z9"
    assert changes[1]["content"] == "改写后的记忆乙"
    assert changes[1]["original_missing"] is True
    assert changes[2]["memory_id"] == ""
    assert changes[2]["content"] == "无 id 的改写丙"
    assert changes[3]["memory_id"] == "Zz9999"
    assert changes[3]["original_missing"] is True
    assert changes[4]["note_ref"] == "note_g3ku_persona"
    assert changes[4]["content"] == "note 正文丁"
    assert changes[5]["note_ref"] == "note_empty"
    assert changes[5]["content"] == ""


def test_parse_legacy_change_preview_keeps_semicolons_inside_content() -> None:
    module = _load_memory_agent_runtime_module()
    preview = "新增：正文里带有；中文分号；的记忆；修改 Ab12Z9：改写也带；分号"
    changes = module.MemoryManager._parse_legacy_change_preview(preview)

    assert len(changes) == 2
    assert changes[0]["type"] == "add"
    assert changes[0]["content"] == "正文里带有；中文分号；的记忆"
    assert changes[1]["type"] == "rewrite"
    assert changes[1]["content"] == "改写也带；分号"


def test_parse_legacy_change_preview_empty_and_markerless_inputs() -> None:
    module = _load_memory_agent_runtime_module()
    assert module.MemoryManager._parse_legacy_change_preview("") == []
    assert module.MemoryManager._parse_legacy_change_preview("   ") == []
    assert module.MemoryManager._parse_legacy_change_preview("没有任何标记的普通文本") == []


@pytest.mark.asyncio
async def test_list_processed_page_backfills_legacy_rows_and_preserves_new_rows(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    # 固定时钟，避免保留期清理按墙上时间淘汰写死的示例批次
    monkeypatch.setattr(module.MemoryManager, "_now_iso", staticmethod(lambda: "2026-08-30T12:00:00+08:00"))

    try:
        legacy_row = {
            "batch_id": "write_legacy",
            "op": "write",
            "source_op": "write",
            "status": "applied",
            "processed_at": "2026-08-24T00:00:00+08:00",
            "write_mode": "mixed",
            "change_preview": "新增：旧格式新增内容；修改 Ab12Z9：旧格式改写内容",
        }
        modern_row = {
            "batch_id": "write_modern",
            "op": "write",
            "source_op": "write",
            "status": "applied",
            "processed_at": "2026-08-29T00:00:00+08:00",
            "changes": [{"type": "add", "memory_id": "", "content": "结构化新增"}],
        }
        noop_row = {
            "batch_id": "write_noop",
            "op": "write",
            "source_op": "write",
            "status": "applied",
            "processed_at": "2026-08-28T00:00:00+08:00",
            "noop_reason": "本批无稳定新内容",
        }
        manager._write_processed_batches([legacy_row, modern_row, noop_row])

        page = await manager.list_processed_page(limit=20, offset=0)
        items = {item["batch_id"]: item for item in page["items"]}

        legacy = items["write_legacy"]
        assert legacy["changes_reconstructed"] is True
        assert [c["type"] for c in legacy["changes"]] == ["add", "rewrite"]
        assert legacy["changes"][1]["original_missing"] is True
        # The persisted row must not be rewritten by the read path.
        assert "changes" not in legacy_row

        modern = items["write_modern"]
        assert "changes_reconstructed" not in modern
        assert modern["changes"][0]["content"] == "结构化新增"

        noop = items["write_noop"]
        assert "changes" not in noop
        assert "changes_reconstructed" not in noop
    finally:
        manager.close()


def _seed_store(
    manager,
    rows: list[tuple[str, str, str, str] | tuple[str, str, str, str, str]],
    *,
    now_iso: str = "2026-04-17T09:00:00+08:00",
) -> None:
    """Seed committed memory the way production does: sqlite first, mirror repainted.

    Writing only `MEMORY.md` used to be enough because a divergent document was parsed
    back into the store; that direction is gone, so tests must use the authoritative one.
    A fifth element, when present, is the note the row owns.
    """
    adds = [
        {
            "id": row[0],
            "content": row[1],
            "minimal_memory": row[2],
            "decision_source": row[3],
            "note_ref": row[4] if len(row) > 4 else "",
        }
        for row in rows
    ]
    manager._memory_repo.apply_mutation(
        adds=adds,
        rewrites=None,
        deletes=None,
        inspired_memory_ids=None,
        now_iso=now_iso,
    )
    manager._rebuild_memory_snapshot_from_sqlite(now_iso=now_iso)


def test_empty_snapshot_never_clears_sqlite_rows_and_mirror_is_repainted(tmp_path: Path) -> None:
    """An empty mirror is a projection that fell behind, not an instruction to erase.

    The old behaviour treated it as authoritative, so one truncated write (or a full
    disk leaving a zero-byte `MEMORY.md`) deleted the whole library on the next commit.
    """
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

        assert stale["memory_id"] in rows
        assert len(rows) == 2
        assert "Fresh memory" in snapshot
        assert "Stale memory" in snapshot

        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")

        assert "Fresh memory" in manager.snapshot_text()
        assert "Stale memory" in manager.snapshot_text()
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
                {"content": "Use headings in every answer", "minimal_memory": "answers->headings", "decision_source": "self"},
                {"content": "Use headings in every answer", "minimal_memory": "answers->headings", "decision_source": "self"},
            ]
        )

        assert apply_result == {"ok": True, "status": "batch_staged"}

        session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
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
        # The model points at block lines; the test asks the store which line each row is on.
        line_first = manager.memory_line_for_id(first["memory_id"])
        line_second = manager.memory_line_for_id(second["memory_id"])
        assert {line_first, line_second} == {1, 2}
        apply_result = session.apply_batch(
            rewrites=[
                {"line": line_first, "content": "Unified rule", "minimal_memory": "same->value"},
                {"line": line_second, "content": "Unified rule", "minimal_memory": "same->value"},
            ]
        )

        assert apply_result == {"ok": True, "status": "batch_staged"}

        session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
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


def test_memory_reconcile_never_lets_the_mirror_write_into_sqlite(tmp_path: Path) -> None:
    """sqlite is the authoritative store: a divergent `MEMORY.md` is archived and
    repainted, never parsed back into the rows. The old behaviour folded
    `memory_body` into whatever the document rendered, which destroyed the full
    text of compressed rows."""
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
            "260425:Updated snapshot text\n",
            encoding="utf-8",
        )

        action = manager._reconcile_mirror_with_sqlite_locked(now_iso="2026-04-25T01:00:00+08:00")

        assert action == "drift_archived"
        rows = manager._memory_repo.list_memories()
        assert len(rows) == 1
        assert rows[0]["memory_id"] == row["memory_id"]
        assert rows[0]["memory_body"] == "Prefer concise answers"
        assert rows[0]["minimal_memory"] == "answers->concise"
        assert rows[0]["refresh_count"] == 7
        assert rows[0]["passed_count"] == 9
        assert rows[0]["is_compressed"] is True
        assert rows[0]["from_user"] is True
        assert "Updated snapshot text" not in manager.snapshot_text()
        assert "answers->concise" in manager.snapshot_text()
        archived = list((tmp_path / "memory").glob("MEMORY.md.bak.*"))
        assert archived and "Updated snapshot text" in archived[0].read_text(encoding="utf-8")
    finally:
        manager.close()


def test_memory_adopt_document_preserves_metadata_and_existing_body(tmp_path: Path) -> None:
    """The one surviving document -> sqlite direction (legacy import) must still keep
    counters for the row it matches by text, and must not fold an existing full body away."""
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())

    try:
        row = manager._memory_repo.create_memory(
            memory_body="Authoritative long body",
            minimal_memory="auth->min",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        manager._memory_repo.mark_compressed(str(row["memory_id"]))
        manager._memory_repo.restore_rows(rows_before=manager._memory_repo.list_memories())

        manager._memory_repo.replace_all_from_document(
            document_text="260425:auth->min\n",
            now_iso="2026-04-25T01:00:00+08:00",
            source="user",
        )

        rows = manager._memory_repo.list_memories()
        assert len(rows) == 1
        assert rows[0]["memory_body"] == "Authoritative long body"
        assert rows[0]["is_compressed"] is True
        assert rows[0]["from_user"] is True
    finally:
        manager.close()


def test_reformat_store_folds_multiline_rows_and_the_second_pass_changes_nothing(tmp_path: Path) -> None:
    """上线要跑的就是这一趟，所以它必须能重跑：第二次一行都不动、投影字节不变。"""
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())

    try:
        multiline = manager._memory_repo.create_memory(
            memory_body="创建文件默认格式要求：\n时间在前\n内容在后",
            minimal_memory="文件->默认格式",
            source="user",
            from_user=True,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        plain = manager._memory_repo.create_memory(
            memory_body="回信先给结论",
            minimal_memory="回信->结论",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:01+08:00",
        )
        manager._memory_repo.mark_compressed(str(multiline["memory_id"]))

        first = manager.reformat_store(now_iso="2026-04-25T00:00:02+08:00")

        assert first["flattened_rows"] == [multiline["memory_id"]]
        assert first["resident_rows"] == 2
        rows = {str(row["memory_id"]): row for row in manager._memory_repo.list_memories()}
        assert rows[multiline["memory_id"]]["memory_body"] == "创建文件默认格式要求： 时间在前 内容在后"
        assert rows[multiline["memory_id"]]["is_compressed"] is True, "格式更新不动压缩位"
        assert rows[plain["memory_id"]]["updated_at"].startswith("2026-04-25"), "没跨行的行完全不被动"

        snapshot = manager.snapshot_text()
        markdown_module = _load_markdown_memory_module()
        assert len(snapshot.strip().splitlines()) == 2, "一条记忆一行，正文里的换行不再产出续行"
        assert f"260425:文件->默认格式" in snapshot, "已降级的行仍按最小摘要渲染"
        assert "260425:回信先给结论" in snapshot
        markdown_module.validate_memory_document(
            snapshot,
            summary_max_chars=int(manager.config.document.summary_max_chars),
            document_max_chars=int(manager.config.document.document_max_chars),
        )

        second = manager.reformat_store(now_iso="2026-04-25T00:00:03+08:00")

        assert second["flattened_rows"] == []
        assert second["chars_before"] == second["chars_after"]
        assert manager.snapshot_text() == snapshot
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

    assert tool_names == ["memory_read_note", "memory_apply_batch", "memory_apply_confirm"]


def test_memory_agent_delete_batch_rejects_noop_reason_and_points_at_already_satisfied(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        session = module._MemoryToolSession(
            snapshot_text="260425:人设已替换为新人设\n",
            notes_dir=tmp_path / "memory" / "notes",
            batch_op="delete",
        )
        apply_tool = next(tool for tool in manager._memory_agent_tools(session) if tool.name == "memory_apply_batch")
        assert "already_satisfied" in apply_tool.args

        rejected = apply_tool.invoke({"noop_reason": "无需变更"})
        assert rejected["ok"] is False
        assert "already_satisfied" in rejected["errors"]["noop_reason"]
        assert session.apply_batch_count == 0

        staged = apply_tool.invoke({"already_satisfied": "第 1 行已是新人设，旧人设不在正文里"})
        assert staged["ok"] is True
        assert session.applied_batch["already_satisfied"].startswith("第 1 行")
    finally:
        manager.close()


def test_memory_agent_write_batch_rejects_already_satisfied(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        session = module._MemoryToolSession(
            snapshot_text="260425:Prefer concise answers\n",
            notes_dir=tmp_path / "memory" / "notes",
            batch_op="write",
        )
        apply_tool = next(tool for tool in manager._memory_agent_tools(session) if tool.name == "memory_apply_batch")

        rejected = apply_tool.invoke({"already_satisfied": "目标已不存在"})
        assert rejected["ok"] is False
        assert rejected["errors"]["already_satisfied"] == "already_satisfied is only valid for delete batches"

        combined = apply_tool.invoke({"deletes": [1], "already_satisfied": "目标已不存在"})
        assert combined["ok"] is False
        assert "already_satisfied" in combined["errors"]
    finally:
        manager.close()


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

        assert "inspired_lines" in apply_tool.args
        result = apply_tool.invoke(
            {
                "adds": [{"content": "Use headings in every answer", "minimal_memory": "answers->headings", "decision_source": "self"}],
                "inspired_lines": [manager.memory_line_for_id(existing["memory_id"])],
            }
        )

        assert result["ok"] is True
        session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        assert validated.inspired_memory_ids == [existing["memory_id"]]
    finally:
        manager.close()


def test_memory_agent_apply_batch_rejects_inspired_lines_that_are_not_resident(tmp_path: Path) -> None:
    """Lines are read off sqlite's resident rows, never off the mirror text: a line the
    store does not have is refused instead of quietly counting as a referenced memory."""
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        manager._memory_repo.create_memory(
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

        result = apply_tool.invoke(
            {
                "adds": [{"content": "Use headings in every answer", "minimal_memory": "answers->headings", "decision_source": "self"}],
                "inspired_lines": [2],
            }
        )

        assert result["ok"] is True
        session.confirm_batch()
        with pytest.raises(module._MemoryAgentValidationError, match=r"inspired_lines\[0\]: line 2 is outside"):
            manager._build_validated_write_from_apply_batch(
                before_text=manager.snapshot_text(),
                session=session,
                batch=batch,
            )
    finally:
        manager.close()


def test_memory_agent_apply_batch_rejects_non_list_inspired_lines_at_staging(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    session = module._MemoryToolSession(snapshot_text="", notes_dir=tmp_path / "memory" / "notes")

    result = session.apply_batch(
        adds=[{"content": "Use headings in every answer", "minimal_memory": "answers->headings", "decision_source": "self"}],
        inspired_lines="3",
    )

    assert result["ok"] is False
    assert result["errors"]["inspired_lines"] == "inspired_lines must be a list"


def test_memory_agent_apply_batch_requires_minimal_memory_and_tracks_inspired_lines(
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
            adds=[{"content": "Use headings in every answer", "minimal_memory": "answers->headings", "decision_source": "self"}],
            inspired_lines=[manager.memory_line_for_id(str(existing["memory_id"]))],
        )

        assert result["ok"] is True
        session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        assert validated.adds[0]["minimal_memory"] == "answers->headings"
        assert validated.inspired_memory_ids == [existing["memory_id"]]
    finally:
        manager.close()


def test_memory_agent_apply_batch_rejects_non_list_inspired_lines_at_validation(tmp_path: Path) -> None:
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
            "adds": [{"content": "Use headings in every answer", "minimal_memory": "answers->headings", "decision_source": "self"}],
            "rewrites": [],
            "deletes": [],
            "note_upserts": {},
            "inspired_lines": "3",
            "noop_reason": "",
        }

        with pytest.raises(ValueError, match="inspired_lines must be a list"):
            session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
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
            session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
            manager._build_validated_write_from_apply_batch(
                before_text=manager.snapshot_text(),
                session=session,
                batch=batch,
            )
    finally:
        manager.close()


def test_memory_agent_apply_batch_rejects_over_limit_content_with_summary_note_hint(tmp_path: Path) -> None:
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
                    "content": "x" * 251,
                    "minimal_memory": "when->use headings",
                    "decision_source": "self",
                }
            ],
        )

        assert result["ok"] is True
        with pytest.raises(ValueError, match="250") as exc_info:
            session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
            manager._build_validated_write_from_apply_batch(
                before_text=manager.snapshot_text(),
                session=session,
                batch=batch,
            )
        assert "摘要" in str(exc_info.value)
        assert "note" in str(exc_info.value)
    finally:
        manager.close()


@pytest.mark.parametrize("mode", ["add", "rewrite"])
def test_memory_agent_apply_batch_links_note_through_explicit_note_ref(
    tmp_path: Path,
    mode: str,
) -> None:
    module = _load_memory_agent_runtime_module()
    cfg = _memory_cfg()
    manager = module.MemoryManager(tmp_path, cfg)

    try:
        rewrite_line = 0
        if mode == "rewrite":
            existing = manager._memory_repo.create_memory(
                memory_body="Use checklist replies",
                minimal_memory="replies->old",
                source="self",
                from_user=False,
                now_iso="2026-04-25T00:00:00+08:00",
            )
            manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")
            rewrite_line = manager.memory_line_for_id(str(existing["memory_id"]))

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
            "note_upserts": {"note_new": "Use checklist replies with the current checklist note."},
        }
        item: dict[str, object] = {
            "content": "Use checklist replies",
            "minimal_memory": "replies->checklist",
            "note_ref": "note_new",
        }
        if mode == "add":
            item["decision_source"] = "self"
            apply_kwargs["adds"] = [dict(item)]
        else:
            item["line"] = rewrite_line
            apply_kwargs["rewrites"] = [dict(item)]

        result = session.apply_batch(**apply_kwargs)

        assert result["ok"] is True
        session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )
        handle = module.note_handle("note_new")
        assert f"(note: {handle})" in validated.document_text
        # The real note name never reaches the resident block: readers get the handle.
        assert "note_new" not in validated.document_text
        parsed_entries = module.parse_memory_document(validated.document_text)
        assert len(parsed_entries) == 1
        assert parsed_entries[0].note_ref == handle
        persisted = validated.adds[0] if mode == "add" else validated.rewrites[0]
        assert persisted["note_ref"] == "note_new"
    finally:
        manager.close()


@pytest.mark.parametrize("marker_field", ["content", "minimal_memory"])
@pytest.mark.parametrize("note_ref_token", ["ref:note_policy", "见noteid:note_policy"])
def test_memory_agent_apply_batch_rejects_note_ref_written_into_memory_text(
    tmp_path: Path,
    note_ref_token: str,
    marker_field: str,
) -> None:
    """The relationship is a field: a marker left in the prose is rejected, not parsed."""
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
        item = {
            "content": "Use headings in every answer",
            "minimal_memory": "answers->headings",
            "decision_source": "self",
        }
        item[marker_field] = f"{item[marker_field]} {note_ref_token}"
        result = session.apply_batch(adds=[item])

        assert result["ok"] is True
        with pytest.raises(ValueError, match="note ownership goes in the item"):
            session.confirm_batch()  # the model's second call: a mutating batch lands only after it reads the preview
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
async def test_self_pruning_enqueue_session_boundary_flushes_pending_turns(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager.record_turn_for_review(
            session_key="session-1",
            turn_id="turn-1",
            user_messages=["记住这个偏好"],
            assistant_text="好的",
            compression_summary={},
            canonical_summary={"stages": []},
        )
        result = await manager.enqueue_session_boundary_flush(
            session_key="session-1",
            channel="web",
            chat_id="chat-1",
            trigger_source="session_boundary_flush",
        )

        assert result["status"] == "queued"
        assert result["request_id"]
        queue_items = await manager.list_queue(limit=10)
        assert len(queue_items) == 1
        assert queue_items[0]["op"] == "write"
        assert queue_items[0]["trigger_source"] == "session_boundary"
        # Boundary flush clears the session's review window afterwards.
        state = manager._read_review_state()
        assert "session-1" not in (state.get("sessions") or {})
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

        # Only token_compression and session_boundary may force-flush the window;
        # stage_compaction (every-turn stage compaction) must keep the buffer intact.
        for trigger_source in ("pre_compression_flush", "stage_compaction"):
            result = await manager.flush_review_window(
                session_key="web:shared",
                trigger_source=trigger_source,
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
async def test_v2_review_turn_payload_renders_visible_tool_records_with_limits(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        huge_output_text = "HUGE_TOOL_OUTPUT_HEAD_MARKER " + "x" * 500 + " HUGE_TOOL_OUTPUT_TAIL_MARKER"
        long_preview_text = "PREVIEW_HEAD_MARKER " + "y" * 300 + " PREVIEW_TAIL_MARKER"
        round_narration = "ROUND_NARRATION_START " + "z" * 600 + " ROUND_NARRATION_END"
        canonical_summary = {
            "active_stage_id": "frontdoor-stage-2",
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_goal": "collect candidate pool",
                    "completed_stage_summary": "verified current ranking methodology",
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-1:round-1",
                            "text": round_narration,
                            "tools": [
                                {
                                    "tool_call_id": "call-visible-1",
                                    "tool_name": "web_fetch",
                                    "status": "error",
                                    "arguments_text": "url=https://example.com",
                                    "output_preview_text": long_preview_text,
                                    "output_text": huge_output_text,
                                }
                            ],
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
        # Visible tool record fields are present.
        assert "stages:" in payload_text
        assert '"tool_name": "web_fetch"' in payload_text
        assert '"status": "error"' in payload_text
        assert "url=https://example.com" in payload_text
        # Full output is truncated: head appears, tail and full text do not.
        assert "HUGE_TOOL_OUTPUT_HEAD_MARKER" in payload_text
        assert "HUGE_TOOL_OUTPUT_TAIL_MARKER" not in payload_text
        assert huge_output_text not in payload_text
        # Preview is truncated the same way.
        assert "PREVIEW_HEAD_MARKER" in payload_text
        assert "PREVIEW_TAIL_MARKER" not in payload_text
        # Round narration is user-visible and must not be truncated.
        assert round_narration in payload_text
        # Non-visible fields stay excluded.
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


def _canonical_with_visible_tool(
    *,
    stage_id: str = "frontdoor-stage-1",
    tool_call_id: str = "",
    tool_name: str = "web_search",
    status: str = "success",
    arguments_text: str = "q=hello",
    output_text: str = "ok",
    round_text: str = "narration",
) -> dict[str, Any]:
    return {
        "active_stage_id": stage_id,
        "stages": [
            {
                "stage_id": stage_id,
                "stage_goal": f"goal {stage_id}",
                "completed_stage_summary": f"summary {stage_id}",
                "rounds": [
                    {
                        "round_id": f"{stage_id}:round-1",
                        "text": round_text,
                        "tools": [
                            {
                                "tool_call_id": tool_call_id,
                                "tool_name": tool_name,
                                "status": status,
                                "arguments_text": arguments_text,
                                "output_text": output_text,
                            }
                        ],
                    }
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_v2_review_window_reports_new_tool_records_once_per_window(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        canonical = _canonical_with_visible_tool(tool_call_id="call-1")
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["first"],
            assistant_text="first reply",
            compression_summary={},
            canonical_summary=canonical,
        )
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-2",
            user_messages=["second"],
            assistant_text="second reply",
            compression_summary={},
            canonical_summary=canonical,
        )
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-3",
            user_messages=["third"],
            assistant_text="third reply",
            compression_summary={},
            canonical_summary=_canonical_with_visible_tool(tool_call_id="call-2"),
        )

        state = manager._read_review_state()
        pending = state["sessions"]["web:shared"]["pending_turns"]
        assert len(pending) == 3
        assert '"tool_name": "web_search"' in pending[0]["payload_text"]
        assert '"tool_name": "web_search"' not in pending[1]["payload_text"]
        assert '"tool_name": "web_search"' in pending[2]["payload_text"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_review_tool_record_cursor_survives_flush(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["first"],
            assistant_text="first reply",
            compression_summary={},
            canonical_summary=_canonical_with_visible_tool(tool_call_id="call-A"),
        )
        flush_result = await manager.flush_review_window(
            session_key="web:shared",
            trigger_source="token_compression",
        )
        assert flush_result["status"] == "queued"
        state_after_flush = manager._read_review_state()
        assert "call-A" in state_after_flush["sessions"]["web:shared"]["reported_tool_keys"]

        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-2",
            user_messages=["second"],
            assistant_text="second reply",
            compression_summary={},
            canonical_summary=_canonical_with_visible_tool(tool_call_id="call-A"),
        )
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-3",
            user_messages=["third"],
            assistant_text="third reply",
            compression_summary={},
            canonical_summary=_canonical_with_visible_tool(tool_call_id="call-B"),
        )

        state = manager._read_review_state()
        pending = state["sessions"]["web:shared"]["pending_turns"]
        assert len(pending) == 2
        assert '"tool_name": "web_search"' not in pending[0]["payload_text"]
        assert '"tool_name": "web_search"' in pending[1]["payload_text"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_review_tool_records_full_capture_has_no_per_turn_cap(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        tools = []
        for index in range(10):
            tools.append(
                {
                    "tool_call_id": f"call-{index}",
                    "tool_name": "web_search",
                    "status": "error" if index in {3, 7} else "success",
                    "arguments_text": f"q=item-{index}",
                    "output_text": f"out-{index}",
                }
            )
        canonical_summary = {
            "active_stage_id": "frontdoor-stage-1",
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_goal": "goal",
                    "completed_stage_summary": "summary",
                    "rounds": [{"round_id": "frontdoor-stage-1:round-1", "text": "", "tools": tools}],
                }
            ],
        }
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["first"],
            assistant_text="first reply",
            compression_summary={},
            canonical_summary=canonical_summary,
        )

        state = manager._read_review_state()
        payload_text = state["sessions"]["web:shared"]["pending_turns"][0]["payload_text"]
        # Full capture: every new record of the turn lands in the buffered payload,
        # regardless of status or recency — no per-turn count cap.
        assert payload_text.count('"tool_name": "web_search"') == 10
        assert all(f"out-{index}" in payload_text for index in range(10))
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_review_stage_with_new_records_carries_full_goal_summary(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        def _canonical_with_tools(*tool_call_ids: str) -> dict[str, Any]:
            return {
                "active_stage_id": "frontdoor-stage-1",
                "stages": [
                    {
                        "stage_id": "frontdoor-stage-1",
                        "stage_goal": "durable stage goal",
                        "completed_stage_summary": "durable stage summary",
                        "rounds": [
                            {
                                "round_id": "frontdoor-stage-1:round-1",
                                "text": "",
                                "tools": [
                                    {
                                        "tool_call_id": tool_call_id,
                                        "tool_name": "web_search",
                                        "status": "success",
                                        "arguments_text": f"q={tool_call_id}",
                                        "output_text": f"out-{tool_call_id}",
                                    }
                                    for tool_call_id in tool_call_ids
                                ],
                            }
                        ],
                    }
                ],
            }

        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["first"],
            assistant_text="first reply",
            compression_summary={},
            canonical_summary=_canonical_with_tools("call-1"),
        )
        # Second turn adds one new tool record to the same stage; goal/summary unchanged.
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-2",
            user_messages=["second"],
            assistant_text="second reply",
            compression_summary={},
            canonical_summary=_canonical_with_tools("call-1", "call-2"),
        )

        state = manager._read_review_state()
        pending = state["sessions"]["web:shared"]["pending_turns"]
        second_payload = pending[1]["payload_text"]
        # The stage entry stays self-describing and uncompressed: full goal/summary
        # accompany the new record even without a stage delta this turn.
        assert "durable stage goal" in second_payload
        assert "durable stage summary" in second_payload
        assert "q=call-2" in second_payload
        # The already-reported record is not captured again.
        assert "q=call-1" not in second_payload
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_v2_review_tool_records_empty_id_fingerprint_dedup(tmp_path: Path) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        canonical = _canonical_with_visible_tool(tool_call_id="", output_text="same-output")
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-1",
            user_messages=["first"],
            assistant_text="first reply",
            compression_summary={},
            canonical_summary=canonical,
        )
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-2",
            user_messages=["second"],
            assistant_text="second reply",
            compression_summary={},
            canonical_summary=canonical,
        )
        await manager.record_turn_for_review(
            session_key="web:shared",
            turn_id="turn-3",
            user_messages=["third"],
            assistant_text="third reply",
            compression_summary={},
            canonical_summary=_canonical_with_visible_tool(tool_call_id="", output_text="changed-output"),
        )

        state = manager._read_review_state()
        pending = state["sessions"]["web:shared"]["pending_turns"]
        assert len(pending) == 3
        assert '"tool_name": "web_search"' in pending[0]["payload_text"]
        assert '"tool_name": "web_search"' not in pending[1]["payload_text"]
        assert '"tool_name": "web_search"' in pending[2]["payload_text"]
        assert all(key.startswith("fingerprint:") for key in state["sessions"]["web:shared"]["reported_tool_keys"])
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
                                        "content": "创建文件默认格式要求",
                                        "minimal_memory": "文件->默认格式",
                                        "decision_source": "user",
                                        "note_ref": "note_policy",
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
        # 一行一条：日期由后端拼，note 句柄挂在同一行末尾，块里没有 id 也没有来源标记。
        assert manager.snapshot_text().strip() == (
            f"260417:创建文件默认格式要求 (note: {module.note_handle('note_policy')})"
        )
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
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/17", raising=False)
        _seed_store(manager, [("Ab12Z9", "创建 Markdown 文档时，默认使用 [G3KU]-时间-前缀命名。", "命名->时间前缀", "user")])
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
                                        "line": 1,
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
        assert manager.snapshot_text().startswith("260417:创建 Markdown 文档时，要使用时间-内容格式命名")
        assert "id:" not in manager.snapshot_text()
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
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        _seed_store(
            manager,
            [("Ab12Z9", "Use [G3KU]-time-prefix for Markdown files.", "命名->时间前缀", "user")],
            now_iso="2026-04-16T09:00:00+08:00",
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
                                        "line": 1,
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
async def test_run_due_batch_once_parks_batch_on_provider_error_without_processed_record(
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
        failed = _read_jsonl(tmp_path / "memory" / "failed.jsonl")

        # provider 失败不再原地卡队头无限重试，而是停车到失败区等待成功信号或人工处理
        assert report["ok"] is True
        assert report["status"] == "parked"
        assert report["category"] == "provider_error"
        assert manager.snapshot_text() == ""
        assert queue_items == []
        assert len(failed) == 1
        assert "provider exploded" in str(failed[0]["last_error_text"])
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
        _seed_store(manager, [("Ab12Z9", "完成任务必须说明任务总耗时", "报告->总耗时", "self")])
        fake_model = _FakeToolCallingModel(
            [
                _fake_response(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "memory_apply_batch",
                            "args": {"deletes": [1]},
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
                payload_text="完成任务必须说明任务总耗时 这条已经不需要了",
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
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/17", raising=False)
        _seed_store(
            manager,
            [
                ("Ab12Z9", "Report total elapsed time", "报告->总耗时", "self"),
                ("Cd34Ef", "Prefer Chinese replies", "回复->中文", "user"),
            ],
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="Report total elapsed time 这条已经不需要了",
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
                            "args": {"deletes": [1]},
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
        _seed_store(manager, [("Ab12Z9", "Report total elapsed time", "报告->总耗时", "self")])
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="delete",
                decision_source="self",
                payload_text="Report total elapsed time 这条已经不需要了",
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
                                        "minimal_memory": "repair->invented",
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
                            "args": {"deletes": [1]},
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
        # 归属住在 note_ref 列里：行删掉，列就没人再引用这份 note，收尾的孤儿清扫才动手。
        _seed_store(manager, [("Ab12Z9", "Detailed workflow", "workflow->detail", "user", "orphan_note")])
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
                                "deletes": [1],
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
        assert "Detailed workflow" not in manager.snapshot_text()
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
                                        "minimal_memory": "replies->checklist",
                                        "decision_source": "self",
                                        "note_ref": "note_policy",
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
        assert f"(note: {module.note_handle('note_policy')})" in manager.snapshot_text()

        manager.close()
        reopened = module.MemoryManager(tmp_path, _memory_cfg())
        try:
            assert note_path.exists()
            assert f"(note: {module.note_handle('note_policy')})" in reopened.snapshot_text()
        finally:
            reopened.close()
        manager = None
    finally:
        if manager is not None:
            manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["add", "rewrite"])
async def test_run_due_batch_once_moves_note_link_and_reaps_the_dropped_note(
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
            monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/17", raising=False)
            _seed_store(manager, [("Ab12Z9", "Use checklist replies", "回复->清单", "self", "old_note")])

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
                "note_new": "Use checklist replies with the current checklist note."
            }
        }
        if mode == "add":
            args["adds"] = [
                {
                    "content": "Use checklist replies",
                    "minimal_memory": "replies->checklist",
                    "decision_source": "self",
                    "note_ref": "note_new",
                }
            ]
        else:
            args["rewrites"] = [
                {
                    "line": 1,
                    "content": "Use checklist replies",
                    "minimal_memory": "replies->checklist",
                    "note_ref": "note_new",
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
        note_new_path = tmp_path / "memory" / "notes" / "note_new.md"

        assert report["ok"] is True
        assert report["status"] == "applied"
        assert f"(note: {module.note_handle('note_new')})" in manager.snapshot_text()
        assert module.note_handle("old_note") not in manager.snapshot_text()
        assert note_new_path.exists()
        assert not old_note_path.exists()
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_once_accepts_inspired_lines_from_the_resident_block(tmp_path: Path, monkeypatch) -> None:
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        monkeypatch.setattr(module.MemoryManager, "_memory_date_text", lambda self, now_iso=None: "2026/4/17", raising=False)
        _seed_store(manager, [("Ab12Z9", "Prefer concise answers", "answers->concise", "user")])
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
                                        "content": "Use headings in every answer",
                                        "minimal_memory": "answers->headings",
                                        "decision_source": "self",
                                    }
                                ],
                                "inspired_lines": [1],
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
@pytest.mark.asyncio
async def test_list_processed_page_projects_payload_texts_with_aligned_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已处理详情只投首屏：counts 与 payload_texts 等长同下标，存储仍是原文。"""
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    monkeypatch.setattr(module.MemoryManager, "_now_iso", staticmethod(lambda: "2026-08-30T12:00:00+08:00"))
    try:
        first = "甲" * 30_000
        second = "短正文"
        manager._write_processed_batches([
            {
                "batch_id": "write_paged_batch",
                "op": "write",
                "source_op": "write",
                "status": "applied",
                "processed_at": "2026-08-29T00:00:00+08:00",
                "request_ids": ["write_a", "write_b"],
                "payload_texts": [first, second],
            }
        ])

        row = (await manager.list_processed_page(limit=20))["items"][0]
        assert [len(text) for text in row["payload_texts"]] == [4000, 3]
        assert row["payload_char_counts"] == [30_000, 3]
        stored = manager._read_processed_batches()[0]["payload_texts"]
        assert [len(text) for text in stored] == [30_000, 3], "投影只发生在读侧拷贝"

        window = await manager.get_payload_window(
            "processed", "write_paged_batch", request_id="write_a", start_char=4001
        )
        assert window["start_char"] == 4001
        assert window["end_char"] == 24000
        assert window["text"] == first[4000:24000]
        assert window["char_count"] == 30_000
        assert window["truncated"] is True

        tail = await manager.get_payload_window(
            "processed", "write_paged_batch", request_id="write_a", start_char=24001
        )
        assert tail["end_char"] == 30_000
        assert tail["truncated"] is False

        with pytest.raises(KeyError):
            await manager.get_payload_window("processed", "write_paged_batch", request_id="write_absent")
        with pytest.raises(KeyError):
            await manager.get_payload_window("nonsense", "write_paged_batch")
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_queue_row_projects_payload_and_pages_from_the_live_row(tmp_path: Path) -> None:
    """队列详情同样只投首屏，取全文读的是队列里的原文。"""
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        payload = "y" * 25_000
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text=payload,
                created_at="2026-08-29T10:00:00+08:00",
                request_id="write_queue_paged",
            )
        )

        row = (await manager.list_queue_page(limit=20))["items"][0]
        assert len(row["payload_text"]) == 4000
        assert row["payload_char_count"] == 25_000

        window = await manager.get_payload_window("queue", "write_queue_paged", start_char=4001)
        assert window["request_id"] == "write_queue_paged"
        assert window["text"] == payload[4000:24000]

        tail = await manager.get_payload_window("queue", "write_queue_paged", start_char=24001)
        assert tail["end_char"] == 25_000
        assert tail["truncated"] is False

        with pytest.raises(KeyError):
            await manager.get_payload_window("queue", "write_absent")
    finally:
        manager.close()
def test_window_payload_split_packs_turns_and_preserves_stage_order() -> None:
    """拆片只切"带多少个阶段"，不切内容：回合头尾每片都带，顺序与总数守恒。"""
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager  # 拆片是无状态类方法，不需要实例
    big = json.dumps(
        {"stages": [{"stage_id": f"s{i}", "completed_stage_summary": "x" * 4000} for i in range(8)]},
        ensure_ascii=False,
        indent=2,
    )
    turn_one = f"[turn_id] t1\nuser_messages:\n- 第一条\nstages:\n{big}\nassistant_text:\n回答一"
    turn_two = "[turn_id] t2\nuser_messages:\n- 第二条\nstages:\n{\"stages\": []}\nassistant_text:\n回答二"
    payload = "[session_key] web:ceo-1\n\n## 窗口回合 1\n" + turn_one + "\n\n## 窗口回合 2\n" + turn_two

    pieces = manager._split_window_payload(payload, max_tokens=2000)

    assert len(pieces) > 1, "超容量的窗口必须被拆开"
    for piece in pieces:
        assert piece.startswith("[session_key] web:ceo-1"), "每片都要自描述会话归属"
        assert module.estimate_tokens(piece) <= 2000, f"单片仍超目标: {module.estimate_tokens(piece)}"
    ids = [m for piece in pieces for m in re.findall(r'"stage_id": "s(\d+)"', piece)]
    assert ids == [str(i) for i in range(8)], f"阶段必须按原顺序全数保留: {ids}"
    assert "第二条" in pieces[-1], "第二个回合的正文必须留在某一片里（小回合跟着尾片）"

    # 不超容量 / 拿不到目标 / 阶段 JSON 解析不了：三种情况都必须原样一条，交回既有闸门处置。
    assert manager._split_window_payload(payload, max_tokens=999_999) == [payload]
    assert manager._split_window_payload(payload, max_tokens=0) == [payload]
    broken = "[session_key] s\n\n## 窗口回合 1\n[turn_id] t\nstages:\n{坏掉的 json"
    assert manager._split_window_payload(broken, max_tokens=10) == [broken]


@pytest.mark.asyncio
async def test_flush_review_window_splits_oversized_window_into_queue_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """入队侧拆片后的每一片都能过发送闸门——整批停在停车区那种结局不再发生。"""
    module = _load_memory_agent_runtime_module()
    manager = module.MemoryManager(tmp_path, _memory_cfg())
    try:
        monkeypatch.setattr(manager, "_memory_window_split_target_tokens", lambda: 2000)
        monkeypatch.setattr(
            module,
            "resolve_send_model_context_window_info",
            lambda **_: SimpleNamespace(context_window_tokens=4000),
        )
        canonical = {
            "stages": [
                {
                    "stage_id": f"s{i}",
                    "stage_goal": f"目标{i}",
                    "completed_stage_summary": "y" * 4000,
                }
                for i in range(8)
            ]
        }
        await manager.record_turn_for_review(
            session_key="web:split",
            turn_id="turn-1",
            user_messages=["把这条长任务的阶段都记下来"],
            assistant_text="已完成",
            compression_summary={"status": ""},
            canonical_summary=canonical,
        )

        result = await manager.flush_review_window(session_key="web:split", trigger_source="token_compression")
        queue_items = await manager.list_queue(limit=20)

        assert result["status"] == "queued"
        assert result["split_parts"] == len(queue_items) > 1
        assert result["request_id"] == result["request_ids"][0]
        assert len({row["request_id"] for row in queue_items}) == len(queue_items), "每片必须是独立幂等键"
        collected: list[str] = []
        for row in queue_items:
            body = str(row["payload_text"] or "")
            assert module.estimate_tokens(body) <= 2000
            collected.extend(re.findall(r'"stage_id": "s(\d+)"', body))
            # 拆完之后每片都要过闸门（窗口 4000 → 容量 3000）
            manager._memory_window_guard(
                runtime_config=SimpleNamespace(),
                model_chain=["memory-primary"],
                request_messages=[{"role": "user", "content": body}],
                tool_schemas=[],
                usage_total={"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
                request_artifacts=[],
            )
        assert sorted(collected) == [str(i) for i in range(8)], f"阶段不许丢: {sorted(collected)}"
    finally:
        manager.close()
