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


def _revision(manager, rev: int, *, changed_ids: list[str] | None = None, note_refs: list[str] | None = None):
    return manager._memory_repo.append_revision(
        created_at=f"2026-04-25T00:00:{rev:02d}+08:00",
        op="write",
        added_ids=[],
        changed_ids=list(changed_ids or []),
        retired_ids=[],
        changed_note_refs=list(note_refs or []),
        rendered="",
    )


def _dream_batch(manager, *, memory_ids: list[str], note_refs: list[str] | None = None):
    module = _module()
    payload = {
        "cluster_key": "note:note_dream",
        "memory_ids": list(memory_ids),
        "note_refs": list(note_refs or []),
        "reason": "test",
        "base_revision": manager.latest_memory_revision(),
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


def test_dream_lane_is_single_row() -> None:
    assert _module()._memory_request_lane(op="dream", trigger_source="dream_scheduler") == "dream"


def test_dream_config_defaults_are_disabled_and_change_thresholded() -> None:
    cfg = importlib.import_module("g3ku.config.schema").MemoryToolsConfig()
    assert cfg.dream.enabled is False
    assert cfg.dream.trigger_changes == 20
    assert cfg.dream.max_interval_seconds == 0


def test_dream_prompt_contains_full_body_global_line_and_note_index(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "完整正文：先读取 note，再执行部署；旧指针 note_missing。", note_ref="note_deploy")
        note = manager.notes_dir / "note_deploy.md"
        note.write_text("部署触发条件\n\n## 步骤\n先读取配置。\n", encoding="utf-8")
        _paint(manager)
        batch = _dream_batch(manager, memory_ids=[memory_id], note_refs=["note_deploy"])

        prompt = manager._dream_agent_user_prompt(batch=batch, snapshot_text=manager.snapshot_text(), repair_reason="")

        assert "1 " in prompt
        assert "created_at=" in prompt and "updated_at=" in prompt
        assert memory_id in prompt
        assert "完整正文：先读取 note，再执行部署；旧指针 note_missing。" in prompt
        assert "note_deploy" in prompt
        assert "note_missing" in prompt
        assert "dangling_note_mention" in prompt
        assert "owner_count" in prompt
        assert '"kind": "ordinary"' in prompt
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
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
        )
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
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
        )
        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}

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


def test_dream_category_delete_is_limited_to_cluster_categories(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "常驻规则")
        retired = _add(manager, "已退场规则")
        (manager.index_dir / "note_outside.md").write_text("外部分类\n", encoding="utf-8")
        manager._memory_repo.mark_consolidated([retired], category_ref="note_outside")
        _paint(manager)
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
            dream_context_loaded=True,
            dream_note_refs=set(),
        )
        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}

        rejected = tools["memory_apply_batch"].invoke({"category_deletes": ["note_outside"]})

        assert rejected["ok"] is False
        assert "cluster" in json.dumps(rejected.get("errors") or {}).lower()

        (manager.notes_dir / "note_outside_ordinary.md").write_text("外部普通 note\n", encoding="utf-8")
        rejected_note = tools["memory_apply_batch"].invoke(
            {"note_edits": {"note_outside_ordinary": [{"old_text": "外部", "new_text": "改动"}]}}
        )
        assert rejected_note["ok"] is False
        assert "outside this dream cluster" in json.dumps(rejected_note.get("errors") or {})
    finally:
        manager.close()


def test_dream_can_purge_an_existing_category_inside_the_cluster(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, "常驻规则")
        retired = _add(manager, "已退场规则")
        (manager.index_dir / "note_inside.md").write_text("cluster 内分类\n", encoding="utf-8")
        manager._memory_repo.mark_consolidated([retired], category_ref="note_inside")
        _paint(manager)
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
            dream_context_loaded=True,
            dream_note_refs={"note_inside"},
        )
        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}

        staged = tools["memory_apply_batch"].invoke({"category_deletes": ["note_inside"]})

        assert staged["ok"] is True, staged
        assert staged["preview"]["purged_categories"][0]["note_ref"] == "note_inside"
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
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
        )
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


def test_note_anchor_empty_new_text_deletes_only_the_target_section(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "拥有 note", note_ref="note_anchor")
        note = manager.notes_dir / "note_anchor.md"
        note.write_text("# 标题\n\n## 删除小节\n旧内容\n\n## 保留小节\n保留内容\n", encoding="utf-8")
        _paint(manager)
        batch = _dream_batch(manager, memory_ids=[memory_id], note_refs=["note_anchor"])
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
        )
        session.apply_batch(note_edits={"note_anchor": [{"anchor": "## 删除小节", "new_text": ""}]})
        session.confirm_batch()
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(), session=session, batch=batch
        )
        manager._commit_validated_write(validated)

        body = note.read_text(encoding="utf-8")
        assert "删除小节" not in body and "旧内容" not in body
        assert "## 保留小节\n保留内容" in body
    finally:
        manager.close()


def test_note_anchor_zero_and_multiple_hits_are_reported(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "拥有 note", note_ref="note_anchor")
        note = manager.notes_dir / "note_anchor.md"
        note.write_text("重复行\n重复行\n", encoding="utf-8")
        _paint(manager)
        batch = _dream_batch(manager, memory_ids=[memory_id], note_refs=["note_anchor"])
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
        )
        session.apply_batch(
            note_edits={
                "note_anchor": [
                    {"anchor": "不存在", "new_text": "x"},
                    {"anchor": "重复行", "new_text": "x"},
                ]
            }
        )
        session.confirm_batch()
        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(), session=session, batch=batch
        )
        manager._commit_validated_write(validated)

        results = validated.note_edit_results
        assert results[0]["applied"] is False and "not found" in results[0]["error"]
        assert results[1]["applied"] is False and "2" in results[1]["error"]
        assert results[1]["candidates"] == [1, 2]
        assert note.read_text(encoding="utf-8") == "重复行\n重复行\n"
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
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
            dream_context_loaded=True,
            dream_note_refs={"note_preview"},
        )
        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}

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


def test_dream_line_edits_are_limited_to_cluster_rows(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        first = _add(manager, "cluster 内规则")
        second = _add(manager, "cluster 外规则")
        _paint(manager)
        session = _module()._MemoryToolSession(
            snapshot_text=manager.snapshot_text(),
            notes_dir=manager.notes_dir,
            index_dir=manager.index_dir,
            batch_op="dream",
            dream_context_loaded=True,
            dream_memory_ids={first},
            dream_note_refs=set(),
        )
        tools = {tool.name: tool for tool in manager._memory_agent_tools(session)}

        outside_line = manager.memory_line_for_id(second)
        inside_line = manager.memory_line_for_id(first)
        rejected = tools["memory_apply_batch"].invoke({
            "deletes": [outside_line],
            "delete_reasons": [{
                "line": outside_line,
                "reason": "outside cluster",
                "replacement_line": inside_line,
                "authority_basis": "duplicate",
                "scope_match": True,
                "evidence": "same scope",
            }],
        })

        assert rejected["ok"] is False
        assert "outside this dream cluster" in json.dumps(rejected.get("errors") or {})
        assert "cluster 外规则" in manager.snapshot_text()
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
async def test_scheduler_counts_distinct_changes_and_waits_for_empty_queue(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, trigger_changes=2)
    try:
        first = _add(manager, "规则一")
        second = _add(manager, "规则二")
        _paint(manager)
        monkeypatch.setattr(manager, "_memory_window_split_target_tokens", lambda ratio=0.5: 100_000, raising=False)
        _revision(manager, 1, changed_ids=[first, first])
        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00") is None
        assert await manager.list_queue(limit=10) == []

        _revision(manager, 2, changed_ids=[second])
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

        queued = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:05+08:00")
        assert queued is not None
        rows = await manager.list_queue(limit=10)
        assert len(rows) == 1
        assert rows[0]["op"] == "dream"
        assert rows[0]["trigger_source"] == "dream_scheduler"
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_scheduler_enforces_minimum_interval_between_attempts(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, trigger_changes=1, min_interval=60)
    try:
        memory_id = _add(manager, "规则一")
        _paint(manager)
        monkeypatch.setattr(manager, "_memory_window_split_target_tokens", lambda ratio=0.5: 100_000, raising=False)
        _revision(manager, 1, changed_ids=[memory_id])
        first = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00")
        assert first is not None
        manager._drop_request_ids({first.request_id})

        assert await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:30+08:00") is None
        second = await manager._maybe_enqueue_dream_locked("2026-04-25T00:01:03+08:00")
        assert second is not None
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_dream_settlement_keeps_revisions_created_while_in_flight(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, trigger_changes=1)
    try:
        memory_id = _add(manager, "规则一", note_ref="note_live")
        _paint(manager)
        monkeypatch.setattr(manager, "_memory_window_split_target_tokens", lambda ratio=0.5: 100_000, raising=False)
        _revision(manager, 1, changed_ids=[memory_id])
        queued = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00")
        assert queued is not None
        base_revision = int(json.loads(queued.payload_text)["base_revision"])
        _revision(manager, 2, changed_ids=[memory_id], note_refs=["note_live"])

        manager._settle_dream_batch(queued, status="applied", now_iso="2026-04-25T00:00:03+08:00")
        state = manager._read_dream_state()
        assert memory_id in state["pending_memory_ids"]
        assert "note_live" in state["pending_note_refs"]
        assert int(state["last_seen_revision"]) == base_revision
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_success_removes_only_the_processed_cluster_from_pending(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, trigger_changes=2)
    try:
        first = _add(manager, "规则一", now_iso="2026-04-25T00:00:00+08:00")
        second = _add(manager, "规则二", now_iso="2026-04-25T00:00:01+08:00")
        _paint(manager)
        monkeypatch.setattr(manager, "_memory_window_split_target_tokens", lambda ratio=0.5: 100_000, raising=False)
        _revision(manager, 1, changed_ids=[first, second])
        queued = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00")
        assert queued is not None
        payload = json.loads(queued.payload_text)
        assert len(payload["memory_ids"]) == 1

        manager._settle_dream_batch(queued, status="applied", now_iso="2026-04-25T00:00:03+08:00")
        state = manager._read_dream_state()
        remaining = set(state["pending_memory_ids"])
        assert remaining == {first, second} - set(payload["memory_ids"])
        assert state["round_robin_cursor"] == 1
        assert state["last_error"] == ""
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_failed_cluster_does_not_spin_and_scheduler_moves_on(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, trigger_changes=2)
    try:
        first = _add(manager, "规则一", now_iso="2026-04-25T00:00:00+08:00")
        second = _add(manager, "规则二", now_iso="2026-04-25T00:00:01+08:00")
        _paint(manager)
        monkeypatch.setattr(manager, "_memory_window_split_target_tokens", lambda ratio=0.5: 100_000, raising=False)
        _revision(manager, 1, changed_ids=[first, second])
        failed = await manager._maybe_enqueue_dream_locked("2026-04-25T00:00:02+08:00")
        assert failed is not None
        manager._settle_dream_batch(failed, status="protocol: test failure", now_iso="2026-04-25T00:00:03+08:00")
        manager._drop_request_ids({failed.request_id})

        retry = await manager._maybe_enqueue_dream_locked("2026-04-25T00:01:03+08:00")
        assert retry is not None
        assert json.loads(retry.payload_text)["cluster_key"] != json.loads(failed.payload_text)["cluster_key"]
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
        monkeypatch.setattr(manager, "_memory_window_split_target_tokens", lambda ratio=0.5: 100_000, raising=False)

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
        assert report["processed"] == 1
        assert await manager.list_queue(limit=10) == []
        state = manager._read_dream_state()
        assert state["pending_memory_ids"] == []
        assert state["last_succeeded_at"]
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_run_due_batch_consumes_dream_and_records_cluster_metadata(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, "规则一")
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
                noop_reason="cluster is already tidy",
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
            cluster_key="memory:test",
            memory_ids=[memory_id],
            note_refs=[],
            reason="test",
            base_revision=manager.latest_memory_revision(),
            trigger_source="dream_scheduler",
        )
        report = await manager.run_due_batch_once(now_iso="2026-04-25T00:00:10+08:00")

        assert report["status"] == "applied"
        assert report["op"] == "dream"
        assert report["dream_cluster_key"] == "memory:test"
        ops = [json.loads(line) for line in manager.ops_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert ops[-1]["dream_cluster_key"] == "memory:test"
        assert ops[-1]["dream_base_revision"] == 0
        assert ops[-1]["noop_reason"] == "cluster is already tidy"
        assert queued["request_id"] == report["request_ids"][0]
    finally:
        manager.close()
