from __future__ import annotations

from g3ku.org_graph.governance.models import (
    AccessDecision,
    PermissionSubject,
    RolePolicyMatrixRecord,
)
from g3ku.org_graph.ids import new_policy_id
from g3ku.org_graph.protocol import now_iso

ACTOR_ROLES = ["ceo", "execution", "inspection"]


class GovernancePolicyEngine:
    def __init__(self, *, store, resource_registry):
        self._store = store
        self._resource_registry = resource_registry

    def sync_default_role_policies(self) -> list[RolePolicyMatrixRecord]:
        now = now_iso()
        records: list[RolePolicyMatrixRecord] = []
        for skill in self._resource_registry.list_skill_resources():
            for actor_role in ACTOR_ROLES:
                records.append(
                    RolePolicyMatrixRecord(
                        policy_id=new_policy_id(),
                        actor_role=actor_role,
                        resource_kind="skill",
                        resource_id=skill.skill_id,
                        action_id="load",
                        effect="allow" if actor_role in skill.allowed_roles else "deny",
                        source="default",
                        created_at=now,
                        updated_at=now,
                    )
                )
        for family in self._resource_registry.list_tool_families():
            for action in family.actions:
                for actor_role in ACTOR_ROLES:
                    records.append(
                        RolePolicyMatrixRecord(
                            policy_id=new_policy_id(),
                            actor_role=actor_role,
                            resource_kind="tool_family",
                            resource_id=family.tool_id,
                            action_id=action.action_id,
                            effect="allow" if actor_role in action.allowed_roles else "deny",
                            source="default",
                            created_at=now,
                            updated_at=now,
                        )
                    )
        self._store.replace_default_role_policies(records)
        return records

    def evaluate_skill_access(self, *, subject: PermissionSubject, skill_id: str) -> AccessDecision:
        skill = self._resource_registry.get_skill_resource(skill_id)
        if skill is None:
            return AccessDecision(
                allowed=False,
                reason_code="resource_disabled",
                source="resource_state",
                requires_request=False,
            )
        return self._evaluate(
            subject=subject,
            resource_kind="skill",
            resource_id=skill.skill_id,
            action_id="load",
            enabled=skill.enabled and skill.available,
            allowed_roles=skill.allowed_roles,
        )

    def evaluate_tool_action(self, *, subject: PermissionSubject, tool_id: str, action_id: str) -> AccessDecision:
        family = self._resource_registry.get_tool_family(tool_id)
        if family is None:
            return AccessDecision(
                allowed=False,
                reason_code="resource_disabled",
                source="resource_state",
                requires_request=False,
            )
        action = next((item for item in family.actions if item.action_id == action_id), None)
        if action is None:
            return AccessDecision(
                allowed=False,
                reason_code="resource_disabled",
                source="resource_state",
                requires_request=False,
            )
        return self._evaluate(
            subject=subject,
            resource_kind="tool_family",
            resource_id=tool_id,
            action_id=action_id,
            enabled=family.enabled and family.available,
            allowed_roles=action.allowed_roles,
        )

    def _evaluate(
        self,
        *,
        subject: PermissionSubject,
        resource_kind: str,
        resource_id: str,
        action_id: str,
        enabled: bool,
        allowed_roles: list[str],
    ) -> AccessDecision:
        if not enabled:
            return AccessDecision(
                allowed=False,
                reason_code="resource_disabled",
                source="resource_state",
                requires_request=False,
                manual_policy_change_required=False,
            )
        if subject.actor_role not in set(allowed_roles):
            return AccessDecision(
                allowed=False,
                reason_code="role_not_allowed",
                source="role_scope",
                requires_request=False,
                manual_policy_change_required=False,
            )
        policy = self._find_role_policy(
            subject=subject,
            resource_kind=resource_kind,
            resource_id=resource_id,
            action_id=action_id,
        )
        if policy is not None and policy.effect == "allow":
            return AccessDecision(
                allowed=True,
                reason_code="role_allowed",
                source="rbac",
                requires_request=False,
            )
        return AccessDecision(
            allowed=False,
            reason_code="policy_denied",
            source="rbac",
            requires_request=False,
            manual_policy_change_required=False,
        )

    def _find_role_policy(
        self,
        *,
        subject: PermissionSubject,
        resource_kind: str,
        resource_id: str,
        action_id: str,
    ) -> RolePolicyMatrixRecord | None:
        policies = [
            policy
            for policy in self._store.list_role_policies()
            if policy.actor_role == subject.actor_role
            and policy.resource_kind == resource_kind
            and policy.resource_id == resource_id
            and (policy.action_id in {None, action_id})
        ]
        policies.sort(key=lambda item: 1 if item.action_id == action_id else 0, reverse=True)
        return policies[0] if policies else None
