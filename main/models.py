from __future__ import annotations

from typing import Any, Literal

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


RESULT_SCHEMA_VERSION = 2
EXECUTION_POLICY_FOCUS = 'focus'
EXECUTION_POLICY_COVERAGE = 'coverage'
EXECUTION_POLICY_MODES = (EXECUTION_POLICY_FOCUS, EXECUTION_POLICY_COVERAGE)
FAILURE_CLASS_ENGINE = 'engine_failure'
FAILURE_CLASS_BUSINESS_UNPASSED = 'business_unpassed'
FAILURE_CLASS_NON_RETRYABLE_BLOCKED = 'non_retryable_blocked'
FAILURE_CLASSES = (
    FAILURE_CLASS_ENGINE,
    FAILURE_CLASS_BUSINESS_UNPASSED,
    FAILURE_CLASS_NON_RETRYABLE_BLOCKED,
)

def normalize_failure_class(value: Any, *, default: str = '') -> str:
    normalized = str(value or '').strip().lower()
    if normalized in FAILURE_CLASSES:
        return normalized
    return str(default or '').strip().lower()




class NodeEvidenceItem(Model):
    kind: Literal['file', 'artifact', 'url'] = 'artifact'
    path: str = ''
    ref: str = ''
    start_line: int | None = None
    end_line: int | None = None
    note: str = ''

    def summary_text(self) -> str:
        location = self.path or self.ref
        if self.start_line and self.end_line and self.end_line >= self.start_line:
            location = f'{location}:{self.start_line}-{self.end_line}' if location else f'lines {self.start_line}-{self.end_line}'
        elif self.start_line:
            location = f'{location}:{self.start_line}' if location else f'line {self.start_line}'
        parts = [part for part in [self.kind, location, self.note] if str(part or '').strip()]
        return ' | '.join(parts)


class NodeFinalResult(Model):
    status: Literal['success', 'failed']
    delivery_status: Literal['final', 'partial', 'blocked'] = 'final'
    summary: str = ''
    answer: str = ''
    evidence: list[NodeEvidenceItem] = Field(default_factory=list)
    remaining_work: list[str] = Field(default_factory=list)
    blocking_reason: str = ''

    @property
    def output(self) -> str:
        return str(self.answer or self.summary or self.blocking_reason or '')

    @property
    def failure_text(self) -> str:
        return str(self.blocking_reason or self.summary or self.output or '')

    def payload_dict(self) -> dict[str, Any]:
        return self.model_dump(mode='json')

    def evidence_summary(self) -> list[str]:
        lines: list[str] = []
        for item in list(self.evidence or []):
            text = item.summary_text()
            if text:
                lines.append(text)
        return lines


class FinalAcceptanceState(Model):
    required: bool = False
    prompt: str = ''
    node_id: str = ''
    status: str = 'pending'


class ExecutionPolicyState(Model):
    mode: Literal['focus', 'coverage'] = EXECUTION_POLICY_FOCUS


class ExecutionStageRound(Model):
    round_id: str = ''
    round_index: int = 0
    created_at: str = ''
    tool_call_ids: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    budget_counted: bool = False


class ExecutionStageKeyRef(Model):
    ref: str = ''
    note: str = ''


class ExecutionStageRecord(Model):
    stage_id: str = ''
    stage_index: int = 0
    stage_kind: Literal['normal', 'compression'] = 'normal'
    system_generated: bool = False
    mode: Literal['自主执行', '包含派生'] = '自主执行'
    status: Literal['进行中', '完成', '失败'] = '进行中'
    stage_goal: str = ''
    completed_stage_summary: str = ''
    final_stage: bool = False
    key_refs: list[ExecutionStageKeyRef] = Field(default_factory=list)
    archive_ref: str = ''
    archive_stage_index_start: int = 0
    archive_stage_index_end: int = 0
    tool_round_budget: int = 0
    tool_rounds_used: int = 0
    created_at: str = ''
    finished_at: str = ''
    rounds: list[ExecutionStageRound] = Field(default_factory=list)


class ExecutionStageState(Model):
    active_stage_id: str = ''
    transition_required: bool = False
    stages: list[ExecutionStageRecord] = Field(default_factory=list)


class SpawnChildSpec(Model):
    goal: str
    prompt: str
    execution_policy: ExecutionPolicyState
    acceptance_prompt: str = ''
    requires_acceptance: bool | None = None


class SpawnChildFailureInfo(Model):
    source: Literal['execution', 'acceptance', 'runtime']
    summary: str = ''
    delivery_status: Literal['final', 'blocked'] = 'blocked'
    blocking_reason: str = ''
    remaining_work: list[str] = Field(default_factory=list)
    output_ref: str = ''
    result_payload_ref: str = ''


class SpawnChildResult(Model):
    goal: str
    check_result: str = ''
    node_output: str = ''
    node_output_summary: str = ''
    node_output_ref: str = ''
    failure_info: SpawnChildFailureInfo | None = None


class TokenUsageSummary(Model):
    tracked: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    call_count: int = 0
    calls_with_usage: int = 0
    calls_without_usage: int = 0
    is_partial: bool = False


class ModelTokenUsageRecord(TokenUsageSummary):
    model_key: str = ''
    provider_id: str = ''
    provider_model: str = ''


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


class TaskMessageDistributionEpoch(Model):
    epoch_id: str
    task_id: str
    root_node_id: str
    root_message: str
    state: str
    created_at: str
    paused_at: str = ''
    distributed_at: str = ''
    completed_at: str = ''
    error_text: str = ''
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskNodeNotification(Model):
    notification_id: str
    task_id: str
    node_id: str
    epoch_id: str
    source_node_id: str = ''
    message: str = ''
    status: str
    created_at: str
    delivered_at: str = ''
    consumed_at: str = ''
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeToolFileChange(Model):
    path: str = ''
    change_type: Literal['created', 'modified', 'deleted'] = 'modified'


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
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
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
    token_usage: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    token_usage_by_model: list[ModelTokenUsageRecord] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def outputs(self) -> list[NodeOutputEntry]:
        return self.output


def normalize_final_acceptance_metadata(value: Any) -> FinalAcceptanceState:
    payload = value.model_dump(mode='json') if isinstance(value, FinalAcceptanceState) else (dict(value) if isinstance(value, dict) else {})
    prompt = str(payload.get('prompt') or '').strip()
    raw_required = payload.get('required')
    required = bool(raw_required) or (raw_required in {None, ''} and bool(prompt))
    status = str(payload.get('status') or 'pending').strip().lower()
    if status not in {'pending', 'running', 'passed', 'failed'}:
        status = 'pending'
    return FinalAcceptanceState(
        required=required,
        prompt=prompt,
        node_id=str(payload.get('node_id') or '').strip(),
        status=status,
    )


def normalize_execution_policy_metadata(
    value: Any,
    *,
    default_mode: str = EXECUTION_POLICY_FOCUS,
) -> ExecutionPolicyState:
    payload = value.model_dump(mode='json') if isinstance(value, ExecutionPolicyState) else (dict(value) if isinstance(value, dict) else {})
    fallback_mode = str(default_mode or EXECUTION_POLICY_FOCUS).strip().lower()
    if fallback_mode not in EXECUTION_POLICY_MODES:
        fallback_mode = EXECUTION_POLICY_FOCUS
    mode = str(payload.get('mode') or fallback_mode).strip().lower()
    if mode not in EXECUTION_POLICY_MODES:
        mode = fallback_mode
    return ExecutionPolicyState(mode=mode)


def build_execution_policy_schema(*, description: str) -> dict[str, Any]:
    return {
        'type': 'object',
        'description': str(description or '').strip(),
        'properties': {
            'mode': {
                'type': 'string',
                'description': (
                    '执行策略模式。`focus` 表示只做最高价值、最必要、与当前目标直接相关的动作；'
                    '`coverage` 表示仍然先做最高价值动作，但在需要时允许扩展范围、补做边缘分支或系统性全量操作。'
                ),
                'enum': list(EXECUTION_POLICY_MODES),
            },
        },
        'required': ['mode'],
    }


def normalize_execution_stage_metadata(value: Any) -> ExecutionStageState:
    payload = value.model_dump(mode='json') if isinstance(value, ExecutionStageState) else (dict(value) if isinstance(value, dict) else {})
    active_stage_id = str(payload.get('active_stage_id') or '').strip()
    transition_required = bool(payload.get('transition_required'))
    stages: list[ExecutionStageRecord] = []
    for item in list(payload.get('stages') or []):
        if not isinstance(item, dict):
            continue
        try:
            stage = ExecutionStageRecord.model_validate(item)
        except Exception:
            continue
        tool_round_budget = max(0, int(stage.tool_round_budget or 0))
        tool_rounds_used = max(0, min(int(stage.tool_rounds_used or 0), tool_round_budget or int(stage.tool_rounds_used or 0)))
        stages.append(
            stage.model_copy(
                update={
                    'tool_round_budget': tool_round_budget,
                    'tool_rounds_used': tool_rounds_used,
                    'archive_stage_index_start': max(0, int(stage.archive_stage_index_start or 0)),
                    'archive_stage_index_end': max(0, int(stage.archive_stage_index_end or 0)),
                    'completed_stage_summary': str(stage.completed_stage_summary or ''),
                    'key_refs': [
                        ExecutionStageKeyRef.model_validate(key_ref)
                        for key_ref in list(stage.key_refs or [])
                    ],
                }
            )
        )
    if active_stage_id and not any(stage.stage_id == active_stage_id for stage in stages):
        active_stage_id = ''
    return ExecutionStageState(
        active_stage_id=active_stage_id,
        transition_required=transition_required,
        stages=stages,
    )


def normalize_tool_file_changes(value: Any) -> list[NodeToolFileChange]:
    changes: list[NodeToolFileChange] = []
    for item in list(value or []):
        if not isinstance(item, dict):
            continue
        try:
            change = NodeToolFileChange.model_validate(item)
        except Exception:
            continue
        path = str(change.path or '').strip()
        change_type = str(change.change_type or 'modified').strip().lower()
        if not path:
            continue
        if change_type not in {'created', 'modified', 'deleted'}:
            change_type = 'modified'
        changes.append(
            change.model_copy(
                update={
                    'path': path,
                    'change_type': change_type,
                }
            )
        )
    return changes


def normalize_result_payload(value: Any) -> NodeFinalResult | None:
    if isinstance(value, NodeFinalResult):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return NodeFinalResult.model_validate(value)
    except Exception:
        return None
