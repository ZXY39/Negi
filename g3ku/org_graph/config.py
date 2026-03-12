from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

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



def _resolve_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _ensure_runtime_schema_cutover(*, project_store_path: Path, checkpoint_store_path: Path, task_monitor_store_path: Path, artifact_dir: Path) -> None:
    sentinel = project_store_path.parent / "runtime-schema-version"
    try:
        current = sentinel.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        current = ""
    if current == "2":
        return

    for target in (project_store_path, checkpoint_store_path, task_monitor_store_path):
        for candidate in (target, target.with_name(f"{target.name}-wal"), target.with_name(f"{target.name}-shm")):
            try:
                if candidate.exists():
                    candidate.unlink()
            except FileNotFoundError:
                pass
    if artifact_dir.exists():
        shutil.rmtree(artifact_dir, ignore_errors=True)

    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("2", encoding="utf-8")



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
    _ensure_runtime_schema_cutover(
        project_store_path=project_store_path,
        checkpoint_store_path=checkpoint_store_path,
        task_monitor_store_path=task_monitor_store_path,
        artifact_dir=artifact_dir,
    )
    project_store_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_store_path.parent.mkdir(parents=True, exist_ok=True)
    task_monitor_store_path.parent.mkdir(parents=True, exist_ok=True)
    governance_store_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ceo_model_chain = cfg.get_role_model_keys("ceo")
    execution_model_chain = cfg.get_role_model_keys("execution")
    inspection_model_chain = cfg.get_role_model_keys("inspection")
    return ResolvedOrgGraphConfig(
        raw=cfg,
        project_store_path=project_store_path,
        checkpoint_store_path=checkpoint_store_path,
        task_monitor_store_path=task_monitor_store_path,
        governance_store_path=governance_store_path,
        artifact_dir=artifact_dir,
        ceo_model=cfg.resolve_role_model_key("ceo"),
        ceo_model_chain=ceo_model_chain,
        execution_model=cfg.resolve_role_model_key("execution"),
        execution_model_chain=execution_model_chain,
        inspection_model=cfg.resolve_role_model_key("inspection"),
        inspection_model_chain=inspection_model_chain,
        default_max_depth=org.default_max_depth,
        hard_max_depth=org.hard_max_depth,
        max_parallel_units_total=org.max_parallel_units_total,
        max_active_projects_per_session=org.max_active_projects_per_session,
        project_notice_retention=org.project_notice_retention,
        event_replay_limit=org.event_replay_limit,
        governance_enabled=org.governance.enabled,
    )

