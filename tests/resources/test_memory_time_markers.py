"""记忆时间标记这条道：退场不是修改、压缩不是修改；归并载荷只编号、日期由后端盖；常驻块形状不动。"""
from __future__ import annotations

import importlib
from pathlib import Path


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _manager(tmp_path: Path):
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.document.summary_max_chars = 300
    cfg.document.document_max_chars = 20000
    cfg.document.compress_trigger_chars = 16000
    cfg.document.compress_target_chars = 13000
    return _module().MemoryManager(tmp_path, cfg)


def _add(manager, *, body: str, minimal: str, now_iso: str) -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body, minimal_memory=minimal, source="self", from_user=False, now_iso=now_iso
    )
    return str(row["memory_id"])


def _row(manager, memory_id: str) -> dict:
    return {str(r["memory_id"]): r for r in manager._memory_repo.list_memories()}[memory_id]


def test_retiring_a_memory_is_not_a_modification(tmp_path: Path) -> None:
    """退场只写 consolidated_into：改了时间，note 里就没法印出归并前那次真实修改时间。"""
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, body="回信先给结论再给依据" + "细" * 40, minimal="回信->结论前置",
                         now_iso="2026-04-25T00:00:00+08:00")
        manager._memory_repo.apply_mutation(
            adds=[],
            rewrites=[{"id": memory_id, "content": "回信先给结论，结论不超过两句" + "粗" * 30, "minimal_memory": "回信->结论两句"}],
            deletes=[],
            inspired_memory_ids=[],
            now_iso="2026-04-26T09:00:00+08:00",
        )
        updated = str(_row(manager, memory_id)["updated_at"])
        assert updated.startswith("2026-04-26"), f"夹具要先证 rewrite 确实动了时间，实测 {updated}"

        retired = manager._memory_repo.mark_consolidated([memory_id], category_ref="note_output_standards")

        assert retired == [memory_id]
        row = _row(manager, memory_id)
        assert str(row["consolidated_into"]) == "note_output_standards"
        assert str(row["updated_at"]) == updated, "归并不是修改，updated_at 必须留在归并前那一次"
        assert str(row["created_at"]).startswith("2026-04-25"), "创建时间也不许被动"
    finally:
        manager.close()


def test_compression_alone_leaves_the_modification_time_alone(tmp_path: Path) -> None:
    """回归护栏（在基线上就应成立）：降级只换渲染，不改内容，也就不是修改。"""
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, body="引用数字要带来源与统计口径" + "实" * 40, minimal="数字->带口径",
                         now_iso="2026-04-25T00:00:00+08:00")
        before = str(_row(manager, memory_id)["updated_at"])

        assert manager._memory_repo.mark_compressed(memory_id) is True
        assert str(_row(manager, memory_id)["updated_at"]) == before

        assert manager._memory_repo.mark_uncompressed(memory_id) is True
        assert str(_row(manager, memory_id)["updated_at"]) == before, "回暖同样不是内容修改"
    finally:
        manager.close()


def test_the_consolidation_payload_numbers_rows_and_the_backend_stamps_the_date(tmp_path: Path) -> None:
    """归并载荷只给编号：时间不靠模型照抄，退场那行由后端按这一行的真实修改日盖上去。"""
    manager = _manager(tmp_path)
    try:
        memory_id = _add(manager, body="改完代码贴受影响文件清单" + "单" * 60, minimal="改完->贴清单",
                         now_iso="2026-04-21T00:00:00+08:00")
        rewritten = "改完代码要贴本次影响的文件清单" + "单" * 60
        manager._memory_repo.apply_mutation(
            adds=[],
            rewrites=[{"id": memory_id, "content": rewritten, "minimal_memory": "改完->贴清单"}],
            deletes=[],
            inspired_memory_ids=[],
            now_iso="2026-04-27T00:00:00+08:00",
        )
        manager._memory_repo.mark_compressed(memory_id)
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-28T00:00:00+08:00")

        payload, picked = manager._build_consolidation_payload(trigger=16000, target=1)

        assert picked == [memory_id]
        assert "1) " in payload, payload[:400]
        assert "创建" not in payload and "修改" not in payload, "两枚时间都不再交给模型抄"
        assert memory_id not in payload, "编号是它唯一能指回去的把手"

        category = "note_work_habits"
        manager.index_dir.mkdir(parents=True, exist_ok=True)
        note_path = manager.index_dir / f"{category}.md"
        note_path.write_text(f"工作习惯\n\n- {rewritten}\n", encoding="utf-8")

        stamps = manager.stamp_retired_note_lines([{"memory_id": memory_id, "note_ref": category}])

        assert stamps == {"stamped": 1, "unmatched": 0}
        assert "- 260427:" in note_path.read_text(encoding="utf-8"), "盖的是归并前那一次真实修改日"
    finally:
        manager.close()


def test_the_resident_block_shape_does_not_change(tmp_path: Path) -> None:
    """常驻块是 prompt cache 载体：时间标记只进归并载荷与 note，一个都不进常驻块。"""
    manager = _manager(tmp_path)
    try:
        _add(manager, body="交付前自己按验收方的读法读一遍" + "读" * 60, minimal="交付前->读者读法",
             now_iso="2026-04-25T00:00:00+08:00")
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-28T00:00:00+08:00")

        snapshot = manager.snapshot_text()

        assert "创建" not in snapshot, "常驻块不加创建时间"
        assert "｜修改" not in snapshot
    finally:
        manager.close()
