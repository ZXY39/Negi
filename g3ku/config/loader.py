"""Configuration loading utilities."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from g3ku.config.schema import Config
from g3ku.resources.tool_settings import MemoryRuntimeSettings, load_tool_settings_from_manifest


def _project_config_path() -> Path:
    return Path.cwd() / ".g3ku" / "config.json"


def _project_example_config_path() -> Path:
    return Path.cwd() / ".g3ku" / "config - example.json"


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


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Required config file not found: {path}. Create {get_config_path()} before starting g3ku."
        ) from exc

    if not raw.strip():
        raise ValueError(
            f"Required config file is empty: {path}. Populate {get_config_path()} before starting g3ku."
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON config from {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a JSON object: {path}")
    return data


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


def _ensure_no_legacy_model_fields(raw_data: dict[str, Any]) -> None:
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


def _ensure_no_legacy_tools_config(raw_data: dict[str, Any]) -> None:
    if "tools" not in raw_data:
        return
    raise ValueError(
        "config.tools has been removed. Move non-secret tool settings into tools/*/resource.yaml settings, "
        "and move secrets into top-level toolSecrets in .g3ku/config.json."
    )


def _referenced_provider_names(cfg: Config) -> list[str]:
    names: set[str] = set()

    for item in list(cfg.models.catalog or []):
        provider_model = str(getattr(item, "provider_model", "") or "").strip()
        if not provider_model:
            continue
        provider_name, _ = cfg.parse_provider_model(provider_model)
        names.add(provider_name)

    try:
        memory_cfg = load_tool_settings_from_manifest(cfg.workspace_path, "memory_runtime", MemoryRuntimeSettings)
    except Exception:
        memory_cfg = None

    if memory_cfg is not None and memory_cfg.enabled:
        for value in (memory_cfg.embedding.provider_model, memory_cfg.retrieval.rerank_provider_model):
            provider_model = str(value or "").strip()
            if not provider_model:
                continue
            provider_name, _ = cfg.parse_provider_model(provider_model)
            names.add(provider_name)

    for provider_name, payload in cfg.providers.model_dump().items():
        if isinstance(payload, dict) and any(value not in (None, "", {}, []) for value in payload.values()):
            names.add(str(provider_name))

    return sorted(names)


def _managed_models_payload(cfg: Config) -> tuple[list[dict[str, object]], dict[str, list[str]]]:
    catalog = [
        {
            "key": item.key,
            "providerModel": item.provider_model,
            "apiKey": item.api_key,
            "apiBase": item.api_base,
            "extraHeaders": item.extra_headers,
            "enabled": item.enabled,
            "maxTokens": item.max_tokens,
            "temperature": item.temperature,
            "reasoningEffort": item.reasoning_effort,
            "retryOn": list(item.retry_on or []),
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
            "multiAgent": {
                "orchestratorModelKey": cfg.agents.multi_agent.orchestrator_model_key,
            },
        },
        "channels": {
            "sendProgress": cfg.channels.send_progress,
            "sendToolHints": cfg.channels.send_tool_hints,
        },
        "models": {
            "catalog": catalog,
            "roles": routes,
        },
        "providers": providers,
        "gateway": {
            "host": cfg.gateway.host,
            "port": cfg.gateway.port,
            "heartbeat": {
                "enabled": cfg.gateway.heartbeat.enabled,
                "intervalS": cfg.gateway.heartbeat.interval_s,
            },
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
    }


def _ensure_runtime_fields_explicit(raw_data: dict[str, Any], cfg: Config) -> None:
    middlewares = ((raw_data.get("agents") or {}).get("defaults") or {}).get("middlewares")
    if middlewares is not None:
        raise ValueError(
            "agents.defaults.middlewares is not allowed in project config. "
            "Remove runtime middlewares from .g3ku/config.json; g3ku start does not use them."
        )

    payload = _runtime_config_payload(cfg)
    missing = [".".join(path) for path in _leaf_paths(payload) if path and not _path_exists(raw_data, path)]
    if missing:
        preview = ", ".join(missing[:12])
        suffix = "" if len(missing) <= 12 else f" ... (+{len(missing) - 12} more)"
        raise ValueError(
            "Config is missing explicit runtime fields. "
            f"Populate these paths in {get_config_path()}: {preview}{suffix}"
        )


def build_project_config_from_example(example_path: Path | None = None) -> Config:
    """Build a strict project config from the checked-in example file."""
    path = example_path or get_example_config_path()
    return Config.model_validate(_migrate_config(_load_json_file(path)))


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from the project-local config file."""
    expected_path = get_config_path().resolve()
    path = Path(config_path).resolve() if config_path is not None else expected_path
    if path != expected_path:
        raise ValueError(f"Config must be loaded from {expected_path}, got {path}")
    raw_data = _load_json_file(expected_path)
    _ensure_no_legacy_model_fields(raw_data)
    _ensure_no_removed_role_scopes(raw_data)
    _ensure_no_legacy_tools_config(raw_data)
    migrated = _migrate_config(deepcopy(raw_data))
    cfg = Config.model_validate(migrated)
    _ensure_runtime_fields_explicit(raw_data, cfg)
    return cfg


def save_config(config: Config, config_path: Path | None = None) -> None:
    """Save configuration to file."""
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = _runtime_config_payload(config)

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
    return data
