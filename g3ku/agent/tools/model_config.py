from __future__ import annotations

from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.config.model_manager import VALID_SCOPES, ModelManager, image_multimodal_chain_decision
from g3ku.config.schema import normalize_role_scope


def _as_int(value: Any, *, field: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer, got {value!r}") from None


def _as_float(value: Any, *, field: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number, got {value!r}") from None


def _as_bool(value: Any, *, field: str) -> bool:
    # 字符串 "false" 走 bool() 会变成 True，而这一格判错就是一条链读不了图。
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        token = value.strip().lower()
        if token in ("1", "true", "yes", "on"):
            return True
        if token in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"{field} must be a boolean, got {value!r}")
    return bool(value)


class ModelConfigTool(Tool):
    """Manage project model catalog and role model chains."""

    @property
    def name(self) -> str:
        return "model_config"

    @property
    def description(self) -> str:
        return (
            "Manage .g3ku/config.json model catalog, provider templates/drafts/bindings, "
            "and the ordered fallback chain for ceo/execution/inspection/memory scopes. "
            "get_scope_chain reads a chain as it is actually stored (ordered entries, load-balance "
            "group slots, per-role iteration and concurrency); set_scope_chain writes it back through "
            "the same payload shape the model config page uses, so group slots survive an edit. "
            "On update actions, leave out every field you do not want to change — a field sent as null "
            "counts as left out, and an empty string clears the ones that can be cleared."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list_templates",
                        "get_template",
                        "validate_draft",
                        "probe_draft",
                        "create_binding",
                        "update_binding",
                        "migrate_legacy",
                        "list_models",
                        "get_model",
                        "add_model",
                        "update_model",
                        "enable_model",
                        "disable_model",
                        "rename_model",
                        "delete_model",
                        "get_scope_chain",
                        "list_load_balance_groups",
                        "set_scope_chain",
                    ],
                    "description": "Model config action to perform.",
                },
                "key": {"type": "string", "description": "Managed model key."},
                "new_key": {
                    "type": "string",
                    "description": "Target key for rename_model. Chain entries and group members are rewritten together.",
                },
                "provider_model": {"type": "string", "description": "Provider:model identifier or managed model reference."},
                "api_key": {"type": "string", "description": "API key for add/update operations. Reads back masked."},
                "api_base": {"type": "string", "description": "API base URL for add/update operations."},
                "extra_headers": {"type": "object", "description": "Optional extra headers for this model."},
                "enabled": {"type": "boolean", "description": "Initial enabled state for add_model only; use enable_model / disable_model afterwards."},
                "max_tokens": {"type": "integer", "description": "Optional max output token cap for this model."},
                "temperature": {"type": "number", "description": "Optional default temperature for this model."},
                "reasoning_effort": {"type": "string", "description": "Optional reasoning effort override."},
                "context_window_tokens": {
                    "type": "integer",
                    "description": (
                        "Declared context window for this model. Required for add_model. This is the value "
                        "the send path and dispatch read, so a window larger than the provider really serves "
                        "shows up as a clamped reply, not an error."
                    ),
                },
                "request_timeout_seconds": {"type": "number", "description": "Optional per-request timeout for this model."},
                "image_multimodal_enabled": {
                    "type": "boolean",
                    "description": (
                        "Whether this model can take images. A chain is treated as image-incapable when any "
                        "member is off, so writing this changes what every chain containing the model can do. "
                        "Writes echo the affected chains back."
                    ),
                },
                "quota_pool_key": {"type": "string", "description": "Optional quota pool this binding shares with others."},
                "name": {"type": "string", "description": "Display name shown in the model catalog and chains."},
                "retry_on": {
                    "type": ["array", "string"],
                    "items": {"type": "string"},
                    "description": (
                        "Custom retry keywords matched as lowercase substrings of the provider "
                        "error text; a hit triggers automatic backoff retry on the same model. "
                        "Accepts a list or a space/comma-separated string, e.g. 'network 429 502'. "
                        "Preset aliases 'network' and '429' expand to curated token lists; "
                        "add_model defaults to 'network 429' when omitted. An empty list disables "
                        "keyword retry instead of restoring the default."
                    ),
                },
                "retry_count": {
                    "type": "integer",
                    "description": (
                        "Max retryable-error rounds for this model before falling back to the "
                        "next model (one round = one pass over all of the model's API keys, "
                        "with exponential backoff + jitter between rounds). 0/omitted uses the "
                        "built-in default of 10 rounds. Non-retryable errors try each key once "
                        "then move to the next model."
                    ),
                },
                "description": {"type": "string", "description": "Optional human-readable description."},
                "scopes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Scopes for add_model, e.g. ceo, execution, inspection.",
                },
                "scope": {"type": "string", "description": "Target scope for get_scope_chain and set_scope_chain."},
                "provider_id": {"type": "string", "description": "Provider template id."},
                "draft": {"type": "object", "description": "New LLM config draft payload."},
                "binding": {"type": "object", "description": "Binding payload containing key/config_id/enabled/retry settings."},
                "model_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ordered model keys for set_scope_chain. This is a flat chain: it replaces the "
                        "stored chain entirely, so a chain that contains load-balance groups must be written "
                        "with route_entries instead."
                    ),
                },
                "route_entries": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "Ordered chain as stored, for set_scope_chain. Each entry is "
                        '{"type":"model","model_key":"..."} or {"type":"load_balance","group_key":"..."}. '
                        "Groups must already exist (see list_load_balance_groups); this action does not "
                        "create or edit them. Load balance entries are only accepted on execution and inspection."
                    ),
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Per-role iteration budget for set_scope_chain.",
                },
                "max_concurrency": {
                    "type": "integer",
                    "description": "Per-role concurrency for set_scope_chain. The memory role is fixed at 1.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> Any:
        action_name = str(action or "").strip().lower()

        if action_name == "list_templates":
            return {"items": ModelManager.load_facade().list_templates()}

        if action_name == "get_template":
            return ModelManager.load_facade().get_template(str(kwargs.get("provider_id") or "").strip())

        if action_name == "validate_draft":
            return ModelManager.load_facade().validate_draft(
                kwargs.get("draft") if isinstance(kwargs.get("draft"), dict) else {}
            )

        if action_name == "probe_draft":
            return ModelManager.load_facade().probe_draft(
                kwargs.get("draft") if isinstance(kwargs.get("draft"), dict) else {}
            )

        manager = ModelManager.load()

        if action_name == "create_binding":
            draft = kwargs.get("draft") if isinstance(kwargs.get("draft"), dict) else {}
            binding = kwargs.get("binding") if isinstance(kwargs.get("binding"), dict) else {}
            result = manager.facade.create_binding(manager.config, draft_payload=draft, binding_payload=binding)
            manager._revalidate()
            manager.save()
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "update_binding":
            result = manager.facade.update_binding(
                manager.config,
                model_key=str(kwargs.get("key") or "").strip(),
                draft_payload=kwargs.get("draft") if isinstance(kwargs.get("draft"), dict) else {},
            )
            manager._revalidate()
            manager.save()
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "migrate_legacy":
            from g3ku.config.loader import load_config

            cfg = load_config()
            # Legacy migration may rewrite memory embedding/rerank bindings,
            # which live outside the memory_runtime fingerprint tree, so the
            # memory runtime sync must be forced (mirrors admin /llm/migrate).
            await self._refresh_runtime(kwargs, force_memory_sync=True)
            return {"ok": True, "workspace": str(cfg.workspace_path)}

        if action_name == "list_models":
            return {"items": manager.list_models()}

        if action_name == "get_model":
            return manager.get_model(str(kwargs.get("key") or "").strip())

        if action_name == "add_model":
            if not self._provided(kwargs, "context_window_tokens"):
                raise ValueError(
                    "context_window_tokens is required for add_model: 声明窗口没有安全的默认值，"
                    "而链准入会拒绝窗口缺失或 ≤25000 的模型。填该模型真实可用的上下文窗口。"
                )
            result = manager.add_model(
                key=str(kwargs.get("key") or "").strip(),
                provider_model=str(kwargs.get("provider_model") or "").strip(),
                api_key=str(kwargs.get("api_key") or "").strip(),
                api_base=str(kwargs.get("api_base") or "").strip(),
                context_window_tokens=_as_int(kwargs.get("context_window_tokens"), field="context_window_tokens"),
                scopes=[str(item) for item in (kwargs.get("scopes") or [])],
                extra_headers=kwargs.get("extra_headers") if isinstance(kwargs.get("extra_headers"), dict) else None,
                enabled=True if not self._provided(kwargs, "enabled") else _as_bool(kwargs.get("enabled"), field="enabled"),
                max_tokens=None if kwargs.get("max_tokens") in (None, "") else _as_int(kwargs.get("max_tokens"), field="max_tokens"),
                temperature=None if kwargs.get("temperature") in (None, "") else _as_float(kwargs.get("temperature"), field="temperature"),
                reasoning_effort=kwargs.get("reasoning_effort"),
                retry_on=kwargs.get("retry_on"),
                retry_count=None if kwargs.get("retry_count") in (None, "") else _as_int(kwargs.get("retry_count"), field="retry_count"),
                description=str(kwargs.get("description") or ""),
                name=str(kwargs.get("name") or ""),
                image_multimodal_enabled=_as_bool(kwargs.get("image_multimodal_enabled", False), field="image_multimodal_enabled"),
                request_timeout_seconds=(
                    None
                    if kwargs.get("request_timeout_seconds") in (None, "")
                    else _as_float(kwargs.get("request_timeout_seconds"), field="request_timeout_seconds")
                ),
            )
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "update_model":
            key = str(kwargs.get("key") or "").strip()
            patch = self._collect_model_patch(kwargs)
            if not patch:
                return "Error: update_model needs at least one field to change"
            result = manager.update_model(key=key, **patch)
            image_rows = self._chain_image_rows(manager, key) if "image_multimodal_enabled" in patch else []
            await self._refresh_runtime(kwargs)
            if image_rows:
                result = {**result, "image_capability_by_chain": image_rows}
            return result

        if action_name == "enable_model":
            result = manager.set_model_enabled(str(kwargs.get("key") or "").strip(), True)
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "disable_model":
            result = manager.set_model_enabled(str(kwargs.get("key") or "").strip(), False)
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "rename_model":
            result = manager.rename_model(
                str(kwargs.get("key") or "").strip(),
                str(kwargs.get("new_key") or "").strip(),
            )
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "delete_model":
            key = str(kwargs.get("key") or "").strip()
            # 删除会静默摘掉链上的这一位，所以先把"从哪几条链消失"读出来交回模型。
            removed_from = [scope for scope in VALID_SCOPES if key in manager.config.get_role_model_keys(scope)]
            result = manager.delete_model(key)
            await self._refresh_runtime(kwargs)
            return {**(result if isinstance(result, dict) else {"item": result}), "removed_from_scopes": removed_from}

        if action_name == "get_scope_chain":
            scope = normalize_role_scope(str(kwargs.get("scope") or "").strip())
            refs = manager.config.get_role_model_keys(scope)
            enabled, blockers = image_multimodal_chain_decision(manager.config, refs)
            return {
                "scope": scope,
                # 两份读数都要给：候选展开视图不代表 fallback 顺序，route_entries 才是。
                "model_keys": refs,
                "route_entries": manager.route_entries_payload(scope),
                "max_iterations": manager.config.get_role_max_iterations(scope),
                "max_concurrency": manager.config.get_role_max_concurrency(scope),
                "chain_image_multimodal_enabled": enabled,
                "image_blockers": blockers,
            }

        if action_name == "list_load_balance_groups":
            groups = manager.load_balance_groups_payload_view()
            used_by: dict[str, list[str]] = {}
            for scope in VALID_SCOPES:
                for entry in manager.config.get_role_model_routes(scope):
                    if str(getattr(entry, "type", "") or "") != "load_balance":
                        continue
                    group_key = str(getattr(entry, "group_key", "") or "").strip()
                    if group_key:
                        used_by.setdefault(group_key, []).append(scope)
            items = [
                {**payload, "group_key": group_key, "used_by_scopes": used_by.get(group_key, [])}
                for group_key, payload in groups.items()
            ]
            return {"items": items}

        if action_name == "set_scope_chain":
            route_kwargs: dict[str, Any] = {}
            if self._provided(kwargs, "model_keys"):
                route_kwargs["model_keys"] = [str(item) for item in (kwargs.get("model_keys") or [])]
            if self._provided(kwargs, "route_entries"):
                route_kwargs["route_entries"] = kwargs.get("route_entries")
            if self._provided(kwargs, "max_iterations"):
                route_kwargs["max_iterations"] = kwargs.get("max_iterations")
            if self._provided(kwargs, "max_concurrency"):
                route_kwargs["max_concurrency"] = kwargs.get("max_concurrency")
            if not route_kwargs:
                return "Error: set_scope_chain needs model_keys, route_entries, max_iterations or max_concurrency"
            result = manager.update_scope_route(
                str(kwargs.get("scope") or "").strip(),
                **route_kwargs,
            )
            await self._refresh_runtime(kwargs)
            return result

        return f"Error: unsupported action '{action_name}'"

    _PATCH_TEXT_FIELDS = ("provider_model", "api_key", "api_base", "reasoning_effort", "description", "name", "quota_pool_key")

    @staticmethod
    def _provided(kwargs: dict[str, Any], field: str) -> bool:
        """模型会按参数表把没用到的字段填成 null，所以"交了这一格"的判据是值不是 None。

        用 `field in kwargs` 判定会让 `route_entries=null` 与 `model_keys=[...]` 同时下去，
        manager 直接报"两者不能同时提供"，整次链写失败。
        """
        return field in kwargs and kwargs.get(field) is not None

    def _collect_model_patch(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """只交模型真的填了的字段：manager 用 _UNSET 区分"不动这一格"和"把它抹成空"。

        把缺席的键当成 None 传下去会清掉 api_key/api_base，或在 parse_provider_model 里
        报 "Invalid provider_model"，两种都不是调用方要的效果。
        """
        patch: dict[str, Any] = {}
        for field in self._PATCH_TEXT_FIELDS:
            # 文本字段连空串一起当作"没填"：清空 api_base 会让整份 draft 过不了 base_url 校验。
            if self._provided(kwargs, field) and str(kwargs.get(field) or "").strip() != "":
                patch[field] = kwargs.get(field)
        if self._provided(kwargs, "extra_headers"):
            value = kwargs.get("extra_headers")
            patch["extra_headers"] = value if isinstance(value, dict) else None
        for field in ("max_tokens", "context_window_tokens"):
            # 数值字段留了显式清空的路：交空串 = 清掉，交 null = 不动。
            if self._provided(kwargs, field):
                patch[field] = None if str(kwargs.get(field) or "").strip() == "" else _as_int(kwargs.get(field), field=field)
        if self._provided(kwargs, "retry_count") and str(kwargs.get("retry_count") or "").strip() != "":
            # retry_count 没有"清空"语义，交 None 会让 manager 的 int(None) 直接抛。
            patch["retry_count"] = _as_int(kwargs.get("retry_count"), field="retry_count")
        for field in ("temperature", "request_timeout_seconds"):
            if self._provided(kwargs, field):
                patch[field] = None if str(kwargs.get(field) or "").strip() == "" else _as_float(kwargs.get(field), field=field)
        if self._provided(kwargs, "retry_on"):
            patch["retry_on"] = kwargs.get("retry_on")
        if self._provided(kwargs, "image_multimodal_enabled"):
            patch["image_multimodal_enabled"] = _as_bool(kwargs.get("image_multimodal_enabled"), field="image_multimodal_enabled")
        return patch

    def _chain_image_rows(self, manager: ModelManager, key: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for scope in VALID_SCOPES:
            refs = manager.config.get_role_model_keys(scope)
            if key not in refs:
                continue
            enabled, blockers = image_multimodal_chain_decision(manager.config, refs)
            rows.append(
                {
                    "scope": scope,
                    "chain_image_multimodal_enabled": enabled,
                    "image_blockers": blockers,
                }
            )
        return rows

    async def _refresh_runtime(self, kwargs: dict[str, Any], *, force_memory_sync: bool = False) -> None:
        runtime = kwargs.get("__g3ku_runtime") if isinstance(kwargs.get("__g3ku_runtime"), dict) else {}
        loop = runtime.get("loop")
        if loop is None:
            return
        try:
            from g3ku.shells.web import refresh_web_agent_runtime, is_no_ceo_model_configured_error
            # Model routing changes do not alter the memory_runtime resource
            # fingerprint, so the memory runtime sync stays fingerprint-gated
            # (force_memory_sync defaults to False). Forcing it would reset the
            # memory runtime mid-turn and disrupt the in-flight turn. Only
            # actions that rewrite memory-affecting settings stored outside the
            # fingerprint tree (migrate_legacy) opt into the forced reset.
            await refresh_web_agent_runtime(
                force=True, reason="model_config_tool", force_memory_sync=force_memory_sync
            )
        except Exception as exc:
            if is_no_ceo_model_configured_error(exc):
                return
            if str(exc or "").strip() == "project is locked":
                # Non-web shells (e.g. the local CLI) have no unlocked global
                # web agent; refresh the executing loop directly instead.
                from g3ku.runtime.config_refresh import refresh_loop_runtime_config

                refresh_loop_runtime_config(
                    loop, force=True, reason="model_config_tool", force_memory_sync=force_memory_sync
                )
                return
            message = str(exc or "web_runtime_refresh_failed").strip() or "web_runtime_refresh_failed"
            raise RuntimeError(
                f"{message} | config_saved=true web_refreshed=false：配置已经落盘，"
                "重放整写不会让它更早生效，下一次刷新或重启会补上"
            ) from exc
        service = getattr(loop, "main_task_service", None)
        if service is None or str(getattr(service, "execution_mode", "") or "").strip().lower() != "web":
            return
        if not bool(getattr(service, "is_worker_online", lambda **_kwargs: False)()):
            return
        try:
            await service.request_worker_runtime_refresh(reason="model_config_tool")
        except Exception as exc:
            message = str(exc or "worker_runtime_refresh_failed").strip() or "worker_runtime_refresh_failed"
            raise RuntimeError(
                f"{message} | config_saved=true web_refreshed=true worker_refreshed=false："
                "本会话侧已经用上新配置，节点道要等下一次刷新或重启"
            ) from exc
