from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from g3ku.runtime.multi_agent.dynamic.types import DynamicSubagentRequest, ModelFallbackTarget


_MUTATING_TOOLS = {'write_file', 'edit_file', 'delete_file'}


@dataclass(slots=True)
class ResolvedDynamicSpec:
    name: str
    description: str = ''
    model_chain: list[ModelFallbackTarget] = field(default_factory=list)
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    injected_skills: list[str] = field(default_factory=list)
    max_iterations: int = 8
    allow_background: bool = True
    mutation_allowed: bool = False
    output_mode: str = 'text'
    destroy_after_sync: bool = True


class CategoryResolver:
    def __init__(self, *, loop, config: Any) -> None:
        self._loop = loop
        self._config = config

    def resolve(self, request: DynamicSubagentRequest) -> ResolvedDynamicSpec:
        metadata = dict(request.metadata or {})
        tools_allow = self._normalize_names(request.tools_allow)
        tools_deny = self._normalize_names(metadata.get('tools_deny') or [])
        if tools_deny:
            denied = set(tools_deny)
            tools_allow = [name for name in tools_allow if name not in denied]
        allow_background_mutation = bool(metadata.get('allow_background_mutation', False))
        if request.run_mode == 'background' and not allow_background_mutation:
            tools_allow = [name for name in tools_allow if name not in _MUTATING_TOOLS]
        mutation_allowed = bool(metadata.get('mutation_allowed', False)) or any(name in _MUTATING_TOOLS for name in tools_allow)
        if request.run_mode == 'background' and not allow_background_mutation:
            mutation_allowed = False
        return ResolvedDynamicSpec(
            name=self._resolve_role_name(request, metadata),
            description=self._resolve_role_description(request, metadata),
            model_chain=self._resolve_model_chain(metadata),
            tools_allow=tools_allow,
            tools_deny=tools_deny,
            injected_skills=self._normalize_names(request.load_skills),
            max_iterations=self._resolve_int(metadata.get('max_iterations'), default=8, minimum=1),
            allow_background=bool(metadata.get('allow_background', True)),
            mutation_allowed=mutation_allowed,
            output_mode=self._resolve_output_mode(request, metadata),
            destroy_after_sync=bool(metadata.get('destroy_after_sync', True)),
        )

    def _resolve_model_chain(self, metadata: dict[str, Any]) -> list[ModelFallbackTarget]:
        raw_chain = metadata.get('model_chain')
        targets: list[ModelFallbackTarget] = []
        if isinstance(raw_chain, Iterable) and not isinstance(raw_chain, (str, bytes, dict)):
            for item in raw_chain:
                try:
                    targets.append(ModelFallbackTarget.model_validate(item))
                except Exception:
                    continue
        provider_model = str(metadata.get('provider_model') or '').strip()
        if provider_model:
            targets.insert(0, ModelFallbackTarget(provider_model=provider_model))
        if targets:
            return self._dedupe_model_chain(targets)
        default_model = None
        if getattr(self._loop, 'provider_name', None) and getattr(self._loop, 'model', None):
            default_model = f"{self._loop.provider_name}:{self._loop.model}"
        elif getattr(self._loop, 'model', None):
            default_model = str(self._loop.model)
        return [ModelFallbackTarget(provider_model=default_model)] if default_model else []

    @staticmethod
    def _resolve_role_name(request: DynamicSubagentRequest, metadata: dict[str, Any]) -> str:
        for value in (request.category, metadata.get('role_name'), metadata.get('role_label')):
            text = str(value or '').strip()
            if text:
                return text
        return 'dynamic_worker'

    @staticmethod
    def _resolve_role_description(request: DynamicSubagentRequest, metadata: dict[str, Any]) -> str:
        for value in (metadata.get('role_description'), metadata.get('objective_summary'), metadata.get('task_summary')):
            text = str(value or '').strip()
            if text:
                return text
        prompt = ' '.join(str(request.prompt or '').split()).strip()
        if prompt:
            return prompt[:120] + ('...' if len(prompt) > 120 else '')
        return '运行时动态派生的临时执行单元'

    @staticmethod
    def _resolve_output_mode(request: DynamicSubagentRequest, metadata: dict[str, Any]) -> str:
        raw = str(metadata.get('output_mode') or '').strip().lower()
        if raw in {'text', 'structured'}:
            return raw
        return 'structured' if request.output_schema else 'text'

    @staticmethod
    def _resolve_int(value: Any, *, default: int, minimum: int) -> int:
        try:
            return max(minimum, int(value))
        except Exception:
            return default

    @staticmethod
    def _normalize_names(values: Iterable[Any] | Any) -> list[str]:
        if isinstance(values, str):
            raw_values = [values]
        elif isinstance(values, Iterable):
            raw_values = list(values)
        else:
            raw_values = []
        seen: set[str] = set()
        normalized: list[str] = []
        for item in raw_values:
            value = str(item or '').strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _dedupe_model_chain(targets: list[ModelFallbackTarget]) -> list[ModelFallbackTarget]:
        seen: set[str] = set()
        deduped: list[ModelFallbackTarget] = []
        for target in targets:
            key = str(target.provider_model or '').strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(target)
        return deduped


ResolvedCategoryProfile = ResolvedDynamicSpec

