from __future__ import annotations

from typing import Any

from pydantic import Field

from main.models import Model, ModelTokenUsageRecord, TokenUsageSummary
from main.types import NodeStatus, TaskStatus


class TaskSpawnRound(Model):
    round_id: str = ''
    round_index: int = 0
    label: str = ''
    is_latest: bool = False
    created_at: str = ''
    child_node_ids: list[str] = Field(default_factory=list)
    source: str = 'explicit'
    total_children: int = 0
    completed_children: int = 0
    running_children: int = 0
    failed_children: int = 0
    children: list['TaskTreeNode'] = Field(default_factory=list)


class TaskTreeNode(Model):
    node_id: str
    parent_node_id: str | None = None
    depth: int = 0
    status: NodeStatus = 'in_progress'
    title: str = ''
    input: str = ''
    input_ref: str = ''
    output: str = ''
    output_ref: str = ''
    check_result: str = ''
    check_result_ref: str = ''
    updated_at: str = ''
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_usage_by_model: list[ModelTokenUsageRecord] = Field(default_factory=list)
    spawn_rounds: list[TaskSpawnRound] = Field(default_factory=list)
    auxiliary_children: list['TaskTreeNode'] = Field(default_factory=list)
    default_round_id: str = ''
    children: list['TaskTreeNode'] = Field(default_factory=list)


class LatestTaskNodeOutput(Model):
    node_id: str
    parent_node_id: str | None = None
    depth: int = 0
    status: NodeStatus = 'in_progress'
    title: str = ''
    updated_at: str = ''
    output: str = ''
    output_ref: str = ''


class TaskLiveToolCall(Model):
    tool_call_id: str = ''
    tool_name: str = ''
    status: str = 'queued'
    started_at: str = ''
    finished_at: str = ''
    elapsed_seconds: float | None = None


class TaskLiveChildPipeline(Model):
    index: int = 0
    goal: str = ''
    status: str = 'queued'
    child_node_id: str = ''
    acceptance_node_id: str = ''
    check_status: str = ''
    started_at: str = ''
    finished_at: str = ''


class TaskLiveFrame(Model):
    node_id: str = ''
    depth: int = 0
    node_kind: str = 'execution'
    phase: str = ''
    tool_calls: list[TaskLiveToolCall] = Field(default_factory=list)
    child_pipelines: list[TaskLiveChildPipeline] = Field(default_factory=list)


class TaskLiveState(Model):
    active_node_ids: list[str] = Field(default_factory=list)
    runnable_node_ids: list[str] = Field(default_factory=list)
    waiting_node_ids: list[str] = Field(default_factory=list)
    frames: list[TaskLiveFrame] = Field(default_factory=list)


class TaskSummaryResult(Model):
    total_tasks: int = 0
    in_progress_tasks: int = 0
    failed_tasks: int = 0
    unread_tasks: int = 0
    text: str = ''


class TaskListItem(Model):
    task_id: str
    session_id: str = ''
    title: str = ''
    brief: str = ''
    status: TaskStatus = 'in_progress'
    is_unread: bool = False
    is_paused: bool = False
    created_at: str = ''
    updated_at: str = ''
    max_depth: int = 0
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)


class TaskProgressResult(Model):
    task_id: str
    task_status: TaskStatus = 'in_progress'
    tree_text: str = ''
    root: TaskTreeNode | None = None
    latest_node: LatestTaskNodeOutput | None = None
    live_state: TaskLiveState | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_usage_by_model: list[ModelTokenUsageRecord] = Field(default_factory=list)
    text: str = ''


TaskSpawnRound.model_rebuild()
TaskTreeNode.model_rebuild()

