"""预置契约的纯判据：原因优先级、体积裁剪、声明名单规范形。

这些用例钉的是**判据本身**，不需要服务层：服务层只负责取数，一旦两处各判一遍优先级就会漂移。
"""

from __future__ import annotations

import pytest

from g3ku.utils.helpers import normalize_seed_tool_names
from main.runtime.node_seed_contracts import (
    NODE_SEED_CONTRACT_SINGLE_CHAR_LIMIT,
    NODE_SEED_CONTRACT_TOTAL_CHAR_LIMIT,
    SEED_DROP_REASONS,
    seed_drop_reason,
    trim_seed_contracts,
)


def _pass_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        'is_never_contract': False,
        'is_known': True,
        'is_visible': True,
        'is_repair_required': False,
        'has_toolskill_body': True,
        'rendered_block': '[TOOL_CONTRACT_V1:x:tcf:0000]\nbody\n[/TOOL_CONTRACT_V1]',
    }
    kwargs.update(overrides)
    return kwargs


def test_seed_drop_reason_returns_empty_when_every_gate_passes() -> None:
    assert seed_drop_reason(**_pass_kwargs()) == ''  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ('overrides', 'expected'),
    [
        ({'is_never_contract': True}, 'already_always_callable'),
        ({'is_known': False}, 'unknown_tool'),
        ({'is_visible': False}, 'not_visible_for_role'),
        ({'is_repair_required': True}, 'repair_required'),
        ({'has_toolskill_body': False}, 'no_toolskill'),
        ({'rendered_block': ''}, 'render_empty'),
        ({'rendered_block': '   '}, 'render_empty'),
    ],
)
def test_seed_drop_reason_maps_each_failed_gate_to_its_own_reason(
    overrides: dict[str, object],
    expected: str,
) -> None:
    assert seed_drop_reason(**_pass_kwargs(**overrides)) == expected  # type: ignore[arg-type]


def test_unknown_tool_wins_over_not_visible_for_role() -> None:
    """拼错的名字同时也不在可见集里；必须先报 `unknown_tool`。

    反过来报「角色不可见」会把派发方引向改角色，而它下一步还是填同一个错名字。
    """
    assert seed_drop_reason(**_pass_kwargs(is_known=False, is_visible=False)) == 'unknown_tool'  # type: ignore[arg-type]


def test_repair_required_wins_over_not_visible_for_role() -> None:
    """待修复资源在治理读口里同样不可见（`enabled = tool.enabled and tool.available`）。

    反过来就把「可以提示修复」误报成「这个角色用不了，别再报这个名字」，派发方从此不再报它。
    """
    assert seed_drop_reason(**_pass_kwargs(is_visible=False, is_repair_required=True)) == 'repair_required'  # type: ignore[arg-type]


def test_never_contract_wins_over_everything() -> None:
    """常驻内置是 no-op 不是失败，必须单列，否则派发方以为预置整条没生效。"""
    reason = seed_drop_reason(  # type: ignore[arg-type]
        **_pass_kwargs(is_never_contract=True, is_known=False, is_visible=False, is_repair_required=True)
    )
    assert reason == 'already_always_callable'


def test_no_toolskill_is_not_render_empty() -> None:
    """正文为空是资源侧没有 toolskill；有正文却印不出块是指纹/身份缺失，排查方向不同。"""
    assert seed_drop_reason(**_pass_kwargs(has_toolskill_body=False, rendered_block='')) == 'no_toolskill'  # type: ignore[arg-type]
    assert seed_drop_reason(**_pass_kwargs(has_toolskill_body=True, rendered_block='')) == 'render_empty'  # type: ignore[arg-type]


def test_trim_keeps_declaration_order_and_drops_only_the_oversized_item() -> None:
    """单份超限不能挤掉后面的瘦工具。"""
    items = [
        {'tool': 'fat', 'chars': NODE_SEED_CONTRACT_SINGLE_CHAR_LIMIT + 1},
        {'tool': 'thin', 'chars': 100},
    ]
    kept, dropped = trim_seed_contracts(items)
    assert [item['tool'] for item in kept] == ['thin']
    assert dropped == [{'tool': 'fat', 'reason': 'single_char_limit'}]


def test_trim_enforces_the_total_budget_in_order() -> None:
    """每条都在单份上限内，但累计顶到总量：超限那条记 total_char_limit，后面装得下的仍装。"""
    items = [
        {'tool': 'a', 'chars': 15000},
        {'tool': 'b', 'chars': 10000},
        {'tool': 'c', 'chars': 5000},
    ]
    kept, dropped = trim_seed_contracts(items)
    assert [item['tool'] for item in kept] == ['a', 'c']
    assert dropped == [{'tool': 'b', 'reason': 'total_char_limit'}]
    assert sum(int(item['chars']) for item in kept) <= NODE_SEED_CONTRACT_TOTAL_CHAR_LIMIT


def test_trim_accepts_everything_within_both_limits() -> None:
    items = [{'tool': 'a', 'chars': 6187}, {'tool': 'b', 'chars': 1655}, {'tool': 'c', 'chars': 860}]
    kept, dropped = trim_seed_contracts(items)
    assert [item['tool'] for item in kept] == ['a', 'b', 'c']
    assert dropped == []


def test_trim_returns_empty_pair_for_empty_input() -> None:
    assert trim_seed_contracts([]) == ([], [])
    assert trim_seed_contracts(None) == ([], [])


def test_normalize_seed_tool_names_strips_dedupes_and_keeps_order() -> None:
    assert normalize_seed_tool_names([' content_open ', 'filesystem_edit', 'content_open', '', None]) == [
        'content_open',
        'filesystem_edit',
    ]


def test_normalize_seed_tool_names_accepts_empty_input() -> None:
    assert normalize_seed_tool_names([]) == []
    assert normalize_seed_tool_names(None) == []


def test_seed_drop_reasons_covers_the_documented_eight() -> None:
    """文档与判据共用这张名单：漏一个 reason 就有一条静默丢弃的路。"""
    assert set(SEED_DROP_REASONS) == {
        'already_always_callable',
        'unknown_tool',
        'not_visible_for_role',
        'repair_required',
        'no_toolskill',
        'render_empty',
        'single_char_limit',
        'total_char_limit',
    }
