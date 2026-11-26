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


def _add(manager, body: str = "owner body", *, note_ref: str = "note_sections") -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory=body,
        source="user",
        from_user=True,
        note_ref=note_ref,
        now_iso="2026-11-20T10:00:00+08:00",
    )
    return str(row["memory_id"])


def _seed_note(manager, ref: str, body: str) -> Path:
    _add(manager, note_ref=ref)
    path = manager.notes_dir / f"{ref}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    manager._initialize_note_meta()
    manager._initialize_note_sections()
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-11-20T10:00:01+08:00")
    return path


def _batch(op: str = "write", payload_text: str = "整理 note 段落") -> object:
    module = _module()
    return module.MemoryBatch(
        op=op,
        items=[
            module.MemoryQueueRequest(
                op=op,
                decision_source="user",
                payload_text=payload_text,
                created_at="2026-11-26T09:00:00+08:00",
                request_id="note_sections",
            )
        ],
    )


def _session(manager, op: str = "write"):
    module = _module()
    return module._MemoryToolSession(
        snapshot_text=manager.snapshot_text(),
        notes_dir=manager.notes_dir,
        index_dir=manager.index_dir,
        batch_op=op,
        owned_note_refs_fn=lambda: set(manager._memory_repo.owned_note_refs()),
    )


def _tools(manager, session):
    return {tool.name: tool for tool in manager._memory_agent_tools(session)}


def _section(manager, ref: str, section_key: str) -> dict:
    rows = manager._memory_repo.list_note_sections(f"ordinary:{ref}")
    return next(row for row in rows if row["section_key"] == section_key)


def _commit(manager, session, batch):
    assert session.confirm_batch()["status"] == "batch_confirmed"
    validated = manager._validate_candidate_state(
        batch=batch, before_text=manager.snapshot_text(), session=session
    )
    manager._commit_validated_write(validated)
    manager._append_terminal_history(
        batch=batch,
        status="applied",
        op=batch.op,
        processed_at="2026-11-26T09:30:00+08:00",
        validated=validated,
    )
    return validated


def test_parser_assigns_stable_non_overlapping_section_ids(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        body = "前言\n\n## A\n甲正文\n### B\n乙正文\n## A\n重复标题\n"
        sections = manager._parse_note_sections(body)
        assert [item["section_key"] for item in sections] == [
            "(preamble)",
            "A",
            "A > B",
            "A#2",
        ]
        assert len({item["section_id"] for item in sections}) == 4
        changed_body = body.replace("甲正文", "甲正文修改")
        changed = manager._parse_note_sections(changed_body)
        assert [item["section_id"] for item in changed] == [item["section_id"] for item in sections]
        renamed = manager._parse_note_sections(body.replace("## A", "## C"))
        assert [item["section_id"] for item in renamed] != [item["section_id"] for item in sections]
    finally:
        manager.close()


def test_existing_sections_migrate_as_unknown_and_appear_in_note_index(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _seed_note(manager, "note_sections", "# 标题\n\n## 旧口径\n旧口径作废的旧正文\n\n## 新口径\n新正文\n")
        rows = manager._memory_repo.list_note_sections("ordinary:note_sections")
        assert rows and all(row["status"] == "unknown" for row in rows)
        assert all(row["decision_at"] == "" for row in rows)
        index = {item["ref"]: item for item in manager._dream_note_index()}["note_sections"]
        assert {item["section_key"] for item in index["sections"]} >= {"标题 > 旧口径", "标题 > 新口径"}
        assert all(item["status"] == "unknown" for item in index["sections"])
        old_section = next(item for item in index["sections"] if item["section_key"] == "标题 > 旧口径")
        assert old_section["evidence_hints"]
        assert index["section_review_candidates"]
    finally:
        manager.close()


def test_supersession_deletes_old_section_and_persists_tombstone(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        note = _seed_note(
            manager,
            "note_sections",
            "# 回评\n\n## 旧口径\n一律零发送。\n\n## 新口径\n按分级选择性回评。\n",
        )
        manager._now_iso = lambda: "2026-11-26T09:20:30+08:00"
        session = _session(manager)
        tools = _tools(manager, session)
        staged = tools["memory_apply_batch"].invoke(
            {
                "note_section_updates": [
                    {
                        "ref": "note_sections",
                        "section_key": "回评 > 旧口径",
                        "status": "superseded",
                        "scope_key": "realstory:douyin:reply",
                        "superseded_by": "回评 > 新口径",
                        "evidence": "用户明确改为分级选择性回评",
                    },
                    {
                        "ref": "note_sections",
                        "section_key": "回评 > 新口径",
                        "status": "active",
                        "scope_key": "realstory:douyin:reply",
                    },
                ],
            }
        )
        assert staged["ok"] is True, staged
        preview = staged["preview"]["note_section_preview"]
        assert any(item["section_key"] == "回评 > 旧口径" and "一律零发送" in item["old_content"] for item in preview)
        assert any(item["section_key"] == "回评 > 旧口径" and "选择性回评" in item["replacement_content"] for item in preview)
        validated = _commit(manager, session, _batch())
        assert "一律零发送" not in note.read_text(encoding="utf-8")
        assert _section(manager, "note_sections", "回评 > 旧口径")["status"] == "superseded"
        assert _section(manager, "note_sections", "回评 > 旧口径")["superseded_by"] == _section(manager, "note_sections", "回评 > 新口径")["section_id"]
        assert _section(manager, "note_sections", "回评 > 新口径")["status"] == "active"
        assert validated.note_section_results
        assert manager.doctor_report()["note_section_meta_stale"] == []
        ops = [json.loads(line) for line in manager.ops_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        section_change = next(change for change in ops[-1]["changes"] if change.get("type") == "note_section")
        assert "一律零发送" in section_change["old_content"]
        assert "选择性回评" in section_change["replacement_content"]
    finally:
        manager.close()


def test_machine_rejects_two_active_sections_with_same_scope(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _seed_note(manager, "note_sections", "## A\n甲\n\n## B\n乙\n")
        session = _session(manager)
        result = _tools(manager, session)["memory_apply_batch"].invoke(
            {
                "note_section_updates": [
                    {"ref": "note_sections", "section_key": "A", "status": "active", "scope_key": "topic"},
                    {"ref": "note_sections", "section_key": "B", "status": "active", "scope_key": "topic"},
                ]
            }
        )
        assert result["ok"] is False
        assert "active" in json.dumps(result["errors"], ensure_ascii=False).lower()
    finally:
        manager.close()


def test_machine_rejects_self_cycle_missing_replacement_and_missing_evidence(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _seed_note(manager, "note_sections", "## A\n甲\n\n## B\n乙\n")
        cases = [
            {"section_key": "A", "status": "superseded", "scope_key": "topic", "superseded_by": "A", "evidence": "x"},
            {"section_key": "A", "status": "superseded", "scope_key": "topic", "superseded_by": "缺失", "evidence": "x"},
            {"section_key": "A", "status": "superseded", "scope_key": "topic", "superseded_by": "B", "evidence": ""},
        ]
        for item in cases:
            session = _session(manager)
            result = _tools(manager, session)["memory_apply_batch"].invoke(
                {"note_section_updates": [{"ref": "note_sections", **item}]}
            )
            assert result["ok"] is False, item
        session = _session(manager)
        result = _tools(manager, session)["memory_apply_batch"].invoke(
            {
                "note_section_updates": [
                    {"ref": "note_sections", "section_key": "A", "status": "superseded", "scope_key": "topic", "superseded_by": "B", "evidence": "x"},
                    {"ref": "note_sections", "section_key": "B", "status": "superseded", "scope_key": "topic", "superseded_by": "A", "evidence": "y"},
                ]
            }
        )
        assert result["ok"] is False
        assert "cycle" in json.dumps(result["errors"], ensure_ascii=False).lower()
    finally:
        manager.close()


def test_dream_rejects_section_updates_outside_cluster(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _seed_note(manager, "note_inside", "## A\n甲\n")
        _seed_note(manager, "note_outside", "## B\n乙\n")
        module = _module()
        session = module._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
            dream_context_loaded=True,
            dream_memory_ids=set(),
            dream_note_refs={"note_inside"},
        )
        result = _tools(manager, session)["memory_apply_batch"].invoke(
            {"note_section_updates": [{"ref": "note_outside", "section_key": "B", "status": "active", "scope_key": "topic"}]}
        )
        assert result["ok"] is False
        assert "cluster" in json.dumps(result["errors"], ensure_ascii=False).lower()
    finally:
        manager.close()


def test_unknown_update_keeps_content_and_is_auditable(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        note = _seed_note(manager, "note_sections", "## A\n证据不足的段落\n")
        before = note.read_text(encoding="utf-8")
        session = _session(manager)
        staged = _tools(manager, session)["memory_apply_batch"].invoke(
            {"note_section_updates": [{"ref": "note_sections", "section_key": "A", "status": "unknown"}]}
        )
        assert staged["ok"] is True, staged
        _commit(manager, session, _batch())
        assert note.read_text(encoding="utf-8") == before
        assert _section(manager, "note_sections", "A")["status"] == "unknown"
    finally:
        manager.close()


def test_external_section_rewrite_is_reported_as_stale(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        note = _seed_note(manager, "note_sections", "## A\n原始段落\n")
        note.write_text("## A\n外部改写\n", encoding="utf-8")
        report = manager.doctor_report()
        assert any(check["name"] == "note_section_meta_stale" and not check["ok"] for check in report["checks"])
        assert "note_sections" in json.dumps(report["note_section_meta_stale"], ensure_ascii=False)
    finally:
        manager.close()


def test_orphan_delete_clears_section_metadata(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, note_ref="note_sections")
        note = manager.notes_dir / "note_sections.md"
        note.write_text("## A\n正文\n", encoding="utf-8")
        manager._initialize_note_meta()
        manager._initialize_note_sections()
        manager._now_iso = lambda: "2026-11-26T09:25:30+08:00"
        validated = _module()._MemoryValidatedWrite(
            document_text="",
            note_writes={},
            note_refs_written=[],
            memory_chars_after=0,
            document_preview="",
            deletes=[memory_id],
        )
        manager._commit_validated_write(validated)
        assert not note.exists()
        assert manager._memory_repo.list_note_sections("ordinary:note_sections") == []
    finally:
        manager.close()


def test_admin_note_edit_syncs_sections_without_touching_owner_time(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, note_ref="note_sections")
        note = manager.notes_dir / "note_sections.md"
        note.write_text("## A\n旧正文\n", encoding="utf-8")
        manager._initialize_note_meta()
        manager._initialize_note_sections()
        manager._now_iso = lambda: "2026-11-26T09:26:30+08:00"
        asyncio.run(manager.update_note("note_sections", body="## B\n新正文\n"))
        rows = manager._memory_repo.list_note_sections("ordinary:note_sections")
        assert next(row for row in rows if row["section_key"] == "A")["status"] == "superseded"
        assert next(row for row in rows if row["section_key"] == "B")["status"] == "unknown"
        owner = next(row for row in manager._memory_repo.list_memories() if row["memory_id"] == memory_id)
        assert owner["updated_at"] == "2026-11-20T10:00:00+08:00"
        session = _session(manager)
        staged = _tools(manager, session)["memory_apply_batch"].invoke(
            {"note_section_updates": [{"ref": "note_sections", "section_key": "B", "status": "active", "scope_key": "topic"}]}
        )
        assert staged["ok"] is True, staged
        _commit(manager, session, _batch())
    finally:
        manager.close()


def test_persona_and_reconcile_placeholders_index_sections(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        manager.set_persona("# 人设\n\n## 语气\n温和克制。\n")
        assert {row["section_key"] for row in manager._memory_repo.list_note_sections("ordinary:note_persona")} >= {
            "人设",
            "人设 > 语气",
        }
        _add(manager, note_ref="note_missing")
        report = manager.reconcile_notes()
        assert report["created_missing_notes"] == ["note_missing"]
        rows = manager._memory_repo.list_note_sections("ordinary:note_missing")
        assert len(rows) == 1 and rows[0]["section_key"] == "(preamble)"
        assert rows[0]["status"] == "unknown"
    finally:
        manager.close()


def test_consolidation_category_note_indexes_sections(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "retired body")
        validated = _module()._MemoryValidatedWrite(
            document_text="",
            note_writes={},
            note_refs_written=[],
            memory_chars_after=0,
            document_preview="",
            is_consolidation=True,
            consolidation_writes=[{
                "note_ref": "note_category_sections",
                "body": "# 分类\n\n## 方法论\n- retired body\n",
                "edits": [],
            }],
            retirements=[{"memory_id": memory_id, "note_ref": "note_category_sections"}],
        )
        manager._commit_validated_write(validated)
        rows = manager._memory_repo.list_note_sections("category:note_category_sections")
        assert {row["section_key"] for row in rows} >= {"分类", "分类 > 方法论"}
        assert all(row["status"] == "unknown" for row in rows)
    finally:
        manager.close()


def test_ordinary_and_dream_inputs_expose_related_sections(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, note_ref="note_sections")
        (manager.notes_dir / "note_sections.md").write_text("## A\n甲\n\n## B\n乙\n", encoding="utf-8")
        manager._initialize_note_meta()
        manager._initialize_note_sections()
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-11-20T10:00:01+08:00")
        ordinary = manager._memory_agent_user_prompt(
            batch=_batch(payload_text="整理 note_sections 的 A/B 段落"),
            snapshot_text=manager.snapshot_text(),
            repair_reason="",
        )
        module = _module()
        dream_batch = module.MemoryBatch(
            op="dream",
            items=[module.MemoryQueueRequest(
                op="dream",
                decision_source="self",
                payload_text=json.dumps({"memory_ids": [memory_id], "note_refs": ["note_sections"]}, ensure_ascii=False),
                created_at="2026-11-26T09:00:00+08:00",
                request_id="dream_sections",
            )],
        )
        dream = manager._dream_agent_user_prompt(batch=dream_batch, snapshot_text=manager.snapshot_text(), repair_reason="")
        for prompt in (ordinary, dream):
            assert '"section_key": "A"' in prompt
            assert '"status": "unknown"' in prompt
    finally:
        manager.close()
