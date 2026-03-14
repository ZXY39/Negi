from __future__ import annotations

from collections import OrderedDict
from typing import Any

from g3ku.resources.models import SkillResourceDescriptor, ToolResourceDescriptor

from main.governance.action_mapper import DEFAULT_ALLOWED_ROLES, get_default_tool_governance
from main.governance.models import SkillResourceRecord, ToolActionRecord, ToolFamilyRecord
from main.governance.roles import to_public_allowed_roles

ALL_ROLES = list(DEFAULT_ALLOWED_ROLES)


def _primary_executor_name(actions: list[ToolActionRecord]) -> str:
    for action in actions:
        for executor_name in action.executor_names or []:
            name = str(executor_name or '').strip()
            if name:
                return name
    return ''


def build_skill_resources(skill_descriptors: list[SkillResourceDescriptor], *, default_risk_level: str, exclude_names: set[str] | None = None) -> list[SkillResourceRecord]:
    excluded = set(exclude_names or set())
    items: list[SkillResourceRecord] = []
    for descriptor in skill_descriptors:
        if descriptor.name in excluded:
            continue
        governance = dict(descriptor.metadata.get('governance') or {})
        content = dict(descriptor.metadata.get('content') or {})
        items.append(
            SkillResourceRecord(
                skill_id=descriptor.name,
                resource_name=None,
                display_name=str(descriptor.metadata.get('display_name') or descriptor.name),
                description=descriptor.description,
                version=str(descriptor.generation or ''),
                legacy=False,
                enabled=bool(governance.get('enabled_by_default', descriptor.enabled)),
                available=bool(descriptor.available),
                allowed_roles=to_public_allowed_roles([str(role) for role in (governance.get('allowed_roles') or ALL_ROLES)]),
                editable_files=[str(item) for item in (governance.get('editable_files') or ['SKILL.md'])],
                risk_level=str(governance.get('risk_level') or default_risk_level),
                requires_tools=[str(item) for item in (descriptor.requires_tools or [])],
                source_path=str(descriptor.root),
                manifest_path=str(descriptor.manifest_path),
                skill_doc_path=str(descriptor.main_path),
                openai_yaml_path=None,
                metadata={
                    'trigger_keywords': list(descriptor.trigger_keywords),
                    'content': content,
                    'requires_bins': list(descriptor.requires_bins),
                    'requires_env': list(descriptor.requires_env),
                    'warnings': list(descriptor.warnings),
                    'errors': list(descriptor.errors),
                },
            )
        )
    return items


def build_tool_families(tool_descriptors: list[ToolResourceDescriptor]) -> list[ToolFamilyRecord]:
    grouped: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for descriptor in tool_descriptors:
        governance = _tool_governance(descriptor)
        if governance is None:
            continue
        tool_id = str(governance.get('tool_id') or descriptor.name)
        family = grouped.setdefault(
            tool_id,
            {
                'tool_id': tool_id,
                'display_name': str(governance.get('display_name') or descriptor.name or tool_id),
                'description': str(governance.get('description') or descriptor.description),
                'enabled': bool(descriptor.enabled),
                'available': bool(descriptor.available),
                'source_path': str(descriptor.root),
                'actions': OrderedDict(),
                'metadata': {'sources': [descriptor.root.name], 'warnings': list(descriptor.warnings), 'errors': list(descriptor.errors)},
            },
        )
        family['enabled'] = bool(family['enabled']) and bool(descriptor.enabled)
        family['available'] = bool(family['available']) or bool(descriptor.available)
        if descriptor.root.name not in family['metadata']['sources']:
            family['metadata']['sources'].append(descriptor.root.name)
        for action in list(governance.get('actions') or []):
            action_id = str(action.get('id') or '').strip()
            if not action_id:
                continue
            current = family['actions'].get(action_id)
            executor_names = [descriptor.name]
            if current is None:
                family['actions'][action_id] = ToolActionRecord(
                    action_id=action_id,
                    label=str(action.get('label') or action_id),
                    risk_level=str(action.get('risk_level') or 'medium'),
                    destructive=bool(action.get('destructive', False)),
                    allowed_roles=to_public_allowed_roles([str(role) for role in (action.get('allowed_roles') or ALL_ROLES)]),
                    executor_names=executor_names,
                )
            else:
                merged_executors = list(current.executor_names)
                if descriptor.name not in merged_executors:
                    merged_executors.append(descriptor.name)
                family['actions'][action_id] = current.model_copy(update={'executor_names': merged_executors})
    items: list[ToolFamilyRecord] = []
    for payload in grouped.values():
        actions = list(payload['actions'].values())
        items.append(
            ToolFamilyRecord(
                tool_id=payload['tool_id'],
                display_name=payload['display_name'],
                description=payload['description'],
                primary_executor_name=_primary_executor_name(actions),
                enabled=payload['enabled'],
                available=payload['available'],
                source_path=payload['source_path'],
                actions=actions,
                metadata=payload['metadata'],
            )
        )
    return items


def _tool_governance(descriptor: ToolResourceDescriptor) -> dict[str, Any] | None:
    governance = dict(descriptor.metadata.get('governance') or {})
    if governance.get('family'):
        return {
            'tool_id': str(governance.get('family')),
            'display_name': str(governance.get('display_name') or descriptor.name),
            'description': str(governance.get('description') or descriptor.description),
            'actions': list(governance.get('actions') or []),
        }
    return get_default_tool_governance(descriptor.name)
