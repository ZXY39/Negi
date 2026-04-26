from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

NODE_DYNAMIC_CONTRACT_KIND = 'node_runtime_tool_contract'
NODE_DYNAMIC_CONTRACT_HEADING = '## Runtime Tool Contract'
NODE_DYNAMIC_CONTRACT_PAYLOAD_KEY = '_node_runtime_tool_contract_payload'


def _normalized_name_list(items: list[Any] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(items or []):
        normalized = str(item or '').strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _normalized_candidate_tool_items(
    items: list[Any] | None,
    *,
    fallback_names: list[str] | None = None,
) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_items = list(items or [])
    if not raw_items and fallback_names:
        raw_items = list(fallback_names)
    for item in raw_items:
        if isinstance(item, dict):
            tool_id = str(item.get('tool_id') or '').strip()
            description = str(item.get('description') or '').strip()
        else:
            tool_id = str(item or '').strip()
            description = ''
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        ordered.append({'tool_id': tool_id, 'description': description})
    return ordered


def _normalized_candidate_skill_items(
    items: list[Any] | None,
    *,
    fallback_ids: list[str] | None = None,
) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_items = list(items or [])
    if not raw_items and fallback_ids:
        raw_items = list(fallback_ids)
    for item in raw_items:
        if isinstance(item, dict):
            skill_id = str(item.get('skill_id') or '').strip()
            description = str(item.get('description') or '').strip()
        else:
            skill_id = str(item or '').strip()
            description = ''
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        ordered.append({'skill_id': skill_id, 'description': description})
    return ordered


def _normalized_repair_required_tool_items(items: list[Any] | None) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        tool_id = str(item.get('tool_id') or '').strip()
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        ordered.append(
            {
                'tool_id': tool_id,
                'description': str(item.get('description') or '').strip(),
                'reason': str(item.get('reason') or '').strip(),
            }
        )
    return ordered


def _normalized_repair_required_skill_items(items: list[Any] | None) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get('skill_id') or '').strip()
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        ordered.append(
            {
                'skill_id': skill_id,
                'description': str(item.get('description') or '').strip(),
                'reason': str(item.get('reason') or '').strip(),
            }
        )
    return ordered


def _active_stage_prompt_view(active_stage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(active_stage, dict):
        return None
    stage_id = str(active_stage.get('stage_id') or '').strip()
    if not stage_id:
        return None
    return {
        'stage_id': stage_id,
        'stage_goal': str(active_stage.get('stage_goal') or '').strip(),
        'tool_round_budget': max(0, int(active_stage.get('tool_round_budget') or 0)),
        'stage_kind': str(active_stage.get('stage_kind') or 'normal').strip() or 'normal',
        'final_stage': bool(active_stage.get('final_stage', False)),
    }


def _stable_stage_payload(stage_payload: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(stage_payload or {})
    return {
        'has_active_stage': bool(payload.get('has_active_stage')),
        'transition_required': bool(payload.get('transition_required')),
        'active_stage': _active_stage_prompt_view(
            payload.get('active_stage') if isinstance(payload.get('active_stage'), dict) else None
        ),
    }


def _stable_selection_trace(selection_trace: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(selection_trace or {})
    return {
        'mode': str(payload.get('mode') or '').strip(),
        'full_callable_tool_names': _normalized_name_list(list(payload.get('full_callable_tool_names') or [])),
        'stage_locked_to_submit_next_stage': bool(payload.get('stage_locked_to_submit_next_stage')),
    }


def _render_name_list(items: list[str] | None) -> str:
    names = _normalized_name_list(items)
    if not names:
        return 'none'
    return ', '.join(f'`{name}`' for name in names)


def _render_candidate_tool_section(items: list[dict[str, str]] | None) -> list[str]:
    normalized_items = _normalized_candidate_tool_items(items)
    if not normalized_items:
        return ['candidate_tools: none']
    lines = ['candidate_tools:']
    for item in normalized_items:
        tool_id = str(item.get('tool_id') or '').strip()
        description = str(item.get('description') or '').strip()
        detail = description if description else 'No description available.'
        lines.append(
            f'- `{tool_id}`: {detail} If it is still only listed here, load it with `load_tool_context(tool_id="{tool_id}")` and wait for the next round before calling it directly.'
        )
    return lines


def _render_candidate_skill_section(items: list[dict[str, str]] | None) -> list[str]:
    normalized_items = _normalized_candidate_skill_items(items)
    if not normalized_items:
        return ['candidate_skills: none']
    lines = ['candidate_skills:']
    for item in normalized_items:
        skill_id = str(item.get('skill_id') or '').strip()
        description = str(item.get('description') or '').strip()
        detail = description if description else 'No description available.'
        lines.append(
            f'- `{skill_id}`: {detail} Load with `load_skill_context(skill_id="{skill_id}")` when needed.'
        )
    return lines


def _render_repair_required_tool_section(items: list[dict[str, str]] | None) -> list[str]:
    normalized_items = _normalized_repair_required_tool_items(items)
    if not normalized_items:
        return []
    lines = [
        'repair_required_tools:',
        '- These tools must be repaired before use.',
        '- Use `load_tool_context(tool_id="<tool_id>")` first.',
        '- Use `exec`, `filesystem_write`, `filesystem_edit`, `filesystem_copy`, `filesystem_move`, or `filesystem_propose_patch` to repair them.',
        '- Reference skill: `repair-tool`.',
    ]
    for item in normalized_items:
        tool_id = str(item.get('tool_id') or '').strip()
        description = str(item.get('description') or '').strip()
        reason = str(item.get('reason') or '').strip()
        detail = description if description else 'No description available.'
        if reason:
            detail = f'{detail} Reason: {reason}'
        lines.append(f'- `{tool_id}`: {detail}')
    return lines


def _render_repair_required_skill_section(items: list[dict[str, str]] | None) -> list[str]:
    normalized_items = _normalized_repair_required_skill_items(items)
    if not normalized_items:
        return []
    lines = [
        'repair_required_skills:',
        '- These skills must be repaired before viewing their body.',
        '- Do not call `load_skill_context` until repaired.',
        '- Use `exec`, `filesystem_write`, `filesystem_edit`, `filesystem_copy`, `filesystem_move`, or `filesystem_propose_patch` to repair them.',
        '- Reference skill: `writing-skills`.',
    ]
    for item in normalized_items:
        skill_id = str(item.get('skill_id') or '').strip()
        description = str(item.get('description') or '').strip()
        reason = str(item.get('reason') or '').strip()
        detail = description if description else 'No description available.'
        if reason:
            detail = f'{detail} Reason: {reason}'
        lines.append(f'- `{skill_id}`: {detail}')
    return lines


def _render_stage_summary(stage_payload: dict[str, Any] | None) -> str:
    payload = _stable_stage_payload(stage_payload)
    active_stage = dict(payload.get('active_stage') or {}) if isinstance(payload.get('active_stage'), dict) else {}
    if not active_stage:
        return (
            'stage_summary: '
            f'has_active_stage={bool(payload.get("has_active_stage"))}; '
            f'transition_required={bool(payload.get("transition_required"))}'
        )
    parts = [
        f'has_active_stage={bool(payload.get("has_active_stage"))}',
        f'transition_required={bool(payload.get("transition_required"))}',
        f'stage_id={str(active_stage.get("stage_id") or "").strip() or "none"}',
    ]
    stage_goal = str(active_stage.get('stage_goal') or '').strip()
    if stage_goal:
        parts.append(f'stage_goal={stage_goal}')
    tool_round_budget = active_stage.get('tool_round_budget')
    if tool_round_budget not in (None, ''):
        parts.append(f'tool_round_budget={int(tool_round_budget)}')
    stage_kind = str(active_stage.get('stage_kind') or '').strip()
    if stage_kind:
        parts.append(f'stage_kind={stage_kind}')
    return 'stage_summary: ' + '; '.join(parts)


def _render_exec_runtime_policy(exec_runtime_policy: dict[str, Any] | None) -> str:
    payload = dict(exec_runtime_policy or {})
    if not payload:
        return 'exec_runtime_policy: none'
    parts: list[str] = []
    mode = str(payload.get('mode') or '').strip()
    if mode:
        parts.append(f'mode={mode}')
    if 'guardrails_enabled' in payload:
        parts.append(f'guardrails_enabled={bool(payload.get("guardrails_enabled"))}')
    summary = str(payload.get('summary') or '').strip()
    if summary:
        parts.append(f'summary={summary}')
    return 'exec_runtime_policy: ' + ('; '.join(parts) if parts else 'none')


def _render_node_dynamic_contract_summary(payload: dict[str, Any]) -> str:
    lines = [
        NODE_DYNAMIC_CONTRACT_HEADING,
        f'kind: {NODE_DYNAMIC_CONTRACT_KIND}',
        f'callable_tools: {_render_name_list(payload.get("callable_tool_names"))}',
        f'hydrated_tools: {_render_name_list(payload.get("hydrated_executor_names"))}',
        'load_tool_context_help: Any surfaced RBAC-visible tool may be loaded by exact `tool_id` for docs/help, including tools that are already callable or already hydrated.',
        'load_tool_context_repeat_guard: For callable, hydrated, or fixed-builtin tools, do not reread the same inline uncompressed toolskill. Reuse it unless the tool state changed or the old result was compressed away.',
        *_render_candidate_tool_section(payload.get('candidate_tools')),
        *_render_candidate_skill_section(payload.get('candidate_skills')),
        *_render_repair_required_tool_section(payload.get('repair_required_tools')),
        *_render_repair_required_skill_section(payload.get('repair_required_skills')),
        _render_stage_summary(payload.get('execution_stage')),
        _render_exec_runtime_policy(payload.get('exec_runtime_policy')),
    ]
    return '\n'.join(lines)


def _node_dynamic_contract_payload_from_message(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    embedded_payload = message.get(NODE_DYNAMIC_CONTRACT_PAYLOAD_KEY)
    if isinstance(embedded_payload, dict):
        payload = dict(embedded_payload)
        if str(payload.get('message_type') or '').strip() == NODE_DYNAMIC_CONTRACT_KIND:
            return payload
    raw_content = message.get('content')
    if not isinstance(raw_content, str):
        return None
    try:
        payload = json.loads(raw_content)
    except Exception:
        return None
    if isinstance(payload, dict) and str(payload.get('message_type') or '').strip() == NODE_DYNAMIC_CONTRACT_KIND:
        return payload
    return None


@dataclass(slots=True)
class NodeRuntimeToolContract:
    node_id: str
    node_kind: str
    callable_tool_names: list[str]
    candidate_tool_names: list[str]
    visible_skills: list[dict[str, str]]
    candidate_skill_ids: list[str]
    stage_payload: dict[str, Any]
    hydrated_executor_names: list[str]
    lightweight_tool_ids: list[str]
    selection_trace: dict[str, Any]
    contract_visible_skill_ids: list[str] | None = None
    skill_visibility_diagnostics: dict[str, Any] | None = None
    candidate_tool_items: list[dict[str, str]] | None = None
    candidate_skill_items: list[dict[str, str]] | None = None
    repair_required_tool_items: list[dict[str, str]] | None = None
    repair_required_skill_items: list[dict[str, str]] | None = None
    exec_runtime_policy: dict[str, Any] | None = None

    def to_message_payload(self) -> dict[str, Any]:
        payload = {
            'message_type': NODE_DYNAMIC_CONTRACT_KIND,
            'callable_tool_names': list(self.callable_tool_names or []),
            'candidate_tools': _normalized_candidate_tool_items(
                list(self.candidate_tool_items or []),
                fallback_names=list(self.candidate_tool_names or []),
            ),
            'candidate_skills': _normalized_candidate_skill_items(
                list(self.candidate_skill_items or []),
                fallback_ids=list(self.candidate_skill_ids or []),
            ),
            'hydrated_executor_names': _normalized_name_list(list(self.hydrated_executor_names or [])),
            'execution_stage': _stable_stage_payload(self.stage_payload),
        }
        repair_required_tools = _normalized_repair_required_tool_items(self.repair_required_tool_items)
        if repair_required_tools:
            payload['repair_required_tools'] = repair_required_tools
        repair_required_skills = _normalized_repair_required_skill_items(self.repair_required_skill_items)
        if repair_required_skills:
            payload['repair_required_skills'] = repair_required_skills
        if self.contract_visible_skill_ids is not None:
            payload['contract_visible_skill_ids'] = _normalized_name_list(
                list(self.contract_visible_skill_ids or [])
            )
        if isinstance(self.skill_visibility_diagnostics, dict):
            payload['skill_visibility_diagnostics'] = dict(self.skill_visibility_diagnostics)
        if isinstance(self.exec_runtime_policy, dict):
            payload['exec_runtime_policy'] = dict(self.exec_runtime_policy)
        return payload

    def to_message(self) -> dict[str, Any]:
        payload = self.to_message_payload()
        return {
            'role': 'assistant',
            'content': _render_node_dynamic_contract_summary(payload),
            NODE_DYNAMIC_CONTRACT_PAYLOAD_KEY: payload,
        }


def is_node_dynamic_contract_message(message: dict[str, Any]) -> bool:
    if _node_dynamic_contract_payload_from_message(message) is not None:
        return True
    if str((message or {}).get('role') or '').strip().lower() != 'assistant':
        return False
    return str((message or {}).get('content') or '').strip().startswith(NODE_DYNAMIC_CONTRACT_HEADING)


def upsert_node_dynamic_contract_message(
    messages: list[dict[str, Any]],
    contract: NodeRuntimeToolContract,
) -> list[dict[str, Any]]:
    contract_message = contract.to_message()
    updated = strip_node_dynamic_contract_messages(messages)
    updated.append(contract_message)
    return updated


def strip_node_dynamic_contract_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        dict(message)
        for message in list(messages or [])
        if isinstance(message, dict) and not is_node_dynamic_contract_message(message)
    ]


def inject_node_dynamic_contract_message(
    messages: list[dict[str, Any]] | None,
    contract: NodeRuntimeToolContract,
) -> list[dict[str, Any]]:
    return upsert_node_dynamic_contract_message(
        strip_node_dynamic_contract_messages(messages),
        contract,
    )


def extract_node_dynamic_contract_payload(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in list(messages or []):
        payload = _node_dynamic_contract_payload_from_message(message)
        if payload is not None:
            return payload
    return None


__all__ = [
    'NODE_DYNAMIC_CONTRACT_KIND',
    'NODE_DYNAMIC_CONTRACT_HEADING',
    'NodeRuntimeToolContract',
    'extract_node_dynamic_contract_payload',
    'inject_node_dynamic_contract_message',
    'is_node_dynamic_contract_message',
    'strip_node_dynamic_contract_messages',
    'upsert_node_dynamic_contract_message',
]
