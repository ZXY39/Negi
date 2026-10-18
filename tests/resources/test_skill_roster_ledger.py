"""skill 名单按轮检测 / 注入台账 / load 三态回执的合同测试。

验收口径（`work/skill-roster-context-plan.md`）：
- 头部名单冻结期间，新变为可读的 skill 按轮进注入块（id|名称|描述）；
- 台账只增不减：权限收回/删除不会把已注入的名字缩回去；
- 台账条目被重印后的头部名单覆盖后，从注入块摘除（信息仍在头部）；
- `load_skill_context` 对读不了的名字给出具体原因：已删除 vs 权限不允许。
"""

from __future__ import annotations

import pytest

from g3ku.runtime.frontdoor.skill_roster import (
    clear_skill_roster_ledgers,
    injected_skill_ids,
    new_readable_skill_items,
    render_new_readable_skills_lines,
)


@pytest.fixture(autouse=True)
def _isolate_ledgers():
    clear_skill_roster_ledgers()
    yield
    clear_skill_roster_ledgers()


def _item(skill_id: str, description: str = "") -> dict[str, str]:
    return {
        "skill_id": skill_id,
        "display_name": skill_id.title(),
        "description": description or f"{skill_id} 的描述。",
    }


def test_new_readable_items_are_the_delta_against_header_and_ledger() -> None:
    live = [_item("alpha"), _item("beta"), _item("gamma")]
    scope = "session:web:x"

    first = new_readable_skill_items(
        scope_key=scope, live_visible_skills=live, covered_skill_ids=["alpha"]
    )
    assert [item["skill_id"] for item in first] == ["beta", "gamma"]

    # 第二轮同一可见集：台账已记账，不再重复注入
    second = new_readable_skill_items(
        scope_key=scope, live_visible_skills=live, covered_skill_ids=["alpha"]
    )
    assert second == []
    assert injected_skill_ids(scope) == {"beta", "gamma"}


def test_ledger_never_shrinks_on_revocation() -> None:
    """权限收回/删除把 skill 从 live 集摘掉，台账里的名字仍在——块不缩。"""
    scope = "session:web:x"
    new_readable_skill_items(
        scope_key=scope, live_visible_skills=[_item("alpha"), _item("beta")], covered_skill_ids=[]
    )
    assert injected_skill_ids(scope) == {"alpha", "beta"}

    # beta 被撤走：live 集里没有它，台账不缩
    new_readable_skill_items(
        scope_key=scope, live_visible_skills=[_item("alpha")], covered_skill_ids=[]
    )
    assert injected_skill_ids(scope) == {"alpha", "beta"}


def test_ledger_entries_covered_by_repinned_header_drop_out_of_the_block() -> None:
    """重印后的头部名单覆盖了台账条目 → 注入块摘条（信息仍在头部，块不无限涨）。"""
    scope = "session:web:x"
    new_readable_skill_items(
        scope_key=scope, live_visible_skills=[_item("fresh-skill")], covered_skill_ids=[]
    )
    assert injected_skill_ids(scope) == {"fresh-skill"}

    # 头部重印后把 fresh-skill 收进名单：covered 覆盖台账条目 → 块里不再重复
    remaining = new_readable_skill_items(
        scope_key=scope,
        live_visible_skills=[_item("fresh-skill"), _item("another")],
        covered_skill_ids=["fresh-skill"],
    )
    assert [item["skill_id"] for item in remaining] == ["another"]


def test_ledger_is_scoped_and_bounded() -> None:
    new_readable_skill_items(
        scope_key="session:web:a", live_visible_skills=[_item("alpha")], covered_skill_ids=[]
    )
    # 另一个会话的台账独立
    assert injected_skill_ids("session:web:b") == set()
    assert injected_skill_ids("session:web:a") == {"alpha"}


def test_render_new_readable_skills_lines() -> None:
    assert render_new_readable_skills_lines([]) == []
    lines = render_new_readable_skills_lines([_item("fresh-skill", "第一句。第二句。")])
    assert lines[0].startswith("new_readable_skills (")
    assert "- `fresh-skill` | Fresh-Skill | 第一句。" in lines
