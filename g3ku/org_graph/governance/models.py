from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from g3ku.org_graph.models import Model
from g3ku.org_graph.types import (
    ActorRole,
    PermissionEffect,
    PermissionRiskLevel,
    ResourceKind,
)


class PermissionSubject(Model):
    user_key: str
    session_id: str
    project_id: str | None = None
    unit_id: str | None = None
    actor_role: ActorRole


class SkillResourceRecord(Model):
    skill_id: str
    resource_name: str | None = Field(default=None, validation_alias='capability_name')
    display_name: str
    description: str
    version: str | None = None
    legacy: bool = False
    enabled: bool = True
    available: bool = True
    allowed_roles: list[ActorRole] = Field(default_factory=list)
    editable_files: list[str] = Field(default_factory=list)
    risk_level: PermissionRiskLevel = 'medium'
    requires_tools: list[str] = Field(default_factory=list)
    source_path: str
    manifest_path: str | None = None
    skill_doc_path: str
    openai_yaml_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolActionRecord(Model):
    action_id: str
    label: str
    risk_level: PermissionRiskLevel = 'medium'
    destructive: bool = False
    allowed_roles: list[ActorRole] = Field(default_factory=list)
    executor_names: list[str] = Field(default_factory=list)


class ToolFamilyRecord(Model):
    tool_id: str
    display_name: str
    description: str
    enabled: bool = True
    available: bool = True
    source_path: str
    actions: list[ToolActionRecord] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RolePolicyMatrixRecord(Model):
    policy_id: str
    actor_role: ActorRole
    resource_kind: ResourceKind
    resource_id: str
    action_id: str | None = None
    effect: PermissionEffect
    source: Literal['default', 'admin_override'] = 'default'
    created_at: str
    updated_at: str

class AccessDecision(Model):
    allowed: bool
    reason_code: str
    matched_rule_id: str | None = None
    source: Literal['resource_state', 'role_scope', 'acl', 'rbac']
    requires_request: bool = False
    manual_policy_change_required: bool = False


