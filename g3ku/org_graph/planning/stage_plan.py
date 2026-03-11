from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ValidationProfilePlan:
    profile_id: str
    acceptance_criteria: str
    validation_tools: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ValidationBindingPlan:
    selector: str
    validation_profile_id: str


@dataclass(slots=True)
class WorkUnitPlan:
    role_title: str
    objective_summary: str
    prompt_preview: str
    mode: str = "local"
    provider_model: str | None = None
    mutation_allowed: bool = False
    validation_profile_id: str | None = None


@dataclass(slots=True)
class StagePlan:
    title: str
    objective_summary: str
    dispatch_shape: str
    work_units: list[WorkUnitPlan] = field(default_factory=list)
    validation_profiles: list[ValidationProfilePlan] = field(default_factory=list)
    validation_bindings: list[ValidationBindingPlan] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionPlan:
    stages: list[StagePlan] = field(default_factory=list)
