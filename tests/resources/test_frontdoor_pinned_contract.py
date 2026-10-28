"""钉住的静态声明（skill 名单 / 执行策略 / 会话临时目录）——前门 head/tail 分工的合同测试。

设计约束：
1. 头部名单 = 钉住时点的**全量可见** skill，每行 `id | name | description`；两次重印之间
   可见集怎么变头部都逐字节不动（新增靠一次性 `[RUNTIME_NOTICE]` 投递，消失靠
   `load_skill_context` 三态回执）。
2. 头部只在重印边界动了才变——重印判据只有 roster epoch（随 token 压缩 / 手动压缩递增）；
   执行策略与临时目录**不进判据**，变化由一次性通知纠正，下次压缩才随头部刷新。
3. 头部在名单为空时也钉静态声明（exec 策略/临时目录/路径锚），否则删尾块后声明会消失。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor.tool_contract import (
    FRONTDOOR_PINNED_CONTRACT_HEADING,
    apply_pinned_contract_to_head,
    build_frontdoor_tool_contract,
    bump_skill_roster_epoch,
    clear_pinned_contract_stores,
    frontdoor_pinned_contract_text,
    merge_pinned_contract_into_system_text,
    pinned_contract_is_carried_by_head,
    pinned_contract_revision_key,
    pinned_contract_section_values,
    pinned_skill_ids_for,
    render_pinned_contract_text,
    skill_roster_epoch_for,
    split_pinned_contract_from_system_text,
)
from g3ku.runtime.skill_roster_notice import clear_skill_roster_notice_stores
from g3ku.stickers import (
    STICKER_HEADER,
    sticker_pack_sections,
    sticker_sections_from_pinned_text,
)


@pytest.fixture(autouse=True)
def _isolate_pinned_contract_stores():
    clear_pinned_contract_stores()
    clear_skill_roster_notice_stores()
    yield
    clear_pinned_contract_stores()
    clear_skill_roster_notice_stores()


def _contract_text(contract) -> str:
    return str(contract.to_message().get("content") or "")


def _items(*ids: str) -> list[dict[str, str]]:
    return [
        {"skill_id": skill_id, "display_name": skill_id.title(), "description": f"{skill_id} 的描述。"}
        for skill_id in ids
    ]


def _pinned(*, session=None, skill_items=None, session_key="web:pinned", epoch=None, policy=None, temp_dir="C:/temp/ceo/pinned", include_extras=True, sticker_items=None):
    if epoch is None:
        epoch = skill_roster_epoch_for(session, session_key=session_key)
    return frontdoor_pinned_contract_text(
        session,
        skill_items=list(skill_items if skill_items is not None else _items("memory", "pdf")),
        exec_runtime_policy=policy,
        session_temp_dir=temp_dir,
        skill_roster_epoch=epoch,
        session_key=session_key,
        include_extras=include_extras,
        sticker_items=sticker_items,
    )


def test_render_pinned_contract_keeps_extras_when_roster_is_empty() -> None:
    """名单为空也要钉静态声明：删尾块后头部是 exec/临时目录/路径锚的唯一载体。"""
    text = render_pinned_contract_text(skill_items=[], exec_runtime_policy=None, session_temp_dir="C:/t")
    assert text.startswith(FRONTDOOR_PINNED_CONTRACT_HEADING)
    assert "candidate_skills" not in text
    assert "exec_runtime_policy: none" in text
    assert "session_temp_dir: C:/t" in text
    assert "path_anchor:" in text
    assert _pinned(skill_items=[]) != ""
    roster_only = render_pinned_contract_text(
        skill_items=_items("memory"), exec_runtime_policy=None, session_temp_dir="", include_extras=False
    )
    assert roster_only.startswith(FRONTDOOR_PINNED_CONTRACT_HEADING)
    assert "- `memory` | Memory | memory 的描述。" in roster_only
    assert "exec_runtime_policy" not in roster_only


def test_pinned_contract_revision_key_only_follows_the_roster_epoch() -> None:
    base = pinned_contract_revision_key(
        exec_runtime_policy={"mode": "auto"}, session_temp_dir="C:/t", skill_roster_epoch=0
    )
    assert (
        pinned_contract_revision_key(
            exec_runtime_policy={"mode": "auto"}, session_temp_dir="C:/t", skill_roster_epoch=0
        )
        == base
    )
    # epoch 变 = 重印边界动了
    assert (
        pinned_contract_revision_key(
            exec_runtime_policy={"mode": "auto"}, session_temp_dir="C:/t", skill_roster_epoch=1
        )
        != base
    )
    # 策略/临时目录变化不是重印边界：头部字节必须保持，靠一次性通知纠正
    assert (
        pinned_contract_revision_key(
            exec_runtime_policy={"mode": "ask"}, session_temp_dir="C:/t", skill_roster_epoch=0
        )
        == base
    )
    assert (
        pinned_contract_revision_key(
            exec_runtime_policy={"mode": "auto"}, session_temp_dir="C:/other", skill_roster_epoch=0
        )
        == base
    )


def test_pinned_contract_bytes_are_frozen_until_the_epoch_moves() -> None:
    """同一 session_key 下可见集变化不改字节；epoch 跳变才重印，重印那份就是当刻名单。"""
    first = _pinned(skill_items=_items("memory", "pdf"))
    assert pinned_skill_ids_for(None, session_key="web:pinned") == ["memory", "pdf"]
    # 可见集变了（新增 docx / memory 被撤走）：epoch 没动，头部逐字节复用
    drifted = _pinned(skill_items=_items("memory", "pdf", "docx"))
    assert drifted == first
    revoked = _pinned(skill_items=_items("pdf"))
    assert revoked == first
    assert pinned_skill_ids_for(None, session_key="web:pinned") == ["memory", "pdf"]
    # epoch 跳变才重印，重印出来的是当刻名单
    bump_skill_roster_epoch(None, session_key="web:pinned")
    repinned = _pinned(skill_items=_items("memory", "pdf", "docx"))
    assert repinned != first
    assert FRONTDOOR_PINNED_CONTRACT_HEADING in repinned
    assert "`docx`" in repinned
    assert pinned_skill_ids_for(None, session_key="web:pinned") == ["docx", "memory", "pdf"]  # 名单按 id 排序


def test_pinned_contract_store_survives_a_different_carrier() -> None:
    """钉住写在会话对象上，同回合另一条装配路只拿得到 session_key：必须命中同一串原文。"""
    session = SimpleNamespace()
    pinned_on_session = _pinned(session=session, skill_items=_items("memory"))
    pinned_on_store = _pinned(session=None, skill_items=_items("memory"))
    assert pinned_on_store == pinned_on_session
    cleared = _pinned(session=None, skill_items=_items("pdf"))
    assert cleared == pinned_on_session  # 仍是冻结的那份，不受本轮名单影响
    clear_pinned_contract_stores()
    after_clear = _pinned(session=None, skill_items=_items("pdf"))
    assert after_clear != pinned_on_session
    assert "pdf" in after_clear


def test_pinned_head_merge_is_idempotent_and_reversible() -> None:
    pinned = _pinned(skill_items=_items("memory"))
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
    pinned = _pinned(skill_items=_items("memory"))
    records = [{"role": "user", "content": "hello"}, {"role": "system", "content": "late note"}]
    assert apply_pinned_contract_to_head(records, pinned) == records
    assert pinned_contract_is_carried_by_head(records, pinned) is False
    assert pinned_contract_is_carried_by_head([], pinned) is False
    assert pinned_contract_is_carried_by_head([{"role": "system", "content": "BASE"}], "") is False


def test_tail_has_no_skill_roster_mechanism_anymore() -> None:
    """尾块不再有 skill 名单机制：candidate/granted/unselected/repair/new_readable 行全删。

    重印点之间新变为可读的 skill 由一次性持久提示落进历史，见
    `tests/resources/test_skill_roster_notice_lane.py`。
    """
    pinned = _pinned(skill_items=_items(*[f"skill-{index}" for index in range(12)]))
    tail = build_frontdoor_tool_contract(
        callable_tool_names=["exec"],
        candidate_tool_names=["exec"],
        candidate_skill_ids=[f"skill-{index}" for index in range(12)],
        visible_skill_ids=[f"skill-{index}" for index in range(12)],
        rbac_visible_tool_names=["exec"],
        rbac_visible_skill_ids=[f"skill-{index}" for index in range(12)],
        repair_required_skill_items=[
            {"skill_id": "broken", "description": "缺正文", "reason": "missing body"},
        ],
        contract_revision="rev-1",
        pinned_contract_text=pinned,
        pinned_skill_ids=pinned_skill_ids_for(None, session_key="web:pinned"),
    )
    text = _contract_text(tail)
    assert "candidate_skills" not in text
    assert "granted_skills" not in text
    assert "unselected_skills" not in text
    assert "repair_required_skills" not in text
    assert "new_readable_skills" not in text
    assert "exec_runtime_policy" not in text
    assert "session_temp_dir" not in text


def test_epoch_store_is_scoped_by_session_key() -> None:
    assert skill_roster_epoch_for(None, session_key="web:a") == 0
    bump_skill_roster_epoch(None, session_key="web:a")
    bump_skill_roster_epoch(None, session_key="web:a")
    assert skill_roster_epoch_for(None, session_key="web:a") == 2
    assert skill_roster_epoch_for(None, session_key="web:b") == 0
    session = SimpleNamespace()
    bump_skill_roster_epoch(session, session_key="web:c")
    assert skill_roster_epoch_for(session, session_key="web:c") == 1
    assert skill_roster_epoch_for(None, session_key="web:c") == 1


def test_continuation_seed_head_is_not_stacked_with_a_second_pinned_block() -> None:
    """续跑回合的头部来自上一跳请求体种子，本身已带钉住块：必须先摘再拼。"""
    from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder

    builder = CeoMessageBuilder(
        loop=SimpleNamespace(main_task_service=None, workspace=None, app_config=None),
        prompt_builder=SimpleNamespace(),
    )
    pinned = _pinned(skill_items=_items("alpha", "bravo"))
    seeded_head = merge_pinned_contract_into_system_text("BASE PROMPT", pinned)
    seed = [
        {"role": "system", "content": seeded_head},
        {"role": "user", "content": "上一轮的问题"},
        {"role": "assistant", "content": "上一轮的回答"},
    ]

    model_messages, stable_messages, _dynamic, _overlay, _in_history = (
        builder._inject_direct_request_body_continuation(
            request_body_seed_messages=seed,
            user_content="这一轮的问题",
            turn_overlay_parts=[],
            memory_snapshot_text="",
            persona_text="",
            query_text="这一轮的问题",
            user_metadata=None,
            pinned_contract_text=pinned,
        )
    )
    head_text = str(stable_messages[0]["content"])
    assert head_text.count(FRONTDOOR_PINNED_CONTRACT_HEADING) == 1
    assert head_text == seeded_head
    assert pinned_contract_is_carried_by_head(stable_messages, pinned) is True
    assert stable_messages[0]["content"] == seeded_head
    assert model_messages[0]["content"] == seeded_head


def test_stacked_pinned_blocks_collapse_back_to_one() -> None:
    """已经叠了两份的携带头部要能收回成一份。"""
    pinned = _pinned(skill_items=_items("alpha", "bravo"))
    stacked = f"BASE\n\n{pinned}\n\n{pinned}"
    merged = merge_pinned_contract_into_system_text(stacked, pinned)
    assert merged == f"BASE\n\n{pinned}"
    assert merged.count(FRONTDOOR_PINNED_CONTRACT_HEADING) == 1
    assert pinned_contract_is_carried_by_head([{"role": "system", "content": merged}], pinned) is True
    base, carried = split_pinned_contract_from_system_text(stacked)
    assert base == "BASE"
    assert carried.count(FRONTDOOR_PINNED_CONTRACT_HEADING) == 2
    assert pinned_contract_is_carried_by_head([{"role": "system", "content": stacked}], pinned) is False


BASE_ROSTER = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
    "golf", "hotel", "india", "juliet", "kilo", "lima",
]
DRIFTED_ROSTER = [name for name in BASE_ROSTER if name != "lima"] + ["mike"]


def _canonical_state(**overrides) -> dict[str, object]:
    state: dict[str, object] = {
        "messages": [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "hello"}],
        "stable_messages": [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "hello"}],
        "tool_names": [],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "visible_skill_ids": list(BASE_ROSTER),
        "candidate_skill_ids": list(BASE_ROSTER),
        "rbac_visible_tool_names": [],
        "rbac_visible_skill_ids": list(BASE_ROSTER),
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


def test_graph_contract_pins_full_roster_into_head_and_keeps_it_frozen() -> None:
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())

    first = _contract(runner, _canonical_state())
    head = str(first.stable_messages[0]["content"])
    assert head.startswith("SYSTEM\n\n")
    # 全量名单、每行 id|名称|描述（测试 loop 没有服务，描述留空、名称回落 id）
    assert "candidate_skills (loadable with `load_skill_context`; `id | name | description`):" in head
    assert "- `alpha` | alpha |" in head
    assert "- `lima` | lima |" in head
    assert first.request_messages[0]["content"] == first.stable_messages[0]["content"]
    tail_text = "\n".join(str(item.get("content") or "") for item in first.dynamic_appendix_messages)
    assert "candidate_skills" not in tail_text
    assert "new_readable_skills" not in tail_text

    # 同一跳再算一次、以及下一跳可见集漂了：epoch 没动，头部字节逐字复用，尾块没有差集行。
    again = _contract(runner, _canonical_state())
    assert again.stable_messages[0]["content"] == first.stable_messages[0]["content"]
    drifted = _contract(
        runner,
        _canonical_state(candidate_skill_ids=list(DRIFTED_ROSTER), visible_skill_ids=list(DRIFTED_ROSTER)),
    )
    assert drifted.stable_messages[0]["content"] == first.stable_messages[0]["content"]
    drifted_tail = "\n".join(str(item.get("content") or "") for item in drifted.dynamic_appendix_messages)
    assert "granted_skills" not in drifted_tail
    assert "unselected_skills" not in drifted_tail

    # epoch 跳变（重印边界）才重印，重印出来的是当刻名单。
    bump_skill_roster_epoch(None, session_key="web:pinned")
    repinned = _contract(runner, _canonical_state(candidate_skill_ids=list(DRIFTED_ROSTER)))
    assert repinned.stable_messages[0]["content"] != first.stable_messages[0]["content"]
    assert "`mike`" in repinned.stable_messages[0]["content"]
    # 重印之后下一跳按新名单继续冻结。
    stable_after = _contract(runner, _canonical_state(candidate_skill_ids=list(DRIFTED_ROSTER)))
    assert stable_after.stable_messages[0]["content"] == repinned.stable_messages[0]["content"]


def _sticker_pack(key: str, theme: str, categories: list[tuple[str, str]]):
    from pathlib import Path

    from g3ku.stickers import Pack, StickerCategory

    return Pack(
        key=key,
        theme=theme,
        categories=tuple(
            StickerCategory(key=name, description=description, count=1)
            for name, description in categories
        ),
        path=Path("C:/ws/stickers") / key,
    )


def test_pinned_head_carries_sticker_catalog_and_round_trips_into_sections(monkeypatch) -> None:
    """头部渲染与通知分节是同一份渲染：解析回来必须逐字相等。"""
    monkeypatch.setattr("g3ku.stickers.config.max_per_reply", lambda: 2)
    packs = [
        _sticker_pack("atri", "动漫少女", [("happy", "开心大笑"), ("awkward", "尴尬")]),
        _sticker_pack("panda", "熊猫头", [("smug", "得意")]),
    ]
    text = render_pinned_contract_text(
        skill_items=[],
        exec_runtime_policy=None,
        session_temp_dir="C:/temp/ceo",
        sticker_items=packs,
    )

    assert STICKER_HEADER in text
    assert "sticker_max_per_reply: 2" in text
    assert "- `atri` | 动漫少女 | awkward(尴尬), happy(开心大笑)" in text
    assert "- `panda` | 熊猫头 | smug(得意)" in text
    assert sticker_sections_from_pinned_text(text) == sticker_pack_sections(packs)
    values = pinned_contract_section_values(text)
    assert values["sticker_pack:atri"] == sticker_pack_sections(packs)["atri"]
    # 头部这一行就是通知分节的初值：两边必须逐字一致，否则会被当成"值变了"再投一条。
    assert values["sticker_max_per_reply"] == "2"


def test_pinned_sticker_lines_stay_frozen_between_repaints() -> None:
    """两次重印之间增删表情包：头部逐字节不动，变化只走通知分节。"""
    packs = [_sticker_pack("atri", "动漫少女", [("happy", "开心大笑")])]
    kwargs = dict(
        skill_items=[],
        policy=None,
        temp_dir="C:/temp/ceo",
        session_key="web:pinned",
    )
    first = _pinned(sticker_items=packs, **kwargs)
    grown = [*packs, _sticker_pack("panda", "熊猫头", [("smug", "得意")])]
    again = _pinned(sticker_items=grown, **kwargs)
    assert again == first
    assert "sticker_pack:panda" not in again

    bump_skill_roster_epoch(None, session_key="web:pinned")
    repinned = _pinned(sticker_items=grown, **kwargs)
    assert repinned != first
    assert "- `panda` | 熊猫头 | smug(得意)" in repinned
