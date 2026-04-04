from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
    trace: dict[str, Any]

    def __init__(
        self,
        *,
        model_messages: list[dict[str, Any]] | None = None,
        tool_names: list[str] | None = None,
        trace: dict[str, Any] | None = None,
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
