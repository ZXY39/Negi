from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

SubagentRunMode = Literal["sync", "background"]
SubagentLifecycleStatus = Literal[
    "pending",
    "injecting",
    "active",
    "yielded",
    "frozen",
    "destroyed",
    "completed",
    "failed",
    "canceled",
]


class ModelFallbackTarget(BaseModel):
    model_key: str
    retry_on: list[str] = Field(default_factory=lambda: ["network", "429", "5xx"])

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_model_key(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if "model_key" not in payload and "provider_model" in payload:
            payload["model_key"] = payload.pop("provider_model")
        payload["model_key"] = str(payload.get("model_key") or "").strip()
        return payload

    @property
    def provider_model(self) -> str:
        return self.model_key


class DynamicSubagentRequest(BaseModel):
    parent_session_id: str
    category: str = ""
    prompt: str
    load_skills: list[str] = Field(default_factory=list)
    tools_allow: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    run_mode: SubagentRunMode = "sync"
    continue_session_id: str | None = None
    action_rules: list[str] = Field(default_factory=list)
    context_constraints: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DynamicSubagentSessionRecord(BaseModel):
    session_id: str
    parent_session_id: str
    task_id: str | None = None
    category: str
    status: SubagentLifecycleStatus
    run_mode: SubagentRunMode
    model_chain: list[ModelFallbackTarget] = Field(default_factory=list)
    granted_tools: list[str] = Field(default_factory=list)
    injected_skills: list[str] = Field(default_factory=list)
    system_fingerprint: str
    created_at: str
    updated_at: str
    last_anchor_index: int = 0
    last_result_summary: str = ""
    freeze_expires_at: str | None = None
    destroy_after_accept: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class DynamicSubagentResult(BaseModel):
    session_id: str
    task_id: str | None = None
    parent_session_id: str
    category: str
    run_mode: SubagentRunMode
    status: str
    ok: bool = True
    output: str = ""
    error: str | None = None
    system_fingerprint: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class BackgroundTaskRecord(BaseModel):
    task_id: str
    session_id: str = ""
    parent_session_id: str
    category: str
    status: Literal["pending", "injecting", "running", "paused", "completed", "failed", "canceled"]
    created_at: str
    updated_at: str
    result_summary: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
