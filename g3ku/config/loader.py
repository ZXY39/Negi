"""Configuration loading utilities."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from importlib.resources import files as resource_files
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from g3ku.china_bridge.registry import china_channel_attr, china_channel_ids, china_channel_spec
from g3ku.config.schema import Config, DEFAULT_ROLE_MAX_ITERATIONS
from g3ku.llm_config.migration import migrate_raw_config_if_needed
from g3ku.security import (
    apply_config_secret_entries,
    extract_config_secret_entries,
    get_bootstrap_security_service,
    strip_config_secret_entries,
)


def _project_config_path() -> Path:
    return Path.cwd() / ".g3ku" / "config.json"


def _project_example_config_path() -> Path:
    return Path.cwd() / ".g3ku" / "config.example.json"


def get_config_path() -> Path:
    """Get the project-local configuration file path."""
    return _project_config_path()


def get_example_config_path() -> Path:
    """Get the project-local example configuration file path."""
    return _project_example_config_path()


def get_data_dir() -> Path:
    """Get the g3ku data directory."""
    from g3ku.utils.helpers import get_data_path

    return get_data_path()


def _bundled_example_config_text() -> tuple[str, str] | None:
    try:
        resource = resource_files("g3ku") / "templates" / "config.example.json"
    except Exception:
        return None
    if not resource.is_file():
        return None
    return resource.read_text(encoding="utf-8"), "bundled:g3ku/templates/config.example.json"


def _read_example_config_text(example_path: Path | None = None) -> tuple[str, str]:
    if example_path is not None:
        path = Path(example_path)
        try:
            return path.read_text(encoding="utf-8"), str(path)
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                f"Required config example file not found: {path}. "
                f"Create {get_example_config_path()} or restore the bundled config example."
            ) from exc

    project_example = get_example_config_path()
    if project_example.exists():
        return project_example.read_text(encoding="utf-8"), str(project_example)

    bundled = _bundled_example_config_text()
    if bundled is not None:
        return bundled

    raise FileNotFoundError(
        "Required config example file not found. "
        f"Checked project-local path {project_example} and bundled template g3ku/templates/config.example.json."
    )


def _load_json_text(raw: str, source: str) -> dict[str, Any]:
    if not raw.strip():
        raise ValueError(f"Required config file is empty: {source}. Populate {get_config_path()} before starting g3ku.")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON config from {source}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a JSON object: {source}")
    return data


def ensure_startup_config_ready(config_path: Path | None = None) -> bool:
    """Apply safe first-start bootstrap tweaks before loading runtime config.

    If the project config file does not exist, bootstrap it from a project-local
    `.g3ku/config.example.json` when present, otherwise fall back to the bundled
    example shipped inside the package. The user still needs to fill in real API
    keys before LLM features will work.
    """
    path = Path(config_path) if config_path is not None else get_config_path()
    if not path.exists():
        try:
            raw_example, source = _read_example_config_text()
        except FileNotFoundError:
            return False
        _load_json_text(raw_example, source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw_example.rstrip() + "\n", encoding="utf-8")
        from loguru import logger

        logger.info(
            "Config bootstrapped from example: {} -> {}. "
            "Please configure your API keys before using LLM features.",
            source,
            path,
        )
        return True
    return False


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Required config file not found: {path}. Create {get_config_path()} before starting g3ku."
        ) from exc

    return _load_json_text(raw, str(path))


def _path_exists(data: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _leaf_paths(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(value, dict):
        out: list[tuple[str, ...]] = []
        for key, child in value.items():
            out.extend(_leaf_paths(child, prefix + (key,)))
        return out
    return [prefix]


def _provider_payload(cfg: Config, provider_name: str) -> dict[str, object]:
    provider_cfg = getattr(cfg.providers, provider_name)
    return {
        "apiKey": provider_cfg.api_key,
        "apiBase": provider_cfg.api_base,
        "extraHeaders": provider_cfg.extra_headers,
    }


_LEGACY_MODEL_PAYLOAD_FIELDS = (
    "providerModel",
    "provider_model",
    "apiKey",
    "api_key",
    "apiBase",
    "api_base",
    "extraHeaders",
    "extra_headers",
    "maxTokens",
    "max_tokens",
    "temperature",
    "reasoningEffort",
    "reasoning_effort",
)


def _ensure_no_removed_model_fields(raw_data: dict[str, Any]) -> None:
    legacy_paths = (
        ("agents", "defaults", "model"),
        ("agents", "multiAgent", "orchestratorModel"),
        ("agents", "multi_agent", "orchestrator_model"),
    )
    hits = [".".join(path) for path in legacy_paths if _path_exists(raw_data, path)]
    if hits:
        preview = ", ".join(hits)
        raise ValueError(
            "Current config only supports models.catalog + models.roles + orchestrator_model_key. "
            f"Remove legacy fields from {get_config_path()}: {preview}"
        )


def _ensure_no_removed_role_scopes(raw_data: dict[str, Any]) -> None:
    models = raw_data.get("models") if isinstance(raw_data.get("models"), dict) else None
    roles = models.get("roles") if isinstance(models, dict) else None
    if isinstance(roles, dict) and "agent" in roles:
        raise ValueError(
            "models.roles.agent has been removed. "
            f"Move its model chain to models.roles.ceo in {get_config_path()} and delete models.roles.agent."
        )


def _ensure_no_removed_tools_config(raw_data: dict[str, Any]) -> None:
    if "tools" not in raw_data:
        return
    raise ValueError(
        "config.tools has been removed. Move non-secret tool settings into tools/*/resource.yaml settings, "
        "and move secrets into top-level toolSecrets in .g3ku/config.json."
    )


def _ensure_no_removed_channel_config(raw_data: dict[str, Any]) -> None:
    channels = raw_data.get("channels")
    if not isinstance(channels, dict):
        return
    legacy_keys = sorted(str(key) for key in channels.keys())
    raise ValueError(
        "Legacy channels config has been removed. "
        "Move China platform settings under chinaBridge.channels using canonical ids "
        "{qqbot,dingtalk,wecom,wecom-app,wecom-kf,wechat-mp,feishu-china}, "
        "move sendProgress/sendToolHints to chinaBridge.sendProgress/chinaBridge.sendToolHints, "
        f"and remove channels.* from {get_config_path()}. Found: {', '.join(legacy_keys)}"
    )


def _ensure_no_removed_gateway_config(raw_data: dict[str, Any]) -> None:
    gateway = raw_data.get("gateway")
    if not isinstance(gateway, dict):
        return
    raise ValueError(
        "Legacy gateway config has been removed. "
        f"Move gateway.host/gateway.port to web.host/web.port in {get_config_path()} and delete gateway."
    )


def _referenced_provider_names(cfg: Config) -> list[str]:
    names: set[str] = set()

    for item in list(cfg.models.catalog or []):
        provider_model = str(getattr(item, "provider_model", "") or "").strip()
        if not provider_model and str(getattr(item, "llm_config_id", "") or "").strip():
            try:
                from g3ku.llm_config.facade import LLMConfigFacade

                provider_model = str(
                    LLMConfigFacade(cfg.workspace_path).get_binding(cfg, item.key).get("provider_model") or ""
                ).strip()
            except Exception:
                provider_model = ""
        if not provider_model:
            continue
        provider_name, _ = cfg.parse_provider_model(provider_model)
        names.add(provider_name)

    for provider_name, payload in cfg.providers.model_dump().items():
        if isinstance(payload, dict) and any(value not in (None, "", {}, []) for value in payload.values()):
            names.add(str(provider_name))

    return sorted(names)


def _raw_uses_inline_model_payload(raw_data: dict[str, Any]) -> bool:
    models = raw_data.get("models") if isinstance(raw_data.get("models"), dict) else None
    catalog = (models or {}).get("catalog")
    if not isinstance(catalog, list):
        return False
    for item in catalog:
        if isinstance(item, dict) and any(field_name in item for field_name in _LEGACY_MODEL_PAYLOAD_FIELDS):
            return True
    return False


def _binding_auth_mode(provider_id: str) -> str:
    if provider_id in {"openai_codex", "github_copilot"}:
        return "oauth_cache"
    return "api_key"


def _normalize_inline_model_bindings(cfg: Config) -> bool:
    from g3ku.llm_config.models import ProviderConfigDraft
    from g3ku.llm_config.facade import LLMConfigFacade

    facade = LLMConfigFacade(cfg.workspace_path)
    changed = False
    for item in cfg.models.catalog:
        if str(item.llm_config_id or "").strip():
            continue
        provider_id, model_id = cfg.parse_provider_model(str(item.provider_model or "").strip())
        provider_cfg = getattr(cfg.providers, provider_id, None)
        draft = ProviderConfigDraft(
            provider_id=provider_id,
            capability="chat",
            auth_mode=_binding_auth_mode(provider_id),
            api_key=str(
                item.api_key
                or (provider_cfg.api_key if provider_cfg is not None else "")
                or ""
            ).strip(),
            base_url=str(
                item.api_base
                or (provider_cfg.api_base if provider_cfg is not None else "")
                or ""
            ).strip(),
            default_model=model_id,
            parameters={
                "timeout_s": 8,
                "temperature": float(item.temperature),
                "max_tokens": int(item.max_tokens),
                **(
                    {"reasoning_effort": str(item.reasoning_effort).strip()}
                    if item.reasoning_effort is not None and str(item.reasoning_effort).strip()
                    else {}
                ),
            },
            extra_headers=dict(
                item.extra_headers
                or (provider_cfg.extra_headers if provider_cfg is not None else {})
                or {}
            ),
            extra_options={},
        )
        validation = facade.config_service.validate_draft(draft)
        if not validation.valid or validation.normalized_preview is None:
            raise ValueError(f"Failed to normalize model binding for {item.key}")
        record = validation.normalized_preview.model_copy(
            update={
                "config_id": uuid4().hex,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        )
        facade.repository.save(record, last_probe_status=None)
        item.llm_config_id = record.config_id
        changed = True
    return changed


def _managed_models_payload(cfg: Config) -> tuple[list[dict[str, object]], dict[str, list[str]]]:
    catalog = [
        {
            "key": item.key,
            "llmConfigId": item.llm_config_id,
            "enabled": item.enabled,
            "retryOn": list(item.retry_on or []),
            "retryCount": int(getattr(item, "retry_count", 0) or 0),
            "description": item.description,
        }
        for item in cfg.models.catalog
    ]
    routes = {
        "ceo": list(cfg.models.roles.ceo),
        "execution": list(cfg.models.roles.execution),
        "inspection": list(cfg.models.roles.inspection),
    }
    return catalog, routes


def _runtime_config_payload(cfg: Config) -> dict[str, object]:
    catalog, routes = _managed_models_payload(cfg)
    providers = {
        provider_name: _provider_payload(cfg, provider_name)
        for provider_name in _referenced_provider_names(cfg)
    }
    channel_payloads = {
        channel_id: getattr(cfg.china_bridge.channels, china_channel_attr(channel_id)).model_dump(by_alias=True, exclude_none=True)
        for channel_id in china_channel_ids()
    }

    return {
        "agents": {
            "defaults": {
                "workspace": cfg.agents.defaults.workspace,
                "runtime": cfg.agents.defaults.runtime,
                "maxTokens": cfg.agents.defaults.max_tokens,
                "temperature": cfg.agents.defaults.temperature,
                "maxToolIterations": cfg.agents.defaults.max_tool_iterations,
                "memoryWindow": cfg.agents.defaults.memory_window,
                "reasoningEffort": cfg.agents.defaults.reasoning_effort,
            },
            "roleIterations": {
                "ceo": cfg.get_role_max_iterations("ceo"),
                "execution": cfg.get_role_max_iterations("execution"),
                "inspection": cfg.get_role_max_iterations("inspection"),
            },
            "multiAgent": {
                "orchestratorModelKey": cfg.agents.multi_agent.orchestrator_model_key,
            },
        },
        "models": {
            "catalog": catalog,
            "roles": routes,
        },
        "providers": providers,
        "web": {
            "host": cfg.web.host,
            "port": cfg.web.port,
        },
        "toolSecrets": {
            str(name): dict(payload or {})
            for name, payload in dict(cfg.tool_secrets or {}).items()
        },
        "resources": {
            "enabled": cfg.resources.enabled,
            "skillsDir": cfg.resources.skills_dir,
            "toolsDir": cfg.resources.tools_dir,
            "manifestName": cfg.resources.manifest_name,
            "reload": {
                "enabled": cfg.resources.reload.enabled,
                "pollIntervalMs": cfg.resources.reload.poll_interval_ms,
                "debounceMs": cfg.resources.reload.debounce_ms,
                "lazyReloadOnAccess": cfg.resources.reload.lazy_reload_on_access,
                "keepLastGoodVersion": cfg.resources.reload.keep_last_good_version,
            },
            "locks": {
                "lockDir": cfg.resources.locks.lock_dir,
                "logicalDeleteGuard": cfg.resources.locks.logical_delete_guard,
                "windowsFsLock": cfg.resources.locks.windows_fs_lock,
            },
            "statePath": cfg.resources.state_path,
        },
        "mainRuntime": {
            "enabled": cfg.main_runtime.enabled,
            "storePath": cfg.main_runtime.store_path,
            "filesBaseDir": cfg.main_runtime.files_base_dir,
            "artifactDir": cfg.main_runtime.artifact_dir,
            "governanceStorePath": cfg.main_runtime.governance_store_path,
            "defaultMaxDepth": cfg.main_runtime.default_max_depth,
            "hardMaxDepth": cfg.main_runtime.hard_max_depth,
        },
        "chinaBridge": {
            "enabled": cfg.china_bridge.enabled,
            "bindHost": cfg.china_bridge.bind_host,
            "publicPort": cfg.china_bridge.public_port,
            "controlHost": cfg.china_bridge.control_host,
            "controlPort": cfg.china_bridge.control_port,
            "controlToken": cfg.china_bridge.control_token,
            "autoStart": cfg.china_bridge.auto_start,
            "nodeBin": cfg.china_bridge.node_bin,
            "npmClient": cfg.china_bridge.npm_client,
            "stateDir": cfg.china_bridge.state_dir,
            "logLevel": cfg.china_bridge.log_level,
            "sendProgress": cfg.china_bridge.send_progress,
            "sendToolHints": cfg.china_bridge.send_tool_hints,
            "channels": channel_payloads,
        },
    }


def _ensure_runtime_fields_explicit(raw_data: dict[str, Any], cfg: Config) -> None:
    middlewares = ((raw_data.get("agents") or {}).get("defaults") or {}).get("middlewares")
    if middlewares is not None:
        raise ValueError(
            "agents.defaults.middlewares is not allowed in project config. "
            "Remove runtime middlewares from .g3ku/config.json; g3ku web does not use them."
        )

    payload = _runtime_config_payload(cfg)
    exempt_prefixes = {
        ("providers",),
        ("mainRuntime",),
        ("chinaBridge",),
    }
    missing = [
        ".".join(path)
        for path in _leaf_paths(payload)
        if path
        and not any(path[: len(prefix)] == prefix for prefix in exempt_prefixes)
        and not _path_exists(raw_data, path)
    ]
    if missing:
        preview = ", ".join(missing[:12])
        suffix = "" if len(missing) <= 12 else f" ... (+{len(missing) - 12} more)"
        raise ValueError(
            "Config is missing explicit runtime fields. "
            f"Populate these paths in {get_config_path()}: {preview}{suffix}"
        )


def _ensure_role_iterations_defaults(raw_data: dict[str, Any]) -> bool:
    agents = raw_data.get("agents")
    if not isinstance(agents, dict):
        return False
    current = agents.get("roleIterations")
    fallback = current if isinstance(current, dict) else agents.get("role_iterations")
    fallback_payload = fallback if isinstance(fallback, dict) else {}
    next_payload: dict[str, int] = {}
    for scope, default in DEFAULT_ROLE_MAX_ITERATIONS.items():
        value = fallback_payload.get(scope, default)
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = default
        next_payload[scope] = normalized if normalized >= 2 else default
    if current == next_payload:
        return False
    agents["roleIterations"] = next_payload
    return True


def build_project_config_from_example(example_path: Path | None = None) -> Config:
    """Build a strict project config from a project-local or bundled example file."""
    raw, source = _read_example_config_text(example_path)
    return Config.model_validate(_migrate_config(_load_json_text(raw, source)))


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from the project-local config file."""
    expected_path = get_config_path().resolve()
    path = Path(config_path).resolve() if config_path is not None else expected_path
    if path != expected_path:
        raise ValueError(f"Config must be loaded from {expected_path}, got {path}")
    raw_data = _load_json_file(expected_path)
    _ensure_no_removed_model_fields(raw_data)
    _ensure_no_removed_role_scopes(raw_data)
    _ensure_no_removed_tools_config(raw_data)
    _ensure_no_removed_channel_config(raw_data)
    _ensure_no_removed_gateway_config(raw_data)
    migrated_llm, changed = migrate_raw_config_if_needed(deepcopy(raw_data), workspace=Path.cwd())
    if changed:
        raw_data = migrated_llm
    changed = _ensure_role_iterations_defaults(raw_data) or changed
    security = get_bootstrap_security_service(Path.cwd())
    migrated = _migrate_config(
        apply_config_secret_entries(deepcopy(raw_data), security.current_overlay())
    )
    cfg = Config.model_validate(migrated)
    _ensure_runtime_fields_explicit(migrated, cfg)
    if changed or _raw_uses_inline_model_payload(raw_data):
        save_config(cfg, expected_path)
    return cfg


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Save configuration to file."""
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    _normalize_inline_model_bindings(config)
    data = _runtime_config_payload(config)
    security = get_bootstrap_security_service(Path.cwd())
    secret_entries = extract_config_secret_entries(data)
    if security.is_unlocked():
        clear_updates = {
            key: None
            for key in security.current_overlay().keys()
            if str(key or "").startswith("config.")
        }
        clear_updates.update(secret_entries)
        security.set_overlay_values(clear_updates)
    data = strip_config_secret_entries(data)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate old config formats to current."""
    data.pop("subagent", None)

    agents = data.get("agents")
    if isinstance(agents, dict):
        defaults = agents.get("defaults")
        if isinstance(defaults, dict):
            workspace = str(defaults.get("workspace", "") or "").strip()
            if workspace in {"~/.g3ku/workspace", str((Path.home() / ".g3ku" / "workspace"))}:
                defaults["workspace"] = "."
        multi_agent = agents.get("multiAgent")
        if isinstance(multi_agent, dict):
            agents.setdefault("multi_agent", multi_agent)
        multi_agent = agents.get("multi_agent")
        if isinstance(multi_agent, dict):
            for camel, snake in (
                ("entryMode", "entry_mode"),
                ("directFastPath", "direct_fast_path"),
                ("maxParallelWorkers", "max_parallel_workers"),
                ("blackboardDir", "blackboard_dir"),
                ("defaultPattern", "default_pattern"),
                ("interruptMode", "interrupt_mode"),
            ):
                if camel in multi_agent and snake not in multi_agent:
                    multi_agent[snake] = multi_agent.pop(camel)

    main_runtime = data.get("mainRuntime")
    if isinstance(main_runtime, dict) and "main_runtime" not in data:
        data["main_runtime"] = main_runtime
    data.pop("mainRuntime", None)

    china_bridge = data.get("chinaBridge")
    if isinstance(china_bridge, dict) and "china_bridge" not in data:
        data["china_bridge"] = china_bridge
    elif "china_bridge" not in data:
        data["china_bridge"] = {}
    data.pop("chinaBridge", None)

    if isinstance(data.get("china_bridge"), dict):
        bridge = data["china_bridge"]
        channels = bridge.get("channels")
        if isinstance(channels, dict):
            legacy_pairs = (
                ("wecomApp", "wecom-app"),
                ("wecom_app", "wecom-app"),
                ("wecomKf", "wecom-kf"),
                ("wecom_kf", "wecom-kf"),
                ("wechatMp", "wechat-mp"),
                ("wechat_mp", "wechat-mp"),
                ("feishuChina", "feishu-china"),
                ("feishu_china", "feishu-china"),
            )
            for old_key, new_key in legacy_pairs:
                if new_key not in channels and isinstance(channels.get(old_key), dict):
                    channels[new_key] = channels[old_key]
    return data
