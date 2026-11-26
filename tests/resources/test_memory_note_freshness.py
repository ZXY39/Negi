from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _manager(tmp_path: Path):
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.document.compress_trigger_chars = 1_000_000
    cfg.document.compress_target_chars = 900_000
    return _module().MemoryManager(tmp_path, cfg)


def _add(manager, body: str, *, note_ref: str = "", now_iso: str = "2026-11-20T10:00:00+08:00") -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory=body,
        source="user",
        from_user=True,
        note_ref=note_ref,
        now_iso=now_iso,
    )
    return str(row["memory_id"])


def _row(manager, memory_id: str) -> dict:
    return {str(row["memory_id"]): row for row in manager._memory_repo.list_memories()}[memory_id]


def _batch(op: str = "write") -> object:
    module = _module()
    return module.MemoryBatch(
        op=op,
        items=[
            module.MemoryQueueRequest(
                op=op,
                decision_source="user",
                payload_text="update note",
                created_at="2026-11-26T09:00:00+08:00",
                request_id="note_freshness",
            )
        ],
    )


def _commit(manager, **payload):
    module = _module()
    session = module._MemoryToolSession(
        snapshot_text=manager.snapshot_text(),
        notes_dir=manager.notes_dir,
        index_dir=manager.index_dir,
        batch_op=str(payload.pop("batch_op", "write")),
        owned_note_refs_fn=lambda: set(manager._memory_repo.owned_note_refs()),
    )
    staged = session.apply_batch(**payload)
    assert staged.get("ok") is True, staged
    assert session.confirm_batch()["status"] == "batch_confirmed"
    validated = manager._build_validated_write_from_apply_batch(
        before_text=manager.snapshot_text(), session=session, batch=_batch(session.batch_op)
    )
    manager._commit_validated_write(validated)
    return validated


def _meta(manager, ref: str, kind: str = "ordinary") -> dict:
    return manager._memory_repo.read_note_meta(ref, kind) or {}


def test_existing_notes_are_registered_with_unknown_freshness(tmp_path: Path) -> None:
    notes = tmp_path / "memory" / "notes"
    index = tmp_path / "memory" / "index"
    notes.mkdir(parents=True)
    index.mkdir(parents=True)
    (notes / "note_old.md").write_text("old ordinary\n", encoding="utf-8")
    (index / "note_category.md").write_text("old category\n", encoding="utf-8")
    manager = _manager(tmp_path)
    try:
        ordinary = _meta(manager, "note_old")
        category = _meta(manager, "note_category", "category")
        assert ordinary["note_key"] == "ordinary:note_old"
        assert ordinary["updated_at"] == ""
        assert ordinary["content_sha256"]
        assert category["note_key"] == "category:note_category"
        assert category["updated_at"] == ""
        index = {item["ref"]: item for item in manager._dream_note_index()}
        assert index["note_old"]["note_updated_at"] == "unknown"
        assert index["note_category"]["note_updated_at"] == "unknown"
    finally:
        manager.close()


def test_note_upsert_refreshes_note_time_without_touching_owner_row(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "owner body", note_ref="note_fresh")
        note = manager.notes_dir / "note_fresh.md"
        note.write_text("first body\n", encoding="utf-8")
        manager._initialize_note_meta()
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-11-20T10:00:01+08:00")
        row_before = dict(_row(manager, memory_id))
        manager._now_iso = lambda: "2026-11-26T09:15:30+08:00"

        _commit(manager, note_upserts={"note_fresh": "second body\n"})

        assert note.read_text(encoding="utf-8") == "second body\n"
        assert _meta(manager, "note_fresh")["updated_at"] == "2026-11-26T09:15:30+08:00"
        row_after = _row(manager, memory_id)
        assert row_after["updated_at"] == row_before["updated_at"]
        assert row_after["created_at"] == row_before["created_at"]

        manager._now_iso = lambda: "2026-11-26T09:16:30+08:00"
        _commit(manager, note_upserts={"note_fresh": "second body\n"})
        assert _meta(manager, "note_fresh")["updated_at"] == "2026-11-26T09:15:30+08:00"
    finally:
        manager.close()


def test_failed_note_edit_does_not_refresh_freshness(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "owner body", note_ref="note_edit")
        note = manager.notes_dir / "note_edit.md"
        note.write_text("stable body\n", encoding="utf-8")
        manager._initialize_note_meta()
        manager._now_iso = lambda: "2026-11-26T09:15:30+08:00"
        _commit(manager, note_edits={"note_edit": [{"old_text": "missing", "new_text": "changed"}]})
        assert _meta(manager, "note_edit")["updated_at"] == ""
        assert note.read_text(encoding="utf-8") == "stable body\n"
    finally:
        manager.close()


def test_admin_persona_and_reconcile_writes_share_note_metadata(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "owner body", note_ref="note_missing")
        manager._now_iso = lambda: "2026-11-26T09:20:30+08:00"

        persona = manager.set_persona("persona body")
        assert persona["updated_at"] == "2026-11-26T09:20:30+08:00"
        assert _meta(manager, "note_persona")["updated_at"] == "2026-11-26T09:20:30+08:00"

        report = manager.reconcile_notes()
        assert report["created_missing_notes"] == ["note_missing"]
        assert _meta(manager, "note_missing")["updated_at"] == "2026-11-26T09:20:30+08:00"

        manager._now_iso = lambda: "2026-11-26T09:21:30+08:00"
        asyncio.run(manager.update_note("note_missing", body="admin body\n"))
        assert _meta(manager, "note_missing")["updated_at"] == "2026-11-26T09:21:30+08:00"
        assert _row(manager, memory_id)["updated_at"] == "2026-11-20T10:00:00+08:00"
    finally:
        manager.close()


def test_orphan_delete_removes_note_metadata(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "owner body", note_ref="note_reap")
        note = manager.notes_dir / "note_reap.md"
        note.write_text("note body\n", encoding="utf-8")
        manager._initialize_note_meta()
        manager._now_iso = lambda: "2026-11-26T09:22:30+08:00"
        validated = _module()._MemoryValidatedWrite(
            document_text="", note_writes={}, note_refs_written=[], memory_chars_after=0,
            document_preview="", deletes=[memory_id],
        )
        manager._commit_validated_write(validated)
        assert not note.exists()
        assert manager._memory_repo.read_note_meta("note_reap", "ordinary") is None
    finally:
        manager.close()


def test_consolidation_category_note_uses_the_same_freshness_lane(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "retired body", now_iso="2026-11-20T10:00:00+08:00")
        manager._now_iso = lambda: "2026-11-26T09:24:30+08:00"
        validated = _module()._MemoryValidatedWrite(
            document_text="", note_writes={}, note_refs_written=[], memory_chars_after=0,
            document_preview="", is_consolidation=True,
            consolidation_writes=[{
                "note_ref": "note_category_fresh",
                "body": "分类:测试\n\n- retired body\n",
                "edits": [],
            }],
            retirements=[{"memory_id": memory_id, "note_ref": "note_category_fresh"}],
        )
        manager._commit_validated_write(validated)
        assert _meta(manager, "note_category_fresh", "category")["updated_at"] == "2026-11-26T09:24:30+08:00"
        assert _row(manager, memory_id)["updated_at"] == "2026-11-20T10:00:00+08:00"
    finally:
        manager.close()


def test_external_note_rewrite_is_reported_as_stale(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "owner body", note_ref="note_external")
        note = manager.notes_dir / "note_external.md"
        note.write_text("runtime body\n", encoding="utf-8")
        manager._initialize_note_meta()
        note.write_text("external body\n", encoding="utf-8")

        report = manager.doctor_report()

        assert any(check["name"] == "note_meta_stale" and not check["ok"] for check in report["checks"])
        assert "note_external" in json.dumps(report["note_meta_stale"], ensure_ascii=False)
        assert note.read_text(encoding="utf-8") == "external body\n"
    finally:
        manager.close()


def test_note_only_revision_emits_updated_and_removed_notices(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        keep_id = _add(manager, "keep owner", note_ref="note_keep")
        drop_id = _add(manager, "drop owner", note_ref="note_drop")
        for ref in ("note_keep", "note_drop"):
            (manager.notes_dir / f"{ref}.md").write_text(f"{ref} body\n", encoding="utf-8")
        manager._initialize_note_meta()
        manager._now_iso = lambda: "2026-11-26T09:25:30+08:00"
        _commit(manager, note_upserts={"note_keep": "keep changed\n"})
        delta = manager.memory_notice_delta(since_rev=0)
        assert "Updated notes:" in delta["text"]
        assert "note_keep (ordinary, updated_at=2026-11-26 09:25)" in delta["text"]

        validated = _module()._MemoryValidatedWrite(
            document_text="", note_writes={}, note_refs_written=[], memory_chars_after=0,
            document_preview="", deletes=[drop_id],
        )
        manager._commit_validated_write(validated)
        delta = manager.memory_notice_delta(since_rev=1)
        assert "Removed notes:" in delta["text"]
        assert "note_drop" in delta["text"]
        assert keep_id in {row["memory_id"] for row in manager._memory_repo.list_memories()}
    finally:
        manager.close()
