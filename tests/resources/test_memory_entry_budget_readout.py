"""记忆批次的字符预算读数：超线要报"哪一条、多少字、超多少、搬到哪"，接近上限要先警告。

实盘形状：`_content_exceeds_message` 只印上限，抛出后经 `_memory_batch_preview` 塌成
`errors.batch` 一个字符串——模型既不知道是哪一条，也不知道自己写了多少字；而带 note 的
行和不带 note 的行挨打回的是同一句话。常驻块因此长成"一行装得下一份答案"的形状。
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _session(tmp_path: Path, *, limit: int, snapshot: str = ""):
    mod = _module()
    notes_dir = tmp_path / "memory" / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    return mod._MemoryToolSession(
        snapshot_text=snapshot,
        notes_dir=notes_dir,
        index_dir=tmp_path / "memory" / "index",
        batch_op="write",
        summary_max_chars=limit,
    )


def _add_entry(*, chars: int, note_ref: str = "") -> dict:
    """A content of exactly `chars` code points, plus the required sibling fields."""
    head = "当要在本机核对某项宣传素材口径时，读 note_play_materials："
    body = "细" * max(0, chars - len(head))
    item = {
        "content": (head + body)[:chars],
        "minimal_memory": "核对素材口径->读 note_play_materials",
        "decision_source": "self",
    }
    if note_ref:
        item["note_ref"] = note_ref
    return item


def test_over_limit_add_names_the_entry_and_counts(tmp_path: Path) -> None:
    session = _session(tmp_path, limit=200)

    result = session.apply_batch(adds=[_add_entry(chars=140), _add_entry(chars=244)])

    assert result.get("ok") is False
    error = str(result.get("errors", {}).get("adds[1].content") or "")
    assert error, result.get("errors")
    # identity, actual, limit, overshoot
    assert "244" in error and "200" in error and "44" in error


def test_over_limit_without_note_says_give_it_a_note(tmp_path: Path) -> None:
    session = _session(tmp_path, limit=200)

    result = session.apply_batch(adds=[_add_entry(chars=244)])

    error = str(result.get("errors", {}).get("adds[0].content") or "")
    assert "note_ref" in error


def test_over_limit_with_note_names_that_note(tmp_path: Path) -> None:
    session = _session(tmp_path, limit=200)

    result = session.apply_batch(
        adds=[_add_entry(chars=244, note_ref="note_play_materials")],
        note_upserts={"note_play_materials": "正文"},
    )

    error = str(result.get("errors", {}).get("adds[0].content") or "")
    assert "note_play_materials" in error


def test_near_over_limit_without_note_warns(tmp_path: Path) -> None:
    session = _session(tmp_path, limit=200)

    result = session.apply_batch(adds=[_add_entry(chars=120)])

    assert result.get("ok") is True, result
    warnings = " ".join(str(item) for item in (result.get("warnings") or []))
    assert "adds[0]" in warnings and "note_ref" in warnings


def test_near_over_limit_with_note_stays_quiet(tmp_path: Path) -> None:
    session = _session(tmp_path, limit=200)

    result = session.apply_batch(
        adds=[_add_entry(chars=120, note_ref="note_play_materials")],
        note_upserts={"note_play_materials": "正文"},
    )

    assert result.get("ok") is True, result
    warnings = " ".join(str(item) for item in (result.get("warnings") or []))
    assert "adds[0]" not in warnings


def test_short_entry_stays_quiet(tmp_path: Path) -> None:
    session = _session(tmp_path, limit=200)

    result = session.apply_batch(adds=[_add_entry(chars=60)])

    assert result.get("ok") is True, result
    warnings = " ".join(str(item) for item in (result.get("warnings") or []))
    assert "adds[0]" not in warnings


def test_staged_batch_carries_per_entry_budget(tmp_path: Path) -> None:
    session = _session(tmp_path, limit=200)

    result = session.apply_batch(
        adds=[_add_entry(chars=90, note_ref="note_play_materials")],
        note_upserts={"note_play_materials": "正文"},
    )

    assert result.get("ok") is True, result
    rows = result.get("entry_budget") or []
    assert rows, result
    row = rows[0]
    assert row["entry"] == "adds[0]"
    assert row["chars"] == 90
    assert row["limit"] == 200
    assert row["note_ref"] == "note_play_materials"


def test_over_limit_rewrite_names_the_line(tmp_path: Path) -> None:
    snapshot = "\n".join(f"260425:第 {index} 条记忆正文" for index in range(1, 4))
    session = _session(tmp_path, limit=200, snapshot=snapshot)

    result = session.apply_batch(
        rewrites=[
            {
                "line": 2,
                "content": _add_entry(chars=244)["content"],
                "minimal_memory": "核对素材口径->读 note_play_materials",
            }
        ]
    )

    assert result.get("ok") is False
    keys = list(result.get("errors") or {})
    assert any("rewrites" in key and "content" in key for key in keys), keys
    error = str(result["errors"][keys[0]])
    assert "244" in error and "200" in error


def test_manager_payload_prints_the_configured_budget(tmp_path: Path) -> None:
    mod = _module()
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.document.summary_max_chars = 173
    cfg.queue.max_wait_seconds = 0
    manager = mod.MemoryManager(tmp_path, cfg)
    batch = mod.MemoryBatch(
        op="write",
        items=[
            mod.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="把这条长期规则记住",
                created_at="2026-04-25T00:00:32+08:00",
                request_id="write_1",
            )
        ],
    )

    prompt = manager._memory_agent_user_prompt(
        batch=batch,
        snapshot_text="260425:一条现有记忆",
        repair_reason="",
    )

    assert "173" in prompt


def _manager(tmp_path: Path, *, limit: int):
    mod = _module()
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.document.summary_max_chars = limit
    cfg.document.document_max_chars = 20000
    cfg.queue.max_wait_seconds = 0
    return mod.MemoryManager(tmp_path, cfg)


def _seed(manager, *, body: str, minimal: str) -> None:
    manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory=minimal,
        source="self",
        from_user=False,
        now_iso="2026-04-25T00:00:00+08:00",
    )
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:30+08:00")


def _write_batch(mod, request_id: str = "write_1"):
    return mod.MemoryBatch(
        op="write",
        items=[
            mod.MemoryQueueRequest(
                op="write",
                decision_source="self",
                payload_text="把这条长期规则记住",
                created_at="2026-04-25T00:00:32+08:00",
                request_id=request_id,
            )
        ],
    )


def test_legacy_over_cap_row_does_not_block_an_unrelated_batch(tmp_path: Path) -> None:
    """A resident row the batch never touched cannot be the reason the batch fails.

    Measured as parked record write_ceb5d98878d5 (2026-11-05 20:31): with the cap moved
    300 -> 200 while 36 of 93 rows were still over it, every batch raised "summary line
    exceeds 200 chars" for a row nobody submitted, and spent all three attempts on an
    error the model cannot fix.
    """
    mod = _module()
    manager = _manager(tmp_path, limit=200)
    try:
        _seed(manager, body="旧" * 250, minimal="旧规则->保留")
        session = _session(tmp_path, limit=200, snapshot=manager.snapshot_text())
        staged = session.apply_batch(
            adds=[_add_entry(chars=90, note_ref="note_play_materials")],
            note_upserts={"note_play_materials": "正文"},
        )
        assert staged.get("ok") is True, staged

        validated = manager._build_validated_write_from_apply_batch(
            before_text=manager.snapshot_text(),
            session=session,
            batch=_write_batch(mod),
        )
        assert validated is not None
    finally:
        manager.close()


def test_line_cap_is_a_write_gate_not_a_history_audit(tmp_path: Path) -> None:
    """Strict mode still prices a document handed in; re-rendering the store does not."""
    markdown = importlib.import_module("g3ku.agent.markdown_memory")
    oversized = "260425:" + "长" * 240 + "\n"

    with pytest.raises(ValueError, match="summary line exceeds 200"):
        markdown.validate_memory_document(oversized, summary_max_chars=200, document_max_chars=20000)

    markdown.validate_memory_document(
        oversized,
        summary_max_chars=200,
        document_max_chars=20000,
        enforce_line_cap=False,
    )

    # The other guarantees stay in force when the cap does not.
    with pytest.raises(ValueError, match="exceeds 100 chars"):
        markdown.validate_memory_document(
            oversized,
            summary_max_chars=200,
            document_max_chars=100,
            enforce_line_cap=False,
        )
    with pytest.raises(ValueError, match="invalid blocks"):
        markdown.validate_memory_document(
            "not a memory line\n",
            summary_max_chars=200,
            document_max_chars=20000,
            enforce_line_cap=False,
        )


def test_doctor_lists_over_cap_rows_as_migration_work(tmp_path: Path) -> None:
    manager = _manager(tmp_path, limit=200)
    try:
        _seed(manager, body="旧" * 250, minimal="旧规则->保留")
        checks = {str(item.get("name")): item for item in manager.doctor_report()["checks"]}

        assert "over_cap_lines" in checks
        assert checks["over_cap_lines"]["ok"] is False
        assert "250" in checks["over_cap_lines"]["detail"]
        # shape is intact; the cap finding is its own row, not a broken-document alarm
        assert checks["memory_document"]["ok"] is True
    finally:
        manager.close()
