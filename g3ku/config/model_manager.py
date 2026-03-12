from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from g3ku.config.loader import get_config_path, load_config, save_config
from g3ku.config.schema import Config, ManagedModelConfig

VALID_SCOPES = ("ceo", "execution", "inspection")


def _normalize_scope(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    mapping = {
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

    def __post_init__(self) -> None:
        self._normalize_shared_catalog()
        self._revalidate()

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
        self._revalidate()
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
        self._revalidate()
        self.save()
        return self.get_model(item.key)

    def set_model_enabled(self, key: str, enabled: bool) -> dict[str, Any]:
        item = self._require_model(key)
        enabled_flag = bool(enabled)
        self._replace_model(item.key, item.model_copy(update={"enabled": enabled_flag}))
        if not enabled_flag:
            self._remove_model_from_roles(item.key)
            if self.config.agents.multi_agent.orchestrator_model_key == item.key:
                self.config.agents.multi_agent.orchestrator_model_key = None
        self._revalidate()
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
            item = self._require_model(model_key)
            if not item.enabled:
                raise ValueError(f"Disabled model cannot be assigned to roles: {model_key}")
            seen.add(model_key)
            clean.append(model_key)
        if not clean:
            raise ValueError(f"models.roles.{normalized_scope} must be non-empty")
        role_routes = self.config.models.roles
        setattr(role_routes, normalized_scope, clean)
        self._revalidate()
        self.save()
        return {"scope": normalized_scope, "model_keys": clean}

    def delete_model(self, key: str) -> dict[str, Any]:
        item = self._require_model(key)
        self.config.models.catalog = [candidate for candidate in self.config.models.catalog if candidate.key != item.key]
        self._remove_model_from_roles(item.key)
        if self.config.agents.multi_agent.orchestrator_model_key == item.key:
            self.config.agents.multi_agent.orchestrator_model_key = None
        self._revalidate()
        self.save()
        return {"key": item.key, "deleted": True}

    def add_model_to_scope(self, key: str, scope: str) -> None:
        model = self._require_model(key)
        if not model.enabled:
            raise ValueError(f"Disabled model cannot be assigned to roles: {model.key}")
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

    def _scope_list(self, scope: str) -> list[str]:
        normalized_scope = _normalize_scope(scope)
        return getattr(self.config.models.roles, normalized_scope)

    def _remove_model_from_roles(self, key: str) -> None:
        role_routes = self.config.models.roles
        for scope in VALID_SCOPES:
            refs = getattr(role_routes, scope)
            setattr(role_routes, scope, [ref for ref in refs if ref != key])

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

    def _normalize_shared_catalog(self) -> None:
        catalog = list(self.config.models.catalog or [])
        if len(catalog) < 2:
            return

        usage: dict[str, set[str]] = {}
        for scope in VALID_SCOPES:
            for ref in getattr(self.config.models.roles, scope):
                key = str(ref or "").strip()
                if not key:
                    continue
                usage.setdefault(key, set()).add(scope)

        groups: dict[tuple[str, str, str, tuple[tuple[str, str], ...]], list[ManagedModelConfig]] = {}
        for item in catalog:
            groups.setdefault(self._shared_signature(item), []).append(item)

        alias_map: dict[str, str] = {}
        for items in groups.values():
            if len(items) < 2:
                continue
            scopes = {scope for item in items for scope in usage.get(item.key, set())}
            if len(scopes) < 2:
                continue
            canonical = items[0]
            for duplicate in items[1:]:
                alias_map[duplicate.key] = canonical.key

        if not alias_map:
            return

        self.config.models.catalog = [item for item in catalog if item.key not in alias_map]
        role_routes = self.config.models.roles
        for scope in VALID_SCOPES:
            refs = getattr(role_routes, scope)
            setattr(role_routes, scope, self._remap_model_refs(refs, alias_map))
        self.config.agents.multi_agent.orchestrator_model_key = self._remap_model_ref(
            self.config.agents.multi_agent.orchestrator_model_key,
            alias_map,
        )

    def _shared_signature(self, item: ManagedModelConfig) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
        headers = tuple(sorted((str(key), str(value)) for key, value in (item.extra_headers or {}).items()))
        return (
            str(item.provider_model or "").strip(),
            str(item.api_key or "").strip(),
            str(item.api_base or "").strip(),
            headers,
        )

    def _remap_model_ref(self, ref: str | None, alias_map: dict[str, str]) -> str | None:
        raw = str(ref or "").strip()
        if not raw:
            return None if ref is None else ""
        return alias_map.get(raw, raw)

    def _remap_model_refs(self, refs: list[str], alias_map: dict[str, str]) -> list[str]:
        clean: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            key = alias_map.get(str(ref or "").strip(), str(ref or "").strip())
            if not key or key in seen:
                continue
            seen.add(key)
            clean.append(key)
        return clean

    def _revalidate(self) -> None:
        self.config = Config.model_validate(self.config.model_dump(mode="python"))
