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
    stable_messages: list[dict[str, Any]]
    dynamic_appendix_messages: list[dict[str, Any]]
    tool_names: list[str]
    candidate_tool_names: list[str]
    trace: dict[str, Any]
    turn_overlay_text: str
    cache_family_revision: str

    def __init__(
        self,
        *,
        model_messages: list[dict[str, Any]] | None = None,
        stable_messages: list[dict[str, Any]] | None = None,
        dynamic_appendix_messages: list[dict[str, Any]] | None = None,
        tool_names: list[str] | None = None,
        candidate_tool_names: list[str] | None = None,
        trace: dict[str, Any] | None = None,
        turn_overlay_text: str | None = None,
        cache_family_revision: str | None = None,
        system_prompt: str | None = None,
        recent_history: list[dict[str, Any]] | None = None,
    ) -> None:
        resolved_stable_messages = list(stable_messages or [])
        if not resolved_stable_messages:
            if model_messages is None:
                if system_prompt is not None:
                    resolved_stable_messages.append({"role": "system", "content": str(system_prompt)})
                resolved_stable_messages.extend(list(recent_history or []))
            else:
                resolved_stable_messages = list(model_messages or [])
        self.stable_messages = resolved_stable_messages
        self.dynamic_appendix_messages = list(dynamic_appendix_messages or [])
        if model_messages is None:
            self.model_messages = [
                *list(self.stable_messages),
                *list(self.dynamic_appendix_messages),
            ]
        else:
            self.model_messages = list(model_messages or [])
        self.tool_names = list(tool_names or [])
        self.candidate_tool_names = list(candidate_tool_names or [])
        self.trace = dict(trace or {})
        self.turn_overlay_text = str(turn_overlay_text or "").strip()
        self.cache_family_revision = str(cache_family_revision or "").strip()

    @property
    def system_prompt(self) -> str:
        for message in self.stable_messages:
            if str(message.get("role") or "").strip().lower() == "system":
                return str(message.get("content") or "")
        return ""

    @property
    def recent_history(self) -> list[dict[str, Any]]:
        if not self.stable_messages:
            return []
        body = list(self.stable_messages)
        if body and str(body[0].get("role") or "").strip().lower() == "system":
            body = body[1:]
        if body and str(body[-1].get("role") or "").strip().lower() == "user":
            body = body[:-1]
        return body
