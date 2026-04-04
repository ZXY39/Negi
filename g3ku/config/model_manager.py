from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from g3ku.config.loader import load_config, save_config
from g3ku.config.schema import Config
from g3ku.llm_config.enums import AuthMode, Capability
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.utils.api_keys import SingleAPIKeyMaxConcurrency, normalize_single_api_key_max_concurrency

VALID_SCOPES = ("ceo", "execution", "inspection")
_UNSET = object()


def _normalize_scope(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    mapping = {
        "ceo": "ceo",
        "execution": "execution",
        "inspection": "inspection",
        "checker": "inspection",
    }
    if raw not in mapping:
        raise ValueError(f"Invalid scope: {value}. Valid scopes: {', '.join(VALID_SCOPES)}")
    return mapping[raw]


def _infer_auth_mode(provider_id: str) -> AuthMode:
    if provider_id in {"openai_codex", "github_copilot"}:
        return AuthMode.OAUTH_CACHE
    return AuthMode.API_KEY


def _optional_chat_parameters(
    *,
    max_tokens: Any = _UNSET,
    temperature: Any = _UNSET,
    reasoning_effort: Any = _UNSET,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if max_tokens is not _UNSET and max_tokens not in (None, ""):
        parameters["max_tokens"] = int(max_tokens)
    if temperature is not _UNSET and temperature not in (None, ""):
        parameters["temperature"] = float(temperature)
    if reasoning_effort is not _UNSET and str(reasoning_effort or "").strip():
        parameters["reasoning_effort"] = str(reasoning_effort).strip()
    return parameters


def _chat_binding_draft(
    *,
    provider_model: str,
    api_key: str,
    api_base: str,
    extra_headers: dict[str, str] | None,
    max_tokens: int | None,
    temperature: float | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    provider_id, model_id = Config.parse_provider_model(provider_model)
    return {
        "provider_id": provider_id,
        "capability": Capability.CHAT,
        "auth_mode": _infer_auth_mode(provider_id),
        "api_key": api_key,
        "base_url": api_base,
        "default_model": model_id,
        "parameters": _optional_chat_parameters(
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
        ),
        "extra_headers": extra_headers or {},
        "extra_options": {},
    }


@dataclass(slots=True)
class ModelManager:
    config: Config
    facade: LLMConfigFacade = field(init=False)

    def __post_init__(self) -> None:
        self.facade = LLMConfigFacade(self.config.workspace_path)
        self._revalidate()

    @classmethod
    def load(cls) -> "ModelManager":
        return cls(load_config())

    def list_templates(self) -> list[dict[str, Any]]:
        return self.facade.list_templates()

    def get_template(self, provider_id: str) -> dict[str, Any]:
        return self.facade.get_template(provider_id)

    def validate_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.facade.validate_draft(payload)

    def probe_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.facade.probe_draft(payload)

    async def probe_max_concurrency_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.facade.probe_max_concurrency_draft(payload)

    def list_models(self) -> list[dict[str, Any]]:
        items = self.facade.list_bindings(self.config)
        for item in items:
            item["scopes"] = [scope for scope in VALID_SCOPES if item["key"] in self._scope_list(scope)]
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
        retry_count: int | None = None,
        single_api_key_max_concurrency: SingleAPIKeyMaxConcurrency = None,
        description: str = "",
    ) -> dict[str, Any]:
        clean_key = str(key or "").strip()
        if not clean_key:
            raise ValueError("Model key is required")
        if self.config.get_managed_model(clean_key) is not None:
            raise ValueError(f"Model key already exists: {clean_key}")
        item = self.facade.create_binding(
            self.config,
            draft_payload=_chat_binding_draft(
                provider_model=str(provider_model or "").strip(),
                api_key=str(api_key or "").strip(),
                api_base=str(api_base or "").strip(),
                extra_headers=extra_headers,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            ),
            binding_payload={
                "key": clean_key,
                "config_id": "",
                "enabled": bool(enabled),
                "description": str(description or "").strip(),
                "retry_on": list(retry_on or ["network", "429", "5xx"]),
                "retry_count": 0 if retry_count is None else int(retry_count),
                "single_api_key_max_concurrency": normalize_single_api_key_max_concurrency(single_api_key_max_concurrency),
            },
        )
        for scope in scopes or []:
            self.add_model_to_scope(clean_key, scope)
        self._revalidate()
        self.save()
        return self.get_model(clean_key)

    def update_model(
        self,
        *,
        key: str,
        provider_model: str | None | object = _UNSET,
        api_key: str | None | object = _UNSET,
        api_base: str | None | object = _UNSET,
        extra_headers: dict[str, str] | None | object = _UNSET,
        max_tokens: int | None | object = _UNSET,
        temperature: float | None | object = _UNSET,
        reasoning_effort: str | None | object = _UNSET,
        retry_on: list[str] | None | object = _UNSET,
        retry_count: int | None | object = _UNSET,
        single_api_key_max_concurrency: SingleAPIKeyMaxConcurrency | object = _UNSET,
        description: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        item = self._require_model(key)
        patch: dict[str, Any] = {}
        if provider_model is not _UNSET:
            provider_id, model_id = self.config.parse_provider_model(str(provider_model).strip())
            patch["provider_id"] = provider_id
            patch["default_model"] = model_id
        if api_key is not _UNSET:
            patch["api_key"] = str(api_key).strip()
        if api_base is not _UNSET:
            patch["base_url"] = str(api_base).strip()
        parameters_present = any(value is not _UNSET for value in (max_tokens, temperature, reasoning_effort))
        parameters: dict[str, Any] = {}
        if max_tokens is not _UNSET:
            parameters["max_tokens"] = None if max_tokens in (None, "") else int(max_tokens)
        if temperature is not _UNSET:
            parameters["temperature"] = None if temperature in (None, "") else float(temperature)
        if reasoning_effort is not _UNSET:
            parameters["reasoning_effort"] = str(reasoning_effort).strip() or None
        if parameters_present:
            patch["parameters"] = parameters
        if extra_headers is not _UNSET:
            patch["extra_headers"] = extra_headers
        if patch:
            self.facade.update_binding(self.config, model_key=key, draft_payload=patch)
        if retry_on is not _UNSET:
            item.retry_on = list(retry_on)
        if retry_count is not _UNSET:
            item.retry_count = int(retry_count)
        if single_api_key_max_concurrency is not _UNSET:
            item.single_api_key_max_concurrency = normalize_single_api_key_max_concurrency(single_api_key_max_concurrency)
        if description is not _UNSET:
            item.description = str(description).strip()
        self._revalidate()
        self.save()
        return self.get_model(key)

    def set_model_enabled(self, key: str, enabled: bool) -> dict[str, Any]:
        item = self._require_model(key)
        item.enabled = bool(enabled)
        if not item.enabled:
            self._remove_model_from_roles(key)
            if self.config.agents.multi_agent.orchestrator_model_key == key:
                self.config.agents.multi_agent.orchestrator_model_key = None
        self._revalidate()
        self.save()
        return self.get_model(key)

    def delete_model(self, key: str) -> dict[str, Any]:
        item = self.get_model(key)
        self.facade.delete_binding(self.config, key)
        self._remove_model_from_roles(key)
        if self.config.agents.multi_agent.orchestrator_model_key == key:
            self.config.agents.multi_agent.orchestrator_model_key = None
        self._revalidate()
        self.save()
        return item

    def set_scope_chain(self, scope: str, model_keys: list[str]) -> dict[str, Any]:
        return self.update_scope_route(scope, model_keys=model_keys)

    def set_scope_max_iterations(self, scope: str, max_iterations: int) -> dict[str, Any]:
        return self.update_scope_route(scope, max_iterations=max_iterations)

    def set_scope_max_concurrency(self, scope: str, max_concurrency: int | None) -> dict[str, Any]:
        return self.update_scope_route(scope, max_concurrency=max_concurrency)

    def _prepare_scope_route_update(
        self,
        scope: str,
        *,
        model_keys: list[str] | None | object = _UNSET,
        max_iterations: Any = _UNSET,
        max_concurrency: Any = _UNSET,
    ) -> tuple[str, dict[str, Any]]:
        normalized_scope = _normalize_scope(scope)
        prepared: dict[str, Any] = {}
        if model_keys is not _UNSET:
            cleaned: list[str] = []
            seen: set[str] = set()
            for ref in list(model_keys or []):
                key = str(ref or "").strip()
                if not key or key in seen:
                    continue
                model = self._require_model(key)
                if not model.enabled:
                    raise ValueError(f"Disabled model cannot be assigned to roles: {key}")
                seen.add(key)
                cleaned.append(key)
            if not cleaned:
                raise ValueError("model_keys must not be empty")
            prepared["model_keys"] = cleaned
        if max_iterations is not _UNSET:
            prepared["max_iterations"] = self._normalize_optional_limit(max_iterations, field_name="max_iterations")
        if max_concurrency is not _UNSET:
            prepared["max_concurrency"] = self._normalize_optional_limit(max_concurrency, field_name="max_concurrency")
        if not prepared:
            raise ValueError("model_keys, max_iterations, or max_concurrency must be provided")
        return normalized_scope, prepared

    def _apply_scope_route_update(self, normalized_scope: str, prepared: dict[str, Any]) -> None:
        if "model_keys" in prepared:
            setattr(self.config.models.roles, normalized_scope, list(prepared["model_keys"]))
        if "max_iterations" in prepared:
            setattr(self.config.agents.role_iterations, normalized_scope, prepared["max_iterations"])
        if "max_concurrency" in prepared:
            setattr(self.config.agents.role_concurrency, normalized_scope, prepared["max_concurrency"])

    def update_scope_route(
        self,
        scope: str,
        *,
        model_keys: list[str] | None | object = _UNSET,
        max_iterations: Any = _UNSET,
        max_concurrency: Any = _UNSET,
    ) -> dict[str, Any]:
        normalized_scope, prepared = self._prepare_scope_route_update(
            scope,
            model_keys=model_keys,
            max_iterations=max_iterations,
            max_concurrency=max_concurrency,
        )
        self._apply_scope_route_update(normalized_scope, prepared)
        self._revalidate()
        self.save()
        return {
            "scope": normalized_scope,
            "model_keys": list(getattr(self.config.models.roles, normalized_scope)),
            "max_iterations": self.config.get_role_max_iterations(normalized_scope),
            "max_concurrency": self.config.get_role_max_concurrency(normalized_scope),
        }

    def update_scope_routes_bulk(self, updates: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not isinstance(updates, dict) or not updates:
            raise ValueError("updates must not be empty")
        prepared_updates: list[tuple[str, dict[str, Any]]] = []
        seen_scopes: set[str] = set()
        for scope, payload in updates.items():
            body = payload if isinstance(payload, dict) else {}
            normalized_scope, prepared = self._prepare_scope_route_update(
                str(scope or ""),
                model_keys=body.get("model_keys", _UNSET),
                max_iterations=body.get("max_iterations", _UNSET),
                max_concurrency=body.get("max_concurrency", _UNSET),
            )
            if normalized_scope in seen_scopes:
                raise ValueError(f"Duplicate scope update: {normalized_scope}")
            seen_scopes.add(normalized_scope)
            prepared_updates.append((normalized_scope, prepared))
        for normalized_scope, prepared in prepared_updates:
            self._apply_scope_route_update(normalized_scope, prepared)
        self._revalidate()
        self.save()
        return {
            "roles": {scope: list(getattr(self.config.models.roles, scope)) for scope in VALID_SCOPES},
            "role_iterations": {scope: self.config.get_role_max_iterations(scope) for scope in VALID_SCOPES},
            "role_concurrency": {scope: self.config.get_role_max_concurrency(scope) for scope in VALID_SCOPES},
            "updated_scopes": [scope for scope, _prepared in prepared_updates],
        }

    def add_model_to_scope(self, key: str, scope: str) -> None:
        model = self._require_model(key)
        if not model.enabled:
            raise ValueError(f"Disabled model cannot be assigned to roles: {model.key}")
        normalized_scope = _normalize_scope(scope)
        route = self._scope_list(normalized_scope)
        if model.key not in route:
            route.append(model.key)

    def get_model(self, key: str) -> dict[str, Any]:
        item = self.facade.get_binding(self.config, key)
        item["scopes"] = [scope for scope in VALID_SCOPES if key in self._scope_list(scope)]
        return item

    def save(self) -> None:
        save_config(self.config)

    def _scope_list(self, scope: str) -> list[str]:
        normalized_scope = _normalize_scope(scope)
        return getattr(self.config.models.roles, normalized_scope)

    @staticmethod
    def _normalize_optional_limit(value: Any, *, field_name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        clean_value = int(value)
        if clean_value < 0:
            raise ValueError(f"{field_name} must be >= 0")
        return clean_value

    def _remove_model_from_roles(self, key: str) -> None:
        for scope in VALID_SCOPES:
            refs = getattr(self.config.models.roles, scope)
            setattr(self.config.models.roles, scope, [ref for ref in refs if ref != key])

    def _require_model(self, key: str):
        item = self.config.get_managed_model(key)
        if item is None:
            raise ValueError(f"Unknown model key: {key}")
        return item

    def _revalidate(self) -> None:
        self.config = Config.model_validate(self.config.model_dump(mode="python"))
