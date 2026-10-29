"""The orchestration topology lives in the project skill, not in the standing prompts."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = REPO_ROOT / 'skills' / 'async-task-orchestration'
FRONTDOOR_PROMPT = REPO_ROOT / 'g3ku' / 'runtime' / 'prompts' / 'ceo_frontdoor.md'
NODE_PROMPT = REPO_ROOT / 'main' / 'prompts' / 'node_execution.md'
SKILL_ID = 'async-task-orchestration'
RETIRED_SKILL_ID = 'multi-node-discussion'


def test_skill_manifest_is_discoverable_and_exposed() -> None:
    manifest = yaml.safe_load((SKILL_ROOT / 'resource.yaml').read_text(encoding='utf-8'))

    assert manifest['kind'] == 'skill'
    assert manifest['name'] == SKILL_ID
    assert manifest['content']['main'] == 'SKILL.md'
    assert manifest['exposure'] == {'agent': True, 'main_runtime': True}
    assert (SKILL_ROOT / 'SKILL.md').is_file()
    assert (SKILL_ROOT / 'references' / 'patterns.md').is_file()
    assert (SKILL_ROOT / 'references' / 'node-messaging.md').is_file()


def test_retired_discussion_skill_is_gone() -> None:
    assert not (REPO_ROOT / 'skills' / RETIRED_SKILL_ID).exists()


def test_skill_carries_the_orchestration_contract() -> None:
    body = (SKILL_ROOT / 'SKILL.md').read_text(encoding='utf-8')

    # 三条铁律：声明等待、等齐与停靠的区别、跨子树不可达
    assert '等谁就写谁' in body
    assert '等齐用批次，等一个是停靠' in body
    assert '跨子树要等，说明拓扑选错了' in body
    # 停靠被拒的三档原因与环打破语义
    assert '永远不会被满足' in body
    assert '强制唤醒环里最浅' in body
    # 辩论契约（并入本 skill 的既有条款）
    assert '父节点不要按轮次重建讨论者' in body
    assert '轮次完成' in body
    assert '收齐本批 N 条' in body
    assert '进入终审' in body
    assert '父节点不代转发言' in body
    assert '不替讨论者拟定每轮质询目标' in body
    assert '找**执行同级**' in body
    assert '不读、不向同级索取证据' in body
    assert '不要用“等整批子节点都终局”当轮次 barrier' in body


def test_skill_names_no_node_side_tool() -> None:
    """The skill is a topology contract, not a tool manifest.

    A hard-coded node-side tool name reads as a capability verdict when that
    name is absent from the target node's turn, and the discussion then degrades
    into file polling or a rejected seed declaration.
    """
    body = (SKILL_ROOT / 'SKILL.md').read_text(encoding='utf-8')

    for tool_name in (
        'send_message',
        'view_message_history',
        'enter_wait',
        'view_node_tree',
        'spawn_child_nodes',
        'manage_child_nodes',
        'list_child_nodes',
        'read_child_progress',
        'submit_final_result',
        'child_final',
        'wait_all_children',
        'schedule_wake',
    ):
        assert tool_name not in body, f'the skill must describe the capability, not `{tool_name}`'


def test_standing_prompts_point_to_the_skill_and_keep_the_append_rule() -> None:
    frontdoor = FRONTDOOR_PROMPT.read_text(encoding='utf-8')
    node = NODE_PROMPT.read_text(encoding='utf-8')

    # The frontdoor only names the capability; the node lane keeps the explicit load form.
    assert SKILL_ID in frontdoor
    assert f'load_skill_context(skill_id="{SKILL_ID}")' in node
    # The frontdoor may append new requirements/constraints to the current task.
    assert '新增需求/约束' in frontdoor
    assert '当前任务仍在进行且任务树仍适用' in frontdoor
    # The detailed topology moved into the skill; the standing prompts only
    # keep the capability and the pointer.
    assert '父节点只发轮次开始/结束与终审' not in frontdoor
    assert '父节点只发轮次开始/结束/进入终审' not in node
    assert '你与执行同级可以直接互相发消息讨论' in node
    # The retired skill id must not survive anywhere in the standing prompts.
    assert RETIRED_SKILL_ID not in frontdoor
    assert RETIRED_SKILL_ID not in node
