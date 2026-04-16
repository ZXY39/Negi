from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND = 'frontdoor_runtime_tool_contract'


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


def _active_stage_summary(frontdoor_stage_state: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(frontdoor_stage_state or {})
    active_stage_id = str(payload.get('active_stage_id') or '').strip()
    stages = [dict(item) for item in list(payload.get('stages') or []) if isinstance(item, dict)]
    active_stage = next(
        (
            item
            for item in stages
            if str(item.get('stage_id') or '').strip() == active_stage_id
        ),
        None,
    )
    return {
        'active_stage_id': active_stage_id,
        'transition_required': bool(payload.get('transition_required')),
        'active_stage': _active_stage_prompt_view(active_stage),
    }


@dataclass(slots=True)
class FrontdoorToolContract:
    callable_tool_names: list[str]
    candidate_tool_names: list[str]
    hydrated_tool_names: list[str]
    stage_summary: dict[str, Any]
    visible_skill_ids: list[str]
    candidate_skill_ids: list[str]
    rbac_visible_tool_names: list[str]
    rbac_visible_skill_ids: list[str]
    contract_revision: str
    candidate_tool_items: list[dict[str, str]] | None = None
    exec_runtime_policy: dict[str, Any] | None = None

    def to_message_payload(self) -> dict[str, Any]:
        return {
            'message_type': FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND,
            'callable_tool_names': list(self.callable_tool_names),
            'candidate_tools': _normalized_candidate_tool_items(
                list(self.candidate_tool_items or []),
                fallback_names=list(self.candidate_tool_names),
            ),
            'hydrated_tool_names': list(self.hydrated_tool_names),
            'visible_skill_ids': list(self.visible_skill_ids),
            'candidate_skill_ids': list(self.candidate_skill_ids),
            'rbac_visible_tool_names': list(self.rbac_visible_tool_names),
            'rbac_visible_skill_ids': list(self.rbac_visible_skill_ids),
            'stage_summary': dict(self.stage_summary),
            'contract_revision': str(self.contract_revision or '').strip(),
            'exec_runtime_policy': (
                dict(self.exec_runtime_policy)
                if isinstance(self.exec_runtime_policy, dict)
                else None
            ),
        }

    def to_message(self) -> dict[str, Any]:
        return {
            'role': 'user',
            'content': json.dumps(self.to_message_payload(), ensure_ascii=False, indent=2),
        }


def _frontdoor_tool_contract_payload_from_content(content: Any) -> dict[str, Any] | None:
    payload: dict[str, Any] | None = None
    if isinstance(content, dict):
        payload = dict(content)
    elif isinstance(content, str):
        text = str(content or '').strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        if isinstance(parsed, dict):
            payload = dict(parsed)
    if not isinstance(payload, dict):
        return None
    if str(payload.get('message_type') or '').strip() != FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND:
        return None
    return payload


def build_frontdoor_tool_contract(
    *,
    callable_tool_names: list[str] | None,
    candidate_tool_names: list[str] | None,
    candidate_tool_items: list[dict[str, str]] | None = None,
    hydrated_tool_names: list[str] | None,
    frontdoor_stage_state: dict[str, Any] | None,
    visible_skill_ids: list[str] | None = None,
    candidate_skill_ids: list[str] | None = None,
    rbac_visible_tool_names: list[str] | None = None,
    rbac_visible_skill_ids: list[str] | None = None,
    contract_revision: str | None = None,
    exec_runtime_policy: dict[str, Any] | None = None,
) -> FrontdoorToolContract:
    callable_names = _normalized_name_list(callable_tool_names)
    candidate_names = [
        name
        for name in _normalized_name_list(candidate_tool_names)
        if name not in set(callable_names)
    ]
    return FrontdoorToolContract(
        callable_tool_names=callable_names,
        candidate_tool_names=candidate_names,
        hydrated_tool_names=_normalized_name_list(hydrated_tool_names),
        stage_summary=_active_stage_summary(frontdoor_stage_state),
        visible_skill_ids=_normalized_name_list(visible_skill_ids),
        candidate_skill_ids=_normalized_name_list(candidate_skill_ids),
        rbac_visible_tool_names=_normalized_name_list(rbac_visible_tool_names),
        rbac_visible_skill_ids=_normalized_name_list(rbac_visible_skill_ids),
        contract_revision=str(contract_revision or '').strip(),
        candidate_tool_items=_normalized_candidate_tool_items(candidate_tool_items, fallback_names=candidate_names),
        exec_runtime_policy=dict(exec_runtime_policy) if isinstance(exec_runtime_policy, dict) else None,
    )


def is_frontdoor_tool_contract_message(message: dict[str, Any]) -> bool:
    if str((message or {}).get('role') or '').strip().lower() != 'user':
        return False
    content = (message or {}).get('content')
    return _frontdoor_tool_contract_payload_from_content(content) is not None


def upsert_frontdoor_tool_contract_message(
    messages: list[dict[str, Any]] | None,
    contract: FrontdoorToolContract,
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    replaced = False
    for message in list(messages or []):
        if is_frontdoor_tool_contract_message(message):
            if not replaced:
                updated.append(contract.to_message())
                replaced = True
            continue
        updated.append(dict(message))
    if not replaced:
        updated.append(contract.to_message())
    return updated
