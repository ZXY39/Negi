from __future__ import annotations

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
        'active_stage': dict(active_stage or {}) if isinstance(active_stage, dict) else None,
    }


@dataclass(slots=True)
class FrontdoorToolContract:
    callable_tool_names: list[str]
    candidate_tool_names: list[str]
    hydrated_tool_names: list[str]
    stage_summary: dict[str, Any]
    visible_skill_ids: list[str]
    contract_revision: str

    def to_message(self) -> dict[str, Any]:
        return {
            'role': 'user',
            'content': {
                'message_type': FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND,
                'callable_tool_names': list(self.callable_tool_names),
                'candidate_tool_names': list(self.candidate_tool_names),
                'hydrated_tool_names': list(self.hydrated_tool_names),
                'visible_skill_ids': list(self.visible_skill_ids),
                'stage_summary': dict(self.stage_summary),
                'contract_revision': str(self.contract_revision or '').strip(),
            },
        }


def build_frontdoor_tool_contract(
    *,
    callable_tool_names: list[str] | None,
    candidate_tool_names: list[str] | None,
    hydrated_tool_names: list[str] | None,
    frontdoor_stage_state: dict[str, Any] | None,
    visible_skill_ids: list[str] | None = None,
    contract_revision: str | None = None,
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
        contract_revision=str(contract_revision or '').strip(),
    )


def is_frontdoor_tool_contract_message(message: dict[str, Any]) -> bool:
    if str((message or {}).get('role') or '').strip().lower() != 'user':
        return False
    content = (message or {}).get('content')
    if not isinstance(content, dict):
        return False
    return str(content.get('message_type') or '').strip() == FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND


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

