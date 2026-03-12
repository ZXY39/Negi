"""Configuration loading utilities."""

from copy import deepcopy
import json
from pathlib import Path

from g3ku.config.schema import Config


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


def _load_json_file(path: Path) -> dict:
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


def _path_exists(data: dict, path: tuple[str, ...]) -> bool:
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return True


def _leaf_paths(value, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
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


def _ensure_no_legacy_model_fields(raw_data: dict) -> None:
    legacy_paths = (
        ("agents", "defaults", "model"),
        ("agents", "multiAgent", "orchestratorModel"),
        ("agents", "multi_agent", "orchestrator_model"),
        ("orgGraph", "ceoModel"),
        ("orgGraph", "executionModel"),
        ("orgGraph", "inspectionModel"),
        ("org_graph", "ceo_model"),
        ("org_graph", "execution_model"),
        ("org_graph", "inspection_model"),
    )
    hits = [".".join(path) for path in legacy_paths if _path_exists(raw_data, path)]
    if hits:
        preview = ", ".join(hits)
        raise ValueError(
            "当前版本只支持 models.catalog + models.roles + orchestrator_model_key。"
            f"请从 {get_config_path()} 删除旧字段: {preview}"
        )


def _referenced_provider_names(cfg: Config) -> list[str]:
    names: set[str] = set()
    values = []
    if cfg.tools.memory.enabled:
        values.append(cfg.tools.memory.embedding.provider_model)
        values.append(cfg.tools.memory.retrieval.rerank_provider_model)

    for value in values:
        provider_model = str(value or "").strip()
        if not provider_model:
            continue
        provider_name, _ = cfg.parse_provider_model(provider_model)
        names.add(provider_name)
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
        "agent": list(cfg.models.roles.agent),
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
        "tools": {
            "web": {
                "proxy": cfg.tools.web.proxy,
                "search": {
                    "apiKey": cfg.tools.web.search.api_key,
                    "maxResults": cfg.tools.web.search.max_results,
                },
            },
            "exec": {
                "timeout": cfg.tools.exec.timeout,
                "pathAppend": cfg.tools.exec.path_append,
            },
            "memory": {
                "enabled": cfg.tools.memory.enabled,
                "mode": cfg.tools.memory.mode,
                "backend": cfg.tools.memory.backend,
                "archVersion": cfg.tools.memory.arch_version,
                "features": {
                    "unifiedContext": cfg.tools.memory.features.unified_context,
                    "layeredLoading": cfg.tools.memory.features.layered_loading,
                    "queryPlanner": cfg.tools.memory.features.query_planner,
                    "commitPipeline": cfg.tools.memory.features.commit_pipeline,
                    "splitStore": cfg.tools.memory.features.split_store,
                    "observability": cfg.tools.memory.features.observability,
                },
                "checkpointer": {
                    "backend": cfg.tools.memory.checkpointer.backend,
                    "path": cfg.tools.memory.checkpointer.path,
                },
                "store": {
                    "backend": cfg.tools.memory.store.backend,
                    "qdrantPath": cfg.tools.memory.store.qdrant_path,
                    "qdrantCollection": cfg.tools.memory.store.qdrant_collection,
                    "sqlitePath": cfg.tools.memory.store.sqlite_path,
                },
                "retrieval": {
                    "denseTopK": cfg.tools.memory.retrieval.dense_top_k,
                    "sparseTopK": cfg.tools.memory.retrieval.sparse_top_k,
                    "fusedTopK": cfg.tools.memory.retrieval.fused_top_k,
                    "contextTopK": cfg.tools.memory.retrieval.context_top_k,
                    "sentenceWindow": cfg.tools.memory.retrieval.sentence_window,
                    "maxContextTokens": cfg.tools.memory.retrieval.max_context_tokens,
                    "defaultLoadLevel": cfg.tools.memory.retrieval.default_load_level,
                    "rerankProviderModel": cfg.tools.memory.retrieval.rerank_provider_model,
                },
                "embedding": {
                    "providerModel": cfg.tools.memory.embedding.provider_model,
                    "batchSize": cfg.tools.memory.embedding.batch_size,
                },
                "isolation": {
                    "mode": cfg.tools.memory.isolation.mode,
                    "namespaceTemplate": cfg.tools.memory.isolation.namespace_template,
                },
                "guard": {
                    "mode": cfg.tools.memory.guard.mode,
                    "autoFactConfidence": cfg.tools.memory.guard.auto_fact_confidence,
                },
                "compat": {
                    "dualWriteLegacyFiles": cfg.tools.memory.compat.dual_write_legacy_files,
                },
                "commit": {
                    "turnTrigger": cfg.tools.memory.commit.turn_trigger,
                    "idleMinutesTrigger": cfg.tools.memory.commit.idle_minutes_trigger,
                },
                "cost": {
                    "maxIncreasePct": cfg.tools.memory.cost.max_increase_pct,
                },
                "bootstrapMode": cfg.tools.memory.bootstrap_mode,
                "retentionDays": cfg.tools.memory.retention_days,
            },
            "agentBrowser": {
                "enabled": cfg.tools.agent_browser.enabled,
                "command": cfg.tools.agent_browser.command,
                "npmCommand": cfg.tools.agent_browser.npm_command,
                "nodeCommand": cfg.tools.agent_browser.node_command,
                "requiredMinVersion": cfg.tools.agent_browser.required_min_version,
                "installSpec": cfg.tools.agent_browser.install_spec,
                "autoInstall": cfg.tools.agent_browser.auto_install,
                "autoUpgradeIfBelowMinVersion": cfg.tools.agent_browser.auto_upgrade_if_below_min_version,
                "autoInstallBrowser": cfg.tools.agent_browser.auto_install_browser,
                "browserInstallArgs": cfg.tools.agent_browser.browser_install_args,
                "defaultHeadless": cfg.tools.agent_browser.default_headless,
                "commandTimeoutS": cfg.tools.agent_browser.command_timeout_s,
                "installTimeoutS": cfg.tools.agent_browser.install_timeout_s,
                "sessionEnvKey": cfg.tools.agent_browser.session_env_key,
                "maxStdoutChars": cfg.tools.agent_browser.max_stdout_chars,
                "maxStderrChars": cfg.tools.agent_browser.max_stderr_chars,
                "extraEnv": cfg.tools.agent_browser.extra_env,
                "allowFileAccess": cfg.tools.agent_browser.allow_file_access,
                "defaultColorScheme": cfg.tools.agent_browser.default_color_scheme,
                "defaultDownloadPath": cfg.tools.agent_browser.default_download_path,
            },
            "fileVault": {
                "enabled": cfg.tools.file_vault.enabled,
                "rootDir": cfg.tools.file_vault.root_dir,
                "indexDbPath": cfg.tools.file_vault.index_db_path,
                "maxStorageBytes": cfg.tools.file_vault.max_storage_bytes,
                "thresholdPct": cfg.tools.file_vault.threshold_pct,
                "cleanupTargetPct": cfg.tools.file_vault.cleanup_target_pct,
                "recentProtectHours": cfg.tools.file_vault.recent_protect_hours,
            },
            "pictureWashing": {
                "baseUrl": cfg.tools.picture_washing.base_url,
                "authorization": cfg.tools.picture_washing.authorization,
                "style": cfg.tools.picture_washing.style,
                "model": cfg.tools.picture_washing.model,
                "stream": cfg.tools.picture_washing.stream,
                "timeoutS": cfg.tools.picture_washing.timeout_s,
                "autoProbeAuthorization": cfg.tools.picture_washing.auto_probe_authorization,
                "authorizationProbeUrl": cfg.tools.picture_washing.authorization_probe_url,
                "authorizationProbeTimeoutS": cfg.tools.picture_washing.authorization_probe_timeout_s,
                "authorizationCookieNames": cfg.tools.picture_washing.authorization_cookie_names,
            },
            "mcpServers": {
                name: server.model_dump(by_alias=True)
                for name, server in cfg.tools.mcp_servers.items()
            },
            "restrictToWorkspace": cfg.tools.restrict_to_workspace,
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
        "orgGraph": {
            "enabled": cfg.org_graph.enabled,
            "projectStorePath": cfg.org_graph.project_store_path,
            "checkpointStorePath": cfg.org_graph.checkpoint_store_path,
            "taskMonitorStorePath": cfg.org_graph.task_monitor_store_path,
            "artifactDir": cfg.org_graph.artifact_dir,
            "defaultMaxDepth": cfg.org_graph.default_max_depth,
            "hardMaxDepth": cfg.org_graph.hard_max_depth,
            "maxParallelUnitsTotal": cfg.org_graph.max_parallel_units_total,
            "maxActiveProjectsPerSession": cfg.org_graph.max_active_projects_per_session,
            "projectNoticeRetention": cfg.org_graph.project_notice_retention,
            "eventReplayLimit": cfg.org_graph.event_replay_limit,
            "governance": {
                "enabled": cfg.org_graph.governance.enabled,
                "governanceStorePath": cfg.org_graph.governance.governance_store_path,
            },
        },
    }


def _ensure_runtime_fields_explicit(raw_data: dict, cfg: Config) -> None:
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
    """
    Load configuration from the project-local config file.

    Args:
        config_path: Optional path to config file. Must point to the project-local file.

    Returns:
        Loaded configuration object.
    """
    expected_path = get_config_path().resolve()
    path = Path(config_path).resolve() if config_path is not None else expected_path
    if path != expected_path:
        raise ValueError(f"Config must be loaded from {expected_path}, got {path}")
    raw_data = _load_json_file(expected_path)
    _ensure_no_legacy_model_fields(raw_data)
    org_graph_raw = raw_data.get('orgGraph') if isinstance(raw_data.get('orgGraph'), dict) else raw_data.get('org_graph')
    if isinstance(org_graph_raw, dict):
        if 'taskMonitorStorePath' not in org_graph_raw and 'task_monitor_store_path' not in org_graph_raw:
            org_graph_raw['taskMonitorStorePath'] = '.g3ku/org-graph/task-monitor.sqlite3'
    migrated = _migrate_config(deepcopy(raw_data))
    cfg = Config.model_validate(migrated)
    _ensure_runtime_fields_explicit(raw_data, cfg)
    return cfg


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = _runtime_config_payload(config)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _migrate_config(data: dict) -> dict:
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

    org_graph = data.get("orgGraph")
    if isinstance(org_graph, dict) and "org_graph" not in data:
        data["org_graph"] = org_graph
    data.pop("orgGraph", None)
    org_graph = data.get("org_graph")
    if isinstance(org_graph, dict):
        if "maxParallelUnitsPerStage" in org_graph and "maxParallelUnitsTotal" not in org_graph:
            org_graph["maxParallelUnitsTotal"] = org_graph.pop("maxParallelUnitsPerStage")
        if "max_parallel_units_per_stage" in org_graph and "max_parallel_units_total" not in org_graph:
            org_graph["max_parallel_units_total"] = org_graph.pop("max_parallel_units_per_stage")
    tools = data.get("tools", {})
    if isinstance(tools, dict):
        tools.pop("deep_mode", None)
        tools.pop("deepMode", None)

    # Move tools.exec.restrictToWorkspace -> tools.restrictToWorkspace
    exec_cfg = tools.get("exec", {})
    if "restrictToWorkspace" in exec_cfg and "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # Normalize tools.pictureWashing key variants for picture_washing tool defaults
    pw = tools.get("pictureWashing")
    if isinstance(pw, dict):
        if "baseurl" in pw and "baseUrl" not in pw and "base_url" not in pw:
            pw["baseUrl"] = pw.pop("baseurl")
        if "apikey" in pw and "authorization" not in pw:
            pw["authorization"] = pw.pop("apikey")

    # Migrate tools.browserProbe -> tools.agentBrowser
    agent_browser = tools.get("agentBrowser")
    legacy_browser = tools.get("browserProbe")
    if isinstance(legacy_browser, dict):
        if not isinstance(agent_browser, dict):
            agent_browser = {}
            tools["agentBrowser"] = agent_browser
        mapping = {
            "headless": "defaultHeadless",
            "timeoutS": "commandTimeoutS",
            "autoInstall": "autoInstall",
            "autoInstallBrowser": "autoInstallBrowser",
            "installTimeoutS": "installTimeoutS",
        }
        for old_key, new_key in mapping.items():
            if old_key in legacy_browser and new_key not in agent_browser:
                agent_browser[new_key] = legacy_browser.get(old_key)
        if "command" not in agent_browser:
            agent_browser["command"] = "agent-browser"
        if "npmCommand" not in agent_browser:
            agent_browser["npmCommand"] = "npm"
        if "nodeCommand" not in agent_browser:
            agent_browser["nodeCommand"] = "node"
        if "requiredMinVersion" not in agent_browser:
            agent_browser["requiredMinVersion"] = "0.16.3"
        if "installSpec" not in agent_browser:
            agent_browser["installSpec"] = "agent-browser@latest"
        tools.pop("browserProbe", None)

    # Normalize tools.memory key variants.
    mem = tools.get("memory")
    if isinstance(mem, dict):
        store = mem.get("store")
        if isinstance(store, dict):
            if "qdrantPath" in store and "qdrant_path" not in store:
                store["qdrant_path"] = store.pop("qdrantPath")
            if "qdrantCollection" in store and "qdrant_collection" not in store:
                store["qdrant_collection"] = store.pop("qdrantCollection")
            if "sqlitePath" in store and "sqlite_path" not in store:
                store["sqlite_path"] = store.pop("sqlitePath")

        retrieval = mem.get("retrieval")
        if isinstance(retrieval, dict):
            for camel, snake in (
                ("denseTopK", "dense_top_k"),
                ("sparseTopK", "sparse_top_k"),
                ("fusedTopK", "fused_top_k"),
                ("contextTopK", "context_top_k"),
                ("sentenceWindow", "sentence_window"),
                ("maxContextTokens", "max_context_tokens"),
                ("defaultLoadLevel", "default_load_level"),
                ("rerankProviderModel", "rerank_provider_model"),
            ):
                if camel in retrieval and snake not in retrieval:
                    retrieval[snake] = retrieval.pop(camel)

        embedding = mem.get("embedding")
        if isinstance(embedding, dict):
            if "providerModel" in embedding and "provider_model" not in embedding:
                embedding["provider_model"] = embedding.pop("providerModel")
            if "batchSize" in embedding and "batch_size" not in embedding:
                embedding["batch_size"] = embedding.pop("batchSize")

        isolation = mem.get("isolation")
        if isinstance(isolation, dict):
            if "namespaceTemplate" in isolation and "namespace_template" not in isolation:
                isolation["namespace_template"] = isolation.pop("namespaceTemplate")

        guard = mem.get("guard")
        if isinstance(guard, dict):
            if "autoFactConfidence" in guard and "auto_fact_confidence" not in guard:
                guard["auto_fact_confidence"] = guard.pop("autoFactConfidence")

        compat = mem.get("compat")
        if isinstance(compat, dict):
            if "dualWriteLegacyFiles" in compat and "dual_write_legacy_files" not in compat:
                compat["dual_write_legacy_files"] = compat.pop("dualWriteLegacyFiles")

        features = mem.get("features")
        if isinstance(features, dict):
            for camel, snake in (
                ("unifiedContext", "unified_context"),
                ("layeredLoading", "layered_loading"),
                ("queryPlanner", "query_planner"),
                ("commitPipeline", "commit_pipeline"),
                ("splitStore", "split_store"),
                ("observability", "observability"),
            ):
                if camel in features and snake not in features:
                    features[snake] = features.pop(camel)

        commit = mem.get("commit")
        if isinstance(commit, dict):
            if "turnTrigger" in commit and "turn_trigger" not in commit:
                commit["turn_trigger"] = commit.pop("turnTrigger")
            if "idleMinutesTrigger" in commit and "idle_minutes_trigger" not in commit:
                commit["idle_minutes_trigger"] = commit.pop("idleMinutesTrigger")

        cost = mem.get("cost")
        if isinstance(cost, dict):
            if "maxIncreasePct" in cost and "max_increase_pct" not in cost:
                cost["max_increase_pct"] = cost.pop("maxIncreasePct")

        checkpointer = mem.get("checkpointer")
        if isinstance(checkpointer, dict):
            if "retentionDays" in checkpointer and "retention_days" not in checkpointer:
                checkpointer["retention_days"] = checkpointer.pop("retentionDays")

        if "archVersion" in mem and "arch_version" not in mem:
            mem["arch_version"] = mem.pop("archVersion")
        if "bootstrapMode" in mem and "bootstrap_mode" not in mem:
            mem["bootstrap_mode"] = mem.pop("bootstrapMode")
        if "retentionDays" in mem and "retention_days" not in mem:
            mem["retention_days"] = mem.pop("retentionDays")

        # Migrate old home-scoped memory defaults to workspace-relative defaults.
        checkpointer = mem.get("checkpointer")
        if isinstance(checkpointer, dict) and checkpointer.get("path") == "~/.g3ku/memory/checkpoints.sqlite3":
            checkpointer["path"] = "memory/checkpoints.sqlite3"

        store = mem.get("store")
        if isinstance(store, dict):
            if store.get("sqlite_path") == "~/.g3ku/memory/memory.db":
                store["sqlite_path"] = "memory/memory.db"
            if store.get("qdrant_path") == "~/.g3ku/memory/qdrant":
                store["qdrant_path"] = "memory/qdrant"
    return data
