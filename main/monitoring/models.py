from __future__ import annotations

from typing import Any

from pydantic import Field

from main.models import Model, ModelTokenUsageRecord, NodeToolFileChange, TokenUsageSummary
from main.types import NodeStatus, TaskStatus


class TaskTreeSnapshotRound(Model):
    round_id: str = ''
    label: str = ''
    is_latest: bool = False
    total_children: int = 0
    completed_children: int = 0
    running_children: int = 0
    failed_children: int = 0
    child_ids: list[str] = Field(default_factory=list)


class TaskTreeSnapshotNode(Model):
    node_id: str
    parent_node_id: str | None = None
    node_kind: str = 'execution'
    status: NodeStatus = 'in_progress'
    title: str = ''
    updated_at: str = ''
    children_fingerprint: str = ''
    default_round_id: str = ''
    rounds: list[TaskTreeSnapshotRound] = Field(default_factory=list)
    auxiliary_child_ids: list[str] = Field(default_factory=list)
    pending_notice_count: int = 0


class TaskTreeSnapshot(Model):
    task_id: str
    root_node_id: str = ''
    generated_at: str = ''
    snapshot_version: str = ''
    nodes_by_id: dict[str, TaskTreeSnapshotNode] = Field(default_factory=dict)


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


class TaskDispatchCounters(Model):
    execution: int = 0
    inspection: int = 0


class TaskLiveFrame(Model):
    node_id: str = ''
    depth: int = 0
    node_kind: str = 'execution'
    phase: str = ''
    stage_mode: str = ''
    stage_status: str = ''
    stage_goal: str = ''
    stage_total_steps: int = 0
    tool_calls: list[TaskLiveToolCall] = Field(default_factory=list)
    child_pipelines: list[TaskLiveChildPipeline] = Field(default_factory=list)


class TaskDistributionState(Model):
    active_epoch_id: str = ''
    state: str = ''
    frontier_node_ids: list[str] = Field(default_factory=list)
    queued_epoch_count: int = 0
    pending_mailbox_count: int = 0


class TaskLiveState(Model):
    active_node_ids: list[str] = Field(default_factory=list)
    runnable_node_ids: list[str] = Field(default_factory=list)
    waiting_node_ids: list[str] = Field(default_factory=list)
    dispatch_limits: TaskDispatchCounters = Field(default_factory=TaskDispatchCounters)
    dispatch_running: TaskDispatchCounters = Field(default_factory=TaskDispatchCounters)
    dispatch_queued: TaskDispatchCounters = Field(default_factory=TaskDispatchCounters)
    frames: list[TaskLiveFrame] = Field(default_factory=list)
    distribution: TaskDistributionState = Field(default_factory=TaskDistributionState)


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
    failure_class: str = ''
    final_acceptance: dict[str, Any] = Field(default_factory=dict)
    retry_count: int = 0
    recovery_notice: str = ''
    is_unread: bool = False
    is_paused: bool = False
    created_at: str = ''
    updated_at: str = ''
    max_depth: int = 0
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)


class TaskRuntimeSummary(Model):
    active_node_ids: list[str] = Field(default_factory=list)
    runnable_node_ids: list[str] = Field(default_factory=list)
    waiting_node_ids: list[str] = Field(default_factory=list)
    dispatch_limits: TaskDispatchCounters = Field(default_factory=TaskDispatchCounters)
    dispatch_running: TaskDispatchCounters = Field(default_factory=TaskDispatchCounters)
    dispatch_queued: TaskDispatchCounters = Field(default_factory=TaskDispatchCounters)
    frames: list[TaskLiveFrame] = Field(default_factory=list)
    distribution: TaskDistributionState = Field(default_factory=TaskDistributionState)


class TaskNodeDetail(Model):
    node_id: str
    task_id: str
    parent_node_id: str | None = None
    depth: int = 0
    node_kind: str = 'execution'
    status: NodeStatus = 'in_progress'
    goal: str = ''
    detail_level: str = 'summary'
    prompt: str = ''
    prompt_summary: str = ''
    input: str = ''
    input_preview: str = ''
    input_ref: str = ''
    actual_request_ref: str = ''
    prompt_cache_key_hash: str = ''
    actual_request_hash: str = ''
    actual_request_message_count: int = 0
    actual_tool_schema_hash: str = ''
    output: str = ''
    output_preview: str = ''
    output_ref: str = ''
    check_result: str = ''
    check_result_preview: str = ''
    check_result_ref: str = ''
    final_output: str = ''
    final_output_preview: str = ''
    final_output_ref: str = ''
    failure_reason: str = ''
    updated_at: str = ''
    children_fingerprint: str = ''
    execution_trace: dict[str, Any] = Field(default_factory=dict)
    execution_trace_summary: dict[str, Any] = Field(default_factory=dict)
    execution_trace_ref: str = ''
    latest_spawn_round_id: str = ''
    append_notice_messages: list[dict[str, Any]] = Field(default_factory=list)
    message_list: list[dict[str, Any]] = Field(default_factory=list)
    direct_child_results: list[dict[str, Any]] = Field(default_factory=list)
    spawn_review_rounds: list[dict[str, Any]] = Field(default_factory=list)
    artifacts_preview: list[dict[str, Any]] = Field(default_factory=list)
    artifact_count: int = 0
    tool_file_changes: list[NodeToolFileChange] = Field(default_factory=list)
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
    children_fingerprint: str = ''
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


class TaskProjectionToolResultRecord(Model):
    task_id: str
    node_id: str
    tool_call_id: str
    order_index: int = 0
    tool_name: str = ''
    arguments_text: str = ''
    status: str = 'queued'
    started_at: str = ''
    finished_at: str = ''
    elapsed_seconds: float | None = None
    output_preview_text: str = ''
    output_ref: str = ''
    ephemeral: bool = False
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskModelCallRecord(Model):
    call_index: int = 0
    created_at: str = ''
    prepared_message_count: int = 0
    prepared_message_chars: int = 0
    response_tool_call_count: int = 0
    delta_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    delta_usage_by_model: list[ModelTokenUsageRecord] = Field(default_factory=list)


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
    latest_node: LatestTaskNodeOutput | None = None
    live_state: TaskLiveState | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_usage_by_model: list[ModelTokenUsageRecord] = Field(default_factory=list)
    model_calls: list[TaskModelCallRecord] = Field(default_factory=list)
    text: str = ''

