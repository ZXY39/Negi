from __future__ import annotations

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


def _add(
    manager,
    body: str,
    *,
    source: str = "self",
    note_ref: str = "",
    created_at: str = "2026-11-20T10:00:00+08:00",
    updated_at: str = "2026-11-20T10:00:00+08:00",
) -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory=body,
        source=source,
        from_user=source == "user",
        note_ref=note_ref,
        now_iso=created_at,
    )
    if updated_at != created_at:
        conn = manager._memory_repo._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE memories SET updated_at = ? WHERE memory_id = ?",
                    (updated_at, row["memory_id"]),
                )
        finally:
            conn.close()
    return str(row["memory_id"])


def _paint(manager) -> None:
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-11-26T09:00:00+08:00")


def _batch(op: str = "write") -> object:
    module = _module()
    return module.MemoryBatch(
        op=op,
        items=[
            module.MemoryQueueRequest(
                op=op, decision_source="user", payload_text="resolve conflict",
                created_at="2026-11-26T09:00:00+08:00", request_id="authority",
            )
        ],
    )


def _dream_batch(memory_id: str) -> object:
    module = _module()
    return module.MemoryBatch(
        op="dream",
        items=[
            module.MemoryQueueRequest(
                op="dream", decision_source="self",
                payload_text=json.dumps(
                    {
                        "mode": "full",
                        "scope_key": "full",
                        "memory_ids": [memory_id],
                        "note_refs": ["note_if"],
                        "base_revision": 0,
                    },
                    ensure_ascii=False,
                ),
                created_at="2026-11-26T09:00:00+08:00", request_id="authority_dream",
            )
        ],
    )


def _session(manager, op: str = "write"):
    module = _module()
    return module._MemoryToolSession(
        snapshot_text=manager.snapshot_text(), notes_dir=manager.notes_dir,
        index_dir=manager.index_dir, batch_op=op,
        owned_note_refs_fn=lambda: set(manager._memory_repo.owned_note_refs()),
    )


def _tools(manager, session):
    return {tool.name: tool for tool in manager._memory_agent_tools(session)}


def _reason(line: int, replacement_line: int, basis: str = "explicit_supersession") -> dict:
    return {
        "line": line,
        "reason": "replacement keeps the full rule and scope",
        "replacement_line": replacement_line,
        "authority_basis": basis,
        "scope_match": True,
        "evidence": "old rule explicitly superseded by the replacement",
    }


def test_row_card_is_shared_by_ordinary_dream_and_read_entry(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(
            manager, "完整正文：只在 IF 线系列使用这套做法，例外保留。",
            source="user", note_ref="note_if",
            created_at="2026-11-23T01:04:59+08:00", updated_at="2026-11-23T15:34:59+08:00",
        )
        (manager.notes_dir / "note_if.md").write_text("IF detail\n", encoding="utf-8")
        manager._memory_repo.mark_compressed(memory_id)
        _paint(manager)
        ordinary = manager._memory_agent_user_prompt(batch=_batch(), snapshot_text=manager.snapshot_text(), repair_reason="")
        dream = manager._dream_agent_user_prompt(batch=_dream_batch(memory_id), snapshot_text=manager.snapshot_text(), repair_reason="")
        session = _session(manager, "dream")
        entry = _tools(manager, session)["memory_read_entry"].invoke({"line": 1})

        for text in (ordinary, dream):
            assert f"1 {memory_id} user created_at=2026-11-23 01:04 updated_at=2026-11-23 15:34" in text
            assert "memory_body: 完整正文：只在 IF 线系列使用这套做法，例外保留。 (note: note_if)" in text
            assert '"minimal_memory":' not in text
            assert "is_compressed" not in text
        assert entry["created_at"] == "2026-11-23 01:04"
        assert entry["updated_at"] == "2026-11-23 15:34"
        assert entry["memory_body"].endswith("(note: note_if)")
        assert "is_compressed" not in entry and "minimal_memory" not in entry
    finally:
        manager.close()


def test_row_card_does_not_duplicate_a_note_address_already_in_body(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "处理 IF 线时读 note_if 的完整判据。", note_ref="note_if")
        _paint(manager)
        row = next(item for item in manager._memory_repo.list_memories() if item["memory_id"] == memory_id)
        card = manager._memory_entry_view(row, line=1)["card"]
        assert card.count("note_if") == 1
        assert "(note: note_if)" not in card
    finally:
        manager.close()


def test_ordinary_prompt_falls_back_to_related_full_cards_without_summaries(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "IF 线系列只在竖版渠道使用，例外保留。", created_at="2026-11-18T10:00:00+08:00")
        _add(manager, "完全无关的厨房收纳偏好。", created_at="2026-11-19T10:00:00+08:00")
        _add(manager, "另一个无关的会议记录规则。", created_at="2026-11-20T10:00:00+08:00")
        _paint(manager)
        monkeypatch.setattr(manager, "_memory_window_split_target_tokens", lambda ratio=0.5: 80)
        batch = _batch()
        batch.items[0].payload_text = "整理 IF 线系列的竖版渠道规则"
        prompt = manager._memory_agent_user_prompt(batch=batch, snapshot_text=manager.snapshot_text(), repair_reason="")
        assert "只展开候选与相关行" in prompt
        assert "未展开行号" in prompt
        assert "memory_body: IF 线系列只在竖版渠道使用，例外保留。" in prompt
        assert "memory_body: 完全无关的厨房收纳偏好。" not in prompt
        assert '"minimal_memory":' not in prompt
    finally:
        manager.close()


def test_delete_without_reason_is_refused(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "old rule")
        _add(manager, "new rule")
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)
        assert "delete_reasons" in tools["memory_apply_batch"].parameters["properties"]
        result = tools["memory_apply_batch"].invoke({"deletes": [1]})
        assert result["ok"] is False
        assert "delete_reasons" in json.dumps(result["errors"], ensure_ascii=False)
    finally:
        manager.close()


def test_explicit_supersession_builds_deletion_impact_and_ops_reason(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        old_id = _add(manager, "旧规则：所有渠道都用 A。", source="user", created_at="2026-11-19T10:00:00+08:00")
        new_id = _add(manager, "新规则：仅 IF 线渠道用 A，其他渠道用 B。", source="user", note_ref="note_new", created_at="2026-11-20T10:00:00+08:00")
        (manager.notes_dir / "note_new.md").write_text("new detail\n", encoding="utf-8")
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)
        staged = tools["memory_apply_batch"].invoke(
            {"deletes": [1], "delete_reasons": [_reason(1, 2)]}
        )
        assert staged["ok"] is True, staged
        impact = staged["preview"]["deletion_impact"][0]
        assert impact["target_memory_id"] == old_id
        assert impact["replacement_memory_id"] == new_id
        assert impact["target_body"] == "旧规则：所有渠道都用 A。"
        assert impact["replacement_body"] == "新规则：仅 IF 线渠道用 A，其他渠道用 B。"
        assert impact["replacement_note_refs"] == ["note_new"]
        assert impact["scope_hints"]["target"]
        assert tools["memory_apply_confirm"].invoke({})["status"] == "batch_confirmed"
        validated = manager._validate_candidate_state(
            batch=_batch(), before_text=manager.snapshot_text(), session=session,
        )
        assert validated.delete_reasons[old_id]["delete_reason"] == "replacement keeps the full rule and scope"
        manager._commit_validated_write(validated)
        manager._append_terminal_history(
            batch=_batch(), status="applied", op="write",
            processed_at="2026-11-26T09:30:00+08:00", validated=validated,
        )
        ops = [json.loads(line) for line in manager.ops_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        delete = next(change for change in ops[-1]["changes"] if change["type"] == "delete")
        assert delete["memory_id"] == old_id
        assert delete["delete_reason"] == "replacement keeps the full rule and scope"
        assert delete["authority_basis"] == "explicit_supersession"
        assert delete["replacement_memory_id"] == new_id
    finally:
        manager.close()


def test_newer_timestamp_is_checked_against_stored_times(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "older", created_at="2026-11-19T10:00:00+08:00", updated_at="2026-11-20T10:00:00+08:00")
        _add(manager, "newer", created_at="2026-11-20T10:00:00+08:00", updated_at="2026-11-21T10:00:00+08:00")
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)
        accepted = tools["memory_apply_batch"].invoke(
            {"deletes": [1], "delete_reasons": [_reason(1, 2, "newer_timestamp")]}
        )
        assert accepted["ok"] is True, accepted
        rejected = tools["memory_apply_batch"].invoke(
            {"deletes": [2], "delete_reasons": [_reason(2, 1, "newer_timestamp")]}
        )
        assert rejected["ok"] is False
        assert "newer_timestamp" in json.dumps(rejected["errors"], ensure_ascii=False)
    finally:
        manager.close()


def test_scope_match_and_replacement_are_machine_checked(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "old")
        _add(manager, "new")
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)
        bad_scope = _reason(1, 2)
        bad_scope["scope_match"] = False
        refused = tools["memory_apply_batch"].invoke({"deletes": [1], "delete_reasons": [bad_scope]})
        assert refused["ok"] is False
        assert "scope_match" in json.dumps(refused["errors"], ensure_ascii=False)

        missing_replacement = _reason(1, 99)
        refused = tools["memory_apply_batch"].invoke({"deletes": [1], "delete_reasons": [missing_replacement]})
        assert refused["ok"] is False
        assert "replacement" in json.dumps(refused["errors"], ensure_ascii=False)
    finally:
        manager.close()


def test_dream_rejects_operator_request_and_accepts_any_replacement(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        first = _add(manager, "cluster old", created_at="2026-11-18T10:00:00+08:00")
        second = _add(manager, "cluster new", created_at="2026-11-19T10:00:00+08:00")
        outside = _add(manager, "outside", created_at="2026-11-20T10:00:00+08:00")
        _paint(manager)
        session = _session(manager, "dream")
        tools = _tools(manager, session)
        operator = _reason(1, 2, "operator_request")
        refused = tools["memory_apply_batch"].invoke({"deletes": [1], "delete_reasons": [operator]})
        assert refused["ok"] is False
        assert "operator_request" in json.dumps(refused["errors"], ensure_ascii=False)

        outside_reason = _reason(1, 3)
        accepted = tools["memory_apply_batch"].invoke({"deletes": [1], "delete_reasons": [outside_reason]})
        assert accepted["ok"] is True, accepted
        assert accepted["preview"]["deletion_impact"][0]["target_memory_id"] == first
        assert accepted["preview"]["deletion_impact"][0]["replacement_memory_id"] == outside
        assert second in {row["memory_id"] for row in manager._memory_repo.list_memories()}
    finally:
        manager.close()


def test_rewrite_keeps_minimal_when_unchanged_and_falls_back_when_changed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "same body with enough length")
        manager._memory_repo.apply_mutation(
            adds=[], rewrites=[{"id": memory_id, "content": "same body with enough length", "minimal_memory": "same->summary"}],
            deletes=[], inspired_memory_ids=[], now_iso="2026-11-21T10:00:00+08:00",
        )
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)
        kept = tools["memory_apply_batch"].invoke(
            {"rewrites": [{"line": 1, "content": "same body with enough length", "note_ref": "note_same"}],
             "note_upserts": {"note_same": "detail\n"}}
        )
        assert kept["ok"] is True, kept
        assert tools["memory_apply_confirm"].invoke({})["ok"] is True
        kept_validated = manager._validate_candidate_state(
            batch=_batch(), before_text=manager.snapshot_text(), session=session,
        )
        assert kept_validated.rewrites[0]["minimal_memory"] == "same->summary"

        changed = tools["memory_apply_batch"].invoke(
            {"rewrites": [{"line": 1, "content": "changed body"}]}
        )
        assert changed["ok"] is True, changed
        assert tools["memory_apply_confirm"].invoke({})["ok"] is True
        changed_validated = manager._validate_candidate_state(
            batch=_batch(), before_text=manager.snapshot_text(), session=session,
        )
        assert changed_validated.rewrites[0]["minimal_memory"] == "changed body"
    finally:
        manager.close()


def test_change_preview_names_delete_reason(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "old")
        _add(manager, "new")
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)
        staged = tools["memory_apply_batch"].invoke(
            {"deletes": [1], "delete_reasons": [_reason(1, 2)]}
        )
        assert staged["ok"] is True
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(), session=session, batch=_batch(),
        )
        assert "删除" in manager._build_change_preview(validated)
        assert "replacement keeps the full rule and scope" in manager._build_change_preview(validated)
    finally:
        manager.close()
