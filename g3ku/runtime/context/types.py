from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ContextBlockKind = Literal[
    "task_continuity",
    "stage_context",
    "latest_archive_overview",
    "older_archive_abstracts",
    "retrieved_context",
    "live_raw_tail",
]


@dataclass(slots=True)
class ContextBlock:
    kind: ContextBlockKind
    content: str
    source: str = ""
    level: str = ""
    tokens: int = 0
    trimmed: bool = False
    trim_reason: str = ""
    degraded_from: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ContextSnapshot:
    blocks: list[ContextBlock] = field(default_factory=list)
    total_tokens: int = 0
    system_tokens: int = 0
    user_tokens: int = 0


@dataclass(slots=True)
class ArchiveContextRecord:
    archive_id: str
    archive_uri: str
    overview_uri: str
    abstract_uri: str
    overview: str = ""
    abstract: str = ""
    score: float = 0.0
    created_at: str = ""
    summary_version: int = 2


@dataclass(slots=True)
class StageContextRecord:
    active_stage: dict[str, Any] | None = None
    completed_abstracts: list[str] = field(default_factory=list)
    source: str = ""


@dataclass(slots=True)
class TaskContinuityRecord:
    active_tasks: list[dict[str, Any]] = field(default_factory=list)
    last_task_memory: dict[str, Any] = field(default_factory=dict)
    source: str = ""


@dataclass(slots=True)
class RetrievedContextBundle:
    query: str
    records: list[dict[str, Any]] = field(default_factory=list)
    grouped: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, init=False)
class ContextAssemblyResult:
    model_messages: list[dict[str, Any]]
    tool_names: list[str]
    trace: dict[str, Any] = field(default_factory=dict)
    context_snapshot: ContextSnapshot = field(default_factory=ContextSnapshot)

    def __init__(
        self,
        *,
        model_messages: list[dict[str, Any]] | None = None,
        tool_names: list[str] | None = None,
        trace: dict[str, Any] | None = None,
        context_snapshot: ContextSnapshot | None = None,
        system_prompt: str | None = None,
        recent_history: list[dict[str, Any]] | None = None,
    ) -> None:
        if model_messages is None:
            assembled: list[dict[str, Any]] = []
            if system_prompt is not None:
                assembled.append({"role": "system", "content": str(system_prompt)})
            assembled.extend(list(recent_history or []))
            self.model_messages = assembled
        else:
            self.model_messages = list(model_messages or [])
        self.tool_names = list(tool_names or [])
        self.trace = dict(trace or {})
        self.context_snapshot = context_snapshot or ContextSnapshot()

    @property
    def system_prompt(self) -> str:
        for message in self.model_messages:
            if str(message.get("role") or "").strip().lower() == "system":
                return str(message.get("content") or "")
        return ""

    @property
    def recent_history(self) -> list[dict[str, Any]]:
        if not self.model_messages:
            return []
        body = list(self.model_messages)
        if body and str(body[0].get("role") or "").strip().lower() == "system":
            body = body[1:]
        if body and str(body[-1].get("role") or "").strip().lower() == "user":
            body = body[:-1]
        return body
