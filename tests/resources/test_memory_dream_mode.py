from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _cfg(*, trigger_changes: int = 20, min_interval: int = 0):
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.dream.enabled = True
    cfg.dream.trigger_changes = trigger_changes
    cfg.dream.min_interval_seconds = min_interval
    cfg.dream.max_interval_seconds = 0
    cfg.document.compress_trigger_chars = 1_000_000
    cfg.document.compress_target_chars = 900_000
    return cfg


def _manager(tmp_path: Path, *, trigger_changes: int = 20, min_interval: int = 0):
    return _module().MemoryManager(tmp_path, _cfg(trigger_changes=trigger_changes, min_interval=min_interval))


def _add(manager, body: str, *, note_ref: str = "", now_iso: str = "2026-04-25T00:00:00+08:00") -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory=body,
        source="self",
        from_user=False,
        note_ref=note_ref,
        now_iso=now_iso,
    )
    return str(row["memory_id"])


def _paint(manager) -> None:
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:01+08:00")


def _revision(
    manager,
    rev: int,
    *,
    changed_ids: list[str] | None = None,
    note_refs: list[str] | None = None,
    op: str = "write",
):
    return manager._memory_repo.append_revision(
        created_at=f"2026-04-25T00:00:{rev:02d}+08:00",
        op=op,
        added_ids=[],
        changed_ids=list(changed_ids or []),
        retired_ids=[],
        changed_note_refs=list(note_refs or []),
        rendered="",
    )


def _dream_batch(
    manager,
    *,
    memory_ids: list[str] | None = None,
    note_refs: list[str] | None = None,
    reason: str = "manual",
    base_revision: int | None = None,
):
    module = _module()
    scope = manager._dream_full_scope_snapshot()
    payload = {
        "mode": "full",
        "scope_key": "full",
        "memory_ids": list(memory_ids if memory_ids is not None else scope["memory_ids"]),
        "note_refs": list(note_refs if note_refs is not None else scope["note_refs"]),
        "reason": reason,
        "base_revision": manager.latest_memory_revision() if base_revision is None else base_revision,
    }
    return module.MemoryBatch(
        op="dream",
        items=[
            module.MemoryQueueRequest(
                op="dream",
                decision_source="self",
                payload_text=json.dumps(payload, ensure_ascii=False),
                created_at="2026-04-25T00:00:10+08:00",
                request_id="dream_test",
                trigger_source="dream_scheduler",
            )
        ],
    )


def _dream_session(manager):
    return _module()._MemoryToolSession(
        snapshot_text=manager.snapshot_text(),
        notes_dir=manager.notes_dir,
        index_dir=manager.index_dir,
        batch_op="dream",
        owned_note_refs_fn=lambda: set(manager._memory_repo.owned_note_refs()),
    )


def _write_batch():
    module = _module()
    return module.MemoryBatch(
        op="write",
        items=[
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="整理记忆",
                created_at="2026-04-25T00:00:00+08:00",
                request_id="write_test",
            )
        ],
    )


def test_dream_lane_is_single_row() -> None:
    assert _module()._memory_request_lane(op="dream", trigger_source="dream_scheduler") == "dream"


def test_dream_config_defaults_are_disabled_and_change_thresholded() -> None:
    cfg = importlib.import_module("g3ku.config.schema").MemoryToolsConfig()
    assert cfg.dream.enabled is False
    assert cfg.dream.trigger_changes == 20
    assert cfg.dream.max_interval_seconds == 0
    assert not hasattr(cfg.dream, "cluster_window_ratio")


def test_dream_round_limit_is_twice_the_memory_role_limit() -> None:
    manager = _module().MemoryManager.__new__(_module().MemoryManager)
    runtime = SimpleNamespace(get_role_max_iterations=lambda role: 4)
    assert manager._memory_round_limit(runtime, batch_op="write") == 4
    assert manager._memory_round_limit(runtime, batch_op="dream") == 8

    fallback = SimpleNamespace(get_role_max_iterations=lambda role: 0)
    assert manager._memory_round_limit(fallback, batch_op="write") == 8
    assert manager._memory_round_limit(fallback, batch_op="dream") == 16


@pytest.mark.asyncio
async def test_manual_dream_trigger_enqueues_full_sweep_ahead_of_backlog_and_dedupes(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        _add(manager, "规则一")
        _paint(manager)
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="普通请求",
                created_at="2026-04-25T00:00:01+08:00",
                request_id="write_1",
            )
        )

        result = await manager.trigger_manual_dream()

        assert result["status"] == "queued"
        rows = manager._read_queue_requests()
        assert rows[0].op == "dream"
        assert rows[1].request_id == "write_1"
        payload = json.loads(rows[0].payload_text)
        assert payload["mode"] == "full"
        assert payload["scope_key"] == "full"
        assert payload["reason"] == "manual"
        assert rows[0].trigger_source == "dream_cli"

        again = await manager.trigger_manual_dream()
        assert again["status"] == "already_queued"
        assert again["request_id"] == result["request_id"]
        assert len(manager._read_queue_requests()) == 2
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_manual_dream_stays_behind_a_processing_head(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        _add(manager, "规则一")
        _paint(manager)
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="处理中",
                created_at="2026-04-25T00:00:01+08:00",
                request_id="write_processing",
                status="processing",
            )
        )
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="等待中",
                created_at="2026-04-25T00:00:02+08:00",
                request_id="write_pending",
            )
        )

        await manager.trigger_manual_dream()

        rows = manager._read_queue_requests()
        assert rows[0].request_id == "write_processing"
        assert rows[1].op == "dream"
        assert rows[2].request_id == "write_pending"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_cancel_pending_queue_request_drops_it_and_records_terminal(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        _add(manager, "规则一")
        _paint(manager)
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="记住甲",
                created_at="2026-04-25T00:00:01+08:00",
                request_id="write_1",
            )
        )

        result = await manager.cancel_queue_request("write_1", reason="manual-ui")

        assert result["status"] == "cancelled"
        assert result["request_id"] == "write_1"
        assert manager._read_queue_requests() == []
        assert "write_1" in manager._processed_request_ids()
        ops = [json.loads(line) for line in manager.ops_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert ops[-1]["status"] == "discarded"
        assert ops[-1]["discard_reason"] == "operator_cancelled"
        assert ops[-1]["request_ids"] == ["write_1"]
        assert "规则一" in manager.snapshot_text()
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_cancel_processing_request_flags_cooperative_abort_and_missing_raises(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="处理中",
                created_at="2026-04-25T00:00:01+08:00",
                request_id="write_processing",
                status="processing",
            )
        )

        result = await manager.cancel_queue_request("write_processing")

        assert result["status"] == "cancel_requested"
        rows = manager._read_queue_requests()
        assert len(rows) == 1
        assert rows[0].cancel_requested is True
        assert manager._batch_cancel_requested(["write_processing"]) is True
        assert manager._batch_cancel_requested(["other"]) is False

        with pytest.raises(KeyError):
            await manager.cancel_queue_request("missing")
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_finalize_cancelled_batch_records_terminal_without_commit(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        _add(manager, "规则一")
        _paint(manager)
        row = module.MemoryQueueRequest(
            op="write",
            decision_source="self",
            payload_text="处理中",
            created_at="2026-04-25T00:00:01+08:00",
            request_id="write_processing",
            status="processing",
            cancel_requested=True,
        )
        await manager._append_queue_request(row)
        manager._note_live_round(["write_processing"], {"input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 1})
        batch = module.MemoryBatch(op="write", items=[row])

        report = manager._finalize_cancelled_batch(
            batch,
            {"usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 1}, "attempt_count": 1},
            now_iso="2026-11-27T09:00:09+08:00",
        )

        assert report["status"] == "cancelled"
        assert manager._read_queue_requests() == []
        assert "write_processing" in manager._processed_request_ids()
        assert manager._live_batch_progress == {}
        ops = [json.loads(line) for line in manager.ops_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert ops[-1]["discard_reason"] == "operator_cancelled"
        assert "规则一" in manager.snapshot_text()
    finally:
        manager.close()


def test_queue_projection_exposes_live_progress_for_processing_rows(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        manager._live_batch_progress["write_1"] = {
            "rounds": 3,
            "usage": {"input_tokens": 100, "output_tokens": 40, "cache_read_tokens": 60},
        }
        processing = manager._project_queue_row_for_read(
            {"request_id": "write_1", "op": "write", "status": "processing", "payload_text": "x"}
        )
        assert processing["live_progress"]["rounds"] == 3
        assert processing["live_progress"]["usage"]["input_tokens"] == 100
        pending = manager._project_queue_row_for_read(
            {"request_id": "write_1", "op": "write", "status": "pending", "payload_text": "x"}
        )
        assert "live_progress" not in pending
    finally:
        manager.close()


def test_dream_prompt_uses_the_ordinary_row_cards_notes_and_no_cluster_blocks(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        first = _add(manager, "完整正文：先读取 note，再执行部署；旧指针 note_missing。", note_ref="note_deploy")
        _add(manager, "无 note 的另一条规则。", now_iso="2026-04-25T00:00:00+08:00")
        (manager.notes_dir / "note_deploy.md").write_text("部署触发条件\n\n## 步骤\n先读取配置。\n", encoding="utf-8")
        (manager.notes_dir / "note_spare.md").write_text("未被任何行引用的 note\n", encoding="utf-8")
        _paint(manager)
        batch = _dream_batch(manager)

        prompt = manager._dream_agent_user_prompt(batch=batch, snapshot_text=manager.snapshot_text(), repair_reason="")
        ordinary = manager._memory_agent_user_prompt(
            batch=_write_batch(), snapshot_text=manager.snapshot_text(), repair_reason=""
        )
        dream_blocks = manager._memory_context_blocks(
            batch=batch,
            snapshot_text=manager.snapshot_text(),
            request_text="",
            include_request_specific_note_sections=False,
            full_rows=True,
        )
        ordinary_blocks = manager._memory_context_blocks(
            batch=_write_batch(),
            snapshot_text=manager.snapshot_text(),
            request_text="整理记忆",
            include_request_specific_note_sections=True,
        )

        assert "Dream 全量整理" in prompt
        assert "本轮没有新的待处理记忆请求" in prompt
        assert "scope_key：full" in prompt
        assert "触发原因：manual" in prompt
        assert "1 " in prompt and "created_at=" in prompt and "updated_at=" in prompt
        assert first in prompt
        assert "完整正文：先读取 note，再执行部署；旧指针 note_missing。" in prompt
        assert "无 note 的另一条规则。" in prompt
        assert '"note_deploy"' in prompt and '"note_spare"' in prompt
        assert "Dream cluster" not in prompt
        assert "相关 note 索引" not in prompt
        assert "doctor findings" not in prompt
        assert "cluster 外" not in prompt
        assert dream_blocks["row_text"] == ordinary_blocks["row_text"]
        assert dream_blocks["note_refs"] == ordinary_blocks["note_refs"]
        assert dream_blocks["selective"] is False and dream_blocks["omitted_lines"] == []
        assert "当前记忆完整行卡" in ordinary
    finally:
        manager.close()


def test_dream_system_prompt_is_separate_from_write_prompt(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        assert manager._memory_agent_system_prompt(batch_op="dream") != manager._memory_agent_system_prompt(batch_op="write")
        assert "noop_reason" in manager._memory_agent_system_prompt(batch_op="dream")
    finally:
        manager.close()


def test_dream_read_tools_expose_full_entry_note_index_and_batch_reads(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "完整正文", note_ref="note_a")
        (manager.notes_dir / "note_a.md").write_text("note A body\n", encoding="utf-8")
        (manager.notes_dir / "note_b.md").write_text("note B body\n", encoding="utf-8")
        (manager.index_dir / "note_category.md").write_text("分类 scope\n", encoding="utf-8")
        manager._memory_repo.mark_compressed(memory_id)
        _paint(manager)
        session = _dream_session(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}

        entry = tools["memory_read_entry"].invoke({"line": 1})
        index = tools["memory_note_index"].invoke({})
        notes = tools["memory_read_notes"].invoke({"refs": ["note_a", "note_missing", "note_b"]})

        assert entry["memory_id"] == memory_id
        assert "is_compressed" not in entry
        assert "minimal_memory" not in entry
        assert entry["memory_body"] == "完整正文 (note: note_a)"
        assert entry["created_at"] == "2026-04-25 00:00"
        assert {item["ref"] for item in index} >= {"note_a", "note_b", "note_category"}
        assert next(item for item in index if item["ref"] == "note_category")["kind"] == "category"
        assert notes["notes"]["note_a"] == "note A body\n"
        assert notes["notes"]["note_b"] == "note B body\n"
        assert "note_missing" in notes["errors"]
    finally:
        manager.close()


def test_dream_allows_merge_adds_and_rejects_already_satisfied(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "旧规则甲", note_ref="note_merge", now_iso="2026-04-24T00:00:00+08:00")
        _add(manager, "可保留的替换规则", now_iso="2026-04-25T00:00:00+08:00")
        _paint(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(_dream_session(manager))}

        staged = tools["memory_apply_batch"].invoke(
            {
                "deletes": [1],
                "delete_reasons": [{
                    "line": 1,
                    "reason": "merged into the retained replacement",
                    "replacement_line": 2,
                    "authority_basis": "duplicate",
                    "scope_match": True,
                    "evidence": "same scope and trigger",
                }],
                "adds": [
                    {
                        "content": "合并后的唯一规则",
                        "minimal_memory": "合并->唯一规则",
                        "decision_source": "self",
                        "note_ref": "note_merge",
                    }
                ],
                "note_upserts": {"note_merge": "合并后的 note\n"},
            }
        )
        assert staged["ok"] is True, staged
        assert staged["status"] == "batch_previewed"

        rejected = tools["memory_apply_batch"].invoke({"already_satisfied": "无需处理"})
        assert rejected["ok"] is False
        assert "already_satisfied" in rejected["errors"]
    finally:
        manager.close()


def test_dream_merges_two_noteless_duplicate_rows_in_one_batch(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "部署前先读配置。", now_iso="2026-04-24T00:00:00+08:00")
        _add(manager, "部署之前需要先读配置。", now_iso="2026-04-25T00:00:00+08:00")
        _paint(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(_dream_session(manager))}

        staged = tools["memory_apply_batch"].invoke(
            {
                "deletes": [1],
                "delete_reasons": [{
                    "line": 1,
                    "reason": "same rule stated twice without notes",
                    "replacement_line": 2,
                    "authority_basis": "duplicate",
                    "scope_match": True,
                    "evidence": "identical trigger and requirement",
                }],
            }
        )

        assert staged["ok"] is True, staged
        assert staged["preview"]["deletion_impact"][0]["target_line"] == 1
        assert staged["preview"]["deletion_impact"][0]["replacement_line"] == 2
    finally:
        manager.close()


def test_dream_can_edit_notes_from_different_families_in_one_batch(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "家族甲规则", note_ref="note_family_a")
        _add(manager, "家族乙规则", note_ref="note_family_b")
        (manager.notes_dir / "note_family_a.md").write_text("甲 note\n", encoding="utf-8")
        (manager.notes_dir / "note_family_b.md").write_text("乙 note\n", encoding="utf-8")
        (manager.notes_dir / "note_unowned.md").write_text("没有行引用的 note\n", encoding="utf-8")
        _paint(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(_dream_session(manager))}

        staged = tools["memory_apply_batch"].invoke(
            {
                "note_edits": {
                    "note_family_a": [{"old_text": "甲 note", "new_text": "甲 note 已整理"}],
                    "note_family_b": [{"old_text": "乙 note", "new_text": "乙 note 已整理"}],
                    "note_unowned": [{"old_text": "没有行引用", "new_text": "仍没有行引用"}],
                }
            }
        )

        assert staged["ok"] is True, staged
        assert {item["note_ref"] for item in staged["preview"]["notes_changed"]} == {
            "note_family_a",
            "note_family_b",
            "note_unowned",
        }
    finally:
        manager.close()


def test_dream_category_delete_accepts_any_existing_category(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "常驻规则")
        retired = _add(manager, "已退场规则")
        (manager.index_dir / "note_outside.md").write_text("外部分类\n", encoding="utf-8")
        manager._memory_repo.mark_consolidated([retired], category_ref="note_outside")
        _paint(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(_dream_session(manager))}

        staged = tools["memory_apply_batch"].invoke({"category_deletes": ["note_outside"]})

        assert staged["ok"] is True, staged
        assert staged["preview"]["purged_categories"][0]["note_ref"] == "note_outside"
    finally:
        manager.close()


def test_dream_rejects_operator_request_deletes(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "旧规则", now_iso="2026-04-24T00:00:00+08:00")
        _add(manager, "新规则", now_iso="2026-04-25T00:00:00+08:00")
        _paint(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(_dream_session(manager))}

        refused = tools["memory_apply_batch"].invoke(
            {
                "deletes": [1],
                "delete_reasons": [{
                    "line": 1,
                    "reason": "operator asked",
                    "replacement_line": 2,
                    "authority_basis": "operator_request",
                    "scope_match": True,
                }],
            }
        )

        assert refused["ok"] is False
        assert "operator_request" in json.dumps(refused["errors"], ensure_ascii=False)
    finally:
        manager.close()


def test_dream_delete_still_requires_reasons_and_real_timestamps(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "older", now_iso="2026-04-24T00:00:00+08:00")
        _add(manager, "newer", now_iso="2026-04-25T00:00:00+08:00")
        _paint(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(_dream_session(manager))}

        missing = tools["memory_apply_batch"].invoke({"deletes": [1]})
        assert missing["ok"] is False
        assert "delete_reasons" in json.dumps(missing["errors"], ensure_ascii=False)

        backwards = tools["memory_apply_batch"].invoke(
            {
                "deletes": [2],
                "delete_reasons": [{
                    "line": 2,
                    "reason": "supposedly newer",
                    "replacement_line": 1,
                    "authority_basis": "newer_timestamp",
                    "scope_match": True,
                }],
            }
        )
        assert backwards["ok"] is False
        assert "newer_timestamp" in json.dumps(backwards["errors"], ensure_ascii=False)
    finally:
        manager.close()


def test_note_anchor_edits_unique_line_and_heading_section(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "拥有 note", note_ref="note_anchor")
        note = manager.notes_dir / "note_anchor.md"
        note.write_text("# 标题\n\n## 旧小节\n旧内容\n\n## 保留小节\n保留内容\n", encoding="utf-8")
        _paint(manager)
        batch = _dream_batch(manager, memory_ids=[memory_id], note_refs=["note_anchor"])
        session = _dream_session(manager)
        staged = session.apply_batch(
            note_edits={
                "note_anchor": [
                    {"anchor": "## 旧小节", "new_text": "## 新小节\n新内容"},
                    {"anchor": "保留内容", "new_text": "仍然保留"},
                ]
            }
        )
        assert staged["ok"] is True, staged
        session.confirm_batch()
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(), session=session, batch=batch
        )
        manager._commit_validated_write(validated)

        body = note.read_text(encoding="utf-8")
        assert "## 新小节\n新内容" in body
        assert "旧内容" not in body
        assert "仍然保留" in body
        assert validated.note_edit_results[0]["anchor"] == "## 旧小节"
    finally:
        manager.close()


def test_note_anchor_preview_shows_section_scope_before_confirm(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "拥有 note", note_ref="note_preview")
        (manager.notes_dir / "note_preview.md").write_text(
            "# 一级标题\n\n## 小节甲\n甲内容\n\n## 小节乙\n乙内容\n", encoding="utf-8"
        )
        _paint(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(_dream_session(manager))}

        staged = tools["memory_apply_batch"].invoke(
            {
                "note_edits": {
                    "note_preview": [
                        {"anchor": "# 一级标题", "new_text": "# 新标题"},
                        {"anchor": "## 小节甲", "new_text": "替换甲"},
                    ]
                }
            }
        )

        assert staged["ok"] is True, staged
        previews = staged["preview"]["note_edit_preview"]
        assert previews[0]["anchor_scope_lines"] == 7
        assert previews[0]["body_chars_before"] > previews[0]["body_chars_after"]
        assert previews[1]["applied"] is False
        assert "anchor not found" in previews[1]["error"]
        assert any("failed op" in warning for warning in staged["warnings"])
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_idle_scheduler_does_not_rewrite_unchanged_state(tmp_path: Path) -> None:
    manager = _manager(tmp_path, trigger_changes=20)
    try:
        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00") is None
        assert not manager.dream_state_file.exists()

        state = manager._read_dream_state()
        state["pending_memory_ids"] = ["stale1"]
        state["pending_since"] = "2026-04-25T00:00:00+08:00"
        manager._write_dream_state(state)
        before = manager.dream_state_file.stat().st_mtime_ns
        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:03+08:00") is None
        assert manager.dream_state_file.stat().st_mtime_ns == before
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_scheduler_enqueues_one_full_sweep_with_every_resident_row_and_note(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, trigger_changes=2)
    try:
        first = _add(manager, "规则一")
        second = _add(manager, "规则二", note_ref="note_live")
        third = _add(manager, "规则三", note_ref="note_other")
        retired = _add(manager, "已退场")
        (manager.notes_dir / "note_live.md").write_text("note live\n", encoding="utf-8")
        (manager.notes_dir / "note_other.md").write_text("note other\n", encoding="utf-8")
        (manager.notes_dir / "note_orphan.md").write_text("orphan note\n", encoding="utf-8")
        (manager.index_dir / "note_category.md").write_text("category note\n", encoding="utf-8")
        manager._memory_repo.mark_consolidated([retired], category_ref="note_category")
        _paint(manager)
        _revision(manager, 1, changed_ids=[first, first])
        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00") is None

        _revision(manager, 2, changed_ids=[second])
        queued = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:05+08:00")
        assert queued is not None
        rows = await manager.list_queue(limit=10)
        assert len(rows) == 1
        assert rows[0]["op"] == "dream"
        assert rows[0]["trigger_source"] == "dream_scheduler"
        payload = json.loads(queued.payload_text)
        assert payload["mode"] == "full"
        assert payload["scope_key"] == "full"
        assert payload["reason"] == "change_threshold"
        assert payload["base_revision"] == 2
        assert set(payload["memory_ids"]) == {first, second, third}
        assert retired not in payload["memory_ids"]
        assert set(payload["note_refs"]) == {"note_live", "note_other", "note_orphan", "note_category"}
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_scheduler_waits_for_empty_queue_and_enforces_minimum_interval(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, trigger_changes=1, min_interval=60)
    try:
        memory_id = _add(manager, "规则一")
        _paint(manager)
        _revision(manager, 1, changed_ids=[memory_id])
        await manager._append_queue_request(
            _module().MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="普通请求",
                created_at="2026-04-25T00:00:03+08:00",
                request_id="write_1",
            )
        )
        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:04+08:00") is None
        manager._drop_request_ids({"write_1"})

        first = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:05+08:00")
        assert first is not None
        manager._drop_request_ids({first.request_id})

        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:30+08:00") is None
        _revision(manager, 2, changed_ids=[memory_id])
        second = await manager._maybe_enqueue_dream_locked("2026-04-25T00:01:06+08:00")
        assert second is not None
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_force_enqueues_a_full_sweep_even_with_empty_pending(tmp_path: Path) -> None:
    manager = _manager(tmp_path, trigger_changes=20)
    try:
        _add(manager, "规则一")
        _paint(manager)

        queued = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00", force=True)

        assert queued is not None
        payload = json.loads(queued.payload_text)
        assert payload["mode"] == "full"
        assert payload["reason"] == "manual"
        assert payload["base_revision"] == 0
        assert queued.trigger_source == "dream_cli"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_failed_sweep_does_not_retry_without_new_revisions(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, trigger_changes=1)
    try:
        memory_id = _add(manager, "规则一")
        _paint(manager)
        _revision(manager, 1, changed_ids=[memory_id])
        failed = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00")
        assert failed is not None
        manager._settle_dream_batch(failed, status="protocol: test failure", now_iso="2026-04-25T00:00:03+08:00")
        manager._drop_request_ids({failed.request_id})
        state = manager._read_dream_state()
        assert state["last_error"] == "protocol: test failure"
        assert set(state["pending_memory_ids"]) == {memory_id}

        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:01:03+08:00") is None

        _revision(manager, 2, changed_ids=[memory_id])
        retry = await manager._maybe_enqueue_dream_locked("2026-04-25T00:02:04+08:00")
        assert retry is not None
        assert json.loads(retry.payload_text)["mode"] == "full"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_legacy_dream_state_fields_are_readable_and_ignored(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        manager.dream_state_file.parent.mkdir(parents=True, exist_ok=True)
        manager.dream_state_file.write_text(
            json.dumps({
                "last_seen_revision": 7,
                "pending_memory_ids": ["mem1"],
                "pending_note_refs": ["note_a"],
                "pending_since": "2026-04-25T00:00:00+08:00",
                "round_robin_cursor": 3,
                "last_cluster_key": "note:old",
                "last_error": "",
            }),
            encoding="utf-8",
        )

        state = manager._read_dream_state()

        assert state["last_seen_revision"] == 7
        assert state["pending_memory_ids"] == ["mem1"]
        assert state["pending_note_refs"] == ["note_a"]
        assert "round_robin_cursor" not in state
        assert "last_cluster_key" not in state
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_full_success_replaces_pending_with_in_flight_external_changes(tmp_path: Path) -> None:
    manager = _manager(tmp_path, trigger_changes=1)
    try:
        memory_id = _add(manager, "规则一", note_ref="note_live")
        _paint(manager)
        _revision(manager, 1, changed_ids=[memory_id])
        queued = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00")
        assert queued is not None
        base_revision = int(json.loads(queued.payload_text)["base_revision"])
        _revision(manager, 2, changed_ids=[memory_id], note_refs=["note_live"])

        manager._settle_dream_batch(queued, status="applied", now_iso="2026-04-25T00:00:03+08:00")

        state = manager._read_dream_state()
        assert state["pending_memory_ids"] == [memory_id]
        assert state["pending_note_refs"] == ["note_live"]
        assert state["pending_since"] == "2026-04-25T00:00:02+08:00"
        assert state["last_error"] == ""
        assert state["last_succeeded_at"] == "2026-04-25T00:00:03+08:00"
        assert int(state["last_seen_revision"]) == base_revision
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_dream_own_revision_does_not_enter_pending_or_retrigger(tmp_path: Path) -> None:
    manager = _manager(tmp_path, trigger_changes=1)
    try:
        memory_id = _add(manager, "规则一")
        _paint(manager)
        _revision(manager, 1, changed_ids=[memory_id])
        queued = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00")
        assert queued is not None
        _revision(manager, 2, changed_ids=[memory_id], op="dream")

        manager._settle_dream_batch(queued, status="applied", now_iso="2026-04-25T00:00:03+08:00")
        state = manager._read_dream_state()
        assert state["pending_memory_ids"] == []
        assert state["pending_note_refs"] == []
        assert state["pending_since"] == ""
        manager._drop_request_ids({queued.request_id})

        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:01:03+08:00") is None
        state = manager._read_dream_state()
        assert state["last_seen_revision"] == 2
        assert state["pending_memory_ids"] == []
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_idle_worker_tick_auto_enqueues_and_consumes_threshold_dream(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    manager = _manager(tmp_path, trigger_changes=1)
    try:
        memory_id = _add(manager, "自动整理规则")
        _paint(manager)
        _revision(manager, 1, changed_ids=[memory_id])
        runtime = SimpleNamespace(get_role_model_keys=lambda role: ["memory-primary"] if role == "memory" else [])
        monkeypatch.setattr(module, "get_runtime_config", lambda force=False: (runtime, 1, False), raising=False)

        async def fake_run(**kwargs):
            validated = module._MemoryValidatedWrite(
                document_text=manager.snapshot_text(),
                note_writes={},
                note_refs_written=[],
                memory_chars_after=manager._resident_memory_chars(),
                document_preview=manager.snapshot_text(),
                noop_reason="already tidy",
            )
            return {
                "validated": validated,
                "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
                "attempt_count": 1,
                "model_chain": ["memory-primary"],
                "attempted_model_chain": ["memory-primary"],
                "provider_request_ids": [],
                "request_artifact_paths": [],
                "usage_counted_requests": 0,
            }

        monkeypatch.setattr(manager, "_run_memory_agent_batch", fake_run)
        report = await manager.run_due_batch_once(now_iso="2026-04-25T00:00:10+08:00")

        assert report["status"] == "applied"
        assert report["op"] == "dream"
        assert report["dream_mode"] == "full"
        assert report["dream_scope_key"] == "full"
        assert report["processed"] == 1
        assert await manager.list_queue(limit=10) == []
        state = manager._read_dream_state()
        assert state["pending_memory_ids"] == []
        assert state["last_succeeded_at"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_consumes_dream_and_records_full_metadata(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        _add(manager, "规则一")
        _paint(manager)
        runtime = SimpleNamespace(get_role_model_keys=lambda role: ["memory-primary"] if role == "memory" else [])
        monkeypatch.setattr(module, "get_runtime_config", lambda force=False: (runtime, 1, False), raising=False)

        async def fake_run(**kwargs):
            validated = module._MemoryValidatedWrite(
                document_text=manager.snapshot_text(),
                note_writes={},
                note_refs_written=[],
                memory_chars_after=manager._resident_memory_chars(),
                document_preview=manager.snapshot_text(),
                noop_reason="library is already tidy",
            )
            return {
                "validated": validated,
                "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0},
                "attempt_count": 1,
                "model_chain": ["memory-primary"],
                "attempted_model_chain": ["memory-primary"],
                "provider_request_ids": [],
                "request_artifact_paths": [],
                "usage_counted_requests": 0,
            }

        monkeypatch.setattr(manager, "_run_memory_agent_batch", fake_run)
        queued = await manager.enqueue_dream_request(
            reason="manual",
            trigger_source="dream_cli",
        )
        report = await manager.run_due_batch_once(now_iso="2026-04-25T00:00:10+08:00")

        assert report["status"] == "applied"
        assert report["op"] == "dream"
        assert report["dream_mode"] == "full"
        assert report["dream_scope_key"] == "full"
        assert report["dream_base_revision"] == 0
        assert report["dream_reason"] == "manual"
        assert "dream_cluster_key" not in report
        ops = [json.loads(line) for line in manager.ops_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert ops[-1]["dream_mode"] == "full"
        assert ops[-1]["dream_scope_key"] == "full"
        assert ops[-1]["dream_base_revision"] == 0
        assert "dream_cluster_key" not in ops[-1]
        assert ops[-1]["noop_reason"] == "library is already tidy"
        assert queued["request_id"] == report["request_ids"][0]
        assert manager._read_queue_requests() == []
        assert "规则一" in manager.snapshot_text()
    finally:
        manager.close()


def test_dream_commit_is_recorded_as_a_dream_revision_and_not_pending(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "部署前先读配置。", now_iso="2026-04-24T00:00:00+08:00")
        _add(manager, "部署之前需要先读配置。", now_iso="2026-04-25T00:00:00+08:00")
        _paint(manager)
        batch = _dream_batch(manager)
        session = _dream_session(manager)
        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}
        staged = tools["memory_apply_batch"].invoke(
            {
                "deletes": [1],
                "delete_reasons": [{
                    "line": 1,
                    "reason": "same rule stated twice",
                    "replacement_line": 2,
                    "authority_basis": "duplicate",
                    "scope_match": True,
                    "evidence": "identical trigger and requirement",
                }],
            }
        )
        assert staged["ok"] is True, staged
        tools["memory_apply_confirm"].invoke({})
        validated = manager._validate_candidate_state(
            batch=batch,
            before_text=manager.snapshot_text(),
            session=session,
        )
        manager._commit_validated_write(validated)

        revisions = manager._memory_repo.revisions_after(0)
        assert revisions[-1]["op"] == "dream"
        manager._settle_dream_batch(batch, status="applied", now_iso="2026-04-25T00:00:03+08:00")
        state = manager._read_dream_state()
        assert state["pending_memory_ids"] == []
        assert state["last_error"] == ""
    finally:
        manager.close()


def test_dream_scope_view_full_payload_summarizes_counts(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        resident_id = _add(manager, "resident body", note_ref="note_play_x")
        _add(manager, "another body")
        (manager.notes_dir / "note_play_x.md").write_text("ordinary note", encoding="utf-8")
        _paint(manager)

        payload = json.dumps({
            "mode": "full",
            "scope_key": "full",
            "memory_ids": [resident_id, "second-id"],
            "note_refs": ["note_play_x", "note_cat_y"],
            "reason": "change_threshold",
            "base_revision": 12,
        })
        scope = manager.dream_scope_view(payload)
        assert scope == {
            "mode": "full",
            "scope_key": "full",
            "reason": "change_threshold",
            "base_revision": 12,
            "memory_count": 2,
            "note_count": 2,
        }
        assert "memories" not in scope and "notes" not in scope

        queue_row = manager._project_queue_row_for_read({"op": "dream", "payload_text": payload})
        assert queue_row["dream_scope"]["mode"] == "full"
        processed_row = manager._project_processed_row_for_read({"op": "dream", "payload_texts": [payload]})
        assert processed_row["dream_scope"]["memory_count"] == 2
        failed_row = manager._project_failed_record_for_read({"op": "dream", "items": [{"payload_text": payload}]})
        assert failed_row["dream_scope"]["note_count"] == 2
        assert "dream_scope" not in manager._project_queue_row_for_read({"op": "write", "payload_text": "hi"})

        assert manager.dream_scope_view("not json") is None
        assert manager.dream_scope_view(json.dumps({"memory_ids": []})) is None
    finally:
        manager.close()


def test_legacy_cluster_dream_scope_view_still_resolves_rows_and_notes(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        resident_id = _add(manager, "resident body", note_ref="note_play_x")
        retired_id = _add(manager, "retired body")
        (manager.notes_dir / "note_play_x.md").write_text("ordinary note", encoding="utf-8")
        (manager.index_dir / "note_cat_y.md").write_text("category note", encoding="utf-8")
        manager._memory_repo.mark_consolidated([retired_id], category_ref="note_cat_y")
        _paint(manager)

        payload = json.dumps({
            "cluster_key": "memory:test",
            "memory_ids": [resident_id, retired_id, "deadbeef"],
            "note_refs": ["note_play_x", "note_cat_y", "note_missing"],
            "reason": "recent_revision_family",
            "base_revision": 3,
        })
        scope = manager.dream_scope_view(payload)
        assert scope is not None
        assert scope["cluster_key"] == "memory:test"
        assert scope["reason"] == "recent_revision_family"
        assert scope["base_revision"] == 3
        by_id = {item["memory_id"]: item for item in scope["memories"]}
        assert by_id[resident_id]["line"] == 1
        assert by_id[resident_id]["body"] == "resident body"
        assert by_id[resident_id]["missing"] is False
        assert by_id[resident_id]["retired_into"] == ""
        assert by_id[retired_id]["line"] is None
        assert by_id[retired_id]["retired_into"] == "note_cat_y"
        assert by_id["deadbeef"]["missing"] is True
        notes = {item["ref"]: item for item in scope["notes"]}
        assert notes["note_play_x"]["kind"] == "ordinary" and notes["note_play_x"]["exists"] is True
        assert notes["note_cat_y"]["kind"] == "category" and notes["note_cat_y"]["exists"] is True
        assert notes["note_missing"]["exists"] is False

        queue_row = manager._project_queue_row_for_read({"op": "dream", "payload_text": payload})
        assert queue_row["dream_scope"]["cluster_key"] == "memory:test"
        processed_row = manager._project_processed_row_for_read({"op": "dream", "payload_texts": [payload]})
        assert processed_row["dream_scope"]["cluster_key"] == "memory:test"
        failed_row = manager._project_failed_record_for_read({"op": "dream", "items": [{"payload_text": payload}]})
        assert failed_row["dream_scope"]["cluster_key"] == "memory:test"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_legacy_queued_cluster_dream_runs_as_full_sweep(tmp_path: Path) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        first = _add(manager, "旧 cluster 内规则")
        _add(manager, "旧 cluster 外规则")
        _paint(manager)
        legacy_payload = {
            "cluster_key": "memory:legacy",
            "memory_ids": [first],
            "note_refs": [],
            "reason": "recent_revision_family",
            "base_revision": 0,
        }
        await manager._append_queue_request(
            module.MemoryQueueRequest(
                op="dream",
                decision_source="self",
                payload_text=json.dumps(legacy_payload, ensure_ascii=False),
                created_at="2026-04-25T00:00:02+08:00",
                request_id="dream_legacy",
                trigger_source="dream_scheduler",
            )
        )
        session = _dream_session(manager)
        batch = await manager.collect_due_batch(now_iso="2026-04-25T00:00:03+08:00")
        assert batch is not None and batch.op == "dream"
        prompt = manager._dream_agent_user_prompt(batch=batch, snapshot_text=manager.snapshot_text(), repair_reason="")

        assert "旧 cluster 内规则" in prompt
        assert "旧 cluster 外规则" in prompt
        assert "Dream 全量整理" in prompt

        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}
        staged = tools["memory_apply_batch"].invoke(
            {
                "deletes": [2],
                "delete_reasons": [{
                    "line": 2,
                    "reason": "outside the legacy cluster but same library",
                    "replacement_line": 1,
                    "authority_basis": "duplicate",
                    "scope_match": True,
                    "evidence": "same library sweep covers every resident row",
                }],
            }
        )
        assert staged["ok"] is True, staged
        assert "旧 cluster 外规则" in manager.snapshot_text()
    finally:
        manager.close()
