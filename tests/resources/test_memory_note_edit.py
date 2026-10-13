from __future__ import annotations

import importlib
import json
from pathlib import Path


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _cfg():
    from g3ku.config.schema import MemoryToolsConfig

    return MemoryToolsConfig()


def _manager(tmp_path: Path):
    return _module().MemoryManager(tmp_path, _cfg())


def _seed_note(manager, tmp_path: Path, ref: str, body: str) -> Path:
    """Create a note file plus the row that owns it.

    Ownership lives in `memories.note_ref`, so a note file no row points at is an
    orphan and the post-commit sweep removes it. Seeding a bare file would test the
    sweep instead of the editor.
    """
    path = tmp_path / "memory" / "notes" / f"{ref}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    manager._memory_repo.apply_mutation(
        adds=[
            {
                "content": f"Rule that owns {ref}",
                "minimal_memory": f"own->note ref:{ref}",
                "decision_source": "user",
            }
        ],
        rewrites=None,
        deletes=None,
        inspired_memory_ids=None,
        now_iso="2026-04-17T10:00:00+08:00",
    )
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-17T10:00:01+08:00")
    return path


def _commit(tmp_path: Path, manager, **payload):
    """Drive a whole batch in through the tool the memory agent actually calls.

    Staging straight into `_MemoryValidatedWrite` would skip `apply_batch` validation
    and the builder, which is where a malformed edit gets refused.
    """
    module = _module()
    batch = module.MemoryBatch(
        op=str(payload.pop("batch_op", "write")),
        items=[
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="adjust a note",
                created_at="2026-04-17T10:00:02+08:00",
                request_id="req_note_edit",
            )
        ],
    )
    payload.setdefault("batch_op", batch.op)
    session = module._MemoryToolSession(
        snapshot_text=manager.snapshot_text(),
        notes_dir=manager.notes_dir,
        batch_op=batch.op,
    )
    result = session.apply_batch(
        adds=payload.get("adds"),
        rewrites=payload.get("rewrites"),
        deletes=payload.get("deletes"),
        note_upserts=payload.get("note_upserts"),
        note_edits=payload.get("note_edits"),
        inspired_memory_ids=payload.get("inspired_memory_ids"),
        noop_reason=payload.get("noop_reason"),
        already_satisfied=payload.get("already_satisfied"),
    )
    assert result.get("ok") is True, result
    validated = manager._build_validated_write_from_apply_batch(
        before_text=manager.snapshot_text(),
        session=session,
        batch=batch,
    )
    manager._commit_validated_write(validated)
    manager._append_terminal_history(
        batch=batch,
        status="applied",
        op=batch.op,
        processed_at="2026-04-17T10:00:03+08:00",
        validated=validated,
    )
    return validated


def _ledger_entries(tmp_path: Path, kind: str) -> list[dict]:
    """Per-op outcomes have to survive into the durable ledger, not just the live object."""
    out: list[dict] = []
    for line in (tmp_path / "memory" / "ops.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        for change in (json.loads(line).get("changes") or []):
            if isinstance(change, dict) and change.get("type") == kind:
                out.append(change)
    return out


def test_note_edit_replaces_one_span_and_leaves_the_rest_intact(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        note = _seed_note(
            manager,
            tmp_path,
            "note_ports",
            "# 端口\n\n- 反代端口：127.0.0.1:8788\n- 网页端口：127.0.0.1:18790\n\n## 启动\nnode src/index.js\n",
        )

        _commit(
            tmp_path,
            manager,
            note_edits={"note_ports": [{"old_text": "127.0.0.1:8788", "new_text": "127.0.0.1:8799"}]},
        )

        after = note.read_text(encoding="utf-8")
        assert "8799" in after
        assert "18790" in after, "an edit must not touch paragraphs it did not name"
        assert "node src/index.js" in after
        assert "8788" not in after
    finally:
        manager.close()


def test_note_edit_with_empty_new_text_deletes_the_span(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        note = _seed_note(manager, tmp_path, "note_steps", "第一步\n第二步（已作废）\n第三步\n")

        _commit(
            tmp_path,
            manager,
            note_edits={"note_steps": [{"old_text": "第二步（已作废）\n", "new_text": ""}]},
        )

        after = note.read_text(encoding="utf-8")
        assert "作废" not in after
        assert "第一步" in after and "第三步" in after
    finally:
        manager.close()


def test_unmatched_edit_is_reported_and_does_not_block_the_rest_of_the_batch(tmp_path: Path) -> None:
    """This contract shape misses ~10% of the time in the field, so a failed op has to
    cost one op. Letting it fail the batch would park roughly a tenth of runs."""
    manager = _manager(tmp_path)
    try:
        note = _seed_note(manager, tmp_path, "note_stable", "keep me\n")

        validated = _commit(
            tmp_path,
            manager,
            adds=[{"content": "New rule text", "minimal_memory": "new->min", "decision_source": "user"}],
            note_edits={"note_stable": [{"old_text": "text that is not there", "new_text": "x"}]},
        )

        assert note.read_text(encoding="utf-8") == "keep me\n"
        assert "New rule text" in manager.snapshot_text()
        failures = [item for item in validated.note_edit_results if not item.get("applied")]
        assert failures and "not found" in str(failures[0].get("error") or "")
    finally:
        manager.close()


def test_ambiguous_edit_demands_a_longer_locator(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        note = _seed_note(manager, tmp_path, "note_dup", "same line\nsame line\n")

        validated = _commit(
            tmp_path,
            manager,
            note_edits={"note_dup": [{"old_text": "same line", "new_text": "changed"}]},
        )

        assert note.read_text(encoding="utf-8") == "same line\nsame line\n"
        assert validated.note_edit_results[0]["applied"] is False
        assert "exactly once" in str(validated.note_edit_results[0].get("error") or "")
    finally:
        manager.close()


def test_editing_a_missing_note_leaves_other_notes_untouched(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        note = _seed_note(manager, tmp_path, "note_other", "untouched\n")

        _commit(
            tmp_path,
            manager,
            note_edits={"note_absent": [{"old_text": "u", "new_text": "b"}]},
        )

        assert note.read_text(encoding="utf-8") == "untouched\n"
    finally:
        manager.close()


def test_apply_batch_refuses_an_empty_locator(tmp_path: Path) -> None:
    """`old_text: ""` would replace the first match of an empty string, silently
    inserting text; the tool has to refuse it instead of guessing."""
    module = _module()
    manager = _manager(tmp_path)
    try:
        session = module._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            batch_op="write",
        )

        result = session.apply_batch(note_edits={"note_x": [{"old_text": "", "new_text": "injected"}]})

        assert result.get("ok") is False
        assert "old_text" in json.dumps(result.get("errors") or {})
    finally:
        manager.close()


def test_ledger_keeps_old_text_so_note_changes_have_a_diff(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _seed_note(manager, tmp_path, "note_diff", "alpha body\n")

        _commit(
            tmp_path,
            manager,
            note_edits={"note_diff": [{"old_text": "alpha body", "new_text": "beta body"}]},
        )
    finally:
        manager.close()

    entries = _ledger_entries(tmp_path, "note_edit")
    assert len(entries) == 1
    assert entries[0]["old_text"] == "alpha body"
    assert entries[0]["new_text"] == "beta body"
    assert entries[0]["applied"] is True
    assert entries[0]["note_ref"] == "note_diff"


def test_failed_edit_reaches_the_ledger_with_its_error(tmp_path: Path) -> None:
    """A dropped paragraph is only triageable later if the ledger says which locator
    missed; a live-only result would leave the note looking untouched by choice."""
    manager = _manager(tmp_path)
    try:
        _seed_note(manager, tmp_path, "note_miss", "keep\n")

        _commit(
            tmp_path,
            manager,
            note_edits={"note_miss": [{"old_text": "gone line", "new_text": "never landed"}]},
        )
    finally:
        manager.close()

    entries = _ledger_entries(tmp_path, "note_edit")
    assert len(entries) == 1
    assert entries[0]["applied"] is False
    assert entries[0]["old_text"] == "gone line"
    assert "not found" in str(entries[0].get("error") or "")


def test_upsert_lands_first_and_edits_apply_on_top_of_it(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _commit(
            tmp_path,
            manager,
            adds=[
                {
                    "content": "Deploy checklist detail",
                    "minimal_memory": "deploy->checklist ref:note_fresh",
                    "decision_source": "user",
                }
            ],
            note_upserts={"note_fresh": "# 新建\n\n初稿段落\n"},
            note_edits={"note_fresh": [{"old_text": "初稿段落", "new_text": "定稿段落"}]},
        )

        body = (tmp_path / "memory" / "notes" / "note_fresh.md").read_text(encoding="utf-8")
        assert "定稿段落" in body and "初稿段落" not in body
    finally:
        manager.close()


def test_memory_apply_batch_schema_exposes_note_edits(tmp_path: Path) -> None:
    """The tool face is derived from the handler signature, so note_edits reaches the
    model only if it is a real parameter - the prompt line alone would be a lie."""
    module = _module()
    manager = _manager(tmp_path)
    try:
        session = module._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            batch_op="write",
        )

        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}
        properties = tools["memory_apply_batch"].parameters["properties"]

        assert "note_edits" in properties
        assert "note_upserts" in properties
    finally:
        manager.close()


def test_a_new_note_name_must_use_the_note_prefix(tmp_path: Path) -> None:
    """The prefix is what stops the next reader from guessing which directory a name
    lives in; existing un-prefixed notes stay editable so history is not broken."""
    module = _module()
    manager = _manager(tmp_path)
    try:
        legacy = _seed_note(manager, tmp_path, "pdf_render_inspect", "旧写法命名")

        def _session():
            return module._MemoryToolSession(
                snapshot_text=manager.snapshot_text(),
                notes_dir=manager.notes_dir,
                index_dir=manager.index_dir,
                batch_op="write",
            )

        refused = _session().apply_batch(note_upserts={"crawler_tips": "新内容"})
        assert refused["ok"] is False
        assert "note_" in json.dumps(refused.get("errors") or {})

        _commit(
            tmp_path,
            manager,
            note_upserts={"pdf_render_inspect": "旧写法仍可改"},
        )
        assert "旧写法仍可改" in legacy.read_text(encoding="utf-8")
    finally:
        manager.close()
