from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from g3ku.org_graph.types import DispatchShape, EventScope, RoleKind, StageStatus, UnitStatus, WorkMode


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class ProjectCreateRequest(Model):
    session_id: str = "web:shared"
    prompt: str
    max_depth: int | None = None
    preferred_title: str | None = None
    output_target: str = "chat"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectRecord(Model):
    project_id: str
    session_id: str
    title: str
    user_request: str
    status: str
    root_unit_id: str
    max_depth: int
    effective_max_depth: int
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    summary: str = ""
    final_result: str = ""
    error_summary: str = ""
    active_unit_count: int = 0
    completed_unit_count: int = 0
    failed_unit_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class UnitAgentRecord(Model):
    unit_id: str
    project_id: str
    parent_unit_id: str | None = None
    root_unit_id: str
    level: int
    role_kind: RoleKind
    role_title: str
    objective_summary: str
    prompt_preview: str
    status: UnitStatus
    current_stage_id: str | None = None
    current_action: str = ""
    result_summary: str = ""
    error_summary: str = ""
    can_delegate: bool
    child_count: int = 0
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    model_key: str | None = None
    model_binding: Literal["live_role", "fixed_key"] = "live_role"
    mutation_allowed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_binding(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "model_key" not in payload and "provider_model" in payload:
            legacy_model = str(payload.get("provider_model") or "").strip()
            payload["model_key"] = legacy_model or None
            payload["model_binding"] = "fixed_key" if legacy_model else payload.get("model_binding") or "live_role"
        payload["model_key"] = str(payload.get("model_key") or "").strip() or None
        payload["model_binding"] = str(payload.get("model_binding") or ("fixed_key" if payload.get("model_key") else "live_role")).strip() or "live_role"
        return payload

    @property
    def provider_model(self) -> str | None:
        return self.model_key


class UnitStageRecord(Model):
    stage_id: str
    project_id: str
    unit_id: str
    index: int
    title: str
    objective_summary: str
    dispatch_shape: DispatchShape
    planned_work_count: int
    status: StageStatus
    result_summary: str = ""
    error_summary: str = ""
    started_at: str | None = None
    finished_at: str | None = None


class ProjectEventRecord(Model):
    event_id: str
    seq: int
    session_id: str
    project_id: str
    unit_id: str | None = None
    stage_id: str | None = None
    scope: EventScope
    event_name: str
    level: str = "info"
    text: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ProjectArtifactRecord(Model):
    artifact_id: str
    project_id: str
    unit_id: str | None = None
    kind: str
    title: str
    path: str | None = None
    mime_type: str | None = None
    preview_text: str = ""
    created_at: str


class PendingProjectNotice(Model):
    notice_id: str
    session_id: str
    project_id: str
    kind: str
    title: str
    text: str
    created_at: str
    acknowledged: bool = False


class UnitTreeNode(Model):
    unit_id: str
    parent_unit_id: str | None = None
    level: int
    role_kind: RoleKind
    role_title: str
    objective_summary: str
    prompt_preview: str
    status: UnitStatus
    current_action: str = ""
    result_summary: str = ""
    error_summary: str = ""
    child_count: int = 0
    children: list["UnitTreeNode"] = Field(default_factory=list)


class ValidationProfileBlueprint(Model):
    profile_id: str
    acceptance_criteria: str
    validation_tools: list[str] = Field(default_factory=list)


class ValidationBindingBlueprint(Model):
    selector: str
    validation_profile_id: str


class WorkUnitBlueprint(Model):
    role_title: str
    objective_summary: str
    prompt_preview: str
    mode: WorkMode = "local"
    model_key: str | None = None
    mutation_allowed: bool = False
    validation_profile_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_model_key(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "model_key" not in payload and "provider_model" in payload:
            payload["model_key"] = payload.pop("provider_model")
        payload["model_key"] = str(payload.get("model_key") or "").strip() or None
        return payload

    @property
    def provider_model(self) -> str | None:
        return self.model_key


class StageBlueprint(Model):
    title: str
    objective_summary: str
    dispatch_shape: DispatchShape
    work_units: list[WorkUnitBlueprint] = Field(default_factory=list)
    validation_profiles: list[ValidationProfileBlueprint] = Field(default_factory=list)
    validation_bindings: list[ValidationBindingBlueprint] = Field(default_factory=list)


class ExecutionPlan(Model):
    stages: list[StageBlueprint] = Field(default_factory=list)

