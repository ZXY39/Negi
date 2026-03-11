from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from g3ku.org_graph.models import Model


MonitorNodeState = Literal['success', 'failed', 'in_progress', 'waiting']
MonitorTaskState = Literal['success', 'failed', 'in_progress', 'paused']


class TaskMonitorNodeRecord(Model):
    node_id: str
    project_id: str
    session_id: str
    parent_node_id: str | None = None
    root_node_id: str
    created_at: str = ''
    state: MonitorNodeState = 'in_progress'
    wait_reason: str | None = None
    input: str = ''
    output: str = ''
    log: list[dict[str, Any]] = Field(default_factory=list)
    check: str = ''
    latest_progress_text: str = ''
    latest_engineering_exception: str = ''
    updated_at: str


class TaskMonitorProjectRecord(Model):
    project_id: str
    session_id: str
    root_node_id: str
    task_state: MonitorTaskState = 'in_progress'
    latest_progress_rev: int = 0
    read_progress_rev: int = 0
    latest_progress_text: str = ''
    latest_engineering_rev: int = 0
    read_engineering_rev: int = 0
    latest_engineering_text: str = ''
    has_active_engineering_exception: bool = False
    updated_at: str


class MonitorTreeNode(Model):
    node_id: str
    parent_node_id: str | None = None
    state: MonitorNodeState = 'in_progress'
    wait_reason: str | None = None
    input: str = ''
    output: str = ''
    check: str = ''
    log: list[dict[str, Any]] = Field(default_factory=list)
    children: list['MonitorTreeNode'] = Field(default_factory=list)
    updated_at: str = ''
