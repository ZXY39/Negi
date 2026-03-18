from __future__ import annotations

from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.config.model_manager import ModelManager


class ModelConfigTool(Tool):
    """Manage project model catalog and role model chains."""

    @property
    def name(self) -> str:
        return "model_config"

    @property
    def description(self) -> str:
        return (
            "Manage .g3ku/config.json model catalog and role model chains. "
            "Supports listing models, adding/updating models, enabling/disabling models, "
            "and setting ordered fallback chains for ceo/execution/inspection scopes."
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
                        "set_memory_models",
                        "migrate_legacy",
                        "list_models",
                        "get_model",
                        "add_model",
                        "update_model",
                        "enable_model",
                        "disable_model",
                        "set_scope_chain",
                    ],
                    "description": "Model config action to perform.",
                },
                "key": {"type": "string", "description": "Managed model key."},
                "provider_model": {"type": "string", "description": "Provider:model identifier or managed model reference."},
                "api_key": {"type": "string", "description": "API key for add/update operations."},
                "api_base": {"type": "string", "description": "API base URL for add/update operations."},
                "extra_headers": {"type": "object", "description": "Optional extra headers for this model."},
                "enabled": {"type": "boolean", "description": "Optional enabled flag for add/update actions."},
                "max_tokens": {"type": "integer", "description": "Optional max output token cap for this model."},
                "temperature": {"type": "number", "description": "Optional default temperature for this model."},
                "reasoning_effort": {"type": "string", "description": "Optional reasoning effort override."},
                "retry_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Retry triggers such as network, 429, 5xx.",
                },
                "retry_count": {
                    "type": "integer",
                    "description": "Retryable failures to allow on the same model before fallback.",
                },
                "description": {"type": "string", "description": "Optional human-readable description."},
                "scopes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Scopes for add_model, e.g. ceo, execution, inspection.",
                },
                "scope": {"type": "string", "description": "Target scope for set_scope_chain."},
                "provider_id": {"type": "string", "description": "Provider template id."},
                "draft": {"type": "object", "description": "New LLM config draft payload."},
                "binding": {"type": "object", "description": "Binding payload containing key/config_id/enabled/retry settings."},
                "embedding_model_key": {"type": "string", "description": "Memory embedding model key."},
                "rerank_model_key": {"type": "string", "description": "Memory rerank model key."},
                "model_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered model keys for set_scope_chain.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, action: str, **kwargs: Any) -> Any:
        manager = ModelManager.load()
        action_name = str(action or "").strip().lower()

        if action_name == "list_templates":
            return {"items": manager.list_templates()}

        if action_name == "get_template":
            return manager.get_template(str(kwargs.get("provider_id") or "").strip())

        if action_name == "validate_draft":
            return manager.validate_draft(kwargs.get("draft") if isinstance(kwargs.get("draft"), dict) else {})

        if action_name == "probe_draft":
            return manager.probe_draft(kwargs.get("draft") if isinstance(kwargs.get("draft"), dict) else {})

        if action_name == "create_binding":
            draft = kwargs.get("draft") if isinstance(kwargs.get("draft"), dict) else {}
            binding = kwargs.get("binding") if isinstance(kwargs.get("binding"), dict) else {}
            result = manager.facade.create_binding(manager.config, draft_payload=draft, binding_payload=binding)
            manager.save()
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "update_binding":
            result = manager.facade.update_binding(
                manager.config,
                model_key=str(kwargs.get("key") or "").strip(),
                draft_payload=kwargs.get("draft") if isinstance(kwargs.get("draft"), dict) else {},
            )
            manager.save()
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "set_memory_models":
            result = manager.facade.get_memory_binding()
            await self._refresh_runtime(kwargs)
            return result.model_dump(mode='json')

        if action_name == "migrate_legacy":
            from g3ku.config.loader import load_config

            cfg = load_config()
            await self._refresh_runtime(kwargs)
            return {"ok": True, "workspace": str(cfg.workspace_path)}

        if action_name == "list_models":
            return {"items": manager.list_models()}

        if action_name == "get_model":
            result = manager.get_model(str(kwargs.get("key") or "").strip())
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "add_model":
            result = manager.add_model(
                key=str(kwargs.get("key") or "").strip(),
                provider_model=str(kwargs.get("provider_model") or "").strip(),
                api_key=str(kwargs.get("api_key") or "").strip(),
                api_base=str(kwargs.get("api_base") or "").strip(),
                scopes=[str(item) for item in (kwargs.get("scopes") or [])],
                extra_headers=kwargs.get("extra_headers") if isinstance(kwargs.get("extra_headers"), dict) else None,
                enabled=bool(kwargs.get("enabled", True)),
                max_tokens=kwargs.get("max_tokens"),
                temperature=kwargs.get("temperature"),
                reasoning_effort=kwargs.get("reasoning_effort"),
                retry_on=[str(item) for item in (kwargs.get("retry_on") or [])] if kwargs.get("retry_on") is not None else None,
                retry_count=kwargs.get("retry_count"),
                description=str(kwargs.get("description") or ""),
            )
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "update_model":
            result = manager.update_model(
                key=str(kwargs.get("key") or "").strip(),
                provider_model=kwargs.get("provider_model"),
                api_key=kwargs.get("api_key"),
                api_base=kwargs.get("api_base"),
                extra_headers=kwargs.get("extra_headers") if isinstance(kwargs.get("extra_headers"), dict) else None,
                max_tokens=kwargs.get("max_tokens"),
                temperature=kwargs.get("temperature"),
                reasoning_effort=kwargs.get("reasoning_effort"),
                retry_on=[str(item) for item in (kwargs.get("retry_on") or [])] if kwargs.get("retry_on") is not None else None,
                retry_count=kwargs.get("retry_count"),
                description=kwargs.get("description"),
            )
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "enable_model":
            result = manager.set_model_enabled(str(kwargs.get("key") or "").strip(), True)
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "disable_model":
            result = manager.set_model_enabled(str(kwargs.get("key") or "").strip(), False)
            await self._refresh_runtime(kwargs)
            return result

        if action_name == "set_scope_chain":
            result = manager.set_scope_chain(
                str(kwargs.get("scope") or "").strip(),
                [str(item) for item in (kwargs.get("model_keys") or [])],
            )
            await self._refresh_runtime(kwargs)
            return result

        return f"Error: unsupported action '{action_name}'"

    async def _refresh_runtime(self, kwargs: dict[str, Any]) -> None:
        runtime = kwargs.get("__g3ku_runtime") if isinstance(kwargs.get("__g3ku_runtime"), dict) else {}
        loop = runtime.get("loop")
        if loop is None:
            return
        try:
            from g3ku.shells.web import refresh_web_agent_runtime

            await refresh_web_agent_runtime(force=True, reason="model_config_tool")
        except Exception:
            # File save succeeded; runtime refresh is best-effort.
            return
