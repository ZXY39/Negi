"""记忆时间标记这条道：退场不是修改、压缩不是修改；归并载荷带两枚时间；常驻块形状不动。"""
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


def test_the_consolidation_payload_carries_both_dates_per_row(tmp_path: Path) -> None:
    """归并 agent 要照抄这两枚时间进 note，看不到就抄不出来。"""
    manager = _manager(tmp_path)
    try:
        first = _add(manager, body="拆任务每个子任务都带验收标准" + "清" * 60, minimal="拆任务->带验收",
                     now_iso="2026-04-20T00:00:00+08:00")
        second = _add(manager, body="改完代码贴受影响文件清单" + "单" * 60, minimal="改完->贴清单",
                      now_iso="2026-04-21T00:00:00+08:00")
        manager._memory_repo.apply_mutation(
            adds=[],
            rewrites=[{"id": second, "content": "改完代码要贴本次影响的文件清单" + "单" * 60, "minimal_memory": "改完->贴清单"}],
            deletes=[],
            inspired_memory_ids=[],
            now_iso="2026-04-27T00:00:00+08:00",
        )
        for memory_id in (first, second):
            manager._memory_repo.mark_compressed(memory_id)
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-28T00:00:00+08:00")

        payload, picked = manager._build_consolidation_payload(trigger=16000, target=1)

        assert set(picked) == {first, second}
        assert f"- id:{first}｜创建 2026/4/20｜修改 2026/4/20" in payload, payload[:400]
        assert f"- id:{second}｜创建 2026/4/21｜修改 2026/4/27" in payload, payload[:400]
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
