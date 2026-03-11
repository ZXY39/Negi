from __future__ import annotations

from g3ku.org_graph.errors import PermissionDeniedError
from g3ku.org_graph.governance.models import PermissionSubject


class GovernanceApprovalService:
    """Compatibility wrapper that now performs direct filter/RBAC checks only."""

    def __init__(self, service, *, store, resource_registry, policy_engine):
        self._service = service
        self._store = store
        self._resource_registry = resource_registry
        self._policy_engine = policy_engine

    def build_subject(
        self,
        *,
        session_id: str,
        actor_role: str,
        project_id: str | None = None,
        unit_id: str | None = None,
    ) -> PermissionSubject:
        return PermissionSubject(
            user_key=session_id,
            session_id=session_id,
            project_id=project_id,
            unit_id=unit_id,
            actor_role=actor_role,
        )

    async def ensure_skill_access(
        self,
        *,
        subject: PermissionSubject,
        skill_id: str,
        actor_label: str,
        project=None,
        unit=None,
        stage_id: str | None = None,
    ) -> None:
        _ = actor_label, project, unit, stage_id
        if self._resource_registry.get_skill_resource(skill_id) is None:
            return
        decision = self._policy_engine.evaluate_skill_access(subject=subject, skill_id=skill_id)
        if decision.allowed:
            return
        raise PermissionDeniedError(f"Skill access denied: {skill_id}")

    async def ensure_tool_action_access(
        self,
        *,
        subject: PermissionSubject,
        tool_id: str,
        action_id: str,
        actor_label: str,
        project=None,
        unit=None,
        stage_id: str | None = None,
    ) -> None:
        _ = actor_label, project, unit, stage_id
        decision = self._policy_engine.evaluate_tool_action(
            subject=subject,
            tool_id=tool_id,
            action_id=action_id,
        )
        if decision.allowed:
            return
        raise PermissionDeniedError(f"Tool action denied: {tool_id}.{action_id}")

    async def resolve_requests_after_policy_change(
        self,
        *,
        resource_kind: str,
        resource_id: str,
        action_id: str | None = None,
        decided_by: str = "manual_policy",
    ) -> list:
        _ = resource_kind, resource_id, action_id, decided_by
        return []

    async def decide_request(
        self,
        request_id: str,
        *,
        effect: str,
        scope: str,
        decided_by: str,
        note: str = "",
    ):
        _ = request_id, effect, scope, decided_by, note
        raise PermissionDeniedError("Permission request workflow has been removed.")
