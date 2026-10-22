"""尾块只承载"这一跳真变的事实"：候选整表、固定处置文案、重复 revision 都不许再出现。

这些断言全部从两车道的进口喂（`build_frontdoor_tool_contract` /
`upsert_frontdoor_tool_contract_message` 与前门同名物），不是喂渲染器：渲染器单测全绿
也挡不住装配层把字段覆盖掉。
"""
from __future__ import annotations

from g3ku.runtime.frontdoor.tool_contract import (
    build_frontdoor_tool_contract,
    upsert_frontdoor_tool_contract_message,
)
from main.runtime.node_prompt_contract import NodeRuntimeToolContract

REPAIR_ITEMS = [
    {
        'tool_id': 'agent_browser',
        'description': 'Browser automation',
        'reason': 'missing required paths',
    }
]
REPAIR_SKILLS = [
    {
        'skill_id': 'writing-skills',
        'description': 'Skill maintenance workflow',
        'reason': 'missing required bins',
    }
]
ATTACHMENTS = [
    {
        'name': 'resume.docx',
        'kind': 'file',
        'mime_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'path': 'D:/Uploads/resume.docx',
    }
]


def _stage_state(active: bool) -> dict:
    stages = (
        [{'stage_id': 'stage:1', 'status': 'active', 'stage_goal': 'dispatch', 'tool_round_budget': 12}]
        if active
        else []
    )
    return {'active_stage_id': 'stage:1' if active else '', 'transition_required': False, 'stages': stages}


def _frontdoor_pair(**overrides) -> list[dict]:
    kwargs = dict(
        callable_tool_names=['exec', 'silent'],
        candidate_tool_names=['web_fetch', 'memory_note'],
        candidate_skill_ids=['find-skills'],
        visible_skill_ids=['find-skills'],
        rbac_visible_tool_names=['exec', 'web_fetch', 'memory_note'],
        contract_revision='exp:abc',
        declared_tool_names=['exec', 'silent', 'web_fetch', 'memory_note'],
        repair_required_tool_items=[],
        repair_required_skill_items=[],
        attachment_reopen_targets=[],
    )
    kwargs.update(overrides)
    return upsert_frontdoor_tool_contract_message([], build_frontdoor_tool_contract(**kwargs))


def _node_pair(**overrides) -> list[dict]:
    kwargs = dict(
        node_id='node-1',
        node_kind='execution',
        callable_tool_names=['exec', 'silent'],
        candidate_tool_names=['web_fetch', 'memory_note'],
        visible_skills=[],
        candidate_skill_ids=['find-skills'],
        lightweight_tool_ids=[],
        selection_trace={},
        declared_tool_names=['exec', 'silent', 'web_fetch', 'memory_note'],
        repair_required_tool_items=[],
        repair_required_skill_items=[],
    )
    kwargs.update(overrides)
    contract = NodeRuntimeToolContract(**kwargs)
    return [contract.to_message()]


def _text(messages: list[dict]) -> str:
    return '\n'.join(str(item.get('content') or '') for item in messages)


def test_declared_bundle_hides_the_derivable_candidate_list() -> None:
    # 候选全在 tools[] 上 ⇒ 整行消失（它等于 tools[] − callable − denied，模型自己读得出来）
    assert 'candidate_tools:' not in _text(_frontdoor_pair())
    assert 'undeclared_candidates' not in _text(_frontdoor_pair())
    assert 'candidate_tools:' not in _text(_node_pair())
    assert 'undeclared_candidates' not in _text(_node_pair())


def test_declaration_lag_renders_one_line_on_both_lanes() -> None:
    # memory_note 这一跳刚被放行、还没进 tools[] ⇒ 两车道各渲同一行，字节必须相同
    frontdoor = _text(_frontdoor_pair(declared_tool_names=['exec', 'silent', 'web_fetch']))
    node = _text(_node_pair(declared_tool_names=['exec', 'silent', 'web_fetch']))
    frontdoor_line = next(line for line in frontdoor.splitlines() if line.startswith('undeclared_candidates'))
    node_line = next(line for line in node.splitlines() if line.startswith('undeclared_candidates'))
    assert frontdoor_line == node_line
    assert '`memory_note`' in frontdoor_line
    assert '`web_fetch`' not in frontdoor_line
    # 已可调的名字绝不重复出现在这一行里
    assert '`exec`' not in frontdoor_line


def test_missing_declaration_falls_back_to_the_full_pool() -> None:
    # 证明不了"可推导"就不许省：退回渲全量，并沿用 candidate_tools 这个自称全集的标签
    for text in (_text(_frontdoor_pair(declared_tool_names=[])), _text(_node_pair(declared_tool_names=[]))):
        assert 'candidate_tools: `web_fetch`, `memory_note`' in text
        assert 'undeclared_candidates' not in text


def test_contract_revision_printed_once_per_request_body() -> None:
    text = _text(_frontdoor_pair())
    assert text.count('contract_revision:') == 1


def test_fixed_disposal_prose_left_the_tail_on_both_lanes() -> None:
    overrides = dict(
        repair_required_tool_items=REPAIR_ITEMS,
        repair_required_skill_items=REPAIR_SKILLS,
    )
    for text in (_text(_frontdoor_pair(**overrides)), _text(_node_pair(**overrides))):
        assert 'repair_required_tools:' in text
        # 待修复工具条目留在块里
        assert '`agent_browser`: Browser automation Reason: missing required paths' in text
        # skill 名单整条离开尾块（头部钉住块承载全量、新增可读走一次性持久提示），
        # 待修复 skill 不再在尾块逐条打印。
        assert 'repair_required_skills:' not in text
        assert '`writing-skills`' not in text
        # 回合内不变的处置文案一份都不许出现
        assert 'Reference skill:' not in text
        assert 'These tools must be repaired before use.' not in text
        assert 'These skills must be repaired before viewing their body.' not in text
        assert 'Do not call `load_skill_context` until repaired.' not in text


def test_attachment_handles_remain_without_the_reopen_prose() -> None:
    text = _text(_frontdoor_pair(attachment_reopen_targets=ATTACHMENTS))
    assert 'attachment_reopen_targets:' in text
    assert 'D:/Uploads/resume.docx' in text
    assert 'authoritative reopen lane' not in text
    assert 'remain reopenable in later turns' not in text




def test_tail_pair_is_user_role_and_carries_no_second_system_message() -> None:
    for pair in (_frontdoor_pair(), _node_pair()):
        assert [item['role'] for item in pair] == ['user']
        assert sum(1 for item in pair if item['role'] == 'system') == 0


def test_contract_bytes_are_byte_stable_for_the_same_inputs() -> None:
    first = _frontdoor_pair()
    second = _frontdoor_pair()
    assert str(first[0]['content']) == str(second[0]['content'])

