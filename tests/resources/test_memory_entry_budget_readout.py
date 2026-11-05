"""记忆批次的字符预算读数：超线要报"哪一条、多少字、超多少、搬到哪"，接近上限要先警告。

实盘形状：`_content_exceeds_message` 只印上限，抛出后经 `_memory_batch_preview` 塌成
`errors.batch` 一个字符串——模型既不知道是哪一条，也不知道自己写了多少字；而带 note 的
行和不带 note 的行挨打回的是同一句话。常驻块因此长成"一行装得下一份答案"的形状。
"""
from __future__ import annotations

import importlib
from pathlib import Path


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
