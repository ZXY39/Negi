from __future__ import annotations

from typing import Any

from g3ku.resources.models import SkillResourceDescriptor, ToolResourceDescriptor
from main.governance.action_mapper import DEFAULT_ALLOWED_ROLES, get_default_tool_governance
from main.governance.models import SkillResourceRecord, ToolRecord
from main.governance.roles import normalize_public_allowed_roles, to_public_allowed_roles

ALL_ROLES = list(DEFAULT_ALLOWED_ROLES)


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


def build_tool_records(tool_descriptors: list[ToolResourceDescriptor]) -> list[ToolRecord]:
    """一个工具一条治理记录。

    只有显式声明 governance 或列在默认治理表里的工具才进管理面；两者都没有的工具
    （常驻内部控制工具）按设计不出现在 exposure 名单里。
    """
    items: list[ToolRecord] = []
    for descriptor in tool_descriptors:
        governance = _tool_governance(descriptor)
        if governance is None:
            continue
        explicit_governance = _has_explicit_tool_governance(descriptor)
        callable_flag = bool(getattr(descriptor, 'callable', True))
        available_flag = bool(descriptor.available)
        metadata: dict[str, Any] = {
            'warnings': list(descriptor.warnings),
            'errors': list(descriptor.errors),
            'repair_required': callable_flag and not available_flag,
        }
        items.append(
            ToolRecord(
                tool_id=descriptor.name,
                display_name=str(governance.get('display_name') or descriptor.name),
                description=str(governance.get('description') or descriptor.description),
                risk_level=str(governance.get('risk_level') or 'medium'),
                destructive=bool(governance.get('destructive', False)),
                agent_visible=bool(governance.get('agent_visible', True)),
                admin_mode=str(governance.get('admin_mode') or 'editable'),
                allowed_roles=_tool_allowed_roles(governance, explicit_governance=explicit_governance),
                enabled=bool(descriptor.enabled),
                available=available_flag,
                tool_type=str(getattr(descriptor, 'tool_type', 'internal') or 'internal'),
                install_dir=str(getattr(descriptor, 'install_dir', '') or '') or None,
                callable=callable_flag,
                source_path=str(descriptor.root),
                metadata=metadata,
            )
        )
    return items


def _has_explicit_tool_governance(descriptor: ToolResourceDescriptor) -> bool:
    governance = descriptor.metadata.get('governance')
    return isinstance(governance, dict) and bool(governance)


def _tool_allowed_roles(governance: dict[str, Any], *, explicit_governance: bool) -> list[str]:
    raw_roles = governance.get('allowed_roles')
    if explicit_governance or raw_roles is not None:
        return normalize_public_allowed_roles([str(role) for role in list(raw_roles or [])])
    return []


def _tool_governance(descriptor: ToolResourceDescriptor) -> dict[str, Any] | None:
    governance = descriptor.metadata.get('governance')
    if isinstance(governance, dict) and governance:
        return dict(governance)
    return get_default_tool_governance(descriptor.name)
