from __future__ import annotations

from typing import Any


def build_ceo_stage_overlay(stage_gate: dict[str, Any] | None) -> str | None:
    gate = dict(stage_gate or {})
    active = gate.get('active_stage') if isinstance(gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
        return (
            'You do not have an active CEO stage. '
            'If you need any tool, first call `submit_next_stage` with a concise `stage_goal` and '
            'a `tool_round_budget` between 1 and 10. If no tool is needed, you may reply directly.'
        )
    used = int(active.get('tool_rounds_used') or 0)
    budget = int(active.get('tool_round_budget') or 0)
    goal = str(active.get('stage_goal') or '').strip() or '(empty)'
    if bool(gate.get('transition_required')):
        return (
            f'Current CEO stage budget is exhausted: {used}/{budget}. '
            f'Stage goal: {goal}. '
            'Before using more tools, summarize progress and call `submit_next_stage` to create the next stage.'
        )
    return (
        f'Active CEO stage goal: {goal}. '
        f'Ordinary tool rounds used: {used}/{budget}. '
        'Any tool use in this turn must directly serve this active stage goal.'
    )


def build_execution_stage_overlay(*, node_kind: str, stage_gate: dict[str, Any]) -> str | None:
    if str(node_kind or '').strip().lower() != 'execution':
        return None
    if not bool(stage_gate.get('enabled')):
        return None
    active = stage_gate.get('active_stage') if isinstance(stage_gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
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
        return (
            f'当前阶段【{mode}】已达到工具轮次预算 {used}/{budget}，阶段目标是：{goal}。'
            '你现在必须先总结当前阶段并调用 `submit_next_stage` 创建下一阶段；'
            f'创建下一阶段时要结合总目标和已完成阶段结果，不能机械重复上一阶段预算 {previous_budget or budget}；'
            '如果上一阶段仍未收敛，应根据剩余工作适当放大预算，但不能超过 10；'
            '在此之前不能继续使用普通工具，也不能继续派生子节点。'
        )
    return (
        f'当前阶段【{mode} | {status}】目标：{goal}。'
        f'当前普通工具轮次使用 {used}/{budget}。'
        '除创建新阶段外，其余所有思考、工具调用和派生行为都必须只服务于当前阶段目标。'
    )


def build_execution_stage_result_block_message(*, node_kind: str, stage_gate: dict[str, Any]) -> str:
    if str(node_kind or '').strip().lower() != 'execution':
        return ''
    if not bool(stage_gate.get('enabled')):
        return ''
    if not bool(stage_gate.get('has_active_stage')):
        return (
            '当前节点还没有创建第一个阶段。请先调用 `submit_next_stage` 创建阶段，'
            '再继续推进，不要直接结束节点。'
        )
    if bool(stage_gate.get('transition_required')):
        return (
            '当前阶段预算已经耗尽。请先总结当前阶段并调用 `submit_next_stage` 创建下一阶段，'
            '之后再继续推进或交付结果。'
        )
    return ''
