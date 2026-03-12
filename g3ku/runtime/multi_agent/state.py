from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

CoordinationPattern = Literal["direct", "router", "supervisor", "orchestrator_worker", "handoff"]


class IntentGateDecision(BaseModel):
    pattern: CoordinationPattern = "direct"
    route: str | None = None
    missing_info: str | None = None
    rationale: str = ""
    confidence: int = Field(default=0, ge=0, le=100)


class BlackboardRef(BaseModel):
    run_id: str
    label: str
    relative_path: str
    abs_path: str
    content_type: str = "text/plain"


class WorkItem(BaseModel):
    item_id: str
    task: str
    label: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkPlan(BaseModel):
    route: str = "dynamic"
    rationale: str = ""
    confidence: int = Field(default=0, ge=0, le=100)
    items: list[WorkItem] = Field(default_factory=list)


class WorkResult(BaseModel):
    item_id: str
    label: str = ""
    summary: str = ""
    confidence_score: int = Field(default=0, ge=0, le=100)
    artifacts: list[BlackboardRef] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    is_error: bool = False


@dataclass(slots=True)
class CompiledAgentRole:
    name: str
    label: str
    description: str
    role_type: str
    resource_name: str
    resource_version: str
    manifest_path: Path
    root: Path
    prompt_path: Path
    prompt: str
    pattern_affinity: list[str] = field(default_factory=list)
    delegate_roles: list[str] = field(default_factory=list)
    reference_paths: list[Path] = field(default_factory=list)
    tools_allow: list[str] = field(default_factory=list)
    skills_allow: list[str] = field(default_factory=list)
    context_policy: dict[str, Any] = field(default_factory=dict)
    output_policy: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    result_schema_path: Path | None = None
    descriptor: Any = None
    runtime_loop: Any = None


@dataclass(slots=True)
class LaneExecutionResult:
    output: str
    pattern: CoordinationPattern = "direct"
    route: str | None = None
    decision: IntentGateDecision | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MultiAgentRunState:
    session_key: str
    pattern: CoordinationPattern = "direct"
    route: str | None = None
    decision: IntentGateDecision | None = None
    output: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class RootGraphState(TypedDict, total=False):
    user_input: Any
    session: Any
    on_progress: Any
    decision: IntentGateDecision
    lane_result: LaneExecutionResult
    selected_pattern: str
    selected_route: str

