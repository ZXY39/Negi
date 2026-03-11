from typing import Literal, TypeAlias

ProjectStatus: TypeAlias = Literal[
    "queued", "planning", "running", "checking", "blocked", "completed", "failed", "canceled", "archived"
]
UnitStatus: TypeAlias = Literal[
    "pending", "planning", "ready", "running", "checking", "blocked", "completed", "failed", "canceled"
]
StageStatus: TypeAlias = Literal["pending", "running", "checking", "rework", "blocked", "completed", "failed", "canceled"]
EventScope: TypeAlias = Literal["ceo", "project", "unit", "stage", "tool", "checker", "system"]
DispatchShape: TypeAlias = Literal["single", "parallel"]
RoleKind: TypeAlias = Literal["execution", "checker"]
WorkMode: TypeAlias = Literal["local", "delegate"]
ActorRole: TypeAlias = Literal["ceo", "execution", "inspection"]
ResourceKind: TypeAlias = Literal["skill", "tool_family"]
PermissionEffect: TypeAlias = Literal["allow", "deny"]
PermissionRiskLevel: TypeAlias = Literal["low", "medium", "high"]
