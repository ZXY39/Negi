"""钉住的静态声明（技能名单 / 执行策略 / 会话临时目录）——前门 head/tail 分工的合同测试。

设计约束只有两条，其余用例都在证这两条：
1. 声明在整份上下文里恰好出现一次：头部带就尾块省，头部没带就尾块全量。
2. 头部字节只在三条刷新边界（曝光提交点 / 执行策略签名 / 会话临时目录）动了才变，
   名单漂移不改头部——头部一改就顶掉身后全部前缀，而每回合的语义挑选本来就会漂。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor.tool_contract import (
    FRONTDOOR_PINNED_CONTRACT_HEADING,
    apply_pinned_contract_to_head,
    build_frontdoor_tool_contract,
    clear_pinned_contract_stores,
    frontdoor_pinned_contract_text,
    merge_pinned_contract_into_system_text,
    pinned_contract_is_carried_by_head,
    pinned_contract_revision_key,
    pinned_skill_difference,
    pinned_skill_ids_for,
    render_pinned_contract_text,
    split_pinned_contract_from_system_text,
)


@pytest.fixture(autouse=True)
def _isolate_pinned_contract_stores():
    clear_pinned_contract_stores()
    yield
    clear_pinned_contract_stores()


def _contract_text(contract) -> str:
    return "\n".join(
        str(item.get("content") or "")
        for item in [contract.to_message(), contract.to_stage_gate_message()]
    )


def _pinned(*, session=None, skill_ids, session_key="web:pinned", revision="rev-1", policy=None, temp_dir="C:/temp/ceo/pinned"):
    return frontdoor_pinned_contract_text(
        session,
        skill_ids=list(skill_ids),
        exec_runtime_policy=policy,
        session_temp_dir=temp_dir,
        contract_revision=revision,
        session_key=session_key,
    )


def test_render_pinned_contract_skips_head_when_roster_is_empty() -> None:
    """没有名单就不钉：为两行短声明去动头部是不划算的买卖。"""
    assert render_pinned_contract_text(skill_ids=[], exec_runtime_policy=None, session_temp_dir="C:/t") == ""
    assert _pinned(skill_ids=[]) == ""
    roster_only = render_pinned_contract_text(
        skill_ids=["memory"], exec_runtime_policy=None, session_temp_dir=""
    )
    assert roster_only.startswith(FRONTDOOR_PINNED_CONTRACT_HEADING)
    assert "candidate_skills (loadable with `load_skill_context`): `memory`" in roster_only


def test_pinned_contract_revision_key_ignores_roster_drift() -> None:
    base = pinned_contract_revision_key(
        exec_runtime_policy={"mode": "auto"}, session_temp_dir="C:/t", contract_revision="rev-1"
    )
    assert (
        pinned_contract_revision_key(
            exec_runtime_policy={"mode": "auto"}, session_temp_dir="C:/t", contract_revision="rev-1"
        )
        == base
    )
    assert (
        pinned_contract_revision_key(
            exec_runtime_policy={"mode": "auto"}, session_temp_dir="C:/t", contract_revision="rev-2"
        )
        != base
    )
    assert (
        pinned_contract_revision_key(
            exec_runtime_policy={"mode": "ask"}, session_temp_dir="C:/t", contract_revision="rev-1"
        )
        != base
    )
    assert (
        pinned_contract_revision_key(
            exec_runtime_policy={"mode": "auto"}, session_temp_dir="C:/other", contract_revision="rev-1"
        )
        != base
    )


def test_pinned_contract_bytes_are_frozen_until_a_refresh_boundary_moves() -> None:
    """同一 session_key 下换名单不改字节；换边界才重印，重印那份就是当刻的名单。"""
    first = _pinned(skill_ids=["memory", "pdf"])
    assert pinned_skill_ids_for(None, session_key="web:pinned") == ["memory", "pdf"]
    drifted = _pinned(skill_ids=["memory", "pdf", "docx"])
    assert drifted == first
    assert pinned_skill_ids_for(None, session_key="web:pinned") == ["memory", "pdf"]
    repinned = _pinned(skill_ids=["memory", "pdf", "docx"], revision="rev-2")
    assert repinned != first
    assert FRONTDOOR_PINNED_CONTRACT_HEADING in repinned
    assert "`docx`" in repinned
    assert pinned_skill_ids_for(None, session_key="web:pinned") == ["memory", "pdf", "docx"]


def test_pinned_contract_store_survives_a_different_carrier() -> None:
    """钉住写在会话对象上，同回合另一条装配路只拿得到 session_key：必须命中同一串原文。"""
    session = SimpleNamespace()
    pinned_on_session = _pinned(session=session, skill_ids=["memory"])
    pinned_on_store = _pinned(session=None, skill_ids=["memory"])
    assert pinned_on_store == pinned_on_session
    cleared = _pinned(session=None, skill_ids=["pdf"])
    assert cleared == pinned_on_session  # 仍是冻结的那份，不受本轮名单影响
    clear_pinned_contract_stores()
    after_clear = _pinned(session=None, skill_ids=["pdf"])
    assert after_clear != pinned_on_session
    assert "pdf" in after_clear


def test_pinned_head_merge_is_idempotent_and_reversible() -> None:
    pinned = _pinned(skill_ids=["memory"])
    once = apply_pinned_contract_to_head([{"role": "system", "content": "BASE"}], pinned)
    twice = apply_pinned_contract_to_head(once, pinned)
    assert twice == once
    assert pinned_contract_is_carried_by_head(once, pinned) is True
    base, carried = split_pinned_contract_from_system_text(once[0]["content"])
    assert base == "BASE"
    assert carried == pinned
    assert merge_pinned_contract_into_system_text(once[0]["content"], "") == once[0]["content"]


def test_tail_must_not_omit_when_head_cannot_carry_the_block() -> None:
    """头部拿不到块（首条不是 system）时判据必须为假，否则声明在整份上下文里消失。"""
    pinned = _pinned(skill_ids=["memory"])
    records = [{"role": "user", "content": "hello"}, {"role": "system", "content": "late note"}]
    assert apply_pinned_contract_to_head(records, pinned) == records
    assert pinned_contract_is_carried_by_head(records, pinned) is False
    assert pinned_contract_is_carried_by_head([], pinned) is False
    assert pinned_contract_is_carried_by_head([{"role": "system", "content": "BASE"}], "") is False


def test_tail_reports_member_difference_only_when_it_drifts() -> None:
    pinned = _pinned(skill_ids=["memory", "pdf"])
    tail = build_frontdoor_tool_contract(
        callable_tool_names=["exec"],
        candidate_tool_names=["exec"],
        hydrated_tool_names=["exec"],
        frontdoor_stage_state={},
        candidate_skill_ids=["memory", "pdf"],
        visible_skill_ids=["memory", "pdf"],
        rbac_visible_tool_names=["exec"],
        rbac_visible_skill_ids=["memory", "pdf"],
        contract_revision="rev-1",
        pinned_contract_text=pinned,
        pinned_skill_ids=pinned_skill_ids_for(None, session_key="web:pinned"),
    )
    text = _contract_text(tail)
    assert "candidate_skills (loadable with" not in text
    assert "granted_skills" not in text
    assert "unselected_skills" not in text
    assert "exec_runtime_policy" not in text
    assert "session_temp_dir" not in text

    drifted = build_frontdoor_tool_contract(
        callable_tool_names=["exec"],
        candidate_tool_names=["exec"],
        hydrated_tool_names=["exec"],
        frontdoor_stage_state={},
        candidate_skill_ids=["memory", "docx"],
        visible_skill_ids=["memory", "docx"],
        rbac_visible_tool_names=["exec"],
        rbac_visible_skill_ids=["memory", "docx"],
        contract_revision="rev-1",
        pinned_contract_text=pinned,
        pinned_skill_ids=pinned_skill_ids_for(None, session_key="web:pinned"),
    )
    drifted_text = _contract_text(drifted)
    assert "granted_skills" in drifted_text and "`docx`" in drifted_text
    assert "unselected_skills" in drifted_text and "`pdf`" in drifted_text
    assert "candidate_skills (loadable with" not in drifted_text

    # 头部没带块时，尾块逐段照旧全量（省略判定看头部原文，不看本轮算没算出来）。
    plain = build_frontdoor_tool_contract(
        callable_tool_names=["exec"],
        candidate_tool_names=["exec"],
        hydrated_tool_names=["exec"],
        frontdoor_stage_state={},
        candidate_skill_ids=["memory", "docx"],
        visible_skill_ids=["memory", "docx"],
        rbac_visible_tool_names=["exec"],
        rbac_visible_skill_ids=["memory", "docx"],
        contract_revision="rev-1",
        session_temp_dir="C:/temp/ceo/pinned",
    )
    plain_text = _contract_text(plain)
    roster_line = next(
        line for line in plain_text.splitlines()
        if line.startswith("candidate_skills (loadable with")
    )
    assert "`docx`" in roster_line and "`memory`" in roster_line
    assert "session_temp_dir: C:/temp/ceo/pinned" in plain_text
    assert "granted_skills" not in plain_text and "unselected_skills" not in plain_text


def test_pinned_skill_difference_reports_membership_not_counts() -> None:
    granted, unselected = pinned_skill_difference(
        pinned_skill_ids=["a", "b"], round_skill_ids=["b", "c"]
    )
    assert granted == ["c"]
    assert unselected == ["a"]
    assert pinned_skill_difference(pinned_skill_ids=["a", "b"], round_skill_ids=["b", "a"]) == ([], [])


def _canonical_state(**overrides) -> dict[str, object]:
    state: dict[str, object] = {
        "messages": [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "hello"}],
        "stable_messages": [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "hello"}],
        "tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "hydrated_tool_names": [],
        "visible_skill_ids": ["memory", "pdf"],
        "candidate_skill_ids": ["memory", "pdf"],
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": ["memory", "pdf"],
        "frontdoor_stage_state": {"active_stage_id": "", "transition_required": False, "stages": []},
        "session_key": "web:pinned",
        "cache_family_revision": "rev-1",
    }
    state.update(dict(overrides))
    return state


def _contract(runner, state):
    return runner._frontdoor_prompt_contract(
        state=state,
        provider_model="openai:gpt-4.1",
        tool_schemas=[],
        session_key="web:pinned",
    )


def test_graph_contract_pins_roster_into_head_and_keeps_it_stable_across_hops() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    first = _contract(runner, _canonical_state())
    assert str(first.stable_messages[0]["content"]).startswith("SYSTEM\n\n")
    assert "candidate_skills (loadable with `load_skill_context`): `memory`, `pdf`" in first.stable_messages[0]["content"]
    assert first.request_messages[0]["content"] == first.stable_messages[0]["content"]
    tail_text = "\n".join(str(item.get("content") or "") for item in first.dynamic_appendix_messages)
    assert "candidate_skills (loadable with" not in tail_text

    # 同一跳再算一次、以及下一跳名单漂了：头部字节不动，尾块只补成员差。
    again = _contract(runner, _canonical_state())
    assert again.stable_messages[0]["content"] == first.stable_messages[0]["content"]
    drifted = _contract(
        runner,
        _canonical_state(candidate_skill_ids=["memory", "docx"], visible_skill_ids=["memory", "docx"]),
    )
    assert drifted.stable_messages[0]["content"] == first.stable_messages[0]["content"]
    drifted_tail = "\n".join(str(item.get("content") or "") for item in drifted.dynamic_appendix_messages)
    assert "granted_skills" in drifted_tail and "`docx`" in drifted_tail
    assert "unselected_skills" in drifted_tail and "`pdf`" in drifted_tail

    # 只有边界动了才重印，且重印出来的是当刻名单；边界没动而名单相同则逐字节复用。
    same_boundary = _contract(runner, _canonical_state(cache_family_revision="rev-2"))
    assert same_boundary.stable_messages[0]["content"] == first.stable_messages[0]["content"]
    repinned = _contract(
        runner,
        _canonical_state(cache_family_revision="rev-3", candidate_skill_ids=["memory", "docx"]),
    )
    assert repinned.stable_messages[0]["content"] != first.stable_messages[0]["content"]
    assert "`docx`" in repinned.stable_messages[0]["content"]
