from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from g3ku.config.loader import load_config
from g3ku.config.schema import Config


@dataclass(slots=True)
class ResolvedOrgGraphConfig:
    raw: Config
    project_store_path: Path
    checkpoint_store_path: Path
    task_monitor_store_path: Path
    governance_store_path: Path
    artifact_dir: Path
    ceo_model: str
    ceo_model_chain: list[str]
    execution_model: str
    execution_model_chain: list[str]
    inspection_model: str
    inspection_model_chain: list[str]
    default_max_depth: int
    hard_max_depth: int
    max_parallel_units_total: int
    max_active_projects_per_session: int
    project_notice_retention: int
    event_replay_limit: int
    governance_enabled: bool
    auto_reload_on_write: bool
    default_risk_level_for_legacy_skill: str
    resource_reload_cache_ttl_s: int



def _resolve_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path



def resolve_org_graph_config(config: Config | None = None) -> ResolvedOrgGraphConfig:
    cfg = config or load_config()
    org = cfg.org_graph
    governance_raw = org.governance.governance_store_path
    if governance_raw == '.g3ku/org-graph/governance.sqlite3' and org.project_store_path != '.g3ku/org-graph/projects.sqlite3':
        governance_raw = str(Path(org.project_store_path).expanduser().with_name('governance.sqlite3'))
    project_store_path = _resolve_path(org.project_store_path)
    checkpoint_store_path = _resolve_path(org.checkpoint_store_path)
    task_monitor_store_path = _resolve_path(getattr(org, 'task_monitor_store_path', '.g3ku/org-graph/task-monitor.sqlite3'))
    governance_store_path = _resolve_path(governance_raw)
    artifact_dir = _resolve_path(org.artifact_dir)
    project_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_store_path.parent.mkdir(parents=True, exist_ok=True)
    task_monitor_store_path.parent.mkdir(parents=True, exist_ok=True)
    governance_store_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    fallback_model = cfg.agents.defaults.model
    ceo_model_chain = cfg.get_scope_model_refs("ceo")
    execution_model_chain = cfg.get_scope_model_refs("execution")
    inspection_model_chain = cfg.get_scope_model_refs("inspection")
    return ResolvedOrgGraphConfig(
        raw=cfg,
        project_store_path=project_store_path,
        checkpoint_store_path=checkpoint_store_path,
        task_monitor_store_path=task_monitor_store_path,
        governance_store_path=governance_store_path,
        artifact_dir=artifact_dir,
        ceo_model=(ceo_model_chain[0] if ceo_model_chain else (org.ceo_model or fallback_model)),
        ceo_model_chain=ceo_model_chain,
        execution_model=(execution_model_chain[0] if execution_model_chain else (org.execution_model or fallback_model)),
        execution_model_chain=execution_model_chain,
        inspection_model=(inspection_model_chain[0] if inspection_model_chain else (org.inspection_model or org.execution_model or fallback_model)),
        inspection_model_chain=inspection_model_chain,
        default_max_depth=org.default_max_depth,
        hard_max_depth=org.hard_max_depth,
        max_parallel_units_total=org.max_parallel_units_total,
        max_active_projects_per_session=org.max_active_projects_per_session,
        project_notice_retention=org.project_notice_retention,
        event_replay_limit=org.event_replay_limit,
        governance_enabled=org.governance.enabled,
        auto_reload_on_write=org.governance.auto_reload_on_write,
        default_risk_level_for_legacy_skill=org.governance.default_risk_level_for_legacy_skill,
        resource_reload_cache_ttl_s=org.governance.resource_reload_cache_ttl_s,
    )

