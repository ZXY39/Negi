from __future__ import annotations

from typing import Any

from main.runtime.stage_budget import (
    CONTEXT_LOADER_STAGE_TOOL_NAMES,
    CONTROL_STAGE_TOOL_NAMES,
    STAGE_TOOL_NAME,
    STAGE_TOOL_ROUND_BUDGET_MAX,
    STAGE_TOOL_ROUND_BUDGET_MIN,
)
from main.runtime.tool_names import FINAL_RESULT_TOOL_NAME

_STAGE_BUDGET_NODE_KINDS = {'execution', 'acceptance'}
# 整回合内 CEO 纯文本收尾最多被打回的次数（仅 B 类：阶段刚创建且无实质工具轮）。
# 预算耗尽不再打回文本收尾：旧 A/B 双拦截在 budget=1 下构成无出口死循环
# （2026-09-05 会话 22124b26b86b 事故：4 次回复全被丢弃、4 个阶段、10 次 LLM 调用、0 送达）。
STAGE_REPLY_BOUNCE_LIMIT = 1
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
    non_budget_tools = ', '.join(f'`{name}`' for name in _NON_BUDGET_ACCOUNTING_TOOL_NAMES)
    note = (
        ' Budget accounting is decided by the runtime, never guessed from tool names: '
        f'the only tools that do not add to this stage `tool_rounds_used` are {non_budget_tools}. '
        'Whether a call is allowed still follows the stage gate and what the runtime returns: a lone ordinary '
        'tool call runs once, and only a later violation is hard-blocked. '
        'Whether a recorded round cost budget is decided only by `budget_counted` on that round, which is the '
        'authoritative field; `tool_rounds_used` counts rounds with `budget_counted=true`.'
    )
    if not rounds:
        return f'{note} No round has been recorded yet.'
    latest = rounds[-1]
    latest_index = int(latest.get('round_index') or len(rounds) or 0)
    latest_tools = [
        str(name or '').strip()
        for name in list(latest.get('tool_names') or [])
        if str(name or '').strip()
    ]
    latest_tools_text = ', '.join(latest_tools) if latest_tools else '(no tool names)'
    latest_counted = str(bool(latest.get('budget_counted'))).lower()
    return (
        f'{note} Rounds recorded: {len(rounds)}, of which {counted_rounds} charged against budget. '
        f'The latest round is round {latest_index}, tools: {latest_tools_text}, budget_counted={latest_counted}.'
    )


def build_ceo_stage_overlay(stage_gate: dict[str, Any] | None) -> str | None:
    gate = dict(stage_gate or {})
    active = gate.get('active_stage') if isinstance(gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
        return (
            'There is no active CEO stage. '
            'If this round needs any tool, submit `submit_next_stage` in the same message as the target tool '
            f'(pass a concise `stage_goal` and a `tool_round_budget` between {STAGE_TOOL_ROUND_BUDGET_MIN} and '
            f'{STAGE_TOOL_ROUND_BUDGET_MAX}): `submit_next_stage` runs first, then the target tool lands as the '
            'first round of the new stage and counts against its budget. '
            'A lone ordinary tool call runs once without an active stage; the next lone call is blocked. '
            'If this round needs no tool, reply directly.'
        )
    used = int(active.get('tool_rounds_used') or 0)
    budget = int(active.get('tool_round_budget') or 0)
    goal = str(active.get('stage_goal') or '').strip() or '(empty)'
    final_note = (
        ' The current stage is marked as the final convergence stage; it is never forced to switch just because '
        'the budget is exhausted, and `spawn_child_nodes` must not be called on it.'
        if bool(active.get('final_stage'))
        else ''
    )
    reminder = ''
    if not bool(gate.get('transition_required')) and not _stage_has_substantive_progress(active):
        reminder = (
            ' The current stage exists but no real tool result has run yet. '
            'Do not output a verbal explanation only, and do not call `submit_next_stage` again; '
            'call one ordinary tool that serves the current stage goal first.'
        )
    if bool(gate.get('transition_required')):
        return (
            f'The CEO stage tool-round budget is exhausted: {used}/{budget}. '
            f'Stage goal: {goal}. '
            'To keep using tools, submit `submit_next_stage` in the same batch as the target tool to open the '
            'next stage; the ordinary tools in that batch run as the first round of the new stage and count '
            'against its budget. '
            'If no further tool calls are needed, output a text reply to close the round; the runtime closes the '
            'current stage automatically.'
        )
    return (
        f'Current CEO stage goal: {goal}. '
        f'Ordinary tool rounds used: {used}/{budget}. '
        'Every tool call this round must directly serve the current stage goal.'
        f'{reminder}'
    )


def build_ceo_stage_reply_bounce_message(stage_gate: dict[str, Any] | None) -> str:
    """CEO 纯文本收尾打回文本（B 类）：仅「阶段刚创建且尚无实质工具轮」时非空。

    预算耗尽（transition_required）与无活动阶段一律返回 ''：耗尽只约束继续调用工具，
    不阻止文本收尾——旧的耗尽打回与无工具轮打回互为出口封堵，在 budget=1 时构成
    无解死循环（会话 22124b26b86b）。打回由 `STAGE_REPLY_BOUNCE_LIMIT` 限次。
    """
    gate = dict(stage_gate or {})
    active = gate.get('active_stage') if isinstance(gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
        return ''
    if bool(gate.get('transition_required')):
        return ''
    if _stage_has_substantive_progress(active):
        return ''
    goal = str(active.get('stage_goal') or '').strip() or '(empty)'
    return (
        'The CEO stage was created but no real tool result exists yet. '
        f'Stage goal: {goal}. '
        'Do not reply with a verbal explanation only, and do not call `submit_next_stage` again. '
        'Call an ordinary tool that serves the current stage goal now; '
        'once at least one real tool round exists, continue with the summary or finish. '
        'If this round genuinely needs no tool call and is meant to deliver the final reply directly, output the '
        'reply text again; the runtime lets it through and closes the current stage automatically.'
    )


def build_ceo_stage_result_block_message(stage_gate: dict[str, Any] | None) -> str:
    gate = dict(stage_gate or {})
    active = gate.get('active_stage') if isinstance(gate.get('active_stage'), dict) else None
    if not isinstance(active, dict):
        return ''
    if not bool(gate.get('transition_required')) and not _stage_has_substantive_progress(active):
        return build_ceo_stage_reply_bounce_message(gate)
    if not bool(gate.get('transition_required')):
        return ''
    used = int(active.get('tool_rounds_used') or 0)
    budget = int(active.get('tool_round_budget') or 0)
    goal = str(active.get('stage_goal') or '').strip() or '(empty)'
    return (
        f'The CEO stage tool-round budget is exhausted: {used}/{budget}. '
        f'Stage goal: {goal}. '
        'To keep calling tools, submit `submit_next_stage` in the same batch as the target tool to open the next '
        'stage; if no more tool calls are needed, output the final reply for the user directly and the runtime '
        'closes the current stage.'
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
                'There is no active stage. If you need tools, submit `submit_next_stage` in the same message as '
                f'the target tool (write a clear `stage_goal` and a `tool_round_budget` between '
                f'{STAGE_TOOL_ROUND_BUDGET_MIN} and {STAGE_TOOL_ROUND_BUDGET_MAX}): submit_next_stage runs first, '
                'then the target tools book the first round of the new stage and count against its budget. '
                'A lone ordinary tool call runs once without an active stage; the next lone call is blocked.'
            )
        return (
            'There is no active stage. If you need tools, submit `submit_next_stage` in the same message as '
            f'the target tool (write a clear `stage_goal` and a `tool_round_budget` between '
            f'{STAGE_TOOL_ROUND_BUDGET_MIN} and {STAGE_TOOL_ROUND_BUDGET_MAX}): submit_next_stage runs first, '
            'then the target tools book the first round of the new stage and count against its budget. '
            'A lone ordinary tool call runs once without an active stage; the next lone call is blocked.'
        )
    used = int(active.get('tool_rounds_used') or 0)
    budget = int(active.get('tool_round_budget') or 0)
    goal = str(active.get('stage_goal') or '').strip() or '(empty)'
    final_note = (
        ' The current stage is marked as the final convergence stage; it is never forced to switch just because '
        'the budget is exhausted, and `spawn_child_nodes` must not be called on it.'
        if bool(active.get('final_stage'))
        else ''
    )
    mode = str(active.get('mode') or '').strip() or 'autonomous'
    status = str(active.get('status') or '').strip() or 'in progress'
    if bool(stage_gate.get('transition_required')):
        completed = list(stage_gate.get('completed_stages') or []) if isinstance(stage_gate, dict) else []
        previous = completed[-1] if completed else {}
        previous_budget = int((previous or {}).get('tool_round_budget') or 0)
        if normalized_kind == 'acceptance':
            return (
                f'The acceptance stage reached its tool-round budget {used}/{budget}. Stage goal: {goal}. '
                'To keep using tools, submit `submit_next_stage` in the same batch as the target tool to open the '
                'next acceptance stage (the ordinary tools in that batch run as the new stage first round); when '
                'creating it, weigh the checks already done and never repeat the previous budget '
                f'{previous_budget or budget} mechanically; if the previous stage did not converge, raise the '
                f'budget for the remaining verification work, but never above {STAGE_TOOL_ROUND_BUDGET_MAX}. '
                'A lone ordinary tool call runs once; the next lone call is blocked. '
                f'{_execution_stage_budget_accounting_note(active)}'
            )
        return (
            f'The current stage [{mode} | {status}] reached its tool-round budget {used}/{budget}. '
            f'Stage goal: {goal}. '
            'To keep using tools, submit `submit_next_stage` in the same batch as the target tool to open the '
            'next stage (the ordinary tools in that batch run as the new stage first round); when creating it, '
            'weigh the overall goal and the completed stage results and never repeat the previous budget '
            f'{previous_budget or budget} mechanically; if the previous stage did not converge, raise the budget '
            f'for the remaining work, but never above {STAGE_TOOL_ROUND_BUDGET_MAX}. '
            'A lone ordinary tool call runs once; the next lone call is blocked. '
            f'{_execution_stage_budget_accounting_note(active)}'
        )
    if normalized_kind == 'acceptance':
        return (
            f'Current acceptance stage [{status}] goal: {goal}. '
            f'Ordinary tool rounds used {used}/{budget}. '
            'All reasoning, tool calls and acceptance verdicts except opening a new stage must serve the current '
            'stage goal.'
            f'{_execution_stage_budget_accounting_note(active)}'
        )
    reminder = ''
    if not _stage_has_substantive_progress(active):
        reminder = (
            ' The current stage has produced no substantive execution round yet; do not call '
            '`submit_next_stage` again — run an ordinary tool or spawn child nodes first.'
        )
    return (
        f'Current stage [{mode} | {status}] goal: {goal}. '
        f'Ordinary tool rounds used {used}/{budget}. '
        'All reasoning, tool calls and spawning behaviour except opening a new stage must serve the current '
        'stage goal.'
        f'{_execution_stage_budget_accounting_note(active)}'
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
                'This acceptance node has not created its first stage yet. If you need tools, submit '
                '`submit_next_stage` in the same batch as the target tool to create an acceptance stage, then keep '
                'verifying or output the acceptance verdict; do not end the node directly.'
            )
        return (
            'This node has not created its first stage yet. If you need tools, submit `submit_next_stage` in the '
            'same batch as the target tool to create a stage, then keep advancing; do not end the node directly.'
        )
    if bool(stage_gate.get('transition_required')):
        if normalized_kind == 'acceptance':
            return (
                'The current acceptance stage budget is exhausted. To keep using tools, submit '
                '`submit_next_stage` in the same batch as the target tool to create the next stage, then keep '
                'verifying or output the acceptance verdict.'
            )
        return (
            'The current stage budget is exhausted. To keep using tools, submit `submit_next_stage` in the same '
            'batch as the target tool to create the next stage, then keep advancing or deliver the result.'
        )
    return ''


def build_node_plain_text_reply_block_message(*, node_kind: str, stage_gate: dict[str, Any]) -> str:
    """纯文本不是交付：没有工具调用时一律打回。

    有活动阶段且未 `transition_required` 时保留阶段目标与预算读数，并给出
    「继续工具 / submit_next_stage / submit_final_result」三档；其余情况给两档
    「继续工具 / submit_final_result」。
    """
    normalized_kind = str(node_kind or '').strip().lower()
    if normalized_kind not in _STAGE_BUDGET_NODE_KINDS:
        return ''
    work = (
        'call ordinary tools to keep verifying the acceptance criteria'
        if normalized_kind == 'acceptance'
        else 'call ordinary tools (spawn child nodes if needed) to keep advancing'
    )
    if (
        bool(stage_gate.get('enabled'))
        and bool(stage_gate.get('has_active_stage'))
        and not bool(stage_gate.get('transition_required'))
    ):
        active = stage_gate.get('active_stage') if isinstance(stage_gate.get('active_stage'), dict) else {}
        goal = str(active.get('stage_goal') or '').strip() or '(empty)'
        mode = str(active.get('mode') or '').strip() or 'autonomous'
        status = str(active.get('status') or '').strip() or 'in progress'
        used = int(active.get('tool_rounds_used') or 0)
        budget = int(active.get('tool_round_budget') or 0)
        return (
            'This round output plain text with no tool call. The runtime does not treat it as a delivery and will not '
            'submit anything on your behalf. '
            f'Current stage [{mode} | {status}] goal: {goal}, ordinary tool rounds used {used}/{budget}. '
            f'The next step is exactly one of three: {work}; '
            'if the goal is done, call `submit_final_result` to submit properly (put `status`, `answer` and '
            '`evidence` all in the tool arguments); '
            'if new work is needed, submit `submit_next_stage` in the same batch as the target tool to open the next '
            'stage. '
            'Writing plans, progress notes or "what I will do next" into the reply body counts as no submission at all.'
        )
    return (
        'This round output plain text with no tool call. The runtime does not treat it as a delivery and will not '
        'submit anything on your behalf. '
        f'The next step is exactly one of two: {work}; '
        'if the goal is done, call `submit_final_result` to submit properly (put `status`, `answer` and '
        '`evidence` all in the tool arguments). '
        'Writing plans, progress notes or "what I will do next" into the reply body counts as no submission at all.'
    )
