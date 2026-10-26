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
    # 当跳思考原样存在行上：正文被外置成 content_ref 时思考不跟着走，且中断轮恢复重建
    # assistant 行时只能从这里取回（写侧闸门判定见 react_loop._node_assistant_reasoning_field）。
    reasoning_content: str = ''
    reasoning_items: list[dict[str, Any]] = Field(default_factory=list)
    # 这一轮的思考读数（只有标量：seconds/chars/est_tokens/tokens），供节点详情按轮显示。
    # 缺键＝这一族口径这次没上报（provider 没报思考 token、模型走不吐思维分片的协议），
    # 与真实的 0 分开，前端据此显示估算或 '--'，不伪造。
    thinking: dict[str, Any] = Field(default_factory=dict)
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


def normalize_optional_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ''
    return str(value).strip()


def normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    items: list[str] = []
    for item in value:
        text = normalize_optional_text(item)
        if text:
            items.append(text)
    return items




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
    # 验收三档裁决（P4）：只有验收节点提交的载荷带这个字段；执行节点恒为空。
    # 进入 result_payload 持久化——验收节点回合结束后，裁决只能从节点行读回
    # （`_drain_acceptance_verdicts` 就是这么消费的）。
    verdict: Literal['', 'pass', 'reject', 'reject_no_progress'] = ''
    # 被验方提交的幂等标识：验收裁决与回执都按它对齐同一次提交。
    submission_id: str = ''
    # This flag controls only the current runner handoff. Result payloads remain
    # backwards-compatible and must never persist an internal pause instruction.
    failure_disposition: Literal['terminal', 'pause'] = Field(default='terminal', exclude=True)

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


class SpawnChildSpec(Model):
    goal: str
    prompt: str
    execution_policy: ExecutionPolicyState
    acceptance_prompt: str = ''
    requires_acceptance: bool | None = None
    # 顺序即优先级：体积超限时按这个顺序裁剪，所以它不是排版字段。
    seed_tools: list[str] = Field(default_factory=list)


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
    # spawn review 拦截的机器可读标记：True 表示该分支未创建、未执行，
    # 父节点必须按 node_output 中的 reason/suggestion 处置，不得视为已完成分支。
    review_blocked: bool = False
    # 预置契约回执：只在派发方声明过 seed_tools 时出现（exclude_none 让未声明时整键消失），
    # 出现时 accepted / dropped 两键恒在。
    seed_tools: dict[str, Any] | None = None
    # 非阻塞派生的创建回执：`created` 表示节点已创建并交给调度器，结果稍后以
    # child_final 消息送达；`rejected` 表示未创建（gate 说明是哪道闸）。
    # 三个字段都是增量，存量 spawn_operations 里的旧 dict 反序列化时取默认值。
    child_node_id: str = ''
    acceptance_node_id: str = ''
    state: str = ''
    gate: str = ''
    reject_reason: str = ''


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
    # 这一发实际落在负载均衡链的哪一格、哪个组、哪个配额桶。三者都是**成员身份**属性，
    # 所以按 model_key 合并时取首个非哨兵值。哨兵：-1 / '' = 这一发没有租约（前门车道与
    # 辅助车道不经过 balancer，结构上恒为哨兵，不是漏写）。
    route_index: int = -1
    group_key: str = ''
    quota_bucket_index: int = -1


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
    # 磁盘治理（P0）：落盘内容的原始字节数与存储编码。
    # artifacts 表整条 payload_json 序列化，新增带默认值字段对旧行零迁移兼容。
    size_bytes: int = 0
    content_encoding: str = 'plain'  # 'plain' | 'gzip'
    content_hash: str = ''


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
    # 消息三态：consumed_at=控制/决策回合处理过；merged_at=内容真正并入模型上下文。
    # consumed 而未 merged 显示「已消费」，merged 后显示「已并入上下文」。
    merged_at: str = ''
    # 以下字段由 `main/runtime/node_message_kind.py` 机械推导，发送方不能自报：
    # kind/priority 决定投递与唤醒。
    # 全部带默认值，存量行（payload_json 里没有这些键）照旧解析。
    kind: str = ''
    priority: str = ''
    # 存量字段：新代码不再写入/读取，只为旧行解析保留。
    intent_kind: str = ''
    fingerprint: str = ''
    in_reply_to: str = ''
    # 大内容外部化指针：消息正文只放摘要，全文走 ref。
    ref: str = ''
    # 仅 acceptance_verdict：绑定被验的那一份提交（迟到裁决规则唯一判据）。
    submission_id: str = ''
    verdict: str = ''
    # `spawn_child_nodes.wait_all_children=true` 的批次：child_final 先落箱、
    # held_at 非空且 released_at 为空时既不注入也不唤醒，批次全部终局后一起释放。
    batch_id: str = ''
    held_at: str = ''
    released_at: str = ''
    payload: dict[str, Any] = Field(default_factory=dict)


class FrontdoorNodeMessage(Model):
    """A root-node message addressed to the frontdoor/session inbox."""

    message_id: str
    session_id: str
    task_id: str
    source_node_id: str = ''
    content: str = ''
    ref: str = ''
    status: str = 'pending'
    created_at: str = ''
    consumed_at: str = ''
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeToolFileChange(Model):
    path: str = ''
    change_type: Literal['created', 'modified', 'deleted'] = 'modified'


class TaskNodePauseRecord(Model):
    id: int = 0
    task_id: str
    node_id: str
    pause_reason: str = ''
    remark: str = ''
    delivered: bool = False
    created_at: str = ''
    updated_at: str = ''
    error_text: str = ''


class TaskNodeTimerRecord(Model):
    """节点自己设的一次性唤醒（`schedule_wake`）。

    每个节点至多一份待触发唤醒：`(task_id, node_id)` 是主键，重新设就是覆盖。唤醒时刻存
    epoch 毫秒而不是 ISO 串——它只用来和时钟比大小，而 `time` 工具把同一个数以两种写法还给
    模型。`claimed_by` / `claimed_at_ms` 是多 worker 下的一次性领取凭证：领取后投递成功即删行；
    进程在中间死掉时，过期（`_WAKE_CLAIM_STALE_MS`）的领取被下一个扫描重新领走——这张表存在
    的意义就是"重启也认"，所以丢掉一条领了一半的凭证等于回到 `exec Start-Sleep` 的老故障。
    """

    task_id: str
    node_id: str
    wake_at_ms: int = 0
    note: str = ''
    armed_at: str = ''
    claimed_by: str = ''
    claimed_at_ms: int = 0


class TaskErrorLogRecord(Model):
    seq: int = 0
    task_id: str
    node_id: str
    node_title: str = ''
    error_text: str = ''
    created_at: str = ''


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
    pause_requested: bool = False
    is_paused: bool = False
    pause_reason: str = ''
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
    from main.runtime.acceptance_handshake import FINAL_ACCEPTANCE_STATUS_VALUES

    if status not in FINAL_ACCEPTANCE_STATUS_VALUES:
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
