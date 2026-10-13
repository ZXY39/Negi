from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path


def _load_module():
    assert importlib.util.find_spec("g3ku.agent.memory_agent_runtime") is not None
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _cfg():
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


def _columns(tmp_path: Path) -> list[str]:
    with sqlite3.connect(str(tmp_path / "memory" / "memory_state.sqlite3")) as conn:
        return [str(row[1]) for row in conn.execute("PRAGMA table_info(memories)")]


def test_store_gains_note_ref_and_consolidated_into_columns(tmp_path: Path) -> None:
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        assert "note_ref" in _columns(tmp_path)
        assert "consolidated_into" in _columns(tmp_path)
    finally:
        manager.close()


def test_existing_bodies_are_migrated_into_the_column(tmp_path: Path) -> None:
    """Rows written before the column existed keep their reference - it moves out of
    the text into the store, so ownership no longer depends on how the row renders."""
    module = _load_module()
    first = module.MemoryManager(tmp_path, _cfg())
    try:
        first._memory_repo.apply_mutation(
            adds=[
                {"content": "Credential scan rule ref:note_scan", "minimal_memory": "scan->creds", "decision_source": "user"},
                {"content": "Plain rule", "minimal_memory": "plain->min 见noteid:note_other", "decision_source": "self"},
            ],
            rewrites=None,
            deletes=None,
            inspired_memory_ids=None,
            now_iso="2026-04-17T10:00:00+08:00",
        )
        first._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")
    finally:
        first.close()

    reopened = module.MemoryManager(tmp_path, _cfg())
    try:
        rows = {str(r["memory_id"]): r for r in reopened._memory_repo.list_memories()}
        refs = {str(r["note_ref"]) for r in rows.values()}
        assert "note_scan" in refs
        assert "note_other" in refs
        # Re-running the migration must not double-append or lose anything.
        again = module.MemoryManager(tmp_path, _cfg())
        try:
            rows2 = {str(r["memory_id"]): r for r in again._memory_repo.list_memories()}
            assert {str(r["note_ref"]) for r in rows2.values()} == refs
        finally:
            again.close()
    finally:
        reopened.close()


def test_orphan_sweep_uses_store_ownership_not_rendered_text(tmp_path: Path) -> None:
    """A row that owns a note through the column protects the file even when neither
    of its text fields carries the marker - the projection is a display surface."""
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[{"content": "Rule without any marker", "minimal_memory": "rule->min", "decision_source": "user"}],
            rewrites=None,
            deletes=None,
            inspired_memory_ids=None,
            now_iso="2026-04-17T10:00:00+08:00",
        )
        memory_id = str(manager._memory_repo.list_memories()[0]["memory_id"])
        with sqlite3.connect(str(tmp_path / "memory" / "memory_state.sqlite3")) as conn:
            conn.execute("UPDATE memories SET note_ref = 'note_owned' WHERE memory_id = ?", (memory_id,))
            conn.commit()
        note = tmp_path / "memory" / "notes" / "note_owned.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("Owned note body\n", encoding="utf-8")
        assert "note_owned" not in manager.snapshot_text()

        manager._cleanup_orphan_notes_locked()

        assert note.exists(), "a note owned through the column must not be swept as an orphan"
    finally:
        manager.close()


def test_sweep_still_removes_truly_unreferenced_notes(tmp_path: Path) -> None:
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[{"content": "Rule with note", "minimal_memory": "rule->min ref:note_kept", "decision_source": "user"}],
            rewrites=None,
            deletes=None,
            inspired_memory_ids=None,
            now_iso="2026-04-17T10:00:00+08:00",
        )
        for ref in ("note_kept", "note_gone"):
            path = tmp_path / "memory" / "notes" / f"{ref}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{ref} body\n", encoding="utf-8")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")

        manager._cleanup_orphan_notes_locked()

        assert (tmp_path / "memory" / "notes" / "note_kept.md").exists()
        assert not (tmp_path / "memory" / "notes" / "note_gone.md").exists()
    finally:
        manager.close()


def test_reaping_is_recorded_with_the_reason_ownership_went_away(tmp_path: Path) -> None:
    """A note deletion cannot be undone and currently leaves no trace anywhere. The
    terminal ledger has to say which file went away and why, or "were any notes swept
    by mistake" stays unanswerable."""
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[
                {"content": "Deleting rule ref:note_doomed", "minimal_memory": "del->min", "decision_source": "user"},
                {"content": "Rewritten rule ref:note_old", "minimal_memory": "rw->min", "decision_source": "self"},
            ],
            rewrites=None,
            deletes=None,
            inspired_memory_ids=None,
            now_iso="2026-04-17T10:00:00+08:00",
        )
        by_body = {str(r["memory_body"]): str(r["memory_id"]) for r in manager._memory_repo.list_memories()}
        for ref in ("note_doomed", "note_old"):
            path = tmp_path / "memory" / "notes" / f"{ref}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{ref} body\n", encoding="utf-8")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")

        validated = module._MemoryValidatedWrite(
            document_text="",
            note_writes={},
            note_refs_written=[],
            memory_chars_after=0,
            document_preview="",
            adds=[],
            rewrites=[
                {
                    "id": by_body["Rewritten rule ref:note_old"],
                    "content": "Rewritten rule now pointing elsewhere ref:note_new",
                    "minimal_memory": "rw->min",
                }
            ],
            deletes=[by_body["Deleting rule ref:note_doomed"]],
            note_upserts={},
            inspired_memory_ids=[],
            noop_reason="",
        )
        (tmp_path / "memory" / "notes" / "note_new.md").write_text("note_new body\n", encoding="utf-8")
        manager._commit_validated_write(validated)

        reasons = {str(item["note_ref"]): str(item["reason"]) for item in validated.reaped_notes}
        assert reasons.get("note_doomed") == "memory_deleted"
        assert reasons.get("note_old") == "ref_dropped"
        assert not (tmp_path / "memory" / "notes" / "note_doomed.md").exists()
        assert not (tmp_path / "memory" / "notes" / "note_old.md").exists()
    finally:
        manager.close()


def test_boot_leaves_a_pre_migration_legacy_mirror_alone(tmp_path: Path) -> None:
    """A mirror with no managed blocks is migration input, not a damaged projection.

    `import-legacy` and `cleanup-legacy` read that file, so boot reconciliation has to
    leave it in place; the commit-time path still archives and repaints, because a live
    session must not keep running on a divergent projection.
    """
    module = _load_module()
    first = module.MemoryManager(tmp_path, _cfg())
    try:
        (tmp_path / "memory" / "MEMORY.md").write_text(
            "---\n2026/4/17-user：\nLegacy pre-migration memory\n",
            encoding="utf-8",
        )
        reopened = module.MemoryManager(tmp_path, _cfg())
        try:
            assert "Legacy pre-migration memory" in reopened.snapshot_text()
            assert not list((tmp_path / "memory").glob("MEMORY.md.bak.*"))
        finally:
            reopened.close()
    finally:
        first.close()


def test_column_set_matches_text_scan_on_live_mirror(tmp_path: Path) -> None:
    """Equivalence guard: while the projection still carries the marker, the store and
    the rendered text must agree - otherwise one of the two is quietly losing refs."""
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[
                {"content": "Alpha rule ref:note_a", "minimal_memory": "alpha->min", "decision_source": "user"},
                {"content": "Beta rule", "minimal_memory": "beta->min 见noteid:note_b", "decision_source": "self"},
            ],
            rewrites=None,
            deletes=None,
            inspired_memory_ids=None,
            now_iso="2026-04-17T10:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")

        from_store = set(manager._referenced_note_refs())
        from_text = set(manager._collect_note_refs(manager.snapshot_text()))
        assert from_store == from_text == {"note_a", "note_b"}
    finally:
        manager.close()
