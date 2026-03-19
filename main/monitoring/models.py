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
    children: list['TaskTreeNode | TaskTreeNodeSummary'] = Field(default_factory=list)


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


class TaskTreeNodeSummary(Model):
    node_id: str
    parent_node_id: str | None = None
    depth: int = 0
    node_kind: str = 'execution'
    status: NodeStatus = 'in_progress'
    title: str = ''
    updated_at: str = ''
    spawn_rounds: list[TaskSpawnRound] = Field(default_factory=list)
    default_round_id: str = ''
    children: list['TaskTreeNodeSummary'] = Field(default_factory=list)


class TaskRuntimeSummary(Model):
    active_node_ids: list[str] = Field(default_factory=list)
    runnable_node_ids: list[str] = Field(default_factory=list)
    waiting_node_ids: list[str] = Field(default_factory=list)
    frames: list[TaskLiveFrame] = Field(default_factory=list)


class TaskNodeDetail(Model):
    node_id: str
    task_id: str
    parent_node_id: str | None = None
    depth: int = 0
    node_kind: str = 'execution'
    status: NodeStatus = 'in_progress'
    goal: str = ''
    prompt: str = ''
    input: str = ''
    input_ref: str = ''
    output: str = ''
    output_ref: str = ''
    check_result: str = ''
    check_result_ref: str = ''
    final_output: str = ''
    final_output_ref: str = ''
    failure_reason: str = ''
    updated_at: str = ''
    execution_trace: dict[str, Any] = Field(default_factory=dict)
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_usage_by_model: list[ModelTokenUsageRecord] = Field(default_factory=list)


class TaskEventEnvelope(Model):
    seq: int = 0
    task_id: str = ''
    session_id: str = ''
    type: str = ''
    data: dict[str, Any] = Field(default_factory=dict)


class TaskProjectionMetaRecord(Model):
    task_id: str
    version: int = 1
    updated_at: str = ''


class TaskProjectionNodeRecord(Model):
    node_id: str
    task_id: str
    parent_node_id: str | None = None
    root_node_id: str = ''
    depth: int = 0
    node_kind: str = 'execution'
    status: NodeStatus = 'in_progress'
    title: str = ''
    updated_at: str = ''
    default_round_id: str = ''
    selected_round_id: str = ''
    round_options_count: int = 0
    sort_key: str = ''
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskProjectionNodeDetailRecord(Model):
    node_id: str
    task_id: str
    updated_at: str = ''
    input_text: str = ''
    input_ref: str = ''
    output_text: str = ''
    output_ref: str = ''
    check_result: str = ''
    check_result_ref: str = ''
    final_output: str = ''
    final_output_ref: str = ''
    failure_reason: str = ''
    prompt_summary: str = ''
    execution_trace_ref: str = ''
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskProjectionRuntimeFrameRecord(Model):
    task_id: str
    node_id: str
    depth: int = 0
    node_kind: str = 'execution'
    phase: str = ''
    active: bool = False
    runnable: bool = False
    waiting: bool = False
    updated_at: str = ''
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskProjectionRoundRecord(Model):
    task_id: str
    parent_node_id: str
    round_id: str
    round_index: int = 0
    label: str = ''
    is_latest: bool = False
    created_at: str = ''
    source: str = 'explicit'
    total_children: int = 0
    completed_children: int = 0
    running_children: int = 0
    failed_children: int = 0
    child_node_ids: list[str] = Field(default_factory=list)


class TaskProgressResult(Model):
    task_id: str
    task_status: TaskStatus = 'in_progress'
    tree_text: str = ''
    root: TaskTreeNode | TaskTreeNodeSummary | None = None
    latest_node: LatestTaskNodeOutput | None = None
    live_state: TaskLiveState | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_usage_by_model: list[ModelTokenUsageRecord] = Field(default_factory=list)
    text: str = ''


TaskSpawnRound.model_rebuild()
TaskTreeNode.model_rebuild()
TaskTreeNodeSummary.model_rebuild()

