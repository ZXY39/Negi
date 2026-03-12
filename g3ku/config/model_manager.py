from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from g3ku.config.loader import get_config_path, load_config, save_config
from g3ku.config.schema import Config, ManagedModelConfig

VALID_SCOPES = ("agent", "ceo", "execution", "inspection")


def _normalize_scope(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    mapping = {
        "agent": "agent",
        "main": "agent",
        "default": "agent",
        "ceo": "ceo",
        "org_graph.ceo": "ceo",
        "org_graph_ceo": "ceo",
        "execution": "execution",
        "org_graph.execution": "execution",
        "org_graph_execution": "execution",
        "inspection": "inspection",
        "checker": "inspection",
        "org_graph.inspection": "inspection",
        "org_graph_inspection": "inspection",
    }
    if raw not in mapping:
        raise ValueError(f"Invalid scope: {value}. Valid scopes: {', '.join(VALID_SCOPES)}")
    return mapping[raw]


@dataclass(slots=True)
class ModelManager:
    config: Config

    @classmethod
    def load(cls) -> "ModelManager":
        return cls(load_config())

    def list_models(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in self.config.models.catalog:
            scopes = [scope for scope in VALID_SCOPES if item.key in self._scope_list(scope)]
            items.append(
                {
                    "key": item.key,
                    "provider_model": item.provider_model,
                    "api_key": item.api_key,
                    "enabled": item.enabled,
                    "api_base": item.api_base,
                    "extra_headers": item.extra_headers,
                    "max_tokens": item.max_tokens,
                    "temperature": item.temperature,
                    "reasoning_effort": item.reasoning_effort,
                    "retry_on": list(item.retry_on or []),
                    "description": item.description,
                    "scopes": scopes,
                }
            )
        return items

    def add_model(
        self,
        *,
        key: str,
        provider_model: str,
        api_key: str,
        api_base: str,
        scopes: list[str] | None = None,
        extra_headers: dict[str, str] | None = None,
        enabled: bool = True,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        retry_on: list[str] | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        clean_key = str(key or "").strip()
        if not clean_key:
            raise ValueError("Model key is required")
        if self.config.get_managed_model(clean_key) is not None:
            raise ValueError(f"Model key already exists: {clean_key}")
        provider_model = str(provider_model or "").strip()
        if not provider_model:
            raise ValueError("provider_model is required")
        self.config.parse_provider_model(provider_model)
        api_key = str(api_key or "").strip()
        api_base = str(api_base or "").strip()
        if not api_key:
            raise ValueError("api_key is required")
        if not api_base:
            raise ValueError("api_base is required")

        item = ManagedModelConfig(
            key=clean_key,
            provider_model=provider_model,
            api_key=api_key,
            api_base=api_base,
            extra_headers=extra_headers or None,
            enabled=bool(enabled),
            max_tokens=int(max_tokens) if max_tokens is not None else 4096,
            temperature=float(temperature) if temperature is not None else 0.1,
            reasoning_effort=str(reasoning_effort) if reasoning_effort is not None and str(reasoning_effort).strip() else None,
            retry_on=list(retry_on or ["network", "429", "5xx"]),
            description=str(description or "").strip(),
        )
        self.config.models.catalog.append(item)
        for scope in scopes or []:
            self.add_model_to_scope(clean_key, scope)
        self._sync_legacy_model_fields()
        self.save()
        return self.get_model(clean_key)

    def update_model(
        self,
        *,
        key: str,
        provider_model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        retry_on: list[str] | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        item = self._require_model(key)
        update: dict[str, Any] = {}
        if provider_model is not None:
            clean_provider_model = str(provider_model).strip()
            self.config.parse_provider_model(clean_provider_model)
            update["provider_model"] = clean_provider_model
        if api_key is not None:
            update["api_key"] = str(api_key).strip()
        if api_base is not None:
            update["api_base"] = str(api_base).strip() or None
        if extra_headers is not None:
            update["extra_headers"] = dict(extra_headers) or None
        if max_tokens is not None:
            update["max_tokens"] = int(max_tokens)
        if temperature is not None:
            update["temperature"] = float(temperature)
        if reasoning_effort is not None:
            value = str(reasoning_effort).strip()
            update["reasoning_effort"] = value or None
        if retry_on is not None:
            update["retry_on"] = list(retry_on)
        if description is not None:
            update["description"] = str(description).strip()
        self._replace_model(item.key, item.model_copy(update=update))
        self._sync_legacy_model_fields()
        self.save()
        return self.get_model(item.key)

    def set_model_enabled(self, key: str, enabled: bool) -> dict[str, Any]:
        item = self._require_model(key)
        self._replace_model(item.key, item.model_copy(update={"enabled": bool(enabled)}))
        self._sync_legacy_model_fields()
        self.save()
        return self.get_model(item.key)

    def set_scope_chain(self, scope: str, model_keys: list[str]) -> dict[str, Any]:
        normalized_scope = _normalize_scope(scope)
        clean = []
        seen: set[str] = set()
        for key in model_keys:
            model_key = str(key or "").strip()
            if not model_key or model_key in seen:
                continue
            self._require_model(model_key)
            seen.add(model_key)
            clean.append(model_key)
        role_routes = self.config.models.roles
        setattr(role_routes, normalized_scope, clean)
        self._sync_legacy_model_fields()
        self.save()
        return {"scope": normalized_scope, "model_keys": clean}

    def add_model_to_scope(self, key: str, scope: str) -> None:
        model = self._require_model(key)
        normalized_scope = _normalize_scope(scope)
        route = self._scope_list(normalized_scope)
        if model.key not in route:
            route.append(model.key)

    def get_model(self, key: str) -> dict[str, Any]:
        model = self._require_model(key)
        scopes = [scope for scope in VALID_SCOPES if model.key in self._scope_list(scope)]
        return {
            "key": model.key,
            "provider_model": model.provider_model,
            "api_key": model.api_key,
            "enabled": model.enabled,
            "api_base": model.api_base,
            "extra_headers": model.extra_headers,
            "max_tokens": model.max_tokens,
            "temperature": model.temperature,
            "reasoning_effort": model.reasoning_effort,
            "retry_on": list(model.retry_on or []),
            "description": model.description,
            "scopes": scopes,
        }

    def save(self) -> None:
        save_config(self.config)

    def _sync_legacy_model_fields(self) -> None:
        agent_chain = self._scope_list("agent")
        ceo_chain = self._scope_list("ceo")
        execution_chain = self._scope_list("execution")
        inspection_chain = self._scope_list("inspection")
        if agent_chain:
            self.config.agents.defaults.model = self._first_enabled_ref(agent_chain)
        elif ceo_chain:
            self.config.agents.defaults.model = self._first_enabled_ref(ceo_chain)
        if ceo_chain:
            self.config.org_graph.ceo_model = self._first_enabled_ref(ceo_chain)
        if execution_chain:
            self.config.org_graph.execution_model = self._first_enabled_ref(execution_chain)
        if inspection_chain:
            self.config.org_graph.inspection_model = self._first_enabled_ref(inspection_chain)

    def _scope_list(self, scope: str) -> list[str]:
        normalized_scope = _normalize_scope(scope)
        return getattr(self.config.models.roles, normalized_scope)

    def _require_model(self, key: str) -> ManagedModelConfig:
        item = self.config.get_managed_model(key)
        if item is None:
            raise ValueError(f"Unknown model key: {key}")
        return item

    def _replace_model(self, key: str, replacement: ManagedModelConfig) -> None:
        catalog = []
        for item in self.config.models.catalog:
            catalog.append(replacement if item.key == key else item)
        self.config.models.catalog = catalog

    def _first_enabled_ref(self, refs: list[str]) -> str:
        for ref in refs:
            item = self.config.get_managed_model(ref)
            if item is None or item.enabled:
                return ref
        return refs[0]
