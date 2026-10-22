"""长期记忆修订流与会话通知车道。

不变量：
- 只有"居民行渲染结果变了"的提交才写修订；计数更新不写。
- 会话按 rev 游标消费增量，首次投递只对齐游标（index 1 的冻结快照就是那份文档）。
- skill 名单与记忆增量合成同一条 `[RUNTIME_NOTICE]` 消息，两节都空时不投。
- 归并提交让下一次投递走 rebase（整份文档），增量超上限同样 rebase。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.skill_roster_notice import (
    MEMORY_NOTICE_HEADING,
    RUNTIME_NOTICE_MARKER,
    clear_skill_roster_notice_stores,
    deliver_for_scope,
    notice_context_for,
)


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _manager(tmp_path):
    schema = importlib.import_module("g3ku.config.schema")
    return _module().MemoryManager(tmp_path, schema.MemoryToolsConfig())


def _commit(manager, *, adds=None, rewrites=None, deletes=None, inspired=None):
    module = _module()
    validated = module._MemoryValidatedWrite(
        document_text="",
        note_writes={},
        note_refs_written=[],
        memory_chars_after=0,
        document_preview="",
        adds=list(adds or []),
        rewrites=list(rewrites or []),
        deletes=list(deletes or []),
        inspired_memory_ids=list(inspired or []),
    )
    manager._commit_validated_write(validated)


def _skill(skill_id: str) -> dict[str, str]:
    return {
        "skill_id": skill_id,
        "display_name": skill_id.title(),
        "description": f"{skill_id} 的描述。",
        "available": True,
    }


def test_revision_is_written_only_for_visible_changes(tmp_path) -> None:
    manager = _manager(tmp_path)
    try:
        row = manager._memory_repo.create_memory(
            memory_body="已有规则",
            minimal_memory="已有规则",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")
        assert manager.latest_memory_revision() == 0

        # 只更新计数器：渲染行不变，不写修订。
        _commit(manager, inspired=[row["memory_id"]])
        assert manager.latest_memory_revision() == 0

        _commit(
            manager,
            adds=[{"content": "新规则：先读再写", "minimal_memory": "先读再写", "decision_source": "self"}],
        )
        assert manager.latest_memory_revision() == 1
        delta = manager.memory_notice_delta(since_rev=0)
        assert delta["rev"] == 1
        assert delta["rebase"] is False
        assert "新规则：先读再写" in delta["text"]
        assert "已有规则" not in delta["text"]

        new_id = next(
            row["memory_id"]
            for row in manager._memory_repo.list_memories()
            if row["memory_body"] == "新规则：先读再写"
        )
        _commit(
            manager,
            rewrites=[{"id": new_id, "content": "新规则：先读再写（修订）", "minimal_memory": "先读再写（修订）"}],
        )
        assert manager.latest_memory_revision() == 2
        changed = manager.memory_notice_delta(since_rev=1)
        assert "（修订）" in changed["text"]
        assert "已有规则" not in changed["text"]
    finally:
        manager.close()


def test_consolidation_commit_forces_a_rebase(tmp_path) -> None:
    manager = _manager(tmp_path)
    try:
        row = manager._memory_repo.create_memory(
            memory_body="要归并的规则",
            minimal_memory="要归并的规则",
            source="self",
            from_user=False,
            now_iso="2026-04-25T00:00:00+08:00",
        )
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")
        module = _module()
        validated = module._MemoryValidatedWrite(
            document_text="",
            note_writes={"play_flow": "## 方法论\n- 先读再写\n"},
            note_refs_written=["play_flow"],
            memory_chars_after=0,
            document_preview="",
            is_consolidation=True,
            consolidation_writes=[
                {"note_ref": "play_flow", "body": "## 方法论\n- 先读再写\n", "edits": []}
            ],
            retirements=[{"memory_id": row["memory_id"], "note_ref": "play_flow"}],
        )
        manager._commit_validated_write(validated)

        assert manager.latest_memory_revision() >= 1
        delta = manager.memory_notice_delta(since_rev=0)
        assert delta["rebase"] is True
        assert delta["text"] == str(manager.snapshot_text() or "").strip()
    finally:
        manager.close()


def test_oversized_delta_falls_back_to_a_rebase(tmp_path) -> None:
    manager = _manager(tmp_path)
    try:
        manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:00+08:00")
        _commit(
            manager,
            adds=[
                {"content": f"第{index}条规则：" + "长" * 240, "minimal_memory": "长规则", "decision_source": "self"}
                for index in range(5)
            ],
        )
        delta = manager.memory_notice_delta(since_rev=0)
        assert delta["rebase"] is True
        assert delta["text"] == str(manager.snapshot_text() or "").strip()
    finally:
        manager.close()


def test_notice_lane_merges_skill_and_memory_into_one_message() -> None:
    clear_skill_roster_notice_stores()
    scope = "session:web:shared"
    calls: list[tuple[int, int]] = []

    def provider(*, since_rev: int, base_rev: int, initialized: bool = False):
        calls.append((since_rev, base_rev))
        if not initialized:
            return {"rev": 3, "text": "", "rebase": False, "align": True}
        if since_rev >= 4:
            return {"rev": 4, "text": "", "rebase": False}
        return {"rev": 4, "text": "261022:新规则：先读再写", "rebase": False}

    first, delivered = deliver_for_scope(
        scope,
        [_skill("alpha")],
        now="2026-04-25T00:00:00+08:00",
        covered_skill_ids=["alpha"],
        memory_provider=provider,
    )
    assert first is None
    assert delivered == []
    assert notice_context_for(scope)["memory_cursor"] == {
        "rev": 3,
        "base_rev": 3,
        "initialized": True,
    }

    second, delivered = deliver_for_scope(
        scope,
        [_skill("alpha"), _skill("beta")],
        now="2026-04-25T00:01:00+08:00",
        covered_skill_ids=["alpha"],
        memory_provider=provider,
    )
    assert second is not None
    content = str(second.get("content") or "")
    assert content.startswith(RUNTIME_NOTICE_MARKER)
    assert "Skills newly readable in this session:" in content
    assert "- `beta` | Beta | beta 的描述。" in content
    assert MEMORY_NOTICE_HEADING in content
    assert "新规则：先读再写" in content
    assert [record["skill_id"] for record in delivered] == ["beta"]
    assert notice_context_for(scope)["memory_cursor"] == {
        "rev": 4,
        "base_rev": 3,
        "initialized": True,
    }
    assert calls[-1] == (3, 3)

    third, _ = deliver_for_scope(
        scope,
        [_skill("alpha"), _skill("beta")],
        now="2026-04-25T00:02:00+08:00",
        covered_skill_ids=["alpha"],
        memory_provider=provider,
    )
    assert third is None


def test_notice_lane_rebase_replaces_the_cursor_base() -> None:
    clear_skill_roster_notice_stores()
    scope = "session:web:rebase"

    def provider(*, since_rev: int, base_rev: int, initialized: bool = False):
        return {"rev": 9, "text": "整份文档", "rebase": True}

    message, _ = deliver_for_scope(
        scope,
        [],
        now="2026-04-25T00:00:00+08:00",
        memory_provider=provider,
    )
    assert message is not None
    assert "整份文档" in str(message.get("content") or "")
    assert notice_context_for(scope)["memory_cursor"] == {"rev": 9, "base_rev": 9, "initialized": True}


def test_frontdoor_provider_aligns_the_first_cursor_without_replaying_history() -> None:
    ops = object.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(
        memory_manager=SimpleNamespace(
            latest_memory_revision=lambda: 9,
            memory_notice_delta=lambda since_rev: {"rev": 9, "text": "增量", "rebase": False},
        )
    )
    first = ops._frontdoor_memory_notice_provider(since_rev=0, base_rev=0, initialized=False)
    assert first == {"rev": 9, "from_rev": 0, "text": "", "rebase": False, "align": True}
    later = ops._frontdoor_memory_notice_provider(since_rev=8, base_rev=8, initialized=True)
    assert later == {"rev": 9, "text": "增量", "rebase": False}
    assert ops._frontdoor_memory_notice_provider(since_rev=9, base_rev=9, initialized=True)["text"] == ""


def test_first_revision_after_an_empty_alignment_is_still_delivered() -> None:
    """首跳时还没有任何修订也要记为"已对齐"：否则会话会跳过第一条真实记忆。"""
    clear_skill_roster_notice_stores()
    scope = "session:web:emptybase"
    revision = {"latest": 0, "text": ""}
    ops = object.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(
        memory_manager=SimpleNamespace(
            latest_memory_revision=lambda: revision["latest"],
            memory_notice_delta=lambda since_rev: {
                "rev": revision["latest"],
                "text": revision["text"],
                "rebase": False,
            },
        )
    )
    first, _ = deliver_for_scope(
        scope,
        [],
        now="2026-04-25T00:00:00+08:00",
        memory_provider=ops._frontdoor_memory_notice_provider,
    )
    assert first is None
    assert notice_context_for(scope)["memory_cursor"] == {
        "rev": 0,
        "base_rev": 0,
        "initialized": True,
    }

    revision.update(latest=1, text="261022:第一条真实记忆")
    second, _ = deliver_for_scope(
        scope,
        [],
        now="2026-04-25T00:01:00+08:00",
        memory_provider=ops._frontdoor_memory_notice_provider,
    )
    assert second is not None
    assert "第一条真实记忆" in str(second.get("content") or "")
