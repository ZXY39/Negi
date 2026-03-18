from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from g3ku.config.loader import load_config, save_config
from g3ku.config.schema import Config
from g3ku.llm_config.enums import AuthMode, Capability
from g3ku.llm_config.facade import LLMConfigFacade

VALID_SCOPES = ("ceo", "execution", "inspection")


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


def _legacy_chat_draft(
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
        "parameters": {
            "timeout_s": 8,
            "temperature": 0.1 if temperature is None else float(temperature),
            "max_tokens": 4096 if max_tokens is None else int(max_tokens),
            **(
                {"reasoning_effort": str(reasoning_effort).strip()}
                if reasoning_effort is not None and str(reasoning_effort).strip()
                else {}
            ),
        },
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
        description: str = "",
    ) -> dict[str, Any]:
        clean_key = str(key or "").strip()
        if not clean_key:
            raise ValueError("Model key is required")
        if self.config.get_managed_model(clean_key) is not None:
            raise ValueError(f"Model key already exists: {clean_key}")
        item = self.facade.create_binding(
            self.config,
            draft_payload=_legacy_chat_draft(
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
        provider_model: str | None = None,
        api_key: str | None = None,
        api_base: str | None = None,
        extra_headers: dict[str, str] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        retry_on: list[str] | None = None,
        retry_count: int | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        item = self._require_model(key)
        patch: dict[str, Any] = {}
        if provider_model is not None:
            provider_id, model_id = self.config.parse_provider_model(str(provider_model).strip())
            patch["provider_id"] = provider_id
            patch["default_model"] = model_id
        if api_key is not None:
            patch["api_key"] = str(api_key).strip()
        if api_base is not None:
            patch["base_url"] = str(api_base).strip()
        parameters: dict[str, Any] = {}
        if max_tokens is not None:
            parameters["max_tokens"] = int(max_tokens)
        if temperature is not None:
            parameters["temperature"] = float(temperature)
        if reasoning_effort is not None:
            parameters["reasoning_effort"] = str(reasoning_effort).strip()
        if parameters:
            patch["parameters"] = parameters
        if extra_headers is not None:
            patch["extra_headers"] = extra_headers
        if patch:
            self.facade.update_binding(self.config, model_key=key, draft_payload=patch)
        if retry_on is not None:
            item.retry_on = list(retry_on)
        if retry_count is not None:
            item.retry_count = int(retry_count)
        if description is not None:
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

    def update_scope_route(
        self,
        scope: str,
        *,
        model_keys: list[str] | None = None,
        max_iterations: int | None = None,
    ) -> dict[str, Any]:
        normalized_scope = _normalize_scope(scope)
        updated = False
        if model_keys is not None:
            cleaned: list[str] = []
            seen: set[str] = set()
            for ref in model_keys:
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
            setattr(self.config.models.roles, normalized_scope, cleaned)
            updated = True
        if max_iterations is not None:
            clean_iterations = int(max_iterations)
            if clean_iterations < 2:
                raise ValueError("max_iterations must be >= 2")
            setattr(self.config.agents.role_iterations, normalized_scope, clean_iterations)
            updated = True
        if not updated:
            raise ValueError("model_keys or max_iterations must be provided")
        self._revalidate()
        self.save()
        return {
            "scope": normalized_scope,
            "model_keys": list(getattr(self.config.models.roles, normalized_scope)),
            "max_iterations": self.config.get_role_max_iterations(normalized_scope),
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
