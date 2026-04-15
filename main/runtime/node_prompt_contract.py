from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

NODE_DYNAMIC_CONTRACT_KIND = 'node_runtime_tool_contract'


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

    def to_message_payload(self) -> dict[str, Any]:
        return {
            'message_type': NODE_DYNAMIC_CONTRACT_KIND,
            'node_id': str(self.node_id or '').strip(),
            'node_kind': str(self.node_kind or '').strip(),
            'callable_tool_names': list(self.callable_tool_names or []),
            'candidate_tools': list(self.candidate_tool_names or []),
            'visible_skills': [dict(item) for item in list(self.visible_skills or []) if isinstance(item, dict)],
            'candidate_skills': [
                str(item or '').strip()
                for item in list(self.candidate_skill_ids or [])
                if str(item or '').strip()
            ],
            'execution_stage': dict(self.stage_payload or {}),
            'hydrated_executor_names': [
                str(item or '').strip()
                for item in list(self.hydrated_executor_names or [])
                if str(item or '').strip()
            ],
            'lightweight_tool_ids': [
                str(item or '').strip()
                for item in list(self.lightweight_tool_ids or [])
                if str(item or '').strip()
            ],
            'model_visible_tool_selection_trace': dict(self.selection_trace or {}),
        }

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
    updated: list[dict[str, Any]] = []
    replaced = False
    for message in list(messages or []):
        if is_node_dynamic_contract_message(message):
            if not replaced:
                updated.append(contract_message)
                replaced = True
            continue
        updated.append(dict(message))
    if not replaced:
        updated.append(contract_message)
    return updated


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
