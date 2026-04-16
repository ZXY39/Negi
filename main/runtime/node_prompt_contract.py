from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

NODE_DYNAMIC_CONTRACT_KIND = 'node_runtime_tool_contract'


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
    candidate_tool_items: list[dict[str, str]] | None = None
    candidate_skill_items: list[dict[str, str]] | None = None
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
            'execution_stage': _stable_stage_payload(self.stage_payload),
        }
        if isinstance(self.exec_runtime_policy, dict):
            payload['exec_runtime_policy'] = dict(self.exec_runtime_policy)
        return payload

    def to_message(self) -> dict[str, str]:
        return {
            'role': 'user',
            'content': json.dumps(self.to_message_payload(), ensure_ascii=False, indent=2),
        }


def is_node_dynamic_contract_message(message: dict[str, Any]) -> bool:
    if str((message or {}).get('role') or '').strip().lower() != 'user':
        return False
    raw_content = (message or {}).get('content')
    if not isinstance(raw_content, str):
        return False
    try:
        payload = json.loads(raw_content)
    except Exception:
        return False
    return isinstance(payload, dict) and str(payload.get('message_type') or '').strip() == NODE_DYNAMIC_CONTRACT_KIND


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
        if not is_node_dynamic_contract_message(message):
            continue
        raw_content = message.get('content')
        if not isinstance(raw_content, str):
            continue
        try:
            payload = json.loads(raw_content)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


__all__ = [
    'NODE_DYNAMIC_CONTRACT_KIND',
    'NodeRuntimeToolContract',
    'extract_node_dynamic_contract_payload',
    'inject_node_dynamic_contract_message',
    'is_node_dynamic_contract_message',
    'strip_node_dynamic_contract_messages',
    'upsert_node_dynamic_contract_message',
]
