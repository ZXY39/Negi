from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from g3ku.config.loader import load_config, save_config
from g3ku.config.schema import (
    LOAD_BALANCE_ROUTE_SCOPES,
    Config,
    ModelLoadBalanceGroup,
    ModelRouteEntry,
    coerce_route_entry,
)
from g3ku.llm_config.enums import AuthMode, Capability
from g3ku.llm_config.facade import LLMConfigFacade
from g3ku.utils.api_keys import SingleAPIKeyMaxConcurrency, normalize_single_api_key_max_concurrency
from g3ku.utils.retry_keywords import DEFAULT_RETRY_ON_KEYWORDS, split_retry_keywords

VALID_SCOPES = ("ceo", "execution", "inspection", "memory")


def _pick_alias(body: dict[str, Any], snake: str, camel: str) -> Any:
    if snake in body:
        return body.get(snake)
    if camel in body:
        return body.get(camel)
    return _UNSET


def _pick_route_entries(body: dict[str, Any]) -> Any:
    return _pick_alias(body, "route_entries", "routeEntries")


def _pick_load_balance_groups(body: dict[str, Any]) -> Any:
    return _pick_alias(body, "load_balance_groups", "loadBalanceGroups")
_UNSET = object()


def _normalize_scope(value: str) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    mapping = {
        "ceo": "ceo",
        "execution": "execution",
        "inspection": "inspection",
        "memory": "memory",
        "checker": "inspection",
    }
    if raw not in mapping:
        raise ValueError(f"Invalid scope: {value}. Valid scopes: {', '.join(VALID_SCOPES)}")
    return mapping[raw]


def _infer_auth_mode(provider_id: str) -> AuthMode:
    return AuthMode.API_KEY


def _optional_chat_parameters(
    *,
    max_tokens: Any = _UNSET,
    temperature: Any = _UNSET,
    reasoning_effort: Any = _UNSET,
    context_window_tokens: Any = _UNSET,
    request_timeout_seconds: Any = _UNSET,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    if max_tokens is not _UNSET and max_tokens not in (None, ""):
        parameters["max_tokens"] = int(max_tokens)
    if temperature is not _UNSET and temperature not in (None, ""):
        parameters["temperature"] = float(temperature)
    if reasoning_effort is not _UNSET and str(reasoning_effort or "").strip():
        parameters["reasoning_effort"] = str(reasoning_effort).strip()
    if context_window_tokens is not _UNSET and context_window_tokens not in (None, ""):
        parameters["context_window_tokens"] = int(context_window_tokens)
    if request_timeout_seconds is not _UNSET and request_timeout_seconds not in (None, ""):
        parameters["request_timeout_seconds"] = float(request_timeout_seconds)
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
    context_window_tokens: int | None,
    request_timeout_seconds: float | None = None,
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
            context_window_tokens=context_window_tokens,
            request_timeout_seconds=request_timeout_seconds,
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

    @classmethod
    def load_facade(cls) -> LLMConfigFacade:
        return LLMConfigFacade()

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
        name: str = "",
        context_window_tokens: int,
        image_multimodal_enabled: bool = False,
        request_timeout_seconds: float | None = None,
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
                context_window_tokens=context_window_tokens,
                request_timeout_seconds=request_timeout_seconds,
            ),
            binding_payload={
                "key": clean_key,
                "config_id": "",
                "enabled": bool(enabled),
                "description": str(description or "").strip(),
                "name": str(name or "").strip(),
                "retry_on": list(DEFAULT_RETRY_ON_KEYWORDS) if retry_on is None else split_retry_keywords(retry_on),
                "retry_count": 0 if retry_count is None else int(retry_count),
                "single_api_key_max_concurrency": normalize_single_api_key_max_concurrency(single_api_key_max_concurrency),
                "image_multimodal_enabled": bool(image_multimodal_enabled),
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
        name: str | None | object = _UNSET,
        context_window_tokens: int | None | object = _UNSET,
        image_multimodal_enabled: bool | object = _UNSET,
        quota_pool_key: str | None | object = _UNSET,
        request_timeout_seconds: float | None | object = _UNSET,
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
        parameters_present = any(
            value is not _UNSET
            for value in (max_tokens, temperature, reasoning_effort, context_window_tokens, request_timeout_seconds)
        )
        parameters: dict[str, Any] = {}
        if max_tokens is not _UNSET:
            parameters["max_tokens"] = None if max_tokens in (None, "") else int(max_tokens)
        if temperature is not _UNSET:
            parameters["temperature"] = None if temperature in (None, "") else float(temperature)
        if reasoning_effort is not _UNSET:
            parameters["reasoning_effort"] = str(reasoning_effort).strip() or None
        if context_window_tokens is not _UNSET:
            parameters["context_window_tokens"] = (
                None if context_window_tokens in (None, "") else int(context_window_tokens)
            )
        if request_timeout_seconds is not _UNSET:
            parameters["request_timeout_seconds"] = (
                None if request_timeout_seconds in (None, "") else float(request_timeout_seconds)
            )
        if parameters_present:
            patch["parameters"] = parameters
        if extra_headers is not _UNSET:
            patch["extra_headers"] = extra_headers
        if patch:
            self.facade.update_binding(self.config, model_key=key, draft_payload=patch)
        if retry_on is not _UNSET and retry_on is not None:
            # 显式提供（含 [] / ""）即尊重其值；空列表表示关闭关键字重试，不再回填默认。
            item.retry_on = split_retry_keywords(retry_on)
        if retry_count is not _UNSET:
            item.retry_count = int(retry_count)
        if single_api_key_max_concurrency is not _UNSET:
            item.single_api_key_max_concurrency = normalize_single_api_key_max_concurrency(single_api_key_max_concurrency)
        if description is not _UNSET:
            item.description = str(description).strip()
        if name is not _UNSET:
            item.name = self.facade._validate_binding_name_unique(
                self.config,
                name,
                exclude_key=key,
            )
        if image_multimodal_enabled is not _UNSET:
            item.image_multimodal_enabled = bool(image_multimodal_enabled)
        if quota_pool_key is not _UNSET:
            cleaned_pool_key = str(quota_pool_key or "").strip()
            item.quota_pool_key = cleaned_pool_key or None
        self._revalidate()
        self.save()
        return self.get_model(key)

    def rename_model(self, key: str, new_key: str) -> dict[str, Any]:
        item = self._require_model(key)
        clean_new_key = str(new_key or "").strip()
        if not clean_new_key:
            raise ValueError("Model key is required")
        if clean_new_key == str(item.key or "").strip():
            return self.get_model(key)
        if self.config.get_managed_model(clean_new_key) is not None:
            raise ValueError(f"Model key already exists: {clean_new_key}")
        item.key = clean_new_key
        # 改名必须同时命中链上的 direct entry 和组内成员；按裸字符串比较会静默失配。
        self.config.rename_model_key_in_routing(key, clean_new_key)
        if self.config.agents.multi_agent.orchestrator_model_key == key:
            self.config.agents.multi_agent.orchestrator_model_key = clean_new_key
        self._revalidate()
        self.save()
        return self.get_model(clean_new_key)

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
        route_entries: Any = _UNSET,
        load_balance_groups: Any = _UNSET,
        max_iterations: Any = _UNSET,
        max_concurrency: Any = _UNSET,
    ) -> tuple[str, dict[str, Any]]:
        normalized_scope = _normalize_scope(scope)
        prepared: dict[str, Any] = {}
        # 组先于链校验：一次保存可以同时定义组和引用它的链。
        groups = None
        if load_balance_groups is not _UNSET:
            groups = self._normalize_load_balance_groups(load_balance_groups)
            for group in groups.values():
                for member in group.model_keys:
                    self._require_chain_model(normalized_scope, member)
            prepared["load_balance_groups"] = groups
        groups_view = {**dict(self.config.models.load_balance_groups or {}), **(groups or {})}
        if model_keys is not _UNSET and route_entries is not _UNSET:
            # 不做静默合并：两份链同时给时以显式 route 为准，否则「保存了什么」和
            # 「运行的是什么」会分叉。
            raise ValueError("model_keys and route_entries cannot be provided together; route_entries wins")
        if route_entries is not _UNSET:
            entries, candidates = self._normalize_route_entries(normalized_scope, route_entries, groups_view)
            for key in candidates:
                self._require_chain_model(normalized_scope, key)
            prepared["route_entries"] = entries
            prepared["route_entry_candidates"] = candidates
        elif model_keys is not _UNSET:
            cleaned: list[str] = []
            seen: set[str] = set()
            for ref in list(model_keys or []):
                key = str(ref or "").strip()
                if not key or key in seen:
                    # 旧扁平链保持现有静默去重：老配置与旧 API 客户端不能被新校验打断。
                    continue
                self._require_chain_model(normalized_scope, key)
                seen.add(key)
                cleaned.append(key)
            if not cleaned and normalized_scope != "memory":
                raise ValueError("model_keys must not be empty")
            prepared["model_keys"] = cleaned
        if max_iterations is not _UNSET:
            prepared["max_iterations"] = self._normalize_optional_limit(max_iterations, field_name="max_iterations")
        if max_concurrency is not _UNSET:
            if normalized_scope == "memory":
                fixed = self._normalize_optional_limit(max_concurrency, field_name="max_concurrency")
                if fixed not in (None, 1):
                    raise ValueError("memory role concurrency is fixed at 1")
                prepared["max_concurrency"] = 1
            else:
                prepared["max_concurrency"] = self._normalize_optional_limit(max_concurrency, field_name="max_concurrency")
        if not prepared:
            raise ValueError("model_keys, route_entries, load_balance_groups, max_iterations, or max_concurrency must be provided")
        return normalized_scope, prepared

    def _require_chain_model(self, normalized_scope: str, key: str) -> None:
        """链/组成员共用的单模型校验：必须存在、enabled、有可信 context window；memory 车道还要 chat capability。"""
        model = self._require_model(key)
        if not model.enabled:
            raise ValueError(f"Disabled model cannot be assigned to roles: {key}")
        context_window_tokens = getattr(model, "context_window_tokens", None)
        if not isinstance(context_window_tokens, int) or context_window_tokens <= 25_000:
            # Some older installs have chat bindings whose `.g3ku/config.json` catalog entry
            # lacks `contextWindowTokens`, even though the bound `llm-config` record has a
            # valid `parameters.context_window_tokens`. Backfill on save so role-chain
            # edits can proceed without forcing operators to manually sync the two stores.
            resolved = None
            try:
                resolved = self.facade.get_binding(self.config, key).get("context_window_tokens")
            except Exception:
                resolved = None
            try:
                resolved_int = int(resolved) if resolved not in (None, "") else None
            except (TypeError, ValueError):
                resolved_int = None
            if isinstance(resolved_int, int) and resolved_int > 25_000:
                model.context_window_tokens = resolved_int
                context_window_tokens = resolved_int

        if not isinstance(context_window_tokens, int) or context_window_tokens <= 25_000:
            raise ValueError(
                f"Model {key} in scope {normalized_scope} must configure context_window_tokens > 25000"
            )
        if normalized_scope == "memory":
            capability = self.facade.get_binding_capability(self.config, key)
            if capability != "chat":
                raise ValueError(f"memory role only accepts chat-capable models: {key}")

    def _normalize_route_entries(self, normalized_scope: str, raw_entries: Any, groups_view: dict[str, Any]) -> tuple[list[Any], list[str]]:
        """显式 route_entries 的严格校验：重复项报错，组引用必须存在且车道允许。

        `groups_view` 是「已有组 + 本次一起提交的组」，所以一次保存里可以同时新建组并
        让链引用它，不会出现先存组再存链的中间悬空态。
        """
        items = raw_entries if isinstance(raw_entries, list) else []
        if not items:
            raise ValueError("route_entries must not be empty")
        entries: list[Any] = []
        candidates: list[str] = []
        seen_route: set[tuple[str, str]] = set()
        for item in items:
            entry = coerce_route_entry(item)
            identity = (entry.type, entry.group_key or entry.model_key or "")
            if not identity[1]:
                raise ValueError("route entry must set modelKey or groupKey")
            if identity in seen_route:
                kind = "group" if entry.type == "load_balance" else "model"
                raise ValueError(f"Duplicate {kind} route entry in models.roles.{normalized_scope}: {identity[1]}")
            seen_route.add(identity)
            entries.append(entry)
            if entry.type == "load_balance":
                if normalized_scope not in LOAD_BALANCE_ROUTE_SCOPES:
                    raise ValueError(
                        f"负载均衡组当前仅支持 {', '.join(LOAD_BALANCE_ROUTE_SCOPES)}，"
                        f"models.roles.{normalized_scope} 不能引用 groupKey: {entry.group_key}"
                    )
                group = groups_view.get(str(entry.group_key))
                if group is None:
                    raise ValueError(f"Unknown load balance group: {entry.group_key}")
                if not bool(getattr(group, "enabled", True)):
                    # 禁用组作为整段跳过处理，但显式保存时给出可读提示，避免运营以为生效了。
                    raise ValueError(f"Load balance group is disabled: {entry.group_key}")
                members = [str(member or "").strip() for member in list(group.model_keys or [])]
                candidates.extend(member for member in members if member)
                continue
            candidates.append(str(entry.model_key))
        return entries, candidates

    def _normalize_load_balance_groups(self, raw_groups: Any) -> dict[str, Any]:
        if raw_groups in (None, {}):
            return {}
        if not isinstance(raw_groups, dict):
            raise ValueError("load_balance_groups must be an object keyed by group key")
        normalized: dict[str, Any] = {}
        for raw_key, raw_group in raw_groups.items():
            group_key = str(raw_key or "").strip()
            if not group_key:
                raise ValueError("load balance group key must not be empty")
            if self.config.get_managed_model(group_key) is not None:
                raise ValueError(f"Group key collides with a model key: {group_key}")
            payload = raw_group if isinstance(raw_group, dict) else {}
            members = payload.get("modelKeys") if "modelKeys" in payload else payload.get("model_keys")
            cleaned_members: list[str] = []
            seen: set[str] = set()
            for member in list(members or []):
                key = str(member or "").strip()
                if not key:
                    continue
                # 组内重复必须报错：静默去重会改变均衡权重并隐藏配置错误。
                if key in seen:
                    raise ValueError(f"Duplicate member in load balance group {group_key}: {key}")
                seen.add(key)
                cleaned_members.append(key)
            if not cleaned_members:
                raise ValueError(f"Load balance group {group_key} must configure modelKeys")
            raw_rounds = payload.get("maxRetryRounds") if "maxRetryRounds" in payload else payload.get("max_retry_rounds")
            normalized[group_key] = ModelLoadBalanceGroup(
                enabled=bool(payload.get("enabled", True)),
                max_retry_rounds=1 if raw_rounds in (None, "") else int(raw_rounds),
                model_keys=cleaned_members,
            )
        return normalized

    def _apply_scope_route_update(self, normalized_scope: str, prepared: dict[str, Any]) -> None:
        # 组先落地再落链，`_revalidate()` 才能看到「链引用的组已存在」的完整图景。
        if "load_balance_groups" in prepared:
            self.config.models.load_balance_groups = dict(prepared["load_balance_groups"])
        if "route_entries" in prepared:
            self.config.set_role_model_routes(normalized_scope, prepared["route_entries"])
        elif "model_keys" in prepared:
            self.config.set_role_model_keys(normalized_scope, list(prepared["model_keys"]))
        if "max_iterations" in prepared:
            setattr(self.config.agents.role_iterations, normalized_scope, prepared["max_iterations"])
        if "max_concurrency" in prepared:
            setattr(self.config.agents.role_concurrency, normalized_scope, prepared["max_concurrency"])

    def update_scope_route(
        self,
        scope: str,
        *,
        model_keys: list[str] | None | object = _UNSET,
        route_entries: Any = _UNSET,
        load_balance_groups: Any = _UNSET,
        max_iterations: Any = _UNSET,
        max_concurrency: Any = _UNSET,
    ) -> dict[str, Any]:
        normalized_scope, prepared = self._prepare_scope_route_update(
            scope,
            model_keys=model_keys,
            route_entries=route_entries,
            load_balance_groups=load_balance_groups,
            max_iterations=max_iterations,
            max_concurrency=max_concurrency,
        )
        self._apply_scope_route_update(normalized_scope, prepared)
        self._revalidate()
        self.save()
        return {
            "scope": normalized_scope,
            **self._scope_route_view(normalized_scope),
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
            route_entries = _pick_route_entries(body)
            groups_payload = _pick_load_balance_groups(body)
            normalized_scope, prepared = self._prepare_scope_route_update(
                str(scope or ""),
                model_keys=body.get("model_keys", _UNSET),
                route_entries=route_entries,
                load_balance_groups=groups_payload,
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
            "roles": {scope: self.config.get_role_model_keys(scope) for scope in VALID_SCOPES},
            "routes": {scope: self.route_entries_payload(scope) for scope in VALID_SCOPES},
            "load_balance_groups": self.load_balance_groups_payload_view(),
            "role_iterations": {scope: self.config.get_role_max_iterations(scope) for scope in VALID_SCOPES},
            "role_concurrency": {scope: self.config.get_role_max_concurrency(scope) for scope in VALID_SCOPES},
            "updated_scopes": [scope for scope, _prepared in prepared_updates],
        }

    def add_model_to_scope(self, key: str, scope: str) -> None:
        model = self._require_model(key)
        if not model.enabled:
            raise ValueError(f"Disabled model cannot be assigned to roles: {model.key}")
        normalized_scope = _normalize_scope(scope)
        if key in self._scope_list(normalized_scope):
            return
        # 追加而不是就地 append：链的元素类型是 ModelRouteEntry，塞裸字符串会让
        # 后续按 key 比较的改名/删除路径静默失配。
        entries = self.config.get_role_model_routes(normalized_scope)
        entries.append(ModelRouteEntry(type="model", model_key=model.key))
        self.config.set_role_model_routes(normalized_scope, entries)

    def get_model(self, key: str) -> dict[str, Any]:
        item = self.facade.get_binding(self.config, key)
        item["scopes"] = [scope for scope in VALID_SCOPES if key in self._scope_list(scope)]
        return item

    def save(self) -> None:
        save_config(self.config)

    def _scope_list(self, scope: str) -> list[str]:
        # 候选展开视图：组成员也算「这个模型被这条链用到」。
        normalized_scope = _normalize_scope(scope)
        return self.config.get_role_model_keys(normalized_scope)

    def route_entries_payload(self, scope: str) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for entry in self.config.get_role_model_routes(scope):
            row: dict[str, Any] = {"type": str(entry.type or "model")}
            if entry.model_key:
                row["model_key"] = str(entry.model_key)
                row["modelKey"] = str(entry.model_key)
            if entry.group_key:
                row["group_key"] = str(entry.group_key)
                row["groupKey"] = str(entry.group_key)
            payload.append(row)
        return payload

    def load_balance_groups_payload_view(self) -> dict[str, Any]:
        groups = dict(self.config.models.load_balance_groups or {})
        return {
            str(group_key): {
                "enabled": bool(group.enabled),
                "max_retry_rounds": int(group.max_retry_rounds or 1),
                "maxRetryRounds": int(group.max_retry_rounds or 1),
                "model_keys": list(group.model_keys),
                "modelKeys": list(group.model_keys),
            }
            for group_key, group in groups.items()
        }

    def _scope_route_view(self, scope: str) -> dict[str, Any]:
        return {
            "model_keys": self.config.get_role_model_keys(scope),
            "route_entries": self.route_entries_payload(scope),
        }

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
        # 链上静默移除（沿用旧行为），但组里删到空会报错并指出被谁引用。
        self.config.remove_model_key_from_routing(key)

    def _require_model(self, key: str):
        item = self.config.get_managed_model(key)
        if item is None:
            raise ValueError(f"Unknown model key: {key}")
        return item

    def _revalidate(self) -> None:
        self.config = Config.model_validate(self.config.model_dump(mode="python"))
