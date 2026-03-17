from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from main.types import NodeKind, NodeStatus, TaskStatus


class Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class NodeOutputEntry(Model):
    seq: int
    content: str = ''
    content_ref: str = ''
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str


class NodeFinalResult(Model):
    status: NodeStatus
    output: str = ''


class SpawnChildSpec(Model):
    goal: str
    prompt: str
    acceptance_prompt: str


class SpawnChildResult(Model):
    goal: str
    check_result: str = ''
    node_output: str = ''
    node_output_summary: str = ''
    node_output_ref: str = ''


class TaskArtifactRecord(Model):
    artifact_id: str
    task_id: str
    node_id: str | None = None
    kind: str
    title: str
    path: str
    mime_type: str = 'text/markdown'
    preview_text: str = ''
    created_at: str


class TaskRecord(Model):
    task_id: str
    session_id: str = 'web:shared'
    title: str
    user_request: str
    status: TaskStatus = 'in_progress'
    root_node_id: str
    max_depth: int = 0
    cancel_requested: bool = False
    pause_requested: bool = False
    is_paused: bool = False
    is_unread: bool = True
    brief_text: str = ''
    created_at: str
    updated_at: str
    finished_at: str | None = None
    final_output: str = ''
    final_output_ref: str = ''
    failure_reason: str = ''
    runtime_state_path: str = ''
    tree_snapshot_path: str = ''
    tree_text_path: str = ''
    metadata: dict[str, Any] = Field(default_factory=dict)


class NodeRecord(Model):
    node_id: str
    task_id: str
    parent_node_id: str | None = None
    root_node_id: str
    depth: int = 0
    node_kind: NodeKind = 'execution'
    status: NodeStatus = 'in_progress'
    goal: str
    prompt: str
    input: str = ''
    input_ref: str = ''
    output: list[NodeOutputEntry] = Field(default_factory=list)
    check_result: str = ''
    check_result_ref: str = ''
    final_output: str = ''
    final_output_ref: str = ''
    can_spawn_children: bool = False
    created_at: str
    updated_at: str
    finished_at: str | None = None
    failure_reason: str = ''
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def outputs(self) -> list[NodeOutputEntry]:
        return self.output
