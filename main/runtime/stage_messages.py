from __future__ import annotations

from typing import Any

from main.runtime.stage_budget import (
    CONTEXT_LOADER_STAGE_TOOL_NAMES,
    CONTROL_STAGE_TOOL_NAMES,
    FINAL_RESULT_TOOL_NAME,
    STAGE_TOOL_NAME,
)

_STAGE_BUDGET_NODE_KINDS = {'execution', 'acceptance'}
_NON_SUBSTANTIVE_PROGRESS_TOOL_NAMES = {STAGE_TOOL_NAME, FINAL_RESULT_TOOL_NAME, *CONTROL_STAGE_TOOL_NAMES}
_PUBLIC_CONTEXT_LOADER_STAGE_TOOL_NAMES = tuple(
    name for name in sorted(CONTEXT_LOADER_STAGE_TOOL_NAMES) if not str(name or '').endswith('_v2')
)
_NON_BUDGET_ACCOUNTING_TOOL_NAMES = (
    STAGE_TOOL_NAME,
    FINAL_RESULT_TOOL_NAME,
    'spawn_child_nodes',
    'wait_tool_execution',
    'stop_tool_execution',
    *_PUBLIC_CONTEXT_LOADER_STAGE_TOOL_NAMES,
)


def _stage_has_substantive_progress(active: dict[str, Any]) -> bool:
    for round_item in list(active.get('rounds') or []):
        if not isinstance(round_item, dict):
            continue
        tool_names = [
            str(name or '').strip()
            for name in list(round_item.get('tool_names') or [])
            if str(name or '').strip()
        ]
        if any(name not in _NON_SUBSTANTIVE_PROGRESS_TOOL_NAMES for name in tool_names):
            return True
    return False


def _execution_stage_budget_accounting_note(active: dict[str, Any]) -> str:
    rounds = [item for item in list(active.get('rounds') or []) if isinstance(item, dict)]
    counted_rounds = sum(1 for item in rounds if bool(item.get('budget_counted')))
    non_budget_tools = '、'.join(f'`{name}`' for name in _NON_BUDGET_ACCOUNTING_TOOL_NAMES)
    note = (
        ' 预算记账由系统决定，不要按工具名猜：'
        f'不会计入本阶段 `tool_rounds_used` 的工具只有 {non_budget_tools}。'
        ' 这不代表预算耗尽后这些工具都一定还能继续调用；是否允许调用仍以阶段门控和系统错误提示为准。'
        ' 历史 round 是否扣预算，只看 `rounds[*].budget_counted`，其中 `budget_counted` 是权威字段；'
        '`tool_rounds_used` 只统计 `budget_counted=true` 的 round。'
    )
    if not rounds:
        return f'{note} 当前还没有任何已记录的 round。'
    latest = rounds[-1]
    latest_index = int(latest.get('round_index') or len(rounds) or 0)
    latest_tools = [
        str(name or '').strip()
        for name in list(latest.get('tool_names') or [])
        if str(name or '').strip()
    ]
    latest_tools_text = '、'.join(latest_tools) if latest_tools else '（无工具名）'
    latest_counted = str(bool(latest.get('budget_counted'))).lower()
    return (
        f'{note} 当前已记录 {len(rounds)} 轮，其中扣预算 {counted_rounds} 轮。'
        f'最近一轮是第 {latest_index} 轮，工具：{latest_tools_text}，budget_counted={latest_counted}。'
    )


def build_ceo_stage_overlay(stage_gate: dict[str, Any] | None) -> str | None:
    gate = dict(stage_gate or {})
    active = gate.get('active_stage') if isinstance(gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
        return (
            '当前没有有效的 CEO 阶段。'
            '如果本轮需要使用任何工具，你现在必须先调用 `submit_next_stage`，并传入简洁的 `stage_goal` 和 1 到 10 的 `tool_round_budget`。'
            '如果本轮不需要使用工具，可以直接回复。'
        )
    used = int(active.get('tool_rounds_used') or 0)
    budget = int(active.get('tool_round_budget') or 0)
    goal = str(active.get('stage_goal') or '').strip() or '（空）'
    final_note = (
        ' 当前阶段已标记为最终收敛阶段；它不会仅因为预算耗尽而被强制切换，且不得调用 `spawn_child_nodes`。'
        if bool(active.get('final_stage'))
        else ''
    )
    if bool(gate.get('transition_required')):
        return (
            f'当前 CEO 阶段工具轮次预算已耗尽：{used}/{budget}。'
            f'阶段目标：{goal}。'
            '如需继续使用工具，必须先总结本阶段进展，并调用 `submit_next_stage` 创建下一阶段。'
        )
    return (
        f'当前 CEO 阶段目标：{goal}。'
        f'当前普通工具轮次使用：{used}/{budget}。'
        '本轮任何工具调用都必须直接服务于当前阶段目标。'
        f'{final_note}'
    )


def build_ceo_stage_result_block_message(stage_gate: dict[str, Any] | None) -> str:
    gate = dict(stage_gate or {})
    active = gate.get('active_stage') if isinstance(gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
        return ''
    if not bool(gate.get('transition_required')):
        return ''
    used = int(active.get('tool_rounds_used') or 0)
    budget = int(active.get('tool_round_budget') or 0)
    goal = str(active.get('stage_goal') or '').strip() or '（空）'
    return (
        f'当前 CEO 阶段工具轮次预算已耗尽：{used}/{budget}。'
        f'阶段目标：{goal}。'
        '先不要直接结束。请先总结本阶段已完成的进展，并调用 `submit_next_stage` 创建下一阶段；'
        '之后再继续工作，或从新阶段交付最终回复。'
    )


def build_execution_stage_overlay(*, node_kind: str, stage_gate: dict[str, Any]) -> str | None:
    normalized_kind = str(node_kind or '').strip().lower()
    if normalized_kind not in _STAGE_BUDGET_NODE_KINDS:
        return None
    if not bool(stage_gate.get('enabled')):
        return None
    active = stage_gate.get('active_stage') if isinstance(stage_gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
        if normalized_kind == 'acceptance':
            return (
                '当前没有活动阶段。你必须先调用 `submit_next_stage` 创建第一个验收阶段，'
                '填写清晰的 `stage_goal` 和 1 到 10 的 `tool_round_budget`，'
                '并在 `stage_goal` 中说明本阶段要重点核验哪些证据、结论和 skills。'
            )
        return (
            '当前没有活动阶段。你必须先调用 `submit_next_stage` 创建第一个阶段，'
            '填写清晰的 `stage_goal` 和 1 到 10 的 `tool_round_budget`，'
            '并在 `stage_goal` 中说明哪些工作优先派生子节点、哪些工作由当前节点自行完成。'
        )
    used = int(active.get('tool_rounds_used') or 0)
    budget = int(active.get('tool_round_budget') or 0)
    goal = str(active.get('stage_goal') or '').strip() or '（空）'
    final_note = (
        ' 当前阶段已标记为最终收敛阶段；它不会仅因为预算耗尽而被强制切换，且不得调用 `spawn_child_nodes`。'
        if bool(active.get('final_stage'))
        else ''
    )
    mode = str(active.get('mode') or '').strip() or '自主执行'
    status = str(active.get('status') or '').strip() or '进行中'
    if bool(stage_gate.get('transition_required')):
        completed = list(stage_gate.get('completed_stages') or []) if isinstance(stage_gate, dict) else []
        previous = completed[-1] if completed else {}
        previous_budget = int((previous or {}).get('tool_round_budget') or 0)
        if normalized_kind == 'acceptance':
            return (
                f'当前验收阶段已达到工具轮次预算 {used}/{budget}，阶段目标是：{goal}。'
                '你现在必须先总结本阶段已经核验的证据、结论和仍未确认的点，'
                '再调用 `submit_next_stage` 创建下一验收阶段；'
                f'创建下一阶段时要结合已检查结果，不能机械重复上一阶段预算 {previous_budget or budget}；'
                '如果上一阶段仍未收敛，应根据剩余核验工作适当放大预算，但不能超过 10；'
                '在此之前不能继续使用普通工具，也不能直接输出最终验收结论。'
                f'{_execution_stage_budget_accounting_note(active)}'
            )
        return (
            f'当前阶段【{mode}】已达到工具轮次预算 {used}/{budget}，阶段目标是：{goal}。'
            '你现在必须先总结当前阶段并调用 `submit_next_stage` 创建下一阶段；'
            f'创建下一阶段时要结合总目标和已完成阶段结果，不能机械重复上一阶段预算 {previous_budget or budget}；'
            '如果上一阶段仍未收敛，应根据剩余工作适当放大预算，但不能超过 10；'
            '在此之前不能继续使用普通工具，也不能继续派生子节点。'
            f'{_execution_stage_budget_accounting_note(active)}'
            f'{final_note}'
        )
    if normalized_kind == 'acceptance':
        return (
            f'当前验收阶段【{status}】目标：{goal}。'
            f'当前普通工具轮次使用 {used}/{budget}。'
            '除创建新阶段外，其余所有思考、工具调用和验收裁定都必须只服务于当前阶段目标。'
            f'{_execution_stage_budget_accounting_note(active)}'
        )
    reminder = ''
    if not _stage_has_substantive_progress(active):
        reminder = (
            ' 当前阶段尚未产生任何实质执行回合；'
            '不要再次调用 `submit_next_stage`，必须先执行普通工具或派生子节点。'
        )
    return (
        f'当前阶段【{mode} | {status}】目标：{goal}。'
        f'当前普通工具轮次使用 {used}/{budget}。'
        '除创建新阶段外，其余所有思考、工具调用和派生行为都必须只服务于当前阶段目标。'
        f'{_execution_stage_budget_accounting_note(active)}'
        f'{final_note}'
        f'{reminder}'
    )


def build_execution_stage_result_block_message(*, node_kind: str, stage_gate: dict[str, Any]) -> str:
    normalized_kind = str(node_kind or '').strip().lower()
    if normalized_kind not in _STAGE_BUDGET_NODE_KINDS:
        return ''
    if not bool(stage_gate.get('enabled')):
        return ''
    if not bool(stage_gate.get('has_active_stage')):
        if normalized_kind == 'acceptance':
            return (
                '当前验收节点还没有创建第一个阶段。请先调用 `submit_next_stage` 创建验收阶段，'
                '再继续验证或输出验收结论，不要直接结束节点。'
            )
        return (
            '当前节点还没有创建第一个阶段。请先调用 `submit_next_stage` 创建阶段，'
            '再继续推进，不要直接结束节点。'
        )
    if bool(stage_gate.get('transition_required')):
        if normalized_kind == 'acceptance':
            return (
                '当前验收阶段预算已经耗尽。请先总结本阶段已核验内容并调用 `submit_next_stage` 创建下一阶段，'
                '之后再继续验证或输出验收结论。'
            )
        return (
            '当前阶段预算已经耗尽。请先总结当前阶段并调用 `submit_next_stage` 创建下一阶段，'
            '之后再继续推进或交付结果。'
        )
    return ''
