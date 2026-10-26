from __future__ import annotations

import json
from pathlib import Path

import pytest

from g3ku.agent.persona import PERSONA_NOTE_REF, persona_note_handle


def _module():
    import g3ku.agent.memory_agent_runtime as module

    return module


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


def _manager(tmp_path: Path):
    module = _module()
    return module.MemoryManager(tmp_path, _memory_cfg())


def _write_session(manager):
    return _module()._MemoryToolSession(
        snapshot_text=manager.snapshot_text(),
        notes_dir=manager.notes_dir,
        index_dir=manager.index_dir,
        batch_op="write",
    )


def _seed_row(manager, *, body: str = "Prefer concise answers") -> dict:
    row = manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory="answers->concise",
        source="user",
        from_user=True,
        now_iso="2026-10-26T00:00:00+08:00",
    )
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-10-26T00:00:00+08:00")
    return row


def test_set_persona_creates_and_updates_the_reserved_note(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        first = manager.set_persona("你是玲音，说话简短。")
        path = tmp_path / "memory" / "notes" / f"{PERSONA_NOTE_REF}.md"
        assert path.exists()
        assert first["ref"] == PERSONA_NOTE_REF
        assert manager.persona_text() == "你是玲音，说话简短。"

        manager.set_persona("你是玲音，先给结论再补一句理由。")
        assert manager.persona_text() == "你是玲音，先给结论再补一句理由。"
        # Still exactly one note, still no memory row.
        assert len(list((tmp_path / "memory" / "notes").glob("*.md"))) == 1
        assert manager._memory_repo.list_memories() == []
    finally:
        manager.close()


def test_set_persona_rejects_empty_body(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        with pytest.raises(ValueError):
            manager.set_persona("   ")
    finally:
        manager.close()


def test_persona_note_survives_orphan_sweep_reconcile_and_doctor(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _seed_row(manager)
        manager.set_persona("你是玲音。")

        removed = manager._cleanup_orphan_notes_locked()
        assert PERSONA_NOTE_REF not in removed
        assert (tmp_path / "memory" / "notes" / f"{PERSONA_NOTE_REF}.md").exists()

        reconciled = manager.reconcile_notes(delete_orphans=True)
        assert PERSONA_NOTE_REF not in reconciled["orphan_notes_detected"]
        assert PERSONA_NOTE_REF not in reconciled["deleted_orphan_notes"]

        report = manager.doctor_report()
        assert PERSONA_NOTE_REF not in report["orphan_notes"]
        assert report["persona_note_present"] is True
        assert report["persona_note_chars"] == len("你是玲音。")
    finally:
        manager.close()


def test_persona_note_is_read_from_the_ordinary_tree_only(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        # A category note squatting on the reserved name must not become the persona body.
        manager.index_dir.mkdir(parents=True, exist_ok=True)
        (manager.index_dir / f"{PERSONA_NOTE_REF}.md").write_text("category body", encoding="utf-8")
        assert manager.persona_text() == ""
        with pytest.raises(FileNotFoundError):
            manager._note_path(PERSONA_NOTE_REF)
        with pytest.raises(FileNotFoundError):
            manager._note_path(persona_note_handle())

        manager.set_persona("ordinary persona")
        assert manager.persona_text() == "ordinary persona"
        assert (tmp_path / "memory" / "notes" / f"{PERSONA_NOTE_REF}.md").exists()
        assert (manager.index_dir / f"{PERSONA_NOTE_REF}.md").read_text(encoding="utf-8") == "category body"
        assert manager._note_write_path(PERSONA_NOTE_REF) == tmp_path / "memory" / "notes" / f"{PERSONA_NOTE_REF}.md"
        assert manager._note_edit_target(persona_note_handle()) == tmp_path / "memory" / "notes" / f"{PERSONA_NOTE_REF}.md"
    finally:
        manager.close()


def test_ordinary_memory_cannot_attach_the_persona_note(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        session = _module()._MemoryToolSession(
            snapshot_text="",
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="write",
        )
        result = session.apply_batch(
            adds=[
                {
                    "content": "用户偏好：先给结论。",
                    "minimal_memory": "回复->先给结论",
                    "decision_source": "user",
                    "note_ref": PERSONA_NOTE_REF,
                }
            ]
        )
        assert result["ok"] is False
        assert any(key.endswith("note_ref") for key in result["errors"])

        result = session.apply_batch(
            adds=[
                {
                    "content": "用户偏好：先给结论。",
                    "minimal_memory": "回复->先给结论",
                    "decision_source": "user",
                    "note_ref": persona_note_handle(),
                }
            ]
        )
        assert result["ok"] is False
        assert any(key.endswith("note_ref") for key in result["errors"])
    finally:
        manager.close()


def test_consolidation_cannot_claim_the_persona_name(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        session = _module()._ConsolidationToolSession(
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            category_names=[],
        )
        result = session.apply_batch(
            note_writes=[{"note_ref": PERSONA_NOTE_REF, "body": "人设: 玲音"}],
            retirements=[],
        )
        assert result["ok"] is False
        assert PERSONA_NOTE_REF in json.dumps(result["errors"], ensure_ascii=False)
    finally:
        manager.close()


def test_memory_agent_sees_the_persona_pointer_not_the_body(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        body = "你是玲音，说话简短，先给结论。"
        manager.set_persona(body)
        module = _module()
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="user",
                    payload_text="用户要求更新人设",
                    created_at="2026-10-26T00:00:00+08:00",
                )
            ],
        )
        prompt = manager._memory_agent_user_prompt(
            batch=batch,
            snapshot_text=manager.snapshot_text(),
            repair_reason="",
        )
        assert f"人设描述：ref={PERSONA_NOTE_REF}" in prompt
        assert f"key 都写 {PERSONA_NOTE_REF}" in prompt
        assert persona_note_handle() not in prompt
        assert body not in prompt
    finally:
        manager.close()


def test_persona_handle_written_as_a_note_name_is_auto_corrected(tmp_path: Path) -> None:
    """The field model saw `(note: n1a2b3)` and wrote `note_n1a2b3`; the write lane maps it
    back to the persona ref and tells the model what it changed, instead of spending a
    repair round on a batch that can only mean one note."""
    manager = _manager(tmp_path)
    try:
        mangled = f"note_{persona_note_handle()}"
        session = _write_session(manager)
        result = session.apply_batch(note_upserts={mangled: "你是玲音。"})

        assert result["ok"] is True, result
        assert session.applied_batch["note_upserts"] == {PERSONA_NOTE_REF: "你是玲音。"}
        assert result["warnings"] == [
            f"note_upserts key used {mangled!r}, which is not a note name; "
            f"auto-corrected to {PERSONA_NOTE_REF!r}. The batch uses {PERSONA_NOTE_REF!r}."
        ]
        assert not (manager.notes_dir / f"{mangled}.md").exists()
    finally:
        manager.close()


def test_memory_apply_batch_reports_the_ref_correction_to_the_model(tmp_path: Path) -> None:
    """The correction has to be in the tool result the model reads, not just in the
    staged payload - otherwise the agent never learns which name it should have used."""
    manager = _manager(tmp_path)
    try:
        session = _write_session(manager)
        tool = {item.name: item for item in manager._memory_agent_tools(session)}["memory_apply_batch"]
        mangled = f"note_{persona_note_handle()}"

        result = tool.invoke({"note_upserts": {mangled: "你是玲音。"}})

        assert result["ok"] is True, result
        assert any("auto-corrected" in warning and PERSONA_NOTE_REF in warning for warning in result["warnings"])
    finally:
        manager.close()


def test_existing_note_handle_used_as_a_ref_is_auto_corrected(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        note = tmp_path / "memory" / "notes" / "note_ports.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("旧端口\n", encoding="utf-8")
        mangled = f"note_{_module().note_handle('note_ports')}"
        session = _write_session(manager)
        result = session.apply_batch(
            note_edits={mangled: [{"old_text": "旧端口", "new_text": "新端口"}]}
        )

        assert result["ok"] is True, result
        assert session.applied_batch["note_edits"] == {
            "note_ports": [{"old_text": "旧端口", "new_text": "新端口"}]
        }
        assert "auto-corrected to 'note_ports'" in result["warnings"][0]
    finally:
        manager.close()


def test_a_handle_shaped_ref_that_matches_no_note_is_still_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        result = _write_session(manager).apply_batch(note_upserts={"note_n0a1b2": "x"})

        assert result["ok"] is False
        assert "matches no existing note" in result["errors"]["note_upserts.note_n0a1b2"]
    finally:
        manager.close()


def test_persona_read_handle_is_normalized_to_the_reserved_ref(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        session = _write_session(manager)
        result = session.apply_batch(note_upserts={persona_note_handle(): "你是玲音。"})

        assert result["ok"] is True, result
        assert session.applied_batch["note_upserts"] == {PERSONA_NOTE_REF: "你是玲音。"}
        assert "auto-corrected" in result["warnings"][0]
    finally:
        manager.close()


def test_persona_note_upsert_lands_and_survives_the_orphan_sweep(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        module = _module()
        batch = module.MemoryBatch(
            op="write",
            items=[
                module.MemoryQueueRequest(
                    op="write",
                    decision_source="user",
                    payload_text="这是人设更新，请修改人设描述，不要新增普通记忆。",
                    created_at="2026-10-26T00:00:00+08:00",
                )
            ],
        )
        session = _write_session(manager)
        result = session.apply_batch(note_upserts={f"note_{persona_note_handle()}": "你是玲音。"})
        assert result["ok"] is True, result
        assert "auto-corrected" in result["warnings"][0]
        session.confirm_batch()
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=batch,
        )

        manager._commit_validated_write(validated)

        assert manager.persona_text() == "你是玲音。"
        assert (tmp_path / "memory" / "notes" / f"{PERSONA_NOTE_REF}.md").exists()
        assert validated.reaped_notes == []
    finally:
        manager.close()


def test_persona_is_not_a_resident_memory_line_or_char(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _seed_row(manager)
        before_chars = manager._resident_memory_chars()
        before_entries = manager.resident_lines()
        manager.set_persona("你是玲音。")
        after_entries = manager.resident_lines()
        assert manager._resident_memory_chars() == before_chars
        assert [entry.memory_id for entry in after_entries] == [entry.memory_id for entry in before_entries]
        assert persona_note_handle() not in {entry.note_ref for entry in after_entries}
    finally:
        manager.close()
