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


def test_sweep_keeps_unreferenced_notes_readable_and_reports_them(tmp_path: Path) -> None:
    """A file no row ever owns stays on disk.

    The read paths glob the directory, so a kept orphan is still readable by the name
    prose mentions, and a later batch can claim it with a row's `note_ref`. Sweeping
    it the moment it appears is how a note vanished seconds after the same commit
    wrote it; the sweep therefore only reports this class.
    """
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[
                {
                    "content": "Rule with note",
                    "minimal_memory": "rule->min",
                    "decision_source": "user",
                    "note_ref": "note_kept",
                }
            ],
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

        records = manager._cleanup_orphan_notes_locked()

        by_ref = {str(item["note_ref"]): item for item in records}
        assert (tmp_path / "memory" / "notes" / "note_kept.md").exists()
        assert (tmp_path / "memory" / "notes" / "note_gone.md").exists(), (
            "an unowned note is kept on disk; only memory_deleted leaves the namespace"
        )
        assert by_ref["note_gone"]["reason"] == "unreferenced"
        assert by_ref["note_gone"]["removed"] == "false"
    finally:
        manager.close()


def test_reaping_is_recorded_with_the_reason_ownership_went_away(tmp_path: Path) -> None:
    """A note deletion cannot be undone and currently leaves no trace anywhere. The
    terminal ledger has to say which file went away and why, or "were any notes swept
    by mistake" stays unanswerable.

    Only `memory_deleted` removes the file: an explicit row deletion retires the
    experience, so the note follows. `ref_dropped` keeps the file on disk and reports
    it, because the rewrite may have dropped the ref without meaning to orphan the
    note - the model is told, and a later batch can claim the file back.
    """
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[
                {
                    "content": "Deleting rule",
                    "minimal_memory": "del->min",
                    "decision_source": "user",
                    "note_ref": "note_doomed",
                },
                {
                    "content": "Rewritten rule",
                    "minimal_memory": "rw->min",
                    "decision_source": "self",
                    "note_ref": "note_old",
                },
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
                    "id": by_body["Rewritten rule"],
                    "content": "Rewritten rule now pointing elsewhere",
                    "minimal_memory": "rw->min",
                    "note_ref": "note_new",
                }
            ],
            deletes=[by_body["Deleting rule"]],
            note_upserts={},
            inspired_memory_ids=[],
            noop_reason="",
        )
        (tmp_path / "memory" / "notes" / "note_new.md").write_text("note_new body\n", encoding="utf-8")
        manager._commit_validated_write(validated)

        reasons = {str(item["note_ref"]): str(item["reason"]) for item in validated.reaped_notes}
        assert reasons.get("note_doomed") == "memory_deleted"
        assert not (tmp_path / "memory" / "notes" / "note_doomed.md").exists()

        kept = {str(item["note_ref"]): str(item["reason"]) for item in validated.kept_orphan_notes}
        assert kept.get("note_old") == "ref_dropped"
        assert (tmp_path / "memory" / "notes" / "note_old.md").exists()
        # The feedback reaches the next agent turn, not the confirm receipt: the commit
        # lands after the turn, so the user prompt is the model's first read afterward.
        assert any("note_old" in line for line in manager._drain_note_feedback_lines())
        assert manager._drain_note_feedback_lines() == []
    finally:
        manager.close()


def test_sweep_never_removes_a_note_the_same_commit_wrote(tmp_path: Path) -> None:
    """Exemption guard for the one failure that motivated the gate: a batch deletes a
    row and upserts the same ref in one breath - the write must win over the sweep."""
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[
                {
                    "content": "Owning rule",
                    "minimal_memory": "own->min",
                    "decision_source": "user",
                    "note_ref": "note_fresh",
                }
            ],
            rewrites=None,
            deletes=None,
            inspired_memory_ids=None,
            now_iso="2026-04-17T10:00:00+08:00",
        )
        by_body = {str(r["memory_body"]): str(r["memory_id"]) for r in manager._memory_repo.list_memories()}
        note_path = tmp_path / "memory" / "notes" / "note_fresh.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("note_fresh body\n", encoding="utf-8")

        validated = module._MemoryValidatedWrite(
            document_text="",
            note_writes={"note_fresh": "note_fresh rewritten body\n"},
            note_refs_written=["note_fresh"],
            memory_chars_after=0,
            document_preview="",
            adds=[],
            rewrites=[],
            deletes=[by_body["Owning rule"]],
            note_upserts={},
            inspired_memory_ids=[],
            noop_reason="",
        )
        manager._commit_validated_write(validated)

        reasons = {str(item["note_ref"]): str(item["reason"]) for item in validated.reaped_notes}
        assert reasons == {}, "a note this commit wrote must not be reaped by this commit"
        assert note_path.read_text(encoding="utf-8") == "note_fresh rewritten body\n"
    finally:
        manager.close()


def test_note_upsert_requires_a_claiming_row_or_an_existing_owner(tmp_path: Path) -> None:
    """The claim gate: an upsert whose ref nothing owns is refused with the fix spelled
    out, so a batch can never produce a note whose body outlives any reference to it."""
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        owned = {"note_already_owned"}
        session = module._MemoryToolSession(
            snapshot_text="",
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            owned_note_refs_fn=lambda: set(owned),
        )
        (manager.notes_dir / "note_already_owned.md").write_text("owned body\n", encoding="utf-8")

        refused = session.apply_batch(
            adds=[],
            note_upserts={"note_unclaimed": "body\n"},
        )
        assert refused["ok"] is False
        assert "note_unclaimed" in refused["errors"]["note_upserts.note_unclaimed"]
        assert "note_ref='note_unclaimed'" in refused["errors"]["note_upserts.note_unclaimed"]

        accepted = session.apply_batch(
            adds=[
                {
                    "content": "Rule carrying the note",
                    "minimal_memory": "rule->min",
                    "decision_source": "user",
                    "note_ref": "note_unclaimed",
                }
            ],
            note_upserts={"note_unclaimed": "body\n"},
        )
        assert accepted["ok"] is True

        accepted_owned = session.apply_batch(
            adds=[],
            note_upserts={"note_already_owned": "rewritten body\n"},
        )
        assert accepted_owned["ok"] is True

        accepted_persona = session.apply_batch(
            adds=[],
            note_upserts={module.PERSONA_NOTE_REF: "persona body\n"},
        )
        assert accepted_persona["ok"] is True
    finally:
        manager.close()


def test_doctor_reports_prose_mentions_no_file_answers(tmp_path: Path) -> None:
    """The block can advertise a name whose file is gone; doctor has to say so instead
    of leaving readers to discover it as a load failure."""
    module = _load_module()
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[
                {
                    "content": "选题前先读 note_play_gone_benchmarks 与 note_upserts 工具说明",
                    "minimal_memory": "选题->对标库",
                    "decision_source": "user",
                }
            ],
            rewrites=None,
            deletes=None,
            inspired_memory_ids=None,
            now_iso="2026-04-17T10:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")

        report = manager.doctor_report()
        dangling = report["dangling_note_mentions"]
        assert list(dangling) == ["note_play_gone_benchmarks"], (
            "the structural token note_upserts is vocabulary, not a note mention"
        )
        check = next(item for item in report["checks"] if item["name"] == "dangling_note_mentions")
        assert check["ok"] is False

        (manager.notes_dir / "note_play_gone_benchmarks.md").write_text("restored\n", encoding="utf-8")
        report = manager.doctor_report()
        assert report["dangling_note_mentions"] == {}
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


def test_projection_carries_no_in_text_marker_and_columns_own_notes(tmp_path: Path) -> None:
    """The mirror renders handles, never a parseable relationship.

    This is the guard the plan asked for before deleting the text scan: after one commit
    every owned note shows up as the handle riding on its memory line and no ` ref:` /
    `见noteid:` token survives in the projection, so a reader that wanted to re-derive
    ownership from text would find nothing - the column set is the whole truth.
    """
    module = _load_module()
    markdown = importlib.import_module("g3ku.agent.markdown_memory")
    manager = module.MemoryManager(tmp_path, _cfg())
    try:
        manager._memory_repo.apply_mutation(
            adds=[
                {
                    "content": "Alpha rule",
                    "minimal_memory": "alpha->min",
                    "decision_source": "user",
                    "note_ref": "note_a",
                },
                {
                    "content": "Beta rule",
                    "minimal_memory": "beta->min",
                    "decision_source": "self",
                    "note_ref": "note_b",
                },
            ],
            rewrites=None,
            deletes=None,
            inspired_memory_ids=None,
            now_iso="2026-04-17T10:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")

        snapshot = manager.snapshot_text()
        from_store = set(manager._referenced_note_refs())
        assert from_store == {"note_a", "note_b"}
        assert module._NOTE_REF_RE.search(snapshot) is None
        assert "note_a" not in snapshot and "note_b" not in snapshot
        rendered = {entry.note_ref for entry in markdown.parse_memory_document(snapshot) if entry.note_ref}
        assert rendered == {module.note_handle(ref) for ref in from_store}
    finally:
        manager.close()
