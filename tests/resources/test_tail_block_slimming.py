"""尾部工具合同块：前端车道已退役，节点车道仍渲染。

前端尾块退役后，静态声明进头部快照、这一跳真变的事实走一次性 `[RUNTIME_NOTICE]`，
`upsert_frontdoor_tool_contract_message` 只剩"剥离历史块"的语义；节点车道的
`## Runtime Tool Contract` 仍在，声明缺失时退回全量候选池。断言都从装配进口喂，
不喂渲染器：渲染器单测全绿也挡不住装配层把字段覆盖掉。
"""
from __future__ import annotations

from g3ku.runtime.frontdoor.tool_contract import (
    FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING,
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


def _frontdoor_args(**overrides) -> dict:
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
    return kwargs


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
    return [NodeRuntimeToolContract(**kwargs).to_message()]


def _text(messages: list[dict]) -> str:
    return '\n'.join(str(item.get('content') or '') for item in messages)


def test_frontdoor_tail_block_is_retired_and_only_strips_history() -> None:
    """这条进口只做剥离：历史块消失，且不会追加第二份新块。"""
    contract = build_frontdoor_tool_contract(**_frontdoor_args())
    historical = {
        'role': 'user',
        'content': (
            f'{FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING}\n'
            'kind: frontdoor_dynamic_tool_contract\ncontract_revision: exp:old'
        ),
    }
    kept = {'role': 'user', 'content': '这一轮的问题'}

    assert upsert_frontdoor_tool_contract_message([historical, kept], contract) == [kept]


def test_frontdoor_lane_renders_no_pool_or_revision_lines_at_all() -> None:
    # 声明缺失也不回填：候选与 revision 都已搬进头部快照。
    contract = build_frontdoor_tool_contract(**_frontdoor_args(declared_tool_names=[]))
    assert _text(upsert_frontdoor_tool_contract_message([], contract)) == ''


def test_declared_bundle_hides_the_derivable_candidate_list() -> None:
    # 候选全在 tools[] 上 ⇒ 整行消失（它等于 tools[] − callable − denied，模型自己读得出来）
    text = _text(_node_pair())
    assert 'candidate_tools:' not in text
    assert 'undeclared_candidates' not in text


def test_declaration_lag_prints_the_undeclared_candidates_once() -> None:
    # memory_note 这一跳刚被放行、还没进 tools[] ⇒ 只印差集那一行
    text = _text(_node_pair(declared_tool_names=['exec', 'silent', 'web_fetch']))
    line = next(item for item in text.splitlines() if item.startswith('undeclared_candidates'))
    assert '`memory_note`' in line
    assert '`web_fetch`' not in line
    # 已可调的名字绝不重复出现在这一行里
    assert '`exec`' not in line


def test_missing_declaration_falls_back_to_the_full_pool() -> None:
    # 证明不了"可推导"就不许省：退回渲全量，并沿用 candidate_tools 这个自称全集的标签
    text = _text(_node_pair(declared_tool_names=[]))
    assert 'candidate_tools: `web_fetch`, `memory_note`' in text
    assert 'undeclared_candidates' not in text


def test_repair_tools_stay_while_skills_leave_the_tail() -> None:
    text = _text(
        _node_pair(repair_required_tool_items=REPAIR_ITEMS, repair_required_skill_items=REPAIR_SKILLS)
    )
    assert 'repair_required_tools:' in text
    assert '`agent_browser`: Browser automation Reason: missing required paths' in text
    # skill 名单整条离开尾块（头部钉住块承载全量、新增可读走一次性持久提示）
    assert 'repair_required_skills:' not in text
    assert '`writing-skills`' not in text
    # 回合内不变的处置文案一份都不许出现
    assert 'Reference skill:' not in text
    assert 'These tools must be repaired before use.' not in text
    assert 'These skills must be repaired before viewing their body.' not in text
    assert 'Do not call `load_skill_context` until repaired.' not in text


def test_attachment_reopen_handles_are_not_reprinted_in_any_tail_block() -> None:
    # 尾部不再承载附件重开面：前端那条进口连内容都不产出。
    contract = build_frontdoor_tool_contract(**_frontdoor_args(attachment_reopen_targets=ATTACHMENTS))
    tail = _text(upsert_frontdoor_tool_contract_message([], contract))
    assert 'attachment_reopen_targets:' not in tail
    assert 'D:/Uploads/resume.docx' not in tail
    assert 'authoritative reopen lane' not in tail


def test_tail_pair_is_user_role_and_carries_no_second_system_message() -> None:
    pair = _node_pair()
    assert [item['role'] for item in pair] == ['user']
    assert sum(1 for item in pair if item['role'] == 'system') == 0


def test_contract_bytes_are_byte_stable_for_the_same_inputs() -> None:
    assert str(_node_pair()[0]['content']) == str(_node_pair()[0]['content'])
