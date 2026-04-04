from __future__ import annotations

from typing import Any

from main.runtime.stage_budget import CONTROL_STAGE_TOOL_NAMES, FINAL_RESULT_TOOL_NAME, STAGE_TOOL_NAME

_STAGE_BUDGET_NODE_KINDS = {'execution', 'acceptance'}
_NON_SUBSTANTIVE_PROGRESS_TOOL_NAMES = {STAGE_TOOL_NAME, FINAL_RESULT_TOOL_NAME, *CONTROL_STAGE_TOOL_NAMES}


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


def build_ceo_stage_overlay(stage_gate: dict[str, Any] | None) -> str | None:
    gate = dict(stage_gate or {})
    active = gate.get('active_stage') if isinstance(gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
        return (
            '你当前没有处于有效的 CEO 阶段. '
            '如需使用任何工具，请先调用 `submit_next_stage` 并传入简洁的 `stage_goal` 和 1 到 10 的 `tool_round_budget`。'
            '如不需要使用工具，可以直接回复。'
        )
    used = int(active.get('tool_rounds_used') or 0)
    budget = int(active.get('tool_round_budget') or 0)
    goal = str(active.get('stage_goal') or '').strip() or '(empty)'
    if bool(gate.get('transition_required')):
        return (
            f'Current CEO stage budget is exhausted: {used}/{budget}. '
            f'Stage goal: {goal}. '
            'Do not continue this stage with plain prose. '
            'If the current stage is already complete, call `deliver_final_answer` to finish it explicitly; '
            'otherwise call `submit_next_stage` to create the next CEO stage before doing more ordinary work.'
        )
    return (
        f'Active CEO stage goal: {goal}. '
        f'Ordinary tool rounds used: {used}/{budget}. '
        'Any tool use in this turn must directly serve this active stage goal.'
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
    goal = str(active.get('stage_goal') or '').strip() or '(empty)'
    return (
        f'Current CEO stage budget is exhausted: {used}/{budget}. '
        f'Stage goal: {goal}. '
        'Do not finish this stage with plain prose. '
        'If the current stage is already complete, call `deliver_final_answer`; '
        'otherwise call `submit_next_stage` to create the next CEO stage before continuing ordinary work.'
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
    goal = str(active.get('stage_goal') or '').strip() or '(empty)'
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
            )
        return (
            f'当前阶段【{mode}】已达到工具轮次预算 {used}/{budget}，阶段目标是：{goal}。'
            '你现在必须先总结当前阶段并调用 `submit_next_stage` 创建下一阶段；'
            f'创建下一阶段时要结合总目标和已完成阶段结果，不能机械重复上一阶段预算 {previous_budget or budget}；'
            '如果上一阶段仍未收敛，应根据剩余工作适当放大预算，但不能超过 10；'
            '在此之前不能继续使用普通工具，也不能继续派生子节点。'
        )
    if normalized_kind == 'acceptance':
        return (
            f'当前验收阶段【{status}】目标：{goal}。'
            f'当前普通工具轮次使用 {used}/{budget}。'
            '除创建新阶段外，其余所有思考、工具调用和验收裁定都必须只服务于当前阶段目标。'
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
