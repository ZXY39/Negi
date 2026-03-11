"""Configuration loading utilities."""

import json
import os
from pathlib import Path

from g3ku.config.schema import Config
from g3ku.utils.helpers import resolve_path_in_workspace


def _project_config_path() -> Path:
    return Path.cwd() / ".g3ku" / "config.json"


def _legacy_config_path() -> Path:
    return Path.home() / ".g3ku" / "config.json"


def get_config_path() -> Path:
    """Get configuration file path.

    Always resolves under current workspace (project directory).
    If `G3KU_CONFIG_PATH` is set, it is force-rebased into workspace.
    """
    env_path = os.environ.get("G3KU_CONFIG_PATH", "").strip()
    if env_path:
        return resolve_path_in_workspace(env_path, Path.cwd())
    return _project_config_path()


def get_data_dir() -> Path:
    """Get the g3ku data directory."""
    from g3ku.utils.helpers import get_data_path

    return get_data_path()


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    path = config_path or get_config_path()

    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            return Config.model_validate(data)
        except json.JSONDecodeError as e:
            print(f"Warning: Failed to parse JSON config from {path}: {e}")
            print("Using default configuration.")
            return Config()
        except ValueError as e:
            raise ValueError(f"Invalid config at {path}: {e}") from e

    # One-time migration from legacy home config to workspace-scoped config path.
    legacy_path = _legacy_config_path()
    if config_path is None and path == _project_config_path() and legacy_path.exists():
        try:
            with open(legacy_path, encoding="utf-8") as f:
                data = json.load(f)
            data = _migrate_config(data)
            cfg = Config.model_validate(data)
            save_config(cfg, path)
            return cfg
        except Exception:
            print(f"Warning: Failed to migrate legacy config from {legacy_path}")

    return Config()


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(by_alias=True)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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


