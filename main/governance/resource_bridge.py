from __future__ import annotations

from collections import OrderedDict
from typing import Any

from g3ku.resources.models import SkillResourceDescriptor, ToolResourceDescriptor

from main.governance.action_mapper import DEFAULT_ALLOWED_ROLES, get_default_tool_governance
from main.governance.models import SkillResourceRecord, ToolActionRecord, ToolFamilyRecord
from main.governance.roles import to_public_allowed_roles

ALL_ROLES = list(DEFAULT_ALLOWED_ROLES)


def _primary_executor_name(actions: list[ToolActionRecord], *, preferred_executor_name: str = '') -> str:
    preferred = str(preferred_executor_name or '').strip()
    visible_actions = [action for action in actions if bool(getattr(action, 'agent_visible', True))]
    governance = get_default_tool_governance(preferred)
    governance_order = [
        str(item.get('id') or '').strip()
        for item in list((governance or {}).get('actions') or [])
        if str(item.get('id') or '').strip()
    ]
    if governance_order:
        action_map = {str(action.action_id or '').strip(): action for action in visible_actions or actions}
        for action_id in governance_order:
            action = action_map.get(action_id)
            if action is None:
                continue
            for executor_name in action.executor_names or []:
                name = str(executor_name or '').strip()
                if name and name != preferred:
                    return name
    for action in visible_actions or actions:
        for executor_name in action.executor_names or []:
            name = str(executor_name or '').strip()
            if name and name != preferred:
                return name
    if preferred:
        for action in visible_actions or actions:
            for executor_name in action.executor_names or []:
                name = str(executor_name or '').strip()
                if name == preferred:
                    return name
    for action in visible_actions or actions:
        for executor_name in action.executor_names or []:
            name = str(executor_name or '').strip()
            if name:
                return name
    return ''


def _ordered_actions(actions: list[ToolActionRecord]) -> list[ToolActionRecord]:
    return sorted(
        actions,
        key=lambda action: (
            not bool(getattr(action, 'agent_visible', True)),
            str(getattr(action, 'admin_mode', 'editable') or 'editable') == 'readonly_system',
        ),
    )


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
                'tool_type': str(getattr(descriptor, 'tool_type', 'internal') or 'internal'),
                'install_dir': str(getattr(descriptor, 'install_dir', '') or '') or None,
                'callable': bool(getattr(descriptor, 'callable', True)),
                'source_path': str(descriptor.root),
                'actions': OrderedDict(),
                'metadata': {
                    'sources': [descriptor.root.name],
                    'warnings': list(descriptor.warnings),
                    'errors': list(descriptor.errors),
                    'repair_required': bool(getattr(descriptor, 'callable', True)) and not bool(descriptor.available),
                },
            },
        )
        family['enabled'] = bool(family['enabled']) and bool(descriptor.enabled)
        family['available'] = bool(family['available']) or bool(descriptor.available)
        family['callable'] = bool(family['callable']) or bool(getattr(descriptor, 'callable', True))
        if bool(getattr(descriptor, 'callable', True)):
            family['tool_type'] = 'internal'
            family['install_dir'] = None
        elif not family.get('install_dir'):
            family['install_dir'] = str(getattr(descriptor, 'install_dir', '') or '') or None
        if descriptor.root.name not in family['metadata']['sources']:
            family['metadata']['sources'].append(descriptor.root.name)
        family['metadata']['repair_required'] = bool(family['callable']) and not bool(family['available'])
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
                    agent_visible=bool(action.get('agent_visible', True)),
                    admin_mode=str(action.get('admin_mode') or 'editable'),
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
        actions = _ordered_actions(list(payload['actions'].values()))
        items.append(
            ToolFamilyRecord(
                tool_id=payload['tool_id'],
                display_name=payload['display_name'],
                description=payload['description'],
                primary_executor_name=_primary_executor_name(actions, preferred_executor_name=payload['tool_id']),
                enabled=payload['enabled'],
                available=payload['available'],
                tool_type=payload['tool_type'],
                install_dir=payload['install_dir'],
                callable=payload['callable'],
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
