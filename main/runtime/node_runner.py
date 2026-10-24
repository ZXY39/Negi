from __future__ import annotations

import asyncio
import copy
import json
import os
import platform
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from g3ku.agent.tools.base import Tool
from g3ku.deployment.data_root import data_work_path
from g3ku.runtime.memory_scope import normalize_memory_scope
from g3ku.runtime.project_environment import current_project_environment
from g3ku.utils.helpers import PATH_ANCHOR_RULE_TEXT, path_anchor_policy
from main.errors import (
    NodePausedError,
    TaskPausedError,
    describe_exception,
    describe_exception_chain,
)
from main.ids import new_command_id, new_node_id
from main.models import (
    RESULT_SCHEMA_VERSION,
    NodeFinalResult,
    NodeRecord,
    SpawnChildFailureInfo,
    SpawnChildResult,
    SpawnChildSpec,
    TokenUsageSummary,
    normalize_execution_policy_metadata,
    normalize_final_acceptance_metadata,
    normalize_result_payload,
)
from main.prompts import load_prompt
from main.protocol import now_iso
from main.runtime.acceptance_handshake import (
    ACCEPTANCE_HANDSHAKE_KEY,
    ACCEPTANCE_STATE_ACCEPTED,
    ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
    ACCEPTANCE_STATE_REJECTED_TERMINAL,
    ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
    normalize_acceptance_handshake,
    set_acceptance_handshake_state,
)
from main.runtime.append_notice_context import (
    APPEND_NOTICE_CONTEXT_KEY,
    PENDING_APPEND_NOTICE_RECORDS_KEY,
    consume_pending_append_notice_records,
    normalize_append_notice_context,
    normalize_pending_append_notice_records,
    record_consumed_notifications,
    supersede_append_notice_records,
)
from main.runtime.child_tools import (
    ListChildNodesTool,
    ManageChildNodesTool,
    ReadChildProgressTool,
    ViewNodeTreeTool,
)
from main.runtime.internal_tools import (
    EnterWaitTool,
    SendMessageTool,
    SpawnChildNodesTool,
    SubmitFinalResultTool,
)
from main.runtime.node_message_kind import (
    INTENT_INSTRUCTION,
    INTENT_PROGRESS,
    KIND_ACCEPTANCE_VERDICT,
    RELATION_CHILD,
    RELATION_PARENT,
)
from main.runtime.node_prompt_contract import extract_node_dynamic_contract_payload
from main.runtime.node_wake import (
    WAKE_ON_ALL_CHILDREN,
    enter_wait_verdict,
)
from main.runtime.pending_notice_state import (
    PENDING_NOTICE_STATE_KEY,
    RESUME_MODE_ORDINARY,
    RESUME_MODE_WAIT_FOR_CHILDREN,
    clear_pending_notice_state,
    normalize_pending_notice_state,
    set_pending_notice_state,
)
from main.runtime.tool_names import (
    ENTER_WAIT_TOOL_NAME,
    LIST_CHILD_NODES_TOOL_NAME,
    MANAGE_CHILD_NODES_TOOL_NAME,
    READ_CHILD_PROGRESS_TOOL_NAME,
    SEND_MESSAGE_TOOL_NAME,
    VIEW_NODE_TREE_TOOL_NAME,
)
from main.service.create_async_task_contract import normalize_create_async_task_file_targets
from main.storage.disk_guard import is_disk_full_error
from main.types import KIND_ACCEPTANCE, KIND_EXECUTION, STATUS_FAILED, STATUS_SUCCESS

SKIPPED_CHECK_RESULT = '未检验'
_RECOVERY_FINGERPRINT_KEY = 'recovery_fingerprint'
# 定向通知导致的"打断验收→回去重做"是按节点累积的事实计数：决策回合的模型只看得见
# 手里这条通知，跨轮、跨重启的返工圈必须有人替它记着，否则"这条线已经重做五次"不可判。
# 它不拦任何模型选择——只作为事实渲染进决策束，重做与否仍由节点决定，问不问用户由前门决定。
NOTICE_RETRY_STATE_KEY = 'notice_retry_state'
_ACCEPTANCE_EVIDENCE_AUDIT_KEY = 'acceptance_evidence_audit'
_REJECTION_HISTORY_KEY = 'rejection_history'
# 节点消息正文（打回反馈、交接摘要、验收 bootstrap、每轮尾块）的内联额度。额度按实盘
# 正文尺寸定：打回反馈 p99=3562，验收每轮尾块摘要 p50=2177/p99=5923，整篇都在额度内；
# 只有超限才退回「头段 + 可复开的 ref」，不在正文里留半截要执行的指令。
_NODE_MESSAGE_INLINE_CHAR_LIMIT = 16_000

# 冻结不是裁定。闸门返回的是 `success + partial`，而 `_handle_acceptance_node_result`
# 的首分支只看 `status == success`：不加识别就会把「还没验」结算成一次通过（握手
# `accepted`，根验收还会把任务写 `passed` 并终态化）。前缀集中在这里，生产端与
# 消费端共用，不做散文匹配。
ACCEPTANCE_FREEZE_PREFIX = '验收冻结：'
FINAL_ACCEPTANCE_FREEZE_PREFIX = '最终验收冻结：'
ACCEPTANCE_FREEZE_PREFIXES = (ACCEPTANCE_FREEZE_PREFIX, FINAL_ACCEPTANCE_FREEZE_PREFIX)


def is_acceptance_freeze_result(result: Any) -> bool:
    """这条 partial 表示「还没验」，不是一次裁定。"""
    if str(getattr(result, 'delivery_status', '') or '').strip().lower() != 'partial':
        return False
    return str(getattr(result, 'summary', '') or '').strip().startswith(ACCEPTANCE_FREEZE_PREFIXES)


def node_message_body(text: str, *, ref: str = '') -> str:
    """节点消息正文的内联形态：额度内整篇给，超限才给头段 + 指向完整正文的 ref。"""
    body = str(text or '').strip()
    if len(body) <= _NODE_MESSAGE_INLINE_CHAR_LIMIT:
        return body
    head = body[:_NODE_MESSAGE_INLINE_CHAR_LIMIT]
    pointer = str(ref or '').strip()
    if pointer:
        return (
            f'{head}\n…（正文共 {len(body)} 字符，超出内联额度 {_NODE_MESSAGE_INLINE_CHAR_LIMIT}；'
            f'完整正文用 content_open(ref="{pointer}") 或 content_search(ref="{pointer}") 复开）'
        )
    return (
        f'{head}\n…（正文共 {len(body)} 字符，已按内联额度 {_NODE_MESSAGE_INLINE_CHAR_LIMIT} 截断；'
        f'本轮没有可复开的完整正文 ref）'
    )


_SUPERSEDED_SPAWN_REASON_PREFIX = 'superseded by newer spawn round'
# 派生轮自身抛异常、但本轮仍绑着非终态节点时的滞留节拍：每片检查一次节点状态，
# 每满 report 秒补一条可检索的告警。不设上限——交付"派生失败"会让父节点重发新轮，
# 新轮开局就把这棵还在飞的子树按 superseded 清扫掉。
_SPAWN_FAULT_HOLD_SLICE_SECONDS = 2.0
_SPAWN_FAULT_HOLD_REPORT_SECONDS = 60.0
# 停等现场要活过心跳渲染端的头尾裁剪（`prompt_lane._overflow_head_and_tail` 只留头 70% +
# 尾 20%，而外置 ref 要查正可能坏掉的那个库才能解开）。所以正文自限到远低于内联额度，
# 结论与观测值全在头尾，不依赖任何取回链路。
_SPAWN_FAULT_HOLD_REPORT_TEXT_LIMIT = 900
_SPAWN_REVIEW_TOOL_NAME = 'review_spawn_candidates'
_SPAWN_REVIEW_BLOCKED_CHECK_RESULT = '派生已被拦截'
# 检验车道自身故障（模型链缺失 / provider 异常 / 连续拿不到可解析判定）时，本批候选
# 是"没被审过"而不是"审了并被拒"。两者必须在 check_result 上可分，否则父节点会把基建
# 故障当成评审意见去改写 spec，账本上也无法区分。
_SPAWN_REVIEW_SYSTEM_FAILURE_CHECK_RESULT = '派生未审查（系统故障）'
_SPAWN_REVIEW_DEFAULT_BLOCK_REASON = '检验派生未批准该候选派生。'
# 兜底建议不能只说「收缩为单一派生」：父节点会据此把可并行分支继续拆轮，或删掉
# requires_acceptance 来凑批量，两条都是往错误方向收敛。
_SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION = (
    '请在当前父节点内自行执行；确需派生时把互不依赖的分支合并为一次 spawn_child_nodes 批量派生，'
    '需要独立验收的分支在该候选上设置 requires_acceptance=true 并给出 acceptance_prompt。'
)
_SPAWN_REVIEW_RETRY_DELAY_SECONDS = 0.1
_SPAWN_REVIEW_MAX_ATTEMPTS = 5
_SPAWN_REVIEW_REPAIR_PREFIX = '上一轮检验派生回复无效。'
# 独立验收节点的 goal 前缀只在这里定义一次：派生审查载荷要按同一口径回显「运行时会
# 创建哪条节点」，改一半就会让评审看到的 goal 与真实节点对不上。
_SPAWN_ACCEPTANCE_GOAL_PREFIX = 'accept:'


def _spawn_acceptance_goal(goal: str) -> str:
    return _SPAWN_ACCEPTANCE_GOAL_PREFIX + str(goal or '')


def _spec_requires_acceptance(spec: SpawnChildSpec) -> bool:
    """`requires_acceptance` 未显式给时按 `acceptance_prompt` 非空推断——载荷与建节点必须同源。"""
    if spec.requires_acceptance is True:
        return True
    if spec.requires_acceptance is False:
        return False
    return bool(str(spec.acceptance_prompt or '').strip())



def _notification_awaits_injection(item: Any) -> bool:
    """消息三态判定：内容尚未真正并入模型上下文的通知。

    delivered = 待处理；consumed 且 merged_at 为空 = 已被控制/决策回合处理、
    等待恢复路径并入（显示「已消费」）；两者都需要被恢复/在途刷新路径看到。
    """
    status = str(getattr(item, 'status', '') or '').strip()
    if status == 'delivered':
        return True
    return status == 'consumed' and not str(getattr(item, 'merged_at', '') or '').strip()


_UNSET = object()


class _NodeTurnGate:
    """同一节点上在飞回合的会合点：后来者等这一个事件，绝不并行开第二个回合。

    `result` / `error` 由持有者（第一个进入 `run_node` 的协程）在回合真正结束后填写；
    等待方等的是 Event 而不是持有者的 task，所以持有者被取消不会连带取消等待方——
    等待方要的是「第一回合的结局」，结局是 `CancelledError` 时它也如实收到。
    """

    __slots__ = ('done', 'result', 'error')

    def __init__(self) -> None:
        self.done = asyncio.Event()
        self.result: NodeFinalResult | None = None
        self.error: BaseException | None = None


class NodeRunner:
    def __init__(
        self,
        *,
        store,
        log_service,
        react_loop,
        tool_provider,
        execution_model_refs: list[str],
        acceptance_model_refs: list[str],
        execution_model_routes: Any = None,
        acceptance_model_routes: Any = None,
        execution_max_iterations: int | None | object = _UNSET,
        acceptance_max_iterations: int | None | object = _UNSET,
        max_parallel_child_pipelines: int | None | object = _UNSET,
        execution_max_concurrency: int | None = None,
        acceptance_max_concurrency: int | None = None,
        context_enricher=None,
        context_preparer=None,
        context_finalizer=None,
        workspace_root_getter=None,
        seed_contract_metadata_builder=None,
    ) -> None:
        self._store = store
        self._log_service = log_service
        self._react_loop = react_loop
        self._tool_provider = tool_provider
        self._execution_model_refs = list(execution_model_refs or [])
        self._acceptance_model_refs = list(acceptance_model_refs or []) or list(execution_model_refs or [])
        # route plan 是执行/检验两条车道真正的路由来源；refs 退化成候选展开视图。
        # 检验缺省时继承整份执行 route plan（不是继承展开后的字符串数组）。
        self._execution_model_routes = execution_model_routes
        self._acceptance_model_routes = acceptance_model_routes if acceptance_model_routes is not None else execution_model_routes
        self._execution_max_iterations = self._normalize_optional_limit(execution_max_iterations, default=16)
        self._acceptance_max_iterations = self._normalize_optional_limit(
            acceptance_max_iterations,
            default=self._execution_max_iterations,
        )
        self._max_parallel_child_pipelines = self._normalize_optional_limit(max_parallel_child_pipelines, default=10)
        self._execution_max_concurrency = self._normalize_optional_limit(execution_max_concurrency, default=None)
        self._acceptance_max_concurrency = self._normalize_optional_limit(
            acceptance_max_concurrency,
            default=self._execution_max_concurrency,
        )
        self._parallel_child_pipelines_enabled = True
        # 统一工具 timeout 全局默认值（秒），由 runtime_service 从配置注入/热更新。
        self._tool_default_timeout_seconds: float | None = None
        # 派生闸门：单任务节点总数上限（含验收节点），由 runtime_service 注入。
        self._max_task_nodes = 200
        # H3：节点道单次工具调用的时长上限（秒），由 runtime_service 注入。
        self._node_tool_timeout_ceiling_seconds = 900.0
        self._adaptive_tool_budget_controller = getattr(react_loop, '_adaptive_tool_budget_controller', None)
        self._context_enricher = context_enricher
        self._context_preparer = context_preparer
        self._context_finalizer = context_finalizer
        self._workspace_root_getter = workspace_root_getter
        self._seed_contract_metadata_builder = seed_contract_metadata_builder
        self.nested_node_executor = None
        self.cancel_node_subtree_executor = None
        # 活性问持有者，不问投影：派发器是唯一说得出「这个节点此刻有没有人在驱动」的一方。
        # 读 dispatcher entry 是调用方的一跳（见 run_node 上方注释的分层），所以在这里注入。
        self.node_liveness_probe = None
        self._spawn_operation_locks: dict[str, asyncio.Lock] = {}
        # 同一节点的在飞回合登记表（single-flight）。派发器在 entry.future 已结束而节点
        # 非终态时会丢掉 entry 并允许重派（`TaskNodeDispatcher._get_or_create_entry`），
        # 本进程因此可能在旧回合仍活着时再叫一次 `run_node`。那个洞不能靠调度器补：
        # 重入守卫必须长在回合入口本身。
        self._node_turn_gates: dict[str, _NodeTurnGate] = {}
        # 节点终局 → 父节点信箱的投递口（由 runtime_service 绑定）。投递是异步的，
        # 而终局写入发生在同步代码里，所以这里只登记任务；`run_task` 的空转判定读
        # `has_pending_terminal_messages()`，避免"终局消息还没送出就把任务收工"。
        self.child_final_sink = None
        # 异常暂停 → 父节点 `child_abnormal_pause` 的投递口（由 runtime_service 绑定）。
        self.abnormal_pause_sink = None
        # 验收车道的结构性消息（交接单 / 三档裁决）→ 信箱的统一投递口，绑定到
        # `MainRuntimeService.deliver_node_message`。走同一个分类/去重/落箱实现，
        # 保证验收消息与模型消息一样带机械推导的 kind/priority。
        self.mailbox_delivery_sink = None
        self._pending_terminal_messages: set[asyncio.Task[None]] = set()
        # 子节点创建后的立即派发口（由 task_actor_service 绑定）。拆掉阻塞承诺后
        # 父节点不再等待子管线，创建完就必须把节点交给调度器；没有这个口时（单测、
        # 或任务没有活跃 dispatcher）由任务演员的可派发扫描兜底。
        self.dispatch_node_callback = None
        # 磁盘治理（P0）：磁盘满时未能落盘的节点错误文本，仅内存诊断用（有界）。
        self._unpersisted_error_logs: deque[tuple[str, str, str]] = deque(maxlen=64)

    @staticmethod
    def _normalized_status(value: Any) -> str:
        return str(value or '').strip().lower()

    def _task_terminal_reason(self, task_id: str, *, task=None) -> str:
        current = self._store.get_task(task_id) or task
        if current is None:
            return ''
        status = self._normalized_status(getattr(current, 'status', ''))
        if status == STATUS_FAILED:
            return str(getattr(current, 'failure_reason', '') or getattr(current, 'brief_text', '') or 'task failed').strip() or 'task failed'
        if status == STATUS_SUCCESS:
            return 'task already completed'
        return ''

    @staticmethod
    def _trim_diagnostic_text(value: Any, *, max_chars: int = 4000) -> str:
        text = str(value or '').strip()
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 3].rstrip() + '...'

    def _capture_memory_error_diagnostics(self, *, task_id: str, node_id: str, exc: MemoryError) -> None:
        task = self._store.get_task(task_id)
        node = self._store.get_node(node_id)
        frame: dict[str, Any] = {}
        reader = getattr(self._log_service, 'read_runtime_frame', None)
        if callable(reader):
            try:
                frame = dict(reader(task_id, node_id) or {})
            except Exception:
                frame = {}
        metadata = dict(getattr(node, 'metadata', {}) or {}) if node is not None else {}
        diagnostics = {
            'recorded_at': now_iso(),
            'task_id': str(task_id or '').strip(),
            'session_id': str(getattr(task, 'session_id', '') or '').strip(),
            'node_id': str(node_id or '').strip(),
            'node_kind': str(getattr(node, 'node_kind', '') or '').strip(),
            'phase': str(frame.get('phase') or '').strip(),
            'await_marker': str(frame.get('await_marker') or '').strip(),
            'exception_type': type(exc).__name__,
            'exception_text': self._trim_diagnostic_text(str(exc or '')),
            'traceback_tail': self._trim_diagnostic_text(traceback.format_exc(), max_chars=6000),
            'memory_error_probe': dict(frame.get('memory_error_probe') or {}),
            'latest_runtime_actual_request_ref': str(
                metadata.get('latest_runtime_actual_request_ref') or ''
            ).strip(),
            'latest_runtime_actual_request_message_count': int(
                metadata.get('latest_runtime_actual_request_message_count') or 0
            ),
            'latest_runtime_observed_input_truth': dict(
                metadata.get('latest_runtime_observed_input_truth') or {}
            ),
            'frame_message_count': len(list(frame.get('messages') or [])),
            'frame_tool_call_count': len(list(frame.get('tool_calls') or [])),
            'frame_child_pipeline_count': len(list(frame.get('child_pipelines') or [])),
        }

        updater = getattr(self._store, 'update_node', None)
        if callable(updater):
            try:
                def _mutate(current: NodeRecord) -> NodeRecord:
                    next_metadata = dict(getattr(current, 'metadata', {}) or {})
                    next_metadata['memory_error_diagnostics'] = diagnostics
                    return current.model_copy(update={'metadata': next_metadata, 'updated_at': now_iso()})

                updater(node_id, _mutate)
            except Exception:
                pass

        frame_updater = getattr(self._log_service, 'update_frame', None)
        if callable(frame_updater):
            try:
                def _mutate_frame(current: dict[str, Any]) -> dict[str, Any]:
                    next_frame = dict(current or {})
                    next_frame['memory_error_diagnostics'] = dict(diagnostics)
                    return next_frame

                frame_updater(task_id, node_id, _mutate_frame, publish_snapshot=False)
            except Exception:
                pass

    def _node_terminal_reason(self, node: NodeRecord | None, *, default_failed: str, default_success: str) -> str:
        if node is None:
            return ''
        status = self._normalized_status(getattr(node, 'status', ''))
        if status == STATUS_FAILED:
            return str(getattr(node, 'failure_reason', '') or getattr(node, 'final_output', '') or default_failed).strip() or default_failed
        if status == STATUS_SUCCESS:
            return default_success
        return ''

    def _spawn_abort_result(self, goal: str, reason: str) -> SpawnChildResult:
        text = str(reason or 'task terminated').strip() or 'task terminated'
        error_text = f'Error: {text}'
        return SpawnChildResult(
            goal=goal,
            check_result=error_text,
            node_output='',
            node_output_summary='',
            node_output_ref='',
            failure_info=self._runtime_spawn_failure_info(error_text),
        )

    @staticmethod
    def _spawn_operation_lock_key(parent_node_id: str) -> str:
        return str(parent_node_id or '').strip()

    def _spawn_operation_lock(self, parent_node_id: str) -> asyncio.Lock:
        key = self._spawn_operation_lock_key(parent_node_id)
        lock = self._spawn_operation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._spawn_operation_locks[key] = lock
        return lock

    @staticmethod
    def _spawn_round_superseded_reason(replacement_round_id: str) -> str:
        round_id = str(replacement_round_id or '').strip()
        if round_id:
            return f'{_SUPERSEDED_SPAWN_REASON_PREFIX}: {round_id}'
        return _SUPERSEDED_SPAWN_REASON_PREFIX

    @staticmethod
    def _spawn_exception_text(exc: BaseException | None) -> str:
        return f'Error: {describe_exception(exc)}'

    def _spawn_runtime_result(self, goal: str, *, error_text: str) -> SpawnChildResult:
        normalized_error_text = str(error_text or 'Error: runtime failure').strip() or 'Error: runtime failure'
        return SpawnChildResult(
            goal=goal,
            check_result=normalized_error_text,
            node_output='',
            node_output_summary='',
            node_output_ref='',
            failure_info=self._runtime_spawn_failure_info(normalized_error_text),
        )

    def _should_propagate_child_pipeline_cancellation(self, *, task_id: str, parent_node_id: str) -> bool:
        """只有**读到**明确的取消/终态/hold 才算真取消，读不到就不算。

        这道门在滞留道之前，所以它自己那两次库读同样不能外抛：在 except 体里抛出的异常会
        顶替原本那个 `CancelledError` 穿出 `_spawn_children`，落进 `react_loop` 的
        `except Exception` 变成 `Error executing spawn_child_nodes`——于是轮次带着活孩子
        提前返回、批后帧写清掉重放意图，正是本道要防的形状。返回 False 的语义本来就对：
        「说不清有没有人取消它」不是「有人取消它」，交给滞留道去等真答案。
        """
        try:
            task = self._store.get_task(task_id)
        except Exception as exc:
            logger.warning(
                'title=spawn_round_hold_probe_failed stage=cancel_check_task task={} error={}',
                str(task_id or '').strip(),
                describe_exception(exc),
            )
            return False
        if task is None:
            return False
        if bool(task.pause_requested) or bool(task.cancel_requested):
            return True
        if self._task_terminal_reason(task_id, task=task):
            return True
        try:
            parent = self._store.get_node(parent_node_id)
        except Exception as exc:
            logger.warning(
                'title=spawn_round_hold_probe_failed stage=cancel_check_parent node={} error={}',
                str(parent_node_id or '').strip(),
                describe_exception(exc),
            )
            return False
        if self._node_terminal_reason(
            parent,
            default_failed='parent node failed',
            default_success='parent node already completed',
        ):
            return True
        return False

    def _spawn_spec_payload(self, spec: SpawnChildSpec | dict[str, Any] | Any) -> dict[str, Any] | None:
        try:
            normalized_spec = spec if isinstance(spec, SpawnChildSpec) else SpawnChildSpec.model_validate(spec)
        except Exception:
            return None
        normalized_policy = normalize_execution_policy_metadata(
            normalized_spec.execution_policy.model_dump(mode='json')
        ).model_dump(mode='json')
        return {
            'goal': str(normalized_spec.goal or ''),
            'prompt': str(normalized_spec.prompt or ''),
            'execution_policy': normalized_policy,
            'acceptance_prompt': str(normalized_spec.acceptance_prompt or ''),
            'requires_acceptance': bool(self._requires_acceptance(normalized_spec)),
        }

    def _spawn_specs_fingerprint(self, specs: list[SpawnChildSpec] | list[dict[str, Any]] | Any) -> str:
        normalized_specs: list[dict[str, Any]] = []
        for item in list(specs or []):
            payload = self._spawn_spec_payload(item)
            if payload is None:
                return ''
            normalized_specs.append(payload)
        if not normalized_specs:
            return ''
        return json.dumps(normalized_specs, ensure_ascii=False, sort_keys=True)

    def _completed_successful_spawn_results(
        self,
        *,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        exclude_cache_key: str = '',
    ) -> list[SpawnChildResult] | None:
        operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(operations, dict):
            return None
        target_fingerprint = self._spawn_specs_fingerprint(specs)
        if not target_fingerprint:
            return None
        for cache_key, payload in reversed(list(operations.items())):
            if str(cache_key or '').strip() == str(exclude_cache_key or '').strip():
                continue
            if not isinstance(payload, dict) or not bool(payload.get('completed')):
                continue
            if self._spawn_specs_fingerprint(payload.get('specs') or []) != target_fingerprint:
                continue
            entries = [item for item in list(payload.get('entries') or []) if isinstance(item, dict)]
            if len(entries) != len(specs):
                continue
            if any(self._normalized_status(item.get('status')) != 'success' for item in entries):
                continue
            results = self._deliver_spawn_round_results(
                task_id=parent.task_id,
                parent_node_id=parent.node_id,
                cache_key=str(cache_key or ''),
                stage='reuse_completed_round',
                entries=entries,
                specs=specs,
            )
            if len(results) != len(specs):
                continue
            if any(result.failure_info is not None for result in results):
                continue
            return results
        return None

    async def run_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        """节点回合的唯一入口：同一节点同一时刻只允许一个回合在跑。

        判据选「后来者阻塞到第一回合结束」，不选「立刻返回一个 in-flight 回执」：
        回执要么得编一个 partial 结果（上层会把它当本回合的结论写盘、甚至把节点签成
        终态，而真正的回合还在跑），要么得抛一个异常（调用方会当成节点失败）。等，
        则后来者拿到的就是第一回合的真实结局：成功同值、暂停同型、取消同型。

        守卫按 node_id 登记；回合结束（含异常/取消）在 finally 摘除并唤醒等待方，
        所以终态或暂停之后的合法重入（验收打回、resume、恢复重派）照常开新回合——
        锁不会泄漏成永久阻塞。这条守卫只挡并发，不挡顺序重入。
        """
        gate = self._node_turn_gates.get(node_id)
        if gate is not None:
            await gate.done.wait()
            if gate.error is not None:
                # 复抛同一异常实例；控制类异常（NodePausedError/TaskPausedError/
                # CancelledError）的语义本来就该原样到达第二个等待者。
                raise gate.error
            if gate.result is None:
                raise RuntimeError(f'node turn finished without a result: {node_id}')
            return gate.result
        gate = _NodeTurnGate()
        self._node_turn_gates[node_id] = gate
        try:
            gate.result = await self._run_node_turn(task_id, node_id)
            return gate.result
        except BaseException as exc:
            gate.error = exc
            raise
        finally:
            self._node_turn_gates.pop(node_id, None)
            gate.done.set()

    async def _run_node_turn(self, task_id: str, node_id: str) -> NodeFinalResult:
        task = self._store.get_task(task_id)
        node = self._store.get_node(node_id)
        if task is None or node is None:
            raise ValueError(f'missing task or node: {task_id} / {node_id}')
        if node.node_kind == KIND_ACCEPTANCE:
            node = self._refresh_acceptance_node_metadata(task=task, node=node)
        if node.status in {STATUS_SUCCESS, STATUS_FAILED}:
            return self._result_from_record(node)
        # Cancellation has priority over a node pause so a canceled task cannot
        # leave a paused node and its dispatcher waiter stranded.
        if bool(getattr(task, 'cancel_requested', False)):
            return self._mark_failed(task_id, node.node_id, reason='canceled')
        if bool(getattr(node, 'pause_requested', False)) or bool(getattr(node, 'is_paused', False)):
            self._log_service.set_node_pause_state(
                task_id,
                node.node_id,
                pause_requested=True,
                is_paused=True,
                pause_reason=str(getattr(node, 'pause_reason', '') or 'manual'),
            )
            raise NodePausedError(task_id, node.node_id)
        if self._pause_requested(task_id):
            self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            raise TaskPausedError(task_id)
        terminal_reason = self._task_terminal_reason(task_id, task=task)
        if terminal_reason:
            return self._mark_failed(task_id, node.node_id, reason=terminal_reason)
        if task.cancel_requested:
            return self._mark_failed(task_id, node.node_id, reason='canceled')
        freeze_reason = self._final_acceptance_freeze_reason(task=task, node=node)
        if freeze_reason:
            self._log_service.refresh_task_view(task_id, mark_unread=True)
            return NodeFinalResult(
                status=STATUS_SUCCESS,
                delivery_status='partial',
                summary=freeze_reason,
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason='',
            )
        try:
            if self._context_preparer is not None:
                await self._await_with_runtime_marker(
                    task_id=task_id,
                    node_id=node.node_id,
                    marker='context_preparer',
                    awaitable=self._context_preparer(task=task, node=node),
                )
            tools = self._build_tools(task=task, node=node)
            runtime_context = self._runtime_context(task=task, node=node)
            react_state = await self._await_with_runtime_marker(
                task_id=task_id,
                node_id=node.node_id,
                marker='resume_react_state',
                awaitable=self._resume_react_state(task=task, node=node),
            )
            if str(react_state.get('message_source') or '').strip() == 'fresh':
                fresh_contract_payload = extract_node_dynamic_contract_payload(
                    list(react_state.get('messages') or [])
                )
                if isinstance(fresh_contract_payload, dict):
                    runtime_context = {
                        **runtime_context,
                        'fresh_node_dynamic_contract_payload': dict(fresh_contract_payload),
                    }
            pending_notification_ids = [
                str(item or '').strip()
                for item in list(react_state.get('pending_notification_ids') or [])
                if str(item or '').strip()
            ]
            pending_root_notice_ids = [
                str(item or '').strip()
                for item in list(react_state.get('pending_root_notice_ids') or [])
                if str(item or '').strip()
            ]
            inflight_notice_consumed = False

            def _consume_inflight_notices_once() -> None:
                nonlocal inflight_notice_consumed
                if inflight_notice_consumed:
                    return
                if not pending_notification_ids and not pending_root_notice_ids:
                    return
                inflight_notice_consumed = True
                self._consume_inflight_notice_ids(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    pending_notification_ids=pending_notification_ids,
                    pending_root_notice_ids=pending_root_notice_ids,
                )

            def _refresh_inflight_notices(
                current_message_history: list[dict[str, Any]],
                current_pending_request_delta_messages: list[dict[str, Any]],
            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
                nonlocal pending_notification_ids, pending_root_notice_ids, inflight_notice_consumed
                previous_notification_ids = list(pending_notification_ids)
                previous_root_notice_ids = list(pending_root_notice_ids)
                (
                    refreshed_history,
                    pending_notification_ids,
                    pending_root_notice_ids,
                    refreshed_request_delta_messages,
                ) = self._refresh_inflight_notice_messages(
                    task=task,
                    node=node,
                    message_history=current_message_history,
                    pending_notification_ids=pending_notification_ids,
                    pending_root_notice_ids=pending_root_notice_ids,
                    pending_request_delta_messages=current_pending_request_delta_messages,
                )
                if (
                    len(pending_notification_ids) > len(previous_notification_ids)
                    or len(pending_root_notice_ids) > len(previous_root_notice_ids)
                ):
                    inflight_notice_consumed = False
                return refreshed_history, refreshed_request_delta_messages

            runtime_context = {
                **runtime_context,
                'consume_inflight_notice_callback': _consume_inflight_notices_once,
                'refresh_inflight_notice_callback': _refresh_inflight_notices,
            }
            result = await self._await_with_runtime_marker(
                task_id=task_id,
                node_id=node.node_id,
                marker='react_loop.run',
                awaitable=self._react_loop.run(
                    task=task,
                    node=node,
                    messages=list(react_state.get('messages') or []),
                    request_body_seed_messages=list(react_state.get('request_body_seed_messages') or []),
                    request_seed_state=str(react_state.get('request_seed_state') or ''),
                    tools=tools,
                    tools_supplier=lambda current_task=task, current_node=node: self._build_tools(
                        task=current_task,
                        node=current_node,
                    ),
                    model_refs=self._model_refs_for(node),
                    model_refs_supplier=lambda current_node=node: self._model_refs_for(current_node),
                    model_routes=self._model_routes_for(node),
                    model_routes_supplier=lambda current_node=node: self._model_routes_for(current_node),
                    runtime_context=runtime_context,
                    max_iterations=self._max_iterations_for(node),
                    max_parallel_tool_calls=self._max_parallel_tool_calls_for(node),
                ),
            )
            _consume_inflight_notices_once()
            if str(result.failure_disposition or '').strip().lower() == 'pause':
                latest_task = self._store.get_task(task_id) or task
                terminal_reason = self._task_terminal_reason(task_id, task=latest_task)
                if bool(getattr(latest_task, 'cancel_requested', False)) or terminal_reason:
                    return self._mark_failed(task_id, node.node_id, reason=terminal_reason or 'canceled')
                if self._pause_requested(task_id):
                    self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
                    raise TaskPausedError(task_id)
                if self._node_pause_requested(task_id, node.node_id):
                    self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
                    self._mark_node_paused(task_id, node.node_id)
                    raise NodePausedError(task_id, node.node_id)
                error_text = result.failure_text
                self._log_service.append_task_error_log(
                    task_id,
                    node.node_id,
                    error_text=error_text,
                    node_title=node.goal,
                )
                self._mark_node_paused(task_id, node.node_id, reason='error', remark=error_text)
                # 非根节点的异常暂停必须升级父节点：心跳只报根节点，漏掉这一跳
                # 就会让协议修复/预检类暂停在子树上静默悬空。
                self.notify_parent_abnormal_pause(
                    task_id=task_id,
                    node_id=node.node_id,
                    reason=error_text,
                )
                raise NodePausedError(task_id, node.node_id)
            if self._pause_requested(task_id):
                self._mark_finished(task_id, node.node_id, result)
                self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
                raise TaskPausedError(task_id)
            if self._node_pause_requested(task_id, node.node_id):
                self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
                self._mark_node_paused(task_id, node.node_id)
                raise NodePausedError(task_id, node.node_id)
            terminal_reason = self._task_terminal_reason(task_id, task=task)
            if terminal_reason:
                return self._mark_failed(task_id, node.node_id, reason=terminal_reason)
            if (self._store.get_task(task_id) or task).cancel_requested:
                return self._mark_failed(task_id, node.node_id, reason='canceled')
            result = await self._maybe_start_acceptance_handshake(task=task, node=node, result=result) or result
            if str(result.delivery_status or '').strip() == 'partial':
                self._log_service.refresh_task_view(task_id, mark_unread=True)
                return result
            return self._mark_finished(task_id, node.node_id, result)
        except TaskPausedError:
            self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
            raise
        except NodePausedError:
            self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
            # 磁盘治理（P0）：pause 记录是表现层写入，磁盘满时不得阻断 NodePausedError 传播。
            try:
                self._mark_node_paused(task_id, node.node_id)
            except Exception:
                pass
            raise
        except asyncio.CancelledError:
            latest_task = self._store.get_task(task_id)
            if latest_task is not None and bool(getattr(latest_task, 'cancel_requested', False)):
                return self._mark_failed(task_id, node.node_id, reason='canceled')
            if self._pause_requested(task_id):
                self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
                self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
                raise TaskPausedError(task_id)
            if self._node_pause_requested(task_id, node.node_id):
                self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
                try:
                    self._mark_node_paused(task_id, node.node_id)
                except Exception:
                    pass
                raise NodePausedError(task_id, node.node_id)
            # 引擎级中断（worker 进程退出收尾、无标志的杂散取消）：绝不落
            # failed/canceled 终态——节点保持 in_progress，任务留给下一个
            # worker 启动的 _recover_interrupted_task 恢复重排（2026-09-16
            # task:eacd0f0467b7 事故：有序退出把在飞节点终态化成 canceled，
            # 抢先于启动恢复，任务永久丢失）。用户取消/暂停已在上方分支解决
            # （两者都先落 cancel_requested / pause_requested 标志再取消执行器）。
            self._flush_latest_valid_result_if_paused(task_id=task_id, node_id=node.node_id)
            raise
        except Exception as exc:
            if isinstance(exc, MemoryError):
                self._capture_memory_error_diagnostics(task_id=task_id, node_id=node.node_id, exc=exc)
            latest_task = self._store.get_task(task_id)
            terminal_reason = self._task_terminal_reason(task_id, task=latest_task or task)
            if latest_task is not None and (bool(getattr(latest_task, 'cancel_requested', False)) or terminal_reason):
                return self._mark_failed(task_id, node.node_id, reason=terminal_reason or 'canceled')
            text = describe_exception(exc)
            # 磁盘治理（P0）：error_log 与 pause 记录各自独立降级——2026-09-09 事故中
            # 这两行在磁盘满时二次写盘再炸，导致 31 个节点连锁 error-pause。
            # 记录失败绝不阻断 NodePausedError 的控制流（先例：react_loop "Error-history
            # persistence must never break the ReAct loop"）。
            self._persist_error_and_pause_best_effort(
                task_id=task_id,
                node_id=node.node_id,
                text=text,
                node_goal=str(getattr(node, 'goal', '') or ''),
            )
            raise NodePausedError(task_id, node.node_id) from exc
        finally:
            if self._context_finalizer is not None:
                self._context_finalizer(task=task, node=node)

    def _set_runtime_await_marker(
        self,
        *,
        task_id: str,
        node_id: str,
        marker: str,
        started_at: str = '',
        publish_snapshot: bool = True,
    ) -> None:
        normalized_marker = str(marker or '').strip()
        normalized_started_at = str(started_at or '').strip()

        def _mutate(frame: dict[str, Any]) -> dict[str, Any]:
            next_frame = dict(frame or {})
            next_frame['await_marker'] = normalized_marker
            next_frame['await_started_at'] = normalized_started_at if normalized_marker else ''
            return next_frame

        self._log_service.update_frame(task_id, node_id, _mutate, publish_snapshot=publish_snapshot)

    async def _await_with_runtime_marker(self, *, task_id: str, node_id: str, marker: str, awaitable: Any) -> Any:
        started_at = datetime.now().isoformat()
        self._set_runtime_await_marker(
            task_id=task_id,
            node_id=node_id,
            marker=marker,
            started_at=started_at,
        )
        try:
            return await awaitable
        finally:
            if self._log_service.read_runtime_frame(task_id, node_id) is not None:
                # 清空标记只落帧不推送：诊断读的是帧行，下一次进入 await 会带着新标记推。
                self._set_runtime_await_marker(
                    task_id=task_id,
                    node_id=node_id,
                    marker='',
                    publish_snapshot=False,
                )

    async def _run_nested_node(self, task_id: str, node_id: str) -> NodeFinalResult:
        executor = self.nested_node_executor
        if callable(executor):
            return await executor(task_id, node_id)
        return await self.run_node(task_id, node_id)

    async def _resume_react_state(self, *, task, node: NodeRecord) -> dict[str, Any]:
        notifications = self._pending_node_notifications(task_id=task.task_id, node_id=node.node_id)
        pending_root_notice_records = self._pending_root_notice_records(node=node)
        request_body_seed_messages, request_seed_state = self._latest_actual_request_seed_messages(
            task=task,
            node=node,
        )
        if notifications or pending_root_notice_records:
            if self._pending_notice_waits_for_children(node=node):
                frame = self._log_service.read_runtime_frame(task.task_id, node.node_id) or {}
                if isinstance(frame.get('messages'), list) and frame.get('messages'):
                    return {
                        'messages': list(frame.get('messages') or []),
                        'message_source': 'restored_frame',
                        'request_body_seed_messages': request_body_seed_messages,
                        'request_seed_state': request_seed_state,
                    }
                return {
                    'messages': await self._build_messages(task=task, node=node),
                    'message_source': 'fresh',
                    'request_body_seed_messages': request_body_seed_messages,
                    'request_seed_state': request_seed_state,
                }
            messages = await self._notice_resume_messages(task=task, node=node)
            messages = self._append_notice_messages(messages=messages, notices=notifications)
            messages = self._append_notice_messages(messages=messages, notices=pending_root_notice_records)
            self._close_active_round_for_message_consumption(task_id=task.task_id, node_id=node.node_id)
            return {
                'messages': messages,
                'message_source': 'notice',
                'pending_notification_ids': [str(item.notification_id or '').strip() for item in notifications],
                'pending_root_notice_ids': [
                    str(item.get('notification_id') or '').strip()
                    for item in list(pending_root_notice_records or [])
                    if str(item.get('notification_id') or '').strip()
                ],
                'request_body_seed_messages': request_body_seed_messages,
                'request_seed_state': request_seed_state,
            }
        frame = self._log_service.read_runtime_frame(task.task_id, node.node_id) or {}
        if isinstance(frame.get('messages'), list) and frame.get('messages'):
            return {
                'messages': list(frame.get('messages') or []),
                'message_source': 'restored_frame',
                'request_body_seed_messages': request_body_seed_messages,
                'request_seed_state': request_seed_state,
            }
        return {
            'messages': await self._build_messages(task=task, node=node),
            'message_source': 'fresh',
            'request_body_seed_messages': request_body_seed_messages,
            'request_seed_state': request_seed_state,
        }

    def _pending_node_notifications(self, *, task_id: str, node_id: str) -> list[Any]:
        # 三态：delivered（待处理）与 consumed-未merged（控制/决策回合已处理、
        # 内容等待恢复路径真正并入）都算"待并入"，恢复与在途刷新都要能看到。
        return [
            item
            for item in list(self._store.list_task_node_notifications(task_id, node_id) or [])
            if _notification_awaits_injection(item)
        ]

    def _notifications_by_ids(self, *, task_id: str, node_id: str, notification_ids: list[str]) -> list[dict[str, Any]]:
        ids = {str(item or '').strip() for item in list(notification_ids or []) if str(item or '').strip()}
        if not ids:
            return []
        items: list[dict[str, Any]] = []
        for notification in list(self._store.list_task_node_notifications(task_id, node_id) or []):
            notification_id = str(notification.notification_id or '').strip()
            if notification_id not in ids:
                continue
            items.append(notification.model_dump(mode='json'))
        return items

    @staticmethod
    def _message_list_from_payload(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ('request_messages', 'model_messages', 'messages'):
                value = payload.get(key)
                if isinstance(value, list):
                    return [dict(item) for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _request_messages_from_payload(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            value = payload.get('request_messages')
            if isinstance(value, list):
                return [dict(item) for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _provider_input_messages_from_payload(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, dict):
            provider_request_body = payload.get('provider_request_body')
            if isinstance(provider_request_body, dict):
                value = provider_request_body.get('input')
                if isinstance(value, list):
                    return [dict(item) for item in value if isinstance(item, dict)]
        return []

    # seed 状态随种子一起返回，供 react_loop 第一跳 adoption 落 request_seed_source 诊断；
    # ok / legacy_provider_shape 之外的状态都会映射为 fallback_* 的投影重组装轮。
    def _latest_actual_request_seed_messages(
        self,
        *,
        task,
        node: NodeRecord,
    ) -> tuple[list[dict[str, Any]], str]:
        frame = self._log_service.read_runtime_frame(task.task_id, node.node_id) or {}
        metadata = dict(node.metadata or {})
        state = 'no_artifact'
        for ref in (
            str(frame.get('actual_request_ref') or '').strip(),
            str(metadata.get('latest_runtime_actual_request_ref') or '').strip(),
        ):
            if not ref:
                continue
            resolved = str(self._log_service.resolve_content_ref(ref) or '').strip()
            if not resolved:
                state = 'unreadable'
                continue
            try:
                parsed = json.loads(resolved)
            except Exception:
                parsed = None
            if not isinstance(parsed, dict):
                state = 'parse_fail'
                continue
            persistence_mode = str(parsed.get('artifact_persistence_mode') or '').strip()
            if persistence_mode and persistence_mode != 'full':
                # memory/disk guard 降级 artifact 不携带可回灌的请求体；
                # 显式标记降级而不是静默返回空种子。
                state = 'degraded'
                continue
            # 内部形态 request_messages 优先：/responses wire 项（function_call 等）
            # 无 role，会被 adapter _convert_messages 静默丢弃，不能回灌；
            # provider input 仅作 legacy 兜底，wire 残片由 adoption 头探针拦截。
            message_list = self._request_messages_from_payload(parsed)
            if message_list:
                if str(parsed.get('projection_kind') or '').strip() == 'token_compacted':
                    # 回灌到的是压缩投影：摘要块是一条 assistant 文本，模型会把它读成
                    # 自己说过的话。P0 只留痕不改状态，采纳基线那批再进 fallback_* 通道。
                    logger.info(
                        f'node seed resolved to token-compacted projection: {node.node_id} '
                        f'messages={len(message_list)} ref={ref}'
                    )
                return message_list, 'ok'
            message_list = self._provider_input_messages_from_payload(parsed)
            if message_list:
                return message_list, 'legacy_provider_shape'
            state = 'empty'
        return [], state

    async def _notice_resume_messages(self, *, task, node: NodeRecord) -> list[dict[str, Any]]:
        frame = self._log_service.read_runtime_frame(task.task_id, node.node_id) or {}
        if isinstance(frame.get('messages'), list) and frame.get('messages'):
            return [dict(item) for item in list(frame.get('messages') or []) if isinstance(item, dict)]
        metadata = dict(node.metadata or {})
        ref = str(metadata.get('latest_runtime_messages_ref') or '').strip()
        if ref:
            resolved = str(self._log_service.resolve_content_ref(ref) or '').strip()
            if resolved:
                try:
                    parsed = json.loads(resolved)
                except Exception:
                    parsed = None
                message_list = self._message_list_from_payload(parsed)
                if message_list:
                    return message_list
        return await self._build_messages(task=task, node=node)

    async def _base_messages_for_reactivated_or_live_node(self, *, task, node: NodeRecord) -> list[dict[str, Any]]:
        frame = self._log_service.read_runtime_frame(task.task_id, node.node_id) or {}
        if isinstance(frame.get('messages'), list) and frame.get('messages'):
            return [dict(item) for item in list(frame.get('messages') or []) if isinstance(item, dict)]
        metadata = dict(node.metadata or {})
        for key in ('latest_runtime_actual_request_ref', 'latest_runtime_messages_ref'):
            ref = str(metadata.get(key) or '').strip()
            if not ref:
                continue
            resolved = str(self._log_service.resolve_content_ref(ref) or '').strip()
            if not resolved:
                continue
            try:
                parsed = json.loads(resolved)
            except Exception:
                parsed = None
            if key == 'latest_runtime_actual_request_ref':
                message_list = self._provider_input_messages_from_payload(parsed)
                if message_list:
                    return message_list
            message_list = self._message_list_from_payload(parsed)
            if message_list:
                return message_list
        return await self._build_messages(task=task, node=node)

    def _append_mailbox_messages(self, *, messages: list[dict[str, Any]], notifications: list[Any]) -> list[dict[str, Any]]:
        appended = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        for notification in list(notifications or []):
            text = str(getattr(notification, 'message', '') or '').strip()
            if not text:
                continue
            appended.append({'role': 'user', 'content': text})
        return appended

    def _append_notice_messages(self, *, messages: list[dict[str, Any]], notices: list[Any]) -> list[dict[str, Any]]:
        appended = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
        for notice in list(notices or []):
            if isinstance(notice, dict):
                text = str(notice.get('message') or '').strip()
            else:
                text = str(getattr(notice, 'message', '') or '').strip()
            if not text:
                continue
            appended.append({'role': 'user', 'content': text})
        return appended

    @staticmethod
    def _notice_delta_messages(*, notices: list[Any]) -> list[dict[str, Any]]:
        delta_messages: list[dict[str, Any]] = []
        for notice in list(notices or []):
            if isinstance(notice, dict):
                text = str(notice.get('message') or '').strip()
            else:
                text = str(getattr(notice, 'message', '') or '').strip()
            if not text:
                continue
            delta_messages.append({'role': 'user', 'content': text})
        return delta_messages

    def _refresh_inflight_notice_messages(
        self,
        *,
        task,
        node: NodeRecord,
        message_history: list[dict[str, Any]],
        pending_notification_ids: list[str],
        pending_root_notice_ids: list[str],
        pending_request_delta_messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str], list[str], list[dict[str, Any]]]:
        updated_history = [dict(item) for item in list(message_history or []) if isinstance(item, dict)]
        updated_notification_ids = [
            str(item or '').strip()
            for item in list(pending_notification_ids or [])
            if str(item or '').strip()
        ]
        updated_root_notice_ids = [
            str(item or '').strip()
            for item in list(pending_root_notice_ids or [])
            if str(item or '').strip()
        ]
        updated_request_delta_messages = [
            dict(item)
            for item in list(pending_request_delta_messages or [])
            if isinstance(item, dict)
        ]
        latest_node = self._store.get_node(str(getattr(node, 'node_id', '') or '').strip()) or node
        if latest_node is None:
            return (
                updated_history,
                updated_notification_ids,
                updated_root_notice_ids,
                updated_request_delta_messages,
            )
        if self._pending_notice_waits_for_children(node=latest_node):
            return (
                updated_history,
                updated_notification_ids,
                updated_root_notice_ids,
                updated_request_delta_messages,
            )
        known_notification_ids = set(updated_notification_ids)
        known_root_notice_ids = set(updated_root_notice_ids)
        new_notifications = [
            item
            for item in self._pending_node_notifications(task_id=task.task_id, node_id=latest_node.node_id)
            if str(getattr(item, 'notification_id', '') or '').strip() not in known_notification_ids
        ]
        new_root_notice_records = [
            dict(item)
            for item in list(self._pending_root_notice_records(node=latest_node) or [])
            if str(item.get('notification_id') or '').strip() not in known_root_notice_ids
        ]
        if not new_notifications and not new_root_notice_records:
            return (
                updated_history,
                updated_notification_ids,
                updated_root_notice_ids,
                updated_request_delta_messages,
            )
        updated_history = self._append_notice_messages(messages=updated_history, notices=new_notifications)
        updated_history = self._append_notice_messages(messages=updated_history, notices=new_root_notice_records)
        # notice 无条件镜像进 delta：链路径（previous+delta+tail）请求本体不含投影，
        # notice 只经 delta 进入请求一份；探针失败回退投影重组装时 delta 被丢弃，
        # notice 只在烤入的投影里一份——两条路径都恰好一份。delta 为空时不再
        # 跳过（旧门会让工具轮之间到达的 notice 触发投影重组装小请求）。
        updated_request_delta_messages.extend(
            self._notice_delta_messages(notices=[*list(new_notifications), *list(new_root_notice_records)])
        )
        updated_notification_ids.extend(
            str(getattr(item, 'notification_id', '') or '').strip()
            for item in list(new_notifications or [])
            if str(getattr(item, 'notification_id', '') or '').strip()
        )
        updated_root_notice_ids.extend(
            str(item.get('notification_id') or '').strip()
            for item in list(new_root_notice_records or [])
            if str(item.get('notification_id') or '').strip()
        )
        self._close_active_round_for_message_consumption(
            task_id=task.task_id,
            node_id=latest_node.node_id,
        )
        return (
            updated_history,
            updated_notification_ids,
            updated_root_notice_ids,
            updated_request_delta_messages,
        )

    def _consume_node_notifications(self, *, task_id: str, node_id: str, notification_ids: list[str]) -> None:
        ids = {str(item or '').strip() for item in list(notification_ids or []) if str(item or '').strip()}
        if not ids:
            return
        merged_at = now_iso()
        for notification in list(self._store.list_task_node_notifications(task_id, node_id) or []):
            if str(notification.notification_id or '').strip() not in ids:
                continue
            # 该路径对应"内容真正并入模型上下文"（在途刷新/恢复注入后的消费），
            # consumed_at 保留控制回合首次处理时间（若有），merged_at 落在并入时刻。
            self._store.upsert_task_node_notification(
                notification.model_copy(
                    update={
                        'status': 'consumed',
                        'consumed_at': str(notification.consumed_at or '').strip() or merged_at,
                        'merged_at': merged_at,
                    }
                )
            )

    def _record_consumed_notice_context(self, *, node_id: str, notifications: list[dict[str, Any]]) -> None:
        consumed_at = now_iso()

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            context = normalize_append_notice_context(metadata.get(APPEND_NOTICE_CONTEXT_KEY))
            metadata[APPEND_NOTICE_CONTEXT_KEY] = record_consumed_notifications(
                context,
                notifications=list(notifications or []),
                consumed_at=consumed_at,
                merged_at=consumed_at,
            )
            return metadata

        self._log_service.update_node_metadata(node_id, _mutate)

    def _supersede_consumed_notices_from_source(self, *, node_id: str, source_node_id: str) -> None:
        """作废目标节点上下文中来自指定来源的已消费通知记录。

        通知消息本身保留在历史与通知台账中（追加式消息不被覆盖）；这里只把
        append_notice_context 里的记录标记为 superseded，使已处理的激活指令
        （阻塞核验激活、打回续验通知等）不再被通知尾窗或压缩段重新注入，
        避免过期指令误导后续轮次。
        """
        normalized_node_id = str(node_id or '').strip()
        normalized_source = str(source_node_id or '').strip()
        if not normalized_node_id or not normalized_source:
            return

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            context = normalize_append_notice_context(metadata.get(APPEND_NOTICE_CONTEXT_KEY))
            records = list(context.get('notice_records') or [])
            if not any(
                not str(item.get('superseded_at') or '').strip()
                and str(item.get('source_node_id') or '').strip() == normalized_source
                for item in records
            ):
                return metadata
            metadata[APPEND_NOTICE_CONTEXT_KEY] = supersede_append_notice_records(
                context,
                source_node_id=normalized_source,
                superseded_at=now_iso(),
            )
            return metadata

        self._log_service.update_node_metadata(normalized_node_id, _mutate)

    def _consume_inflight_notice_ids(
        self,
        *,
        task_id: str,
        node_id: str,
        pending_notification_ids: list[str],
        pending_root_notice_ids: list[str],
    ) -> None:
        if pending_notification_ids:
            pending_notifications = self._notifications_by_ids(
                task_id=task_id,
                node_id=node_id,
                notification_ids=pending_notification_ids,
            )
            if pending_notifications:
                self._record_consumed_notice_context(
                    node_id=node_id,
                    notifications=pending_notifications,
                )
            self._consume_node_notifications(
                task_id=task_id,
                node_id=node_id,
                notification_ids=pending_notification_ids,
            )
        if pending_root_notice_ids:
            pending_root_notifications = self._pending_root_notice_records_by_ids(
                node_id=node_id,
                notification_ids=pending_root_notice_ids,
            )
            if pending_root_notifications:
                self._record_consumed_notice_context(
                    node_id=node_id,
                    notifications=pending_root_notifications,
                )
            self._consume_pending_root_notice_records(
                node_id=node_id,
                notification_ids=pending_root_notice_ids,
            )
        self._clear_pending_notice_state_if_idle(node_id=node_id)
        # 并入上下文 = 徽标该减了：主动补一帧 node.patch（含 pending_notice_count），
        # 不再等下一次整树快照。
        publish_node_state = getattr(self._log_service, 'publish_node_state', None)
        if callable(publish_node_state):
            publish_node_state(task_id, node_id)

    def _required_acceptance_node(self, *, task, node: NodeRecord) -> NodeRecord | None:
        if str(getattr(node, 'node_kind', '') or '').strip().lower() != KIND_EXECUTION:
            return None
        is_root = str(getattr(node, 'node_id', '') or '').strip() == str(getattr(task, 'root_node_id', '') or '').strip()
        if not is_root:
            # 非根执行节点的验收节点在派生时已提前创建并挂在它下面（设计偏差 4：
            # 保留提前创建）。旧模型只让根节点走握手，子节点的"执行→验收"循环由
            # `_run_child_pipeline` 私下驱动；拆掉那条循环后，子节点必须和根一样从
            # 提交进入 `waiting_acceptance`，否则验收节点没有任何东西会派发它。
            for candidate in list(self._store.list_children(node.node_id) or []):
                if str(getattr(candidate, 'node_kind', '') or '').strip().lower() != KIND_ACCEPTANCE:
                    continue
                metadata = dict(getattr(candidate, 'metadata', {}) or {})
                accepted_id = str(metadata.get('accepted_node_id') or '').strip()
                if accepted_id and accepted_id != str(node.node_id or '').strip():
                    continue
                return candidate
            return None
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        if not bool(final_acceptance.required):
            return None
        acceptance_node_id = str(final_acceptance.node_id or '').strip()
        acceptance = self._store.get_node(acceptance_node_id) if acceptance_node_id else None
        if acceptance is None:
            for candidate in list(self._store.list_children(node.node_id) or []):
                if str(getattr(candidate, 'node_kind', '') or '').strip().lower() != KIND_ACCEPTANCE:
                    continue
                if not bool(dict(getattr(candidate, 'metadata', {}) or {}).get('final_acceptance')):
                    continue
                acceptance = candidate
                break
        if acceptance is None:
            return None
        if str(getattr(acceptance, 'task_id', '') or '').strip() != str(getattr(task, 'task_id', '') or '').strip():
            return None
        if str(getattr(acceptance, 'node_kind', '') or '').strip().lower() != KIND_ACCEPTANCE:
            return None
        return acceptance

    # 握手里唯一表示"已登记一次待裁定的 final 提交"的两个状态。其余都在冻结侧：
    # `idle`＝从没提交过，`waiting_execution_retry`＝已被打回、正在重做（此时派验
    # 拿的是上一轮的旧指针），`accepted` / `rejected_terminal`＝裁定已经落过。
    _PENDING_VERDICT_STATES = frozenset({
        ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
    })

    def _final_acceptance_freeze_reason(self, *, task, node: NodeRecord) -> str:
        """验收抢跑闸门：被检验节点没有一次**待裁定的闭合提交**时，验收不开回合。

        不变式与节点状态、暂停与否无关：对方还在执行，它的验收节点就不允许激活。
        闭合只有两种读法——对方已终态；或握手处于待裁定状态且登记了可解析的
        `latest_execution_result_ref`。判据落在握手状态机本身而不是登记位：三条派验
        车道（根最终验收、spawn 首轮、阻塞核验）都在派发前写这两个状态之一，而重做中的
        节点写的是 `waiting_execution_retry`。违反它的代价实测过：验收节点与子节点同批
        物化，bootstrap 定稿时必然是空交接（`_ensure_spawn_acceptance_node`），此时开
        回合只能对"什么都没有"下结论——实盘 37 个被误恢复的验收节点里 16 个当场开跑、
        150 次模型调用，7 份判词全是「交付物不存在」，其中 2 份是 `failed + blocked`
        终局形态，会在对方真提交时把它直接打死。

        只按根节点的最终验收另有一条：执行节点还压着未消费通知，需等它消费完并重提交。
        """
        if str(getattr(node, 'node_kind', '') or '').strip().lower() != KIND_ACCEPTANCE:
            return ''
        execution = self._accepted_execution_node(task_id=task.task_id, acceptance=node)
        if execution is None:
            return ''
        execution_node_id = str(getattr(execution, 'node_id', '') or '').strip()
        if self._normalized_status(getattr(execution, 'status', '')) in {STATUS_SUCCESS, STATUS_FAILED}:
            return ''
        handshake = normalize_acceptance_handshake((execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY))
        if (
            str(handshake.get('state') or '').strip() not in self._PENDING_VERDICT_STATES
            or not str(handshake.get('latest_execution_result_ref') or '').strip()
        ):
            return (
                f'{ACCEPTANCE_FREEZE_PREFIX}被检验执行节点没有待裁定的闭合提交，'
                '需待其提交 final 结果并登记交接后再继续核验。'
            )
        if execution_node_id != str(getattr(task, 'root_node_id', '') or '').strip():
            return ''
        pending_node_ids = set(self.nodes_with_pending_distribution_notices(task_id=task.task_id))
        if execution_node_id not in pending_node_ids:
            return ''
        return (
            f'{FINAL_ACCEPTANCE_FREEZE_PREFIX}被检验执行节点仍有未消费通知，'
            '需待其消费通知并重新提交后再继续核验。'
        )

    def resumable_final_acceptance_node_id(self, task_id: str) -> str:
        """握手仍等它的根最终验收节点 id；不构成承诺时返回空串。

        验收节点在回合第一步就消费并合并自己的交接通知，从此离开通知账本
        （`nodes_with_pending_distribution_notices` 只数未合并的通知）。它的半截
        回合因此两头无着：孤儿收尸只管 `spawn_owner_kind='child'` 的执行节点，
        notice-resume 又只认通知账本。进程一退，`run_task` 便落回
        `dispatcher.execute_node(root)`，被检验的节点重跑一整轮新回合，而它等的那份
        裁定其实一直挂在握手状态里（事故复盘：task:c7f1dbfae6e2，验收节点 15:48
        中断后被弃三小时）。

        握手状态本身就是这半截回合的持久承诺，所以这里只读既有状态，不新增任何
        持久字段。范围只覆盖根的最终验收：子节点的验收由子管线自己驱动。
        是否"已无人执行"要读 dispatcher entry，那是调用方（`TaskActorService.
        _resume_inflight_final_acceptance`）的一跳。
        """
        task = self._store.get_task(task_id)
        if task is None:
            return ''
        root_node_id = str(getattr(task, 'root_node_id', '') or '').strip()
        root = self._store.get_node(root_node_id) if root_node_id else None
        if root is None or self._normalized_status(root.status) in {STATUS_SUCCESS, STATUS_FAILED}:
            return ''
        handshake = normalize_acceptance_handshake((root.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY))
        if str(handshake.get('state') or '').strip() not in {
            ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
        }:
            return ''
        acceptance = self._required_acceptance_node(task=task, node=root)
        if acceptance is None:
            return ''
        node_id = str(getattr(acceptance, 'node_id', '') or '').strip()
        if not node_id:
            return ''
        named_node_id = str(handshake.get('acceptance_node_id') or '').strip()
        if named_node_id and named_node_id != node_id:
            # 握手点名的是另一份验收节点（已被重建）：复活旧节点只会产出一份
            # 裁定不了当前提交的假验收。
            return ''
        if self._normalized_status(acceptance.status) in {STATUS_SUCCESS, STATUS_FAILED}:
            return ''
        if bool(getattr(acceptance, 'pause_requested', False)) or bool(getattr(acceptance, 'is_paused', False)):
            return ''
        if self._final_acceptance_freeze_reason(task=task, node=acceptance):
            # 冻结中的验收一被派发就返回 success + partial，而
            # _handle_acceptance_node_result 的首个分支只看 status==success，会把这份
            # "还没验"读成一次通过（握手直接落 accepted）。所以这里绝不复活冻结中的验收。
            return ''
        return node_id

    def _persist_acceptance_candidate_result(self, *, task_id: str, node_id: str, result: NodeFinalResult) -> str:
        self._persist_result_payload(task_id, node_id, result)
        latest = self._log_service.ensure_node_result_payload_externalized(task_id, node_id) or self._store.get_node(node_id)
        if latest is None:
            return ''
        return str((latest.metadata or {}).get('result_payload_ref') or '').strip()

    def _externalized_result_payload_ref(self, *, task_id: str, node: NodeRecord) -> str:
        """裁决正文完整副本所在的 ref。外置不可用时返回空串，由 `node_message_body` 如实标注。"""
        latest = self._log_service.ensure_node_result_payload_externalized(task_id, node.node_id) or self._store.get_node(node.node_id)
        if latest is None:
            return ''
        return str((latest.metadata or {}).get('result_payload_ref') or '').strip()

    @staticmethod
    def _acceptance_feedback_text(result: NodeFinalResult) -> str:
        """打回正文 = 不通过原因 + 逐条修复要求；blocking_reason 属于 blocked 形态，此处不读。"""
        parts: list[str] = []
        conclusion = str(result.summary or result.answer or '').strip()
        if conclusion:
            parts.append(conclusion)
        remaining_work = [
            str(item or '').strip()
            for item in list(result.remaining_work or [])
            if str(item or '').strip()
        ]
        if remaining_work:
            parts.append('修复要求：\n' + '\n'.join(f'- {item}' for item in remaining_work))
        text = '\n\n'.join(parts).strip()
        return text or str(result.blocking_reason or '').strip()

    def _accepted_execution_node(self, *, task_id: str, acceptance: NodeRecord) -> NodeRecord | None:
        metadata = dict(getattr(acceptance, 'metadata', {}) or {})
        accepted_node_id = str(metadata.get('accepted_node_id') or acceptance.parent_node_id or '').strip()
        if not accepted_node_id:
            return None
        execution = self._store.get_node(accepted_node_id)
        if execution is None:
            return None
        if str(getattr(execution, 'task_id', '') or '').strip() != str(task_id or '').strip():
            return None
        if str(getattr(execution, 'node_kind', '') or '').strip().lower() != KIND_EXECUTION:
            return None
        return execution

    def _acceptance_updates_task_final_acceptance(self, *, task, execution: NodeRecord | None, acceptance: NodeRecord) -> bool:
        if execution is None:
            return False
        if str(getattr(execution, 'node_id', '') or '').strip() != str(getattr(task, 'root_node_id', '') or '').strip():
            return False
        metadata = dict(getattr(acceptance, 'metadata', {}) or {})
        if bool(metadata.get('final_acceptance')):
            return True
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        return str(final_acceptance.node_id or '').strip() == str(getattr(acceptance, 'node_id', '') or '').strip()

    def _set_task_final_acceptance_state(
        self,
        *,
        task_id: str,
        acceptance_node_id: str,
        status: str,
        final_execution_output: str | None = None,
    ) -> None:
        normalized_node_id = str(acceptance_node_id or '').strip()
        normalized_status = str(status or '').strip().lower()
        normalized_output = None if final_execution_output is None else str(final_execution_output or '').strip()

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            final_acceptance = normalize_final_acceptance_metadata(metadata.get('final_acceptance')).model_dump(mode='json')
            if normalized_node_id:
                final_acceptance['node_id'] = normalized_node_id
            if normalized_status:
                final_acceptance['status'] = normalized_status
            metadata['final_acceptance'] = final_acceptance
            if normalized_output is None:
                return metadata
            if normalized_output:
                metadata['final_execution_output'] = normalized_output
            else:
                metadata.pop('final_execution_output', None)
            return metadata

        self._log_service.update_task_metadata(task_id, _mutate, mark_unread=True)

    def _update_execution_acceptance_handshake(
        self,
        *,
        node_id: str,
        state: str,
        acceptance_node_id: str,
        rejection_count: int,
        latest_execution_result_ref: str,
        latest_execution_result_summary: str,
        latest_rejection_feedback_ref: str,
        latest_rejection_feedback_summary: str,
        latest_execution_submission_id: str | None = None,
    ) -> None:
        updated_at = now_iso()

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            current = normalize_acceptance_handshake(metadata.get(ACCEPTANCE_HANDSHAKE_KEY))
            metadata[ACCEPTANCE_HANDSHAKE_KEY] = set_acceptance_handshake_state(
                current,
                state=state,
                acceptance_node_id=str(acceptance_node_id or '').strip(),
                rejection_count=int(rejection_count or 0),
                latest_execution_result_ref=str(latest_execution_result_ref or '').strip(),
                latest_execution_result_summary=str(latest_execution_result_summary or '').strip(),
                latest_rejection_feedback_ref=str(latest_rejection_feedback_ref or '').strip(),
                latest_rejection_feedback_summary=str(latest_rejection_feedback_summary or '').strip(),
                updated_at=updated_at,
                latest_execution_submission_id=latest_execution_submission_id,
            )
            return metadata

        self._log_service.update_node_metadata(node_id, _mutate)

    def _bump_notice_interrupt_count(self, node_id: str) -> None:
        """记一次"因定向通知打断验收"的事实计数。

        这个数只被读出来渲染进决策束，不参与任何放行判断：返工圈的历史必须有人替模型
        记住（它每回合只看得到手里这条通知），但重做与否仍是模型的选择。
        """

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            raw = metadata.get(NOTICE_RETRY_STATE_KEY)
            state = dict(raw) if isinstance(raw, dict) else {}
            try:
                count = max(0, int(state.get('notice_interrupts') or 0))
            except (TypeError, ValueError):
                count = 0
            state['notice_interrupts'] = count + 1
            state['last_interrupt_at'] = now_iso()
            metadata[NOTICE_RETRY_STATE_KEY] = state
            return metadata

        self._log_service.update_node_metadata(node_id, _mutate)

    @staticmethod
    def _notice_retry_state(metadata: dict[str, Any]) -> dict[str, Any]:
        raw = metadata.get(NOTICE_RETRY_STATE_KEY)
        state = raw if isinstance(raw, dict) else {}
        try:
            count = max(0, int(state.get('notice_interrupts') or 0))
        except (TypeError, ValueError):
            count = 0
        return {
            'notice_interrupts': count,
            'last_interrupt_at': str(state.get('last_interrupt_at') or '').strip(),
        }

    def _reactivate_node_for_retry(self, *, task_id: str, node_id: str) -> NodeRecord | None:
        self._stash_node_verdict_history(node_id)
        updated = self._store.update_node(
            node_id,
            lambda record: record.model_copy(
                update={
                    'status': 'in_progress',
                    'final_output': '',
                    'final_output_ref': '',
                    'failure_reason': '',
                    'finished_at': '',
                    'updated_at': now_iso(),
                }
            ),
        )
        if updated is not None:
            self._log_service.publish_node_state(task_id, node_id)
        return updated

    def _interrupt_acceptance_for_notice_retry(
        self,
        *,
        task,
        execution: NodeRecord | None,
        acceptance: NodeRecord,
        handshake: dict[str, Any],
    ) -> None:
        """定向通知打断验收后的执行节点恢复（拒绝预算不变、无交接消息）。"""
        if execution is None:
            return
        self._update_execution_acceptance_handshake(
            node_id=execution.node_id,
            state=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
            acceptance_node_id=acceptance.node_id,
            rejection_count=int(handshake.get('rejection_count') or 0),
            latest_execution_result_ref=str(handshake.get('latest_execution_result_ref') or '').strip(),
            latest_execution_result_summary=str(handshake.get('latest_execution_result_summary') or '').strip(),
            latest_rejection_feedback_ref='',
            latest_rejection_feedback_summary='',
        )
        self._bump_notice_interrupt_count(execution.node_id)
        self._reactivate_node_for_retry(task_id=task.task_id, node_id=execution.node_id)
        if self._acceptance_updates_task_final_acceptance(task=task, execution=execution, acceptance=acceptance):
            self._set_task_final_acceptance_state(
                task_id=task.task_id,
                acceptance_node_id=acceptance.node_id,
                status=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
                final_execution_output='',
            )
        self._log_service.refresh_task_view(task.task_id, mark_unread=True)

    def _persist_rejection_feedback_and_keep_acceptance_live(
        self,
        *,
        task,
        execution: NodeRecord | None,
        acceptance: NodeRecord,
        feedback_text: str,
        rejection_count: int,
    ) -> None:
        if execution is None:
            return
        feedback_ref = self._externalized_result_payload_ref(task_id=task.task_id, node=acceptance)
        current = normalize_acceptance_handshake(((execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)))
        self._update_execution_acceptance_handshake(
            node_id=execution.node_id,
            state=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
            acceptance_node_id=acceptance.node_id,
            rejection_count=int(rejection_count or 0),
            latest_execution_result_ref=str(current.get('latest_execution_result_ref') or '').strip(),
            latest_execution_result_summary=str(current.get('latest_execution_result_summary') or '').strip(),
            latest_rejection_feedback_ref=feedback_ref,
            latest_rejection_feedback_summary=str(feedback_text or '').strip(),
        )
        self._reactivate_node_for_retry(task_id=task.task_id, node_id=acceptance.node_id)
        self._reactivate_node_for_retry(task_id=task.task_id, node_id=execution.node_id)
        if self._acceptance_updates_task_final_acceptance(task=task, execution=execution, acceptance=acceptance):
            self._set_task_final_acceptance_state(
                task_id=task.task_id,
                acceptance_node_id=acceptance.node_id,
                status=ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
                final_execution_output='',
            )
        self._log_service.refresh_task_view(task.task_id, mark_unread=True)

    def _finalize_acceptance_no_progress(
        self,
        *,
        task,
        execution: NodeRecord | None,
        acceptance: NodeRecord,
        result: NodeFinalResult,
        rejection_count: int,
    ) -> None:
        """第三档裁决的落账：验收终局、被验节点暂停·abnormal、父节点升 high。

        与 terminal reject 的区别：终局失败的是**验收这条线**，被验节点保留
        现场供人工查看（暂停而不是 failed），因此这里不写 final_acceptance.failed——
        任务级终态由操作员决定是否放弃。
        """
        feedback_text = self._acceptance_feedback_text(result) or str(result.summary or '').strip()
        if str(getattr(acceptance, 'status', '') or '').strip().lower() != STATUS_FAILED:
            self._log_service.update_node_status(
                task.task_id,
                acceptance.node_id,
                status=STATUS_FAILED,
                final_output=str(result.output or '').strip(),
                failure_reason=feedback_text or 'reject_no_progress',
            )
        if execution is None:
            return
        current = normalize_acceptance_handshake(((execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)))
        self._update_execution_acceptance_handshake(
            node_id=execution.node_id,
            state=ACCEPTANCE_STATE_REJECTED_TERMINAL,
            acceptance_node_id=acceptance.node_id,
            rejection_count=int(rejection_count or 0),
            latest_execution_result_ref=str(current.get('latest_execution_result_ref') or '').strip(),
            latest_execution_result_summary=str(current.get('latest_execution_result_summary') or '').strip(),
            latest_rejection_feedback_ref=self._externalized_result_payload_ref(task_id=task.task_id, node=acceptance),
            latest_rejection_feedback_summary=feedback_text,
            latest_execution_submission_id=str(result.submission_id or '').strip(),
        )
        self._mark_node_paused(
            task.task_id,
            execution.node_id,
            reason='error',
            remark=feedback_text or 'acceptance rejected without progress',
        )
        self.notify_parent_abnormal_pause(
            task_id=task.task_id,
            node_id=execution.node_id,
            reason=feedback_text or 'acceptance rejected without progress',
        )
        self._log_service.refresh_task_view(task.task_id, mark_unread=True)

    def notify_parent_abnormal_pause(self, *, task_id: str, node_id: str, reason: str) -> None:
        """异常暂停 → 父节点信箱（`child_abnormal_pause`，high 优先级）。

        与 `child_final` 同一个异步投递形态：登记在 `_pending_terminal_messages`，
        `run_task` 的空转判定会等它送出。
        """
        sink = getattr(self, 'abnormal_pause_sink', None)
        if not callable(sink):
            return
        try:
            node = self._store.get_node(node_id)
        except Exception:
            return
        if node is None or not str(getattr(node, 'parent_node_id', '') or '').strip():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._emit_abnormal_pause(task_id=task_id, node_id=node_id, reason=reason),
            name=f'node-abnormal-pause:{task_id}:{node_id}',
        )
        self._pending_terminal_messages.add(task)
        task.add_done_callback(self._pending_terminal_messages.discard)

    async def _emit_abnormal_pause(self, *, task_id: str, node_id: str, reason: str) -> None:
        sink = getattr(self, 'abnormal_pause_sink', None)
        if not callable(sink):
            return
        try:
            await sink(task_id=task_id, node_id=node_id, reason=reason)
        except Exception as exc:
            logger.debug(f'abnormal pause delivery skipped: {node_id}: {describe_exception(exc)}')

    def _finalize_acceptance_pass(
        self,
        *,
        task,
        execution: NodeRecord | None,
        acceptance: NodeRecord,
        result: NodeFinalResult,
    ) -> None:
        if str(getattr(acceptance, 'status', '') or '').strip().lower() != STATUS_SUCCESS:
            self._log_service.update_node_status(
                task.task_id,
                acceptance.node_id,
                status=STATUS_SUCCESS,
                final_output=str(result.output or '').strip(),
                failure_reason='',
            )
        if execution is None:
            return
        current = normalize_acceptance_handshake(((execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)))
        self._update_execution_acceptance_handshake(
            node_id=execution.node_id,
            state=ACCEPTANCE_STATE_ACCEPTED,
            acceptance_node_id=acceptance.node_id,
            rejection_count=int(current.get('rejection_count') or 0),
            latest_execution_result_ref=str(current.get('latest_execution_result_ref') or '').strip(),
            latest_execution_result_summary=str(current.get('latest_execution_result_summary') or '').strip(),
            latest_rejection_feedback_ref='',
            latest_rejection_feedback_summary='',
            # pass 不再投递裁决消息，提交绑定改由握手保存——否则"哪一份提交
            # 被通过"只剩验收节点的载荷，执行节点一侧断链。
            latest_execution_submission_id=str(result.submission_id or '').strip(),
        )
        if self._acceptance_updates_task_final_acceptance(task=task, execution=execution, acceptance=acceptance):
            self._set_task_final_acceptance_state(
                task_id=task.task_id,
                acceptance_node_id=acceptance.node_id,
                status='passed',
                final_execution_output='',
            )
        self._log_service.refresh_task_view(task.task_id, mark_unread=True)

    def _cancel_waiting_acceptance_for_execution_failure(
        self,
        *,
        task_id: str,
        execution_node_id: str,
        reason: str,
    ) -> None:
        execution = self._store.get_node(execution_node_id)
        if execution is None:
            return
        handshake = normalize_acceptance_handshake(((execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)))
        if str(handshake.get('state') or '').strip() not in {
            ACCEPTANCE_STATE_WAITING_EXECUTION_RETRY,
        }:
            return
        acceptance_node_id = str(handshake.get('acceptance_node_id') or '').strip()
        if not acceptance_node_id:
            return
        acceptance = self._store.get_node(acceptance_node_id)
        if self._normalized_status(getattr(acceptance, 'status', '')) in {STATUS_SUCCESS, STATUS_FAILED}:
            return
        if acceptance is not None:
            cancel_reason = str(reason or 'canceled by execution failure').strip() or 'canceled by execution failure'
            # 覆盖前先把验收节点既有裁决文本存入 rejection_history，原始结论必须可追溯。
            self._stash_node_verdict_history(acceptance.node_id)

            def _mutate(record: NodeRecord) -> NodeRecord:
                update: dict[str, Any] = {
                    'status': STATUS_FAILED,
                    'finished_at': now_iso(),
                    'updated_at': now_iso(),
                }
                # 不再覆盖验收节点已有的非空结论文本；仅在为空时写入取消原因。
                if not str(record.final_output or '').strip() and not str(record.failure_reason or '').strip():
                    update['final_output'] = ''
                    update['final_output_ref'] = ''
                    update['failure_reason'] = cancel_reason
                next_metadata = dict(record.metadata or {})
                next_metadata['canceled_by_execution_failure'] = {
                    'at': now_iso(),
                    'execution_node_id': str(execution_node_id or ''),
                    'reason': cancel_reason,
                }
                update['metadata'] = next_metadata
                return record.model_copy(update=update)

            self._store.update_node(acceptance.node_id, _mutate)
            # 终局判定也要进树：这条写绕过了 `update_node_status`，不发布的话验收节点在
            # 快照和浏览器缓存里都会停在非终态，「终局失败才显示 fail」就只剩打回那半成立。
            self._log_service.publish_node_state(task_id, acceptance.node_id)
        self._update_execution_acceptance_handshake(
            node_id=execution.node_id,
            state=ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
            acceptance_node_id=acceptance_node_id,
            rejection_count=int(handshake.get('rejection_count') or 0),
            latest_execution_result_ref=str(handshake.get('latest_execution_result_ref') or '').strip(),
            latest_execution_result_summary=str(handshake.get('latest_execution_result_summary') or '').strip(),
            latest_rejection_feedback_ref='',
            latest_rejection_feedback_summary=str(reason or '').strip(),
        )
        task = self._store.get_task(task_id)
        if (
            task is not None
            and str(getattr(execution, 'node_id', '') or '').strip() == str(getattr(task, 'root_node_id', '') or '').strip()
            and str(normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance')).node_id or '').strip() == acceptance_node_id
        ):
            self._set_task_final_acceptance_state(
                task_id=task_id,
                acceptance_node_id=acceptance_node_id,
                status=ACCEPTANCE_STATE_CANCELED_BY_EXECUTION_FAILURE,
                final_execution_output='',
            )
        self._log_service.refresh_task_view(task_id, mark_unread=True)

    async def _handle_acceptance_node_result(
        self,
        *,
        task,
        acceptance: NodeRecord,
        result: NodeFinalResult,
    ) -> NodeFinalResult:
        if is_acceptance_freeze_result(result):
            # 还没验，不是裁定：不写 accepted / passed，也不计一次拒收，原样退回给
            # 调用方去等对方闭合提交（暂停一条在飞验收轮后再恢复就走这条）。
            return result
        execution = self._accepted_execution_node(task_id=task.task_id, acceptance=acceptance)
        handshake = normalize_acceptance_handshake(((execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) if execution is not None else {}))
        try:
            self._record_acceptance_evidence_audit(acceptance=acceptance, result=result)
        except Exception as exc:  # 审计是旁路：记账失败绝不影响验收控制流
            logger.debug(f'acceptance evidence audit skipped: {acceptance.node_id}: {describe_exception(exc)}')
        if str(result.status or '').strip() == STATUS_SUCCESS:
            # pass 不回流被验收节点：它随这次裁定立即终局，投递只会在一个终态
            # 节点上留下永远读不到的未读消息（还会把树上的未读徽标长期挂住）。
            # "这一份提交被通过"的绑定由握手 latest_execution_submission_id 与
            # 验收节点自己的裁决载荷保存，取证链不断。
            self._finalize_acceptance_pass(task=task, execution=execution, acceptance=acceptance, result=result)
            return NodeFinalResult(
                status=STATUS_SUCCESS,
                delivery_status='final',
                summary=str(result.summary or ''),
                answer=str(result.answer or ''),
                evidence=list(result.evidence or []),
                remaining_work=[],
                blocking_reason='',
            )

        next_rejection_count = int(handshake.get('rejection_count') or 0) + 1
        verdict = str(result.verdict or '').strip().lower()
        if verdict == 'reject_no_progress':
            # 第三档：不是「再来一轮」，而是「这条线在原地打转」。被验节点转
            # 暂停·abnormal（pause_reason=error），父节点收 high 优先级的
            # child_abnormal_pause 并带上验收方的驳回理由——恢复与否交给人。
            self._finalize_acceptance_no_progress(
                task=task,
                execution=execution,
                acceptance=acceptance,
                result=result,
                rejection_count=next_rejection_count,
            )
            # 先落暂停再投裁决：`wake=False` 是必须的——唤醒路径会清掉暂停位，
            # 而这一档的语义正是"停下来等人处理"。priority=high 由分类器按 verdict
            # 推导（不是靠这里的字面量）。
            await self._deliver_acceptance_verdict(
                task=task,
                acceptance=acceptance,
                execution=execution,
                result=result,
                verdict='reject_no_progress',
                message=node_message_body(
                    self._acceptance_feedback_text(result) or '验收判定为无进展。',
                )
                + '\n\n裁决：驳回·无进展。被验节点已转入异常暂停，等待上级或人工处理。',
                wake=False,
            )
            return NodeFinalResult(
                status=STATUS_FAILED,
                delivery_status='final',
                summary=str(result.summary or result.answer or '').strip(),
                answer=str(result.answer or ''),
                evidence=[],
                remaining_work=[],
                blocking_reason='',
                verdict='reject_no_progress',
                submission_id=str(result.submission_id or '').strip(),
            )
        # 拒收无次数上限：每一次 reject 都只是打回——把验收反馈
        # 投给执行节点、复活验收与执行双方，交给下一轮「执行→验收」继续，
        # 绝不因拒收次数把节点对或任务判成终态。
        feedback_text = self._acceptance_feedback_text(result)
        feedback_ref = self._externalized_result_payload_ref(task_id=task.task_id, node=acceptance)
        self._persist_rejection_feedback_and_keep_acceptance_live(
            task=task,
            execution=execution,
            acceptance=acceptance,
            feedback_text=feedback_text,
            rejection_count=next_rejection_count,
        )
        # 打回意见就是 reject 档的裁决消息。被验节点已被复活，投递即唤醒——
        # 这正是设计 §11.2 的"acceptance_verdict 唤醒被验节点，同一回合继续干"。
        await self._deliver_acceptance_verdict(
            task=task,
            acceptance=acceptance,
            execution=execution,
            result=result,
            verdict='reject',
            message=node_message_body(feedback_text, ref=feedback_ref),
            wake=True,
        )
        return NodeFinalResult(
            status=STATUS_SUCCESS,
            delivery_status='partial',
            summary='waiting for execution retry',
            answer=feedback_text,
            evidence=[],
            remaining_work=[],
            blocking_reason='',
        )

    def _set_execution_waiting_acceptance_state(
        self,
        *,
        task_id: str,
        execution_node_id: str,
        acceptance_node_id: str,
        result_ref: str,
        result_summary: str,
        rejection_count: int | None = None,
        submission_id: str = '',
    ) -> None:
        execution = self._store.get_node(execution_node_id)
        current = normalize_acceptance_handshake(
            ((execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) if execution is not None else {})
        )
        self._update_execution_acceptance_handshake(
            node_id=execution_node_id,
            state=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
            acceptance_node_id=acceptance_node_id,
            rejection_count=int(rejection_count if rejection_count is not None else current.get('rejection_count') or 0),
            latest_execution_result_ref=str(result_ref or '').strip(),
            latest_execution_result_summary=str(result_summary or '').strip(),
            latest_rejection_feedback_ref='',
            latest_rejection_feedback_summary='',
            latest_execution_submission_id=str(submission_id or '').strip(),
        )
        # 「等待验收」必须落成帧相位：它在语义上是停靠（回合已结束、不再跑模型），
        # 而不是 running。旧实现不写这一格，于是 (1) 看门狗把它当运行中，10 分钟
        # 无事件就判停（实盘 task:a0d1bd9bc9fa：14:29:36 提交、14:42:52 被看门狗
        # 判停，验收其实还在跑）；(2) 投递唤醒把裁决消息当 in-flight 吞掉，只能靠
        # 可派发扫描兜底。写入时机在提交工具回执之后、回合收尾之外，不会被
        # react_loop 的 waiting_tool_results 覆盖。
        self._log_service.update_frame(
            task_id,
            execution_node_id,
            lambda frame: {**frame, 'phase': 'waiting_acceptance'},
        )
        task = self._store.get_task(task_id)
        acceptance = self._store.get_node(acceptance_node_id)
        if task is not None and acceptance is not None and self._acceptance_updates_task_final_acceptance(task=task, execution=execution, acceptance=acceptance):
            self._set_task_final_acceptance_state(
                task_id=task_id,
                acceptance_node_id=acceptance_node_id,
                status=ACCEPTANCE_STATE_WAITING_ACCEPTANCE,
                final_execution_output='',
            )

    async def _deliver_acceptance_verdict(
        self,
        *,
        task,
        acceptance: NodeRecord,
        execution: NodeRecord | None,
        result: NodeFinalResult,
        verdict: str,
        message: str,
        wake: bool,
    ) -> dict[str, Any]:
        """驳回裁决 → 被验节点信箱（`acceptance_verdict`，只发 reject 两档）。

        pass 不投递：收件人随即终局，消息只会变成一封永远读不到的未读；
        通过绑定由握手 `latest_execution_submission_id` 保存。

        `submission_id` 把裁决绑定到"这一份提交"（迟到裁决规则的唯一判据）。
        载荷里没带时退回握手上登记的提交 id——旧库里的裁决载荷没有这个字段，
        丢绑定会让迟到裁决作用到新提交上。

        `wake` 由档位决定：reject 要唤醒被验节点继续干（T6）；reject_no_progress
        的收件人刚被暂停，不该叫醒。
        """
        if execution is None:
            return {'status': 'skipped', 'reason': 'no_execution_node'}
        submission_id = str(result.submission_id or '').strip()
        if not submission_id:
            handshake = normalize_acceptance_handshake(
                (execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY)
            )
            submission_id = str(handshake.get('latest_execution_submission_id') or '').strip()
        delivered = await self._mailbox_deliver(
            task_id=task.task_id,
            target_node_id=execution.node_id,
            message=message,
            origin='node',
            # 发送方是验收节点（被验节点的直接子节点）。kind 由 system_kind 机械
            # 决定；relation 只保证信封上的方向语义与之一致（我是 child）。
            relation=RELATION_CHILD,
            system_kind=KIND_ACCEPTANCE_VERDICT,
            source_node_id=str(acceptance.node_id or '').strip(),
            intent_kind=INTENT_PROGRESS,
            verdict=verdict,
            submission_id=submission_id,
            wake=wake,
        )
        if str(delivered.get('status') or '') not in {'delivered', 'deduplicated'}:
            logger.warning(
                f'acceptance verdict not delivered: task={task.task_id} '
                f'acceptance={acceptance.node_id} execution={execution.node_id} '
                f'verdict={verdict} result={delivered!r}'
            )
        return delivered

    async def _maybe_start_acceptance_handshake(
        self,
        *,
        task,
        node: NodeRecord,
        result: NodeFinalResult,
    ) -> NodeFinalResult | None:
        if str(getattr(node, 'node_kind', '') or '').strip().lower() != KIND_EXECUTION:
            return None
        if str(result.status or '').strip() != STATUS_SUCCESS:
            return None
        if str(result.delivery_status or '').strip() != 'final':
            return None
        acceptance = self._required_acceptance_node(task=task, node=node)
        if acceptance is None:
            return None
        if self._normalized_status(getattr(acceptance, 'status', '')) in {STATUS_SUCCESS, STATUS_FAILED}:
            # 验收节点已是终态：通知无法再唤醒它。新提交到达时重置回 in_progress，
            # 让正常握手重新核验本次提交（旧裁决先存入 rejection_history）。
            acceptance = self._reset_terminal_acceptance_for_reverify(task=task, acceptance=acceptance)
        result_ref = self._persist_acceptance_candidate_result(task_id=task.task_id, node_id=node.node_id, result=result)
        # 提交即展示：verify 还要等一轮，但「提交输出」此刻就该是这份内容。
        update_node_output = getattr(self._log_service, 'update_node_output', None)
        if callable(update_node_output):
            update_node_output(task.task_id, node.node_id, result.output)
        self._set_execution_waiting_acceptance_state(
            task_id=task.task_id,
            execution_node_id=node.node_id,
            acceptance_node_id=acceptance.node_id,
            result_ref=result_ref,
            result_summary=str(result.summary or '').strip(),
            submission_id=str(result.submission_id or '').strip(),
        )
        # 新一轮交付到达前，作废验收节点上下文中来自本执行节点的旧交接通知，
        # 保证通知尾窗里只保留最新一次"请继续核验"的指令。
        self._supersede_consumed_notices_from_source(
            node_id=str(acceptance.node_id or ''),
            source_node_id=str(node.node_id or ''),
        )
        # 交接单走信箱投递（而不是直写通知表），这样它与模型消息共用同一套机械
        # 推导：执行节点是验收节点的父，所以是下行 `parent_notice`。
        # `wake=False`：验收节点由"提交"这个事件唤醒（派发扫描看握手状态），
        # 设计 §5.2 明确它不由普通消息唤醒；在这里唤醒会在本回合未收尾时就起验收回合。
        delivered = await self._mailbox_deliver(
            task_id=task.task_id,
            target_node_id=acceptance.node_id,
            message=self._acceptance_handoff_message(
                node=node,
                result=result,
                result_ref=result_ref,
            ),
            origin='node',
            relation=RELATION_PARENT,
            source_node_id=str(node.node_id or '').strip(),
            intent_kind=INTENT_INSTRUCTION,
            wake=False,
        )
        if str(delivered.get('status') or '') not in {'delivered', 'deduplicated'}:
            logger.warning(
                f'acceptance handoff not delivered: task={task.task_id} '
                f'acceptance={acceptance.node_id} result={delivered!r}'
            )
        self._log_service.refresh_task_view(task.task_id, mark_unread=True)
        return NodeFinalResult(
            status=STATUS_SUCCESS,
            delivery_status='partial',
            summary='waiting for acceptance',
            answer=str(result.answer or ''),
            evidence=list(result.evidence or []),
            remaining_work=[],
            blocking_reason='',
        )

    def _reset_terminal_acceptance_for_reverify(self, *, task, acceptance: NodeRecord) -> NodeRecord:
        """将已终态的验收节点作废回 in_progress，让它重新验本轮交付。

        终态验收节点不会再开回合，而它的判词可能是在本轮交付之前下的（空手验收、
        或上一轮的拒收）。两条车道共用这里：阻塞核验在派验前调用，spawn 验收在
        复用 entry 已绑定的节点时调用。旧判词先进 `rejection_history` 再清。
        """
        if self._normalized_status(getattr(acceptance, 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}:
            return acceptance
        self._stash_node_verdict_history(acceptance.node_id)
        self._store.update_node(
            acceptance.node_id,
            lambda record: record.model_copy(
                update={
                    'status': 'in_progress',
                    'final_output': '',
                    'final_output_ref': '',
                    'failure_reason': '',
                    'finished_at': '',
                    'updated_at': now_iso(),
                }
            ),
        )
        self._log_service.publish_node_state(task.task_id, acceptance.node_id)
        self._log_service.refresh_task_view(task.task_id, mark_unread=True)
        return self._store.get_node(acceptance.node_id) or acceptance

    def _acceptance_handoff_message(
        self,
        *,
        node: NodeRecord,
        result: NodeFinalResult,
        result_ref: str,
    ) -> str:
        """交接通知：只给指针 + 内联额度内的摘要，交付正文的完整副本由验收节点按 ref 解析。

        旧形态把 `result.answer` 整篇塞进通知，而通知会被归档进验收节点的持久上下文，
        于是每打回一轮就多一份全文——核验方面对的不再是"当前这一份提交"，而是历次
        交付的堆叠。`result_ref` 按设计在载荷内容变化时轮转，它指向的才是本轮该判的
        那一份。
        """
        summary = str(result.summary or '').strip() or str(result.answer or '').strip()
        lines = [
            '执行节点提交了新的待验交付。',
            f'被检验节点：{node.node_id}',
            f'待验提交结果载荷 ref：{result_ref or "(none)"}',
            f'提交摘要：{node_message_body(summary, ref=result_ref) or "(empty)"}',
            '只判该 ref 指向的当前提交；上下文里更早的交付正文一律不再是核验对象。',
        ]
        return '\n'.join(lines)

    def _record_acceptance_evidence_audit(self, *, acceptance: NodeRecord, result: NodeFinalResult) -> None:
        """记录验收裁定引用的文件证据在裁定时刻的磁盘真值（中立事实，不做判定）。

        裁定的路径与体积声明是验收误判高发区：出现过引用根本不存在的路径当「造假证据」、
        把内容工具对二进制文件返回的占位串长度当「实测文件大小」。这里只落盘存在性/体积/mtime，
        供事后对账；判定门槛本身由验收提示词的证据纪律约束（见 `main/prompts/acceptance_execution.md`）。
        """
        cited: list[dict[str, Any]] = []
        for item in list(result.evidence or []):
            if str(getattr(item, 'kind', '') or '').strip().lower() != 'file':
                continue
            path_text = str(getattr(item, 'path', '') or '').strip()
            if not path_text:
                continue
            entry: dict[str, Any] = {'path': path_text}
            try:
                info = Path(path_text).stat()
            except OSError:
                entry['exists'] = False
            else:
                entry['exists'] = True
                entry['size_bytes'] = int(info.st_size)
                entry['mtime'] = datetime.fromtimestamp(info.st_mtime).isoformat(timespec='seconds')
            cited.append(entry)
        if not cited:
            return
        record = {
            'at': now_iso(),
            'status': str(result.status or '').strip(),
            'delivery_status': str(result.delivery_status or '').strip(),
            'cited_files': cited,
            'missing_count': sum(1 for item in cited if not item.get('exists')),
        }

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            history = list(metadata.get(_ACCEPTANCE_EVIDENCE_AUDIT_KEY) or [])
            history.append(record)
            metadata[_ACCEPTANCE_EVIDENCE_AUDIT_KEY] = history[-5:]
            return metadata

        self._log_service.update_node_metadata(acceptance.node_id, _mutate)

    def _stash_node_verdict_history(self, node_id: str) -> None:
        node = self._store.get_node(node_id)
        if node is None:
            return
        final_output = str(getattr(node, 'final_output', '') or '').strip()
        failure_reason = str(getattr(node, 'failure_reason', '') or '').strip()
        if not final_output and not failure_reason:
            return
        entry = {
            'at': now_iso(),
            'status': str(getattr(node, 'status', '') or ''),
            'final_output': final_output,
            'failure_reason': failure_reason,
        }

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            history = list(metadata.get(_REJECTION_HISTORY_KEY) or [])
            history.append(entry)
            metadata[_REJECTION_HISTORY_KEY] = history[-20:]
            return metadata

        self._log_service.update_node_metadata(node_id, _mutate)

    def _clear_pending_notice_state_if_idle(self, *, node_id: str) -> None:
        node = self._store.get_node(node_id)
        if node is None:
            return
        if self._pending_root_notice_records(node=node):
            return
        if any(
            _notification_awaits_injection(item)
            for item in list(self._store.list_task_node_notifications(node.task_id, node_id) or [])
        ):
            return

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            metadata[PENDING_NOTICE_STATE_KEY] = clear_pending_notice_state(
                metadata.get(PENDING_NOTICE_STATE_KEY)
            )
            return metadata

        self._log_service.update_node_metadata(node_id, _mutate)

    def _pending_root_notice_records(self, *, node: NodeRecord) -> list[dict[str, Any]]:
        metadata = dict(node.metadata or {}) if isinstance(getattr(node, 'metadata', None), dict) else {}
        return normalize_pending_append_notice_records(metadata.get(PENDING_APPEND_NOTICE_RECORDS_KEY))

    def _pending_notice_state(self, *, node: NodeRecord) -> dict[str, str]:
        metadata = dict(node.metadata or {}) if isinstance(getattr(node, 'metadata', None), dict) else {}
        return normalize_pending_notice_state(metadata.get(PENDING_NOTICE_STATE_KEY))

    def _pending_notice_waits_for_children(self, *, node: NodeRecord) -> bool:
        state = self._pending_notice_state(node=node)
        if str(state.get('resume_mode') or '').strip() != RESUME_MODE_WAIT_FOR_CHILDREN:
            return False
        latest_round = self._latest_incomplete_spawn_round(parent=node)
        if latest_round is None:
            self._update_pending_notice_state(
                node_id=node.node_id,
                resume_mode=RESUME_MODE_ORDINARY,
                epoch_id='',
                holding_round_id='',
                updated_at=now_iso(),
            )
            return False
        return True

    def _update_pending_notice_state(
        self,
        *,
        node_id: str,
        resume_mode: str,
        epoch_id: str,
        holding_round_id: str,
        updated_at: str | None = None,
    ) -> None:
        normalized_node_id = str(node_id or '').strip()
        if not normalized_node_id:
            return
        normalized_updated_at = str(updated_at or now_iso()).strip()

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            metadata[PENDING_NOTICE_STATE_KEY] = set_pending_notice_state(
                metadata.get(PENDING_NOTICE_STATE_KEY),
                resume_mode=resume_mode,
                epoch_id=epoch_id,
                holding_round_id=holding_round_id,
                updated_at=normalized_updated_at,
            )
            return metadata

        self._log_service.update_node_metadata(normalized_node_id, _mutate)

    def pending_distribution_mailbox_count(self, *, task_id: str) -> int:
        return self._store.count_task_notifications_awaiting_injection(task_id)

    def nodes_with_pending_distribution_notices(self, *, task_id: str) -> list[str]:
        """哪些节点还欠一次「把补充消息并入上下文」的注入。

        两半都不回运行时节点表：子节点那半走一次按节点分组的通知查询；根节点那半走
        投影行的 `pending_append_notice_count`（写通知时同步）。只有旧投影行没有该字段
        时才批量补读一次运行时元数据——逐节点 `get_node` 在实盘要把 246 KB/行的 payload
        读回并建模 938 次。
        """
        awaiting_children = self._store.map_task_notifications_awaiting_injection(task_id)
        records = [
            record
            for record in list(self._store.list_task_nodes(task_id) or [])
            if str(getattr(record, 'node_id', '') or '').strip()
        ]
        pending_root_counts: dict[str, int] = {}
        missing_pending_ids: list[str] = []
        for record in records:
            node_id = str(getattr(record, 'node_id', '') or '').strip()
            payload = dict(getattr(record, 'payload', None) or {})
            count = payload.get('pending_append_notice_count')
            if isinstance(count, int):
                pending_root_counts[node_id] = count
            else:
                missing_pending_ids.append(node_id)
        if missing_pending_ids:
            wanted = set(missing_pending_ids)
            for node in self._store.iter_nodes(task_id):
                normalized_node_id = str(getattr(node, 'node_id', '') or '').strip()
                if normalized_node_id in wanted:
                    pending_root_counts[normalized_node_id] = len(
                        self._pending_root_notice_records(node=node)
                    )
        node_ids: list[str] = []
        seen: set[str] = set()
        for record in records:
            node_id = str(getattr(record, 'node_id', '') or '').strip()
            if not node_id or node_id in seen:
                continue
            if not pending_root_counts.get(node_id) and not awaiting_children.get(node_id):
                continue
            seen.add(node_id)
            node_ids.append(node_id)
        return node_ids

    def _pending_root_notice_records_by_ids(self, *, node_id: str, notification_ids: list[str]) -> list[dict[str, Any]]:
        ids = {str(item or '').strip() for item in list(notification_ids or []) if str(item or '').strip()}
        if not ids:
            return []
        node = self._store.get_node(node_id)
        if node is None:
            return []
        return [
            dict(item)
            for item in list(self._pending_root_notice_records(node=node) or [])
            if str(item.get('notification_id') or '').strip() in ids
        ]

    def _consume_pending_root_notice_records(self, *, node_id: str, notification_ids: list[str]) -> None:
        ids = [str(item or '').strip() for item in list(notification_ids or []) if str(item or '').strip()]
        if not ids:
            return

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            remaining = consume_pending_append_notice_records(
                metadata.get(PENDING_APPEND_NOTICE_RECORDS_KEY),
                notification_ids=ids,
            )
            if remaining:
                metadata[PENDING_APPEND_NOTICE_RECORDS_KEY] = remaining
            else:
                metadata.pop(PENDING_APPEND_NOTICE_RECORDS_KEY, None)
            return metadata

        self._log_service.update_node_metadata(node_id, _mutate)

    def _close_active_round_for_message_consumption(self, *, task_id: str, node_id: str) -> None:
        self._log_service.update_frame(
            task_id,
            node_id,
            lambda frame: {
                **dict(frame or {}),
                'phase': 'before_model',
                'active_round_id': '',
                'active_round_tool_call_ids': [],
            },
            publish_snapshot=False,
        )

    def _flush_latest_valid_result_if_paused(self, *, task_id: str, node_id: str) -> NodeFinalResult | None:
        latest = self._store.get_node(node_id)
        if latest is None or latest.status in {STATUS_SUCCESS, STATUS_FAILED}:
            return None
        _ = task_id
        return None

    def _runtime_frame_messages(self, *, task_id: str, node_id: str) -> list[dict[str, Any]]:
        frame = self._log_service.read_runtime_frame(task_id, node_id) or {}
        messages = frame.get('messages')
        if isinstance(messages, list):
            return [item for item in messages if isinstance(item, dict)]
        return []

    MAX_TREE_NODES = 200

    def _task_node_records(self, task_id: str) -> list[NodeRecord]:
        try:
            return list(self._store.list_nodes(task_id) or [])
        except Exception:
            return []

    def _direct_child_records(self, task_id: str, node_id: str) -> list[NodeRecord]:
        return [
            item
            for item in self._task_node_records(task_id)
            if str(getattr(item, 'parent_node_id', '') or '').strip() == node_id
        ]

    def _node_frame_dict(self, task_id: str, node_id: str) -> dict[str, Any]:
        try:
            frame = self._log_service.read_runtime_frame(task_id, node_id)
        except Exception:
            return {}
        return dict(frame or {}) if isinstance(frame, dict) else {}

    def _unread_from_node(self, task_id: str, node_id: str) -> int:
        try:
            rows = list(self._store.list_task_node_notifications(task_id, node_id) or [])
        except Exception:
            return 0
        return sum(1 for item in rows if str(item.status or '').strip() == 'delivered')

    @staticmethod
    def _node_state_label(node: NodeRecord, phase: str) -> str:
        status = str(getattr(node, 'status', '') or '').strip().lower()
        if status in {'success', 'failed'}:
            return status
        if bool(getattr(node, 'is_paused', False) or getattr(node, 'pause_requested', False)):
            return 'paused'
        if phase in {'waiting_explicit', 'waiting_acceptance'}:
            return 'waiting'
        return 'running'

    def _node_brief(self, task_id: str, node: NodeRecord) -> dict[str, Any]:
        phase = str(self._node_frame_dict(task_id, node.node_id).get('phase') or '')
        return {
            'node_id': str(node.node_id or ''),
            'goal': str(node.goal or '')[:200],
            'node_kind': str(node.node_kind or 'execution'),
            'state': self._node_state_label(node, phase),
            'state_detail': phase,
            'settled': str(node.status or '').strip().lower() in {'success', 'failed'},
            'unread_count': self._unread_from_node(task_id, node.node_id),
            'pause_reason': str(getattr(node, 'pause_reason', '') or ''),
        }

    async def _list_children(
        self,
        *,
        task_id: str,
        node: NodeRecord,
        state: Any = None,
        settled: Any = None,
    ) -> dict[str, Any]:
        wanted = (
            {str(item).strip().lower() for item in state if str(item).strip()}
            if isinstance(state, (list, tuple, set))
            else set()
        )
        rows: list[dict[str, Any]] = []
        for child in self._direct_child_records(task_id, node.node_id):
            brief = self._node_brief(task_id, child)
            if wanted and brief['state'] not in wanted:
                continue
            if settled is not None and bool(brief['settled']) != bool(settled):
                continue
            rows.append(brief)
        return {'children': rows, 'count': len(rows)}

    async def _read_child_progress(
        self,
        *,
        task_id: str,
        node: NodeRecord,
        node_id: str,
        focus: str = '',
    ) -> dict[str, Any]:
        children = self._direct_child_records(task_id, node.node_id)
        target = next((item for item in children if str(item.node_id or '') == node_id), None)
        if target is None:
            return {
                'ok': False,
                'error': 'not_a_direct_child',
                'node_id': node_id,
                'legal_targets': [str(item.node_id or '') for item in children],
            }
        frame = self._node_frame_dict(task_id, target.node_id)
        last_text = ''
        recent_tools: list[dict[str, Any]] = []
        for message in list(frame.get('messages') or []):
            if not isinstance(message, dict) or str(message.get('role') or '') != 'assistant':
                continue
            text = str(message.get('content') or '').strip()
            if text:
                last_text = text[:800]
            for call in list(message.get('tool_calls') or []):
                function = (call or {}).get('function') or {}
                name = str(function.get('name') or (call or {}).get('name') or '').strip()
                if name:
                    recent_tools.append({'name': name})
        metadata = dict(getattr(target, 'metadata', {}) or {})
        handshake = dict(metadata.get('acceptance_handshake') or {})
        brief = self._node_brief(task_id, target)
        abnormal = ''
        if str(getattr(target, 'pause_reason', '') or '').strip() == 'error':
            abnormal = str(getattr(target, 'failure_reason', '') or '')[:400]
        return {
            'ok': True,
            'node_id': target.node_id,
            'state': brief['state'],
            'state_detail': brief['state_detail'],
            'last_progress_text': last_text,
            'recent_tool_calls': recent_tools[-12:],
            'unread_from_child': brief['unread_count'],
            'final_result_ref': str(getattr(target, 'final_output_ref', '') or ''),
            'abnormal_pause_reason': abnormal,
            'watchdog_history': [
                dict(item) for item in list(metadata.get('watchdog_history') or [])[-5:] if isinstance(item, dict)
            ],
            'acceptance_rounds': int(handshake.get('rejection_count') or 0),
            'latest_verdict_ref': str(handshake.get('latest_rejection_feedback_ref') or ''),
            'focus': str(focus or ''),
        }

    async def _view_tree(
        self,
        *,
        task_id: str,
        node: NodeRecord,
        depth: Any = None,
    ) -> dict[str, Any]:
        try:
            limit_depth = max(1, int(depth)) if depth is not None else 3
        except (TypeError, ValueError):
            limit_depth = 3
        nodes = self._task_node_records(task_id)
        lines: list[dict[str, Any]] = []
        truncated = False

        def walk(parent_id: str, level: int) -> None:
            nonlocal truncated
            if level > limit_depth or truncated:
                return
            for child in nodes:
                if str(getattr(child, 'parent_node_id', '') or '').strip() != parent_id:
                    continue
                if len(lines) >= self.MAX_TREE_NODES:
                    truncated = True
                    return
                brief = self._node_brief(task_id, child)
                lines.append(
                    {
                        'node_id': brief['node_id'],
                        'level': level,
                        'state': brief['state'],
                        'node_kind': brief['node_kind'],
                        'goal': brief['goal'][:80],
                    }
                )
                walk(str(child.node_id or ''), level + 1)

        walk(str(node.node_id or ''), 1)
        return {
            'ok': True,
            'root': str(node.node_id or ''),
            'depth': limit_depth,
            'nodes': lines,
            'truncated': truncated,
            'pointer': 'use read_child_progress on a node_id for detail' if truncated else '',
        }

    NODE_WAIT_WAKE_ON_KEY = 'node_wait_wake_on'
    NODE_WAIT_NOTE_KEY = 'node_wait_note'

    async def _enter_wait(
        self,
        *,
        task_id: str,
        node: NodeRecord,
        wake_on: str = 'any',
        note: str = '',
    ) -> dict[str, Any]:
        """记下停靠意图；真正的停靠由回合结束表达（见 react_loop 的 _park_requested）。

        防丢唤醒（R3）：入睡前先看有没有已经满足条件的未读。有就**不睡**——未读立即
        进入上下文、回合继续，因此不存在"最后一条完成消息在入睡前到达"的竞态。
        """
        policy = str(wake_on or '').strip() or 'any'
        if policy not in {'any', WAKE_ON_ALL_CHILDREN}:
            return {'status': 'rejected', 'reason': f'unknown_wake_on:{policy}'}

        unread = [
            item
            for item in list(self._store.list_task_node_notifications(task_id, node.node_id) or [])
            if str(item.status or '').strip() == 'delivered'
        ]
        children: list[dict[str, Any]] = []
        if policy == WAKE_ON_ALL_CHILDREN:
            children = [
                {
                    'node_id': str(child.node_id or ''),
                    'status': str(child.status or ''),
                    'node_kind': str(child.node_kind or ''),
                }
                for child in list(self._store.list_nodes(task_id) or [])
                if str(getattr(child, 'parent_node_id', '') or '').strip() == node.node_id
            ]
        finals = [
            str(item.source_node_id or '').strip()
            for item in unread
            if str(item.kind or '').strip() == 'child_final'
        ]
        may_park, refusal = enter_wait_verdict(
            wake_on=policy,
            unread_count=len(unread),
            children=children,
            unread_final_sources=finals,
        )
        if not may_park:
            if refusal.startswith('unknown_wake_on'):
                return {'status': 'rejected', 'reason': refusal}
            return {
                'status': 'not_parked_unread_present',
                'wake_on': policy,
                'unread_injected': len(unread),
                'reason': refusal,
            }

        self._store.update_node(
            node.node_id,
            lambda record: record.model_copy(
                update={
                    'metadata': {
                        **dict(record.metadata or {}),
                        self.NODE_WAIT_WAKE_ON_KEY: policy,
                        self.NODE_WAIT_NOTE_KEY: str(note or '').strip(),
                    }
                }
            ),
        )
        self._log_service.update_frame(
            task_id,
            node.node_id,
            lambda frame: {**frame, 'phase': 'waiting_explicit'},
        )
        return {
            'status': 'parked',
            'wake_on': policy,
            'note': str(note or '').strip(),
            'unread_injected': 0,
        }

    async def _deliver_node_message(
        self,
        *,
        task_id: str,
        node: NodeRecord,
        to: Any,
        intent_kind: str = '',
        content: str = '',
        ref: str = '',
        in_reply_to: str = '',
    ) -> dict[str, Any]:
        """`send_message` 的运行时侧：寻址与投递都由服务层裁定，这里只转交。

        回调缺失时返回一个可读错误而不是静默成功——工具在表里却什么都不做，
        是比工具不在表里更难查的一类缺陷。
        """
        callback = getattr(self, 'node_message_delivery_callback', None)
        if not callable(callback):
            return {'ok': False, 'error': 'node_message_delivery_unavailable', 'results': []}
        return await callback(
            task_id=task_id,
            sender_node_id=str(node.node_id or '').strip(),
            sender_is_root=str(node.node_id or '').strip()
            == str(node.root_node_id or '').strip(),
            to=to,
            intent_kind=str(intent_kind or '').strip() or INTENT_PROGRESS,
            content=content,
            ref=ref,
            in_reply_to=in_reply_to,
        )

    async def _mailbox_deliver(self, **payload: Any) -> dict[str, Any]:
        """结构性/系统消息的投递口：与 `send_message` 共用 `deliver_node_message`。

        验收交接单与三档裁决不是模型的 `send_message`，但必须走同一条投递实现，
        否则它们会落成没有 kind/priority 的裸通知（实盘 `task:eb3f3a7753f7`）。
        NodeRunner 没有 service 引用，通道由 runtime_service 绑定；缺通道时返回
        可读错误而不是静默成功——"消息没落箱"必须被看见。
        """
        sink = getattr(self, 'mailbox_delivery_sink', None)
        if not callable(sink):
            return {'status': 'rejected', 'reason': 'node_mailbox_delivery_unavailable'}
        return await sink(**payload)

    async def _manage_child_nodes(
        self,
        *,
        task_id: str,
        node: NodeRecord,
        ops: Any = None,
    ) -> dict[str, Any]:
        """`manage_child_nodes` 的运行时侧：只做转发与直接子节点预筛。

        寻址、暂停/恢复/终止的实际裁定都在服务层（那里才有 dispatcher、暂停账本
        与投递通道）。这里先挡掉明显不是直接子节点的条目，避免服务层为一次笔误
        付整套控制的代价；返回里带合法目标集合，模型不用猜。
        """
        callback = getattr(self, 'manage_child_nodes_callback', None)
        if not callable(callback):
            return {
                'ok': False,
                'error': 'manage_child_nodes_unavailable',
                'items': [],
                'legal_targets': [],
            }
        children = {
            str(child.node_id or '').strip(): child
            for child in self._direct_child_records(task_id, node.node_id)
        }
        legal_targets = [node_id for node_id in children if node_id]
        normalized_ops: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for raw in list(ops or []) if isinstance(ops, (list, tuple)) else []:
            item = dict(raw) if isinstance(raw, dict) else {}
            node_id = str(item.get('node_id') or '').strip()
            action = str(item.get('action') or '').strip().lower()
            if node_id not in children:
                results.append(
                    {
                        'node_id': node_id,
                        'action': action,
                        'result': 'rejected',
                        'reason': 'not_a_direct_child',
                    }
                )
                continue
            normalized_ops.append({**item, 'node_id': node_id, 'action': action})
        if normalized_ops:
            applied = await callback(
                task_id=task_id,
                sender_node_id=str(node.node_id or '').strip(),
                ops=normalized_ops,
            )
            results.extend(list(applied.get('items') or []))
        return {
            'ok': bool(results) and all(str(item.get('result') or '') != 'rejected' for item in results),
            'items': results,
            'legal_targets': legal_targets,
        }

    def _build_tools(self, *, task, node: NodeRecord) -> dict[str, Tool]:
        tools = dict(self._tool_provider(node) or {})
        if node.node_kind in {KIND_EXECUTION, KIND_ACCEPTANCE}:
            tools['submit_final_result'] = SubmitFinalResultTool(
                lambda payload: self._submit_final_result(task=task, node=node, payload=payload),
                node_kind=node.node_kind,
            )
        else:
            tools.pop('submit_final_result', None)
        if node.node_kind == KIND_EXECUTION:
            tools[LIST_CHILD_NODES_TOOL_NAME] = ListChildNodesTool(
                lambda state=None, settled=None: self._list_children(
                    task_id=task.task_id, node=node, state=state, settled=settled
                )
            )
            tools[READ_CHILD_PROGRESS_TOOL_NAME] = ReadChildProgressTool(
                lambda node_id='', focus='': self._read_child_progress(
                    task_id=task.task_id, node=node, node_id=node_id, focus=focus
                )
            )
            tools[VIEW_NODE_TREE_TOOL_NAME] = ViewNodeTreeTool(
                lambda depth=None: self._view_tree(
                    task_id=task.task_id, node=node, depth=depth
                )
            )
            tools[ENTER_WAIT_TOOL_NAME] = EnterWaitTool(
                lambda wake_on='any', note='': self._enter_wait(
                    task_id=task.task_id,
                    node=node,
                    wake_on=wake_on,
                    note=note,
                )
            )
            tools[SEND_MESSAGE_TOOL_NAME] = SendMessageTool(
                lambda **payload: self._deliver_node_message(
                    task_id=task.task_id,
                    node=node,
                    **payload,
                )
            )
            tools[MANAGE_CHILD_NODES_TOOL_NAME] = ManageChildNodesTool(
                lambda ops=None: self._manage_child_nodes(
                    task_id=task.task_id,
                    node=node,
                    ops=ops,
                )
            )
            if node.can_spawn_children:
                tools['spawn_child_nodes'] = SpawnChildNodesTool(
                    lambda children, call_id=None: self._spawn_children(
                        task_id=task.task_id,
                        parent_node_id=node.node_id,
                        specs=children,
                        call_id=call_id,
                    )
                )
            else:
                tools.pop('spawn_child_nodes', None)
        else:
            tools.pop('spawn_child_nodes', None)
        return tools

    async def _build_messages(self, *, task, node: NodeRecord) -> list[dict[str, Any]]:
        system_prompt = self._build_system_prompt(node=node)
        core_requirement = self._resolve_core_requirement(task)
        execution_policy = self._resolve_execution_policy(task, node=node)
        payload: dict[str, Any] = {
            'task_id': task.task_id,
            'node_id': node.node_id,
            'node_kind': node.node_kind,
            'depth': node.depth,
            'can_spawn_children': bool(node.can_spawn_children),
            'goal': node.goal,
            'prompt': str(node.prompt or ''),
            'core_requirement': core_requirement,
            'execution_policy': execution_policy.model_dump(mode='json'),
            'file_targets': self._resolve_file_targets(task),
            'runtime_environment': self._runtime_environment_payload(task=task),
        }
        completion_contract = self._completion_contract_payload(task=task, node=node)
        if completion_contract is not None:
            payload['completion_contract'] = completion_contract
        messages = [
            {'role': 'system', 'content': system_prompt},
            {
                'role': 'user',
                'content': json.dumps(payload, ensure_ascii=False, indent=2),
            },
        ]
        if node.node_kind == KIND_ACCEPTANCE:
            turn_tail = self._acceptance_turn_tail(task=task, node=node)
            if turn_tail:
                messages.append({'role': 'user', 'content': turn_tail})
        if self._context_enricher is not None:
            enriched = await self._context_enricher(task=task, node=node, messages=list(messages))
            if isinstance(enriched, list) and enriched:
                return enriched
        return messages

    def _build_system_prompt(self, *, node: NodeRecord) -> str:
        system_name = 'acceptance_execution.md' if node.node_kind == KIND_ACCEPTANCE else 'node_execution.md'
        return load_prompt(system_name).strip()

    def _completion_contract_payload(self, *, task, node: NodeRecord) -> dict[str, Any] | None:
        if node.node_kind != KIND_EXECUTION or node.parent_node_id is not None:
            return None
        final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
        return {
            'result_schema_version': RESULT_SCHEMA_VERSION,
            'final_acceptance_required': bool(final_acceptance.required),
            'final_acceptance_prompt': str(final_acceptance.prompt or ''),
        }

    def _model_refs_for(self, node: NodeRecord) -> list[str]:
        """候选展开视图（不再是 fallback 顺序）。

        有 route plan 时取 plan 展开的候选；没有 plan 的存量注入保持旧的扁平链。
        需要按序 fallback 的调用方必须用 `_model_routes_for`。
        """
        routes = self._model_routes_for(node)
        if routes is not None:
            candidates = list(routes.candidate_model_keys)
            if candidates:
                return candidates
        return list(self._acceptance_model_refs if node.node_kind == KIND_ACCEPTANCE else self._execution_model_refs)

    def _model_routes_for(self, node: NodeRecord) -> Any:
        return self._acceptance_model_routes if node.node_kind == KIND_ACCEPTANCE else self._execution_model_routes

    def _has_load_balance_routes(self, node: NodeRecord) -> bool:
        routes = self._model_routes_for(node)
        return bool(routes is not None and list(getattr(routes, 'load_balance_group_keys', []) or []))

    def _load_balance_group_keys(self, node: NodeRecord) -> list[str]:
        routes = self._model_routes_for(node)
        return list(getattr(routes, 'load_balance_group_keys', []) or []) if routes is not None else []

    def _model_route_entries_payload(self, node: NodeRecord) -> list[dict[str, Any]]:
        """给上下文与诊断用的有序 route 视图；顺序才是 fallback 语义。"""
        routes = self._model_routes_for(node)
        if routes is None:
            return [{'type': 'model', 'model_key': key} for key in self._model_refs_for(node)]
        payload: list[dict[str, Any]] = []
        for route in list(getattr(routes, 'routes', []) or []):
            if bool(getattr(route, 'is_load_balance', False)):
                group = getattr(route, 'group', None)
                payload.append(
                    {
                        'type': 'load_balance',
                        'group_key': str(getattr(route, 'group_key', '') or ''),
                        'model_keys': list(getattr(group, 'candidate_model_keys', []) or route.candidates),
                        'max_retry_rounds': int(getattr(group, 'max_retry_rounds', 1) or 1) if group is not None else 1,
                    }
                )
                continue
            payload.append({'type': 'model', 'model_key': str(getattr(route, 'model_key', '') or '')})
        return payload

    def _max_iterations_for(self, node: NodeRecord) -> int | None:
        return self._acceptance_max_iterations if node.node_kind == KIND_ACCEPTANCE else self._execution_max_iterations

    def _max_parallel_tool_calls_for(self, node: NodeRecord) -> int | None:
        role_limit = self._acceptance_max_concurrency if node.node_kind == KIND_ACCEPTANCE else self._execution_max_concurrency
        global_limit = getattr(self._react_loop, '_max_parallel_tool_calls', None)
        if role_limit is None:
            return global_limit
        if global_limit is None:
            return role_limit
        return min(int(role_limit), int(global_limit))

    def _max_parallel_child_pipelines_for(self, node: NodeRecord) -> int | None:
        _ = node
        return self._max_parallel_child_pipelines

    def _runtime_context(self, *, task, node: NodeRecord) -> dict[str, Any]:
        memory_scope = normalize_memory_scope(
            (task.metadata or {}).get('memory_scope') if isinstance(task.metadata, dict) else None,
            fallback_session_key=task.session_id,
        )
        model_refs = self._model_refs_for(node)
        project_environment = current_project_environment(
            shell_family=self._shell_family(),
            workspace_root=self._workspace_root(),
            process_cwd=self._process_cwd(),
        )
        task_temp_dir = str(self._task_temp_dir(getattr(task, 'task_id', None)))
        return {
            'session_key': task.session_id,
            'task_id': task.task_id,
            'node_id': node.node_id,
            'depth': node.depth,
            'node_kind': node.node_kind,
            'model_refs': list(model_refs or []),
            'model_route_mode': 'load_balance' if self._has_load_balance_routes(node) else 'ordered_chain',
            'model_routes': self._model_route_entries_payload(node),
            'load_balance_group_keys': self._load_balance_group_keys(node),
            'provider_model': str((list(model_refs or []) or [''])[0] or '').strip(),
            'actor_role': self._actor_role_for_node(node),
            'can_spawn_children': bool(node.can_spawn_children),
            'memory_channel': str(memory_scope.get('channel') or 'unknown'),
            'memory_chat_id': str(memory_scope.get('chat_id') or 'unknown'),
            'project_python': str(project_environment.get('project_python') or ''),
            'project_python_dir': str(project_environment.get('project_python_dir') or ''),
            'project_scripts_dir': str(project_environment.get('project_scripts_dir') or ''),
            'project_path_entries': list(project_environment.get('project_path_entries') or []),
            'project_virtual_env': str(project_environment.get('project_virtual_env') or ''),
            'project_python_hint': str(project_environment.get('project_python_hint') or ''),
            'task_temp_dir': task_temp_dir,
            'temp_dir': task_temp_dir,
            'tool_watchdog': self._tool_watchdog_runtime_config(),
            # H3：节点道的工具超时上限由 react_loop 夹紧（它才是每次调用的唯一入口）。
            'node_tool_timeout_ceiling_seconds': float(
                getattr(self, '_node_tool_timeout_ceiling_seconds', 900.0) or 900.0
            ),
            'tool_snapshot_supplier': (
                (lambda current_task_id=task.task_id: self._tool_snapshot_supplier(current_task_id))
                if callable(getattr(self, '_tool_snapshot_supplier', None))
                else None
            ),
            # skill 名单的每跳实时取数口：react_loop 没有 service 引用，节点道的 live
            # 检测只能靠主运行时递进来的闭包。取数失败由调用侧回落回合初快照。
            'live_skill_roster_supplier': (
                (lambda: self._skill_roster_supplier(task=task, node=node))
                if callable(getattr(self, '_skill_roster_supplier', None))
                else None
            ),
            'exec_runtime_policy_supplier': (
                (lambda: self._exec_runtime_policy_supplier())
                if callable(getattr(self, '_exec_runtime_policy_supplier', None))
                else None
            ),
        }

    def _tool_watchdog_runtime_config(self) -> dict[str, Any]:
        """节点运行时工具合同里的统一 timeout 全局默认值入口。"""
        value = getattr(self, '_tool_default_timeout_seconds', None)
        try:
            parsed = float(value) if value is not None else 0.0
        except (TypeError, ValueError):
            return {}
        return {'default_timeout_seconds': parsed} if parsed >= 1 else {}

    def _workspace_root(self) -> Path:
        getter = self._workspace_root_getter
        if callable(getter):
            value = getter()
            if value is not None:
                return Path(value).expanduser().resolve()
        return Path.cwd().expanduser().resolve()

    @staticmethod
    def _process_cwd() -> Path:
        return Path(os.getcwd()).expanduser().resolve()

    @staticmethod
    def _os_family() -> str:
        system = platform.system().strip().lower()
        if system:
            return system
        return 'windows' if os.name == 'nt' else os.name or 'unknown'

    @staticmethod
    def _shell_family() -> str:
        if os.name == 'nt':
            return 'powershell'
        shell = str(os.environ.get('SHELL') or '').strip().lower()
        if 'powershell' in shell or 'pwsh' in shell:
            return 'powershell'
        if 'bash' in shell:
            return 'bash'
        if 'zsh' in shell:
            return 'zsh'
        if shell:
            return Path(shell).name
        return 'sh'

    def _task_temp_dir(self, task_id: str | None) -> Path:
        # 兜底落点与 `MainRuntimeService._temp_root()`、`log_service._fallback_task_temp_dir()`
        # 同一个根（数据根/temp），不再挂工作区。
        fallback = data_work_path('temp').resolve()
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return fallback
        getter = getattr(self._log_service, 'read_task_runtime_meta', None)
        if not callable(getter):
            return fallback
        try:
            runtime_meta = dict(getter(normalized_task_id) or {})
        except Exception:
            return fallback
        raw = str(runtime_meta.get('task_temp_dir') or '').strip()
        if not raw:
            return fallback
        try:
            candidate = Path(raw).expanduser().resolve(strict=False)
        except Exception:
            return fallback
        return candidate if candidate.is_absolute() else fallback

    def _runtime_environment_payload(self, *, task=None) -> dict[str, Any]:
        project_environment = current_project_environment(
            shell_family=self._shell_family(),
            workspace_root=self._workspace_root(),
            process_cwd=self._process_cwd(),
        )
        task_temp_dir = str(self._task_temp_dir(getattr(task, 'task_id', None)))
        return {
            'os_family': self._os_family(),
            'shell_family': str(project_environment.get('shell_family') or self._shell_family()),
            'process_cwd': str(project_environment.get('process_cwd') or self._process_cwd()),
            'workspace_root': str(project_environment.get('workspace_root') or self._workspace_root()),
            'project_python': str(project_environment.get('project_python') or ''),
            'project_python_dir': str(project_environment.get('project_python_dir') or ''),
            'project_scripts_dir': str(project_environment.get('project_scripts_dir') or ''),
            'project_virtual_env': str(project_environment.get('project_virtual_env') or ''),
            'project_python_hint': str(project_environment.get('project_python_hint') or ''),
            'task_temp_dir': task_temp_dir,
            'path_policy': {
                **path_anchor_policy(),
                'filesystem_requires_absolute_path': False,
                'content_requires_absolute_path': False,
                'exec_default_working_dir': 'task_temp_dir',
                'exec_requires_explicit_working_dir_for_target_dir': True,
            },
            'tool_guidance': {
                'filesystem': (
                    PATH_ANCHOR_RULE_TEXT +
                    '新建脚本、抓取结果、缓存、调试输出和其他中间文件默认写到 '
                    'runtime_environment.task_temp_dir；只有为了满足任务要求且只能写到其他目录时才允许例外。'
                ),
                'content': (
                    '单个内容体优先用 ref 导航或文件路径（短写法与绝对路径都可）；'
                    '需要本地文件正文证据时，优先用 '
                    '`content_open(path=<目标路径>, start_line, end_line)`；'
                    '不要把它当成目录搜索工具。'
                ),
                'exec': (
                    'Windows 上的 exec 运行在 PowerShell 中，其他系统运行在宿主 shell 中。'
                    '未传 working_dir 时，默认在 runtime_environment.task_temp_dir 执行，并优先把所有中间文件写到该目录。'
                    '它会继承运行时进程的同一套 Python 环境，并把该解释器注入 PATH。'
                    '不要假设 bash heredoc、rg 或 `true` 这类 Unix shell 内建一定可用。'
                    '需要特定目录时，显式传入 working_dir。'
                    '它更适合目录发现、文件名发现和环境探查；如果结果反复退化成 `head_preview`，'
                    '不要继续用它抽取本地文件正文，应改用 '
                    '`content_open(path=<目标路径>, start_line, end_line)`。'
                    f"当解释器选择必须精确一致时，优先使用 `{project_environment.get('project_python_hint') or 'python'}`，"
                    '不要假设裸 `python` 一定会解析到正确解释器。'
                ),
            },
        }

    @staticmethod
    def _actor_role_for_node(node: NodeRecord) -> str:
        return 'inspection' if node.node_kind == KIND_ACCEPTANCE else 'execution'

    async def _spawn_children(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        specs: list[SpawnChildSpec],
        call_id: str | None,
    ) -> list[SpawnChildResult]:
        cache_key = self._spawn_round_cache_key(specs=specs, call_id=call_id)
        async with self._spawn_operation_lock(parent_node_id):
            try:
                return await self._spawn_children_locked(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    specs=specs,
                    call_id=call_id,
                )
            except (TaskPausedError, NodePausedError):
                # 冻结类控制信号照旧上抛，绝不改写成一次交付（见 main-task-runtime
                # 「Node-Level Pause and Recovery」的结算车道合同）。
                raise
            except asyncio.CancelledError as exc:
                # 真取消（任务/父节点级）继续上抛；来源不明的取消（例如看门狗因观测
                # 读取失败而 request_tool_cancellation）不得留下假结论，交给滞留道等
                # 真实终态——孩子由各自派发 entry 驱动，并没有被这次取消影响。
                if self._should_propagate_child_pipeline_cancellation(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                ):
                    raise
                held = await self._hold_spawn_round_for_live_nodes(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    specs=specs,
                    fault=exc,
                )
                if held is None:
                    raise
                return held
            except Exception as exc:
                held = await self._hold_spawn_round_for_live_nodes(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    specs=specs,
                    fault=exc,
                )
                if held is None:
                    raise
                return held

    @staticmethod
    def _spawn_round_cache_key(*, specs: list[SpawnChildSpec], call_id: str | None) -> str:
        return str(call_id or f'call:{len(specs or [])}')

    async def _hold_spawn_round_for_live_nodes(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        specs: list[SpawnChildSpec],
        fault: BaseException,
    ) -> list[SpawnChildResult] | None:
        """本轮仍绑着未收敛节点时返回真实结论，否则返回 None 让原异常照旧上抛。

        `Error executing spawn_child_nodes` 一旦进了父节点的消息链就收不回：它消费掉本轮
        tool_call_id（幂等重放通道随之关闭），父节点只能重发同一批，而重发开局就把这棵
        仍在飞的子树结清成 `superseded by newer spawn round`。派生工具对模型的承诺是
        "全部子管线终态才返回"，所以故障发生在记账上时由这里兜住：等节点自己收敛。

        三个出口：节点全落终态 → 交付真实结论；任务/父节点显式终态 → 按该理由收尾；
        没有任何可观测推进（`driven` 空而 `unknown` 非空）持续满一个复报周期 → 把父节点
        可见地停下请人核对（`_park_spawn_round_for_operator`），而不是静默挂到永远。
        """
        state = self._spawn_round_hold_probe(
            task_id=task_id, parent_node_id=parent_node_id, cache_key=cache_key,
        )
        if not (state['driven'] or state['unknown']):
            return None
        self._emit_spawn_diagnostic(
            title='spawn_round_fault_holds_for_live_nodes',
            task_id=task_id,
            parent_node_id=parent_node_id,
            cache_key=cache_key,
            index=None,
            entry=None,
            detail=self._spawn_round_hold_state_text(state=state, fault=fault),
        )
        held_seconds = 0.0
        stuck_seconds = 0.0
        next_report_at = _SPAWN_FAULT_HOLD_REPORT_SECONDS
        while True:
            stop_reason = self._spawn_round_hold_stop_reason_safe(
                task_id=task_id, parent_node_id=parent_node_id,
            )
            if stop_reason:
                self._emit_spawn_diagnostic(
                    title='spawn_round_hold_released_by_stop_reason',
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    index=None,
                    entry=None,
                    detail=(
                        f'driven={len(state["driven"])} unknown={len(state["unknown"])} '
                        f'held_seconds={held_seconds:.0f} reason={stop_reason}'
                    ),
                )
                return [self._spawn_abort_result(spec.goal, stop_reason) for spec in list(specs or [])]
            await asyncio.sleep(_SPAWN_FAULT_HOLD_SLICE_SECONDS)
            held_seconds += _SPAWN_FAULT_HOLD_SLICE_SECONDS
            state = self._spawn_round_hold_probe(
                task_id=task_id, parent_node_id=parent_node_id, cache_key=cache_key,
            )
            if not (state['driven'] or state['unknown']):
                break
            # 只有"没有任何可观测的推进"才算停等：driven 非空就还有人在跑，再久也只复报。
            # 停等要持续满一个复报周期才升级为暂停——刚重启完的那一拍所有节点都还没有
            # entry，立刻暂停会把「还没来得及派发」误报成「卡住」。
            stuck_seconds = (
                stuck_seconds + _SPAWN_FAULT_HOLD_SLICE_SECONDS
                if not state['driven'] else 0.0
            )
            if held_seconds >= next_report_at:
                next_report_at += _SPAWN_FAULT_HOLD_REPORT_SECONDS
                self._emit_spawn_diagnostic(
                    title='spawn_round_fault_hold_still_waiting',
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    index=None,
                    entry=None,
                    detail=self._spawn_round_hold_state_text(
                        state=state, held_seconds=held_seconds, stuck_seconds=stuck_seconds,
                    ),
                )
            if stuck_seconds >= _SPAWN_FAULT_HOLD_REPORT_SECONDS:
                self._park_spawn_round_for_operator(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    state=state,
                    fault=fault,
                    held_seconds=held_seconds,
                    stuck_seconds=stuck_seconds,
                )
        self._emit_spawn_diagnostic(
            title='spawn_round_fault_hold_converged',
            task_id=task_id,
            parent_node_id=parent_node_id,
            cache_key=cache_key,
            index=None,
            entry=None,
            detail=f'held_seconds={held_seconds:.0f}',
        )
        return self._deliver_spawn_round_results(
            task_id=task_id,
            parent_node_id=parent_node_id,
            cache_key=cache_key,
            stage='hold_converged',
            entries_loader=lambda: self._spawn_round_entries(
                parent_node_id=parent_node_id, cache_key=cache_key,
            ),
            specs=specs,
        )

    def _spawn_round_entries(self, *, parent_node_id: str, cache_key: str) -> list[dict[str, Any]]:
        """本轮 entry 清单：先读记账，记账缺位时按 owner 戳从节点树合成。

        走到滞留这条路的原因往往就是记账写失败，所以 `spawn_operations` 不能是唯一来源。
        """
        parent = self._store.get_node(parent_node_id)
        payload = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key) if parent else None
        entries = [dict(item) for item in list((payload or {}).get('entries') or []) if isinstance(item, dict)]
        if entries:
            return entries
        for node in list(self._store.list_children(parent_node_id) or []):
            metadata = dict(node.metadata or {})
            if str(metadata.get('spawn_owner_round_id') or '').strip() != str(cache_key or '').strip():
                continue
            if str(metadata.get('spawn_owner_parent_node_id') or '').strip() != str(parent_node_id or '').strip():
                continue
            entry: dict[str, Any] = {
                'index': int(metadata.get('spawn_owner_entry_index') or 0),
                'goal': str(node.goal or ''),
                'child_node_id': node.node_id,
            }
            for acceptance in list(self._store.list_children(node.node_id) or []):
                if str(acceptance.node_kind or '').strip().lower() == KIND_ACCEPTANCE:
                    entry['acceptance_node_id'] = acceptance.node_id
                    break
            entries.append(entry)
        return entries

    def _read_spawn_round_ledger_entries(
        self, *, parent_node_id: str, cache_key: str,
    ) -> list[dict[str, Any]]:
        """已完成轮次重放时现读记账；读失败/读空都交给停车道，不合成、不降级。

        与 `_spawn_round_entries` 的区别是**不做节点树合成**：重放要交付的是这一轮记账
        里的判决，合成出来的只是名单，没有判决的名单交给模型与空结论同罪。
        """
        parent = self._store.get_node(parent_node_id)
        if parent is None:
            raise RuntimeError(f'spawn round parent missing: {parent_node_id}')
        payload = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
        if not isinstance(payload, dict):
            raise RuntimeError(f'spawn round ledger missing: {cache_key}')
        return [dict(item) for item in list(payload.get('entries') or []) if isinstance(item, dict)]

    def _spawn_round_non_terminal_node_ids(self, *, entries: list[dict[str, Any]]) -> set[str]:
        pending: set[str] = set()
        for entry in list(entries or []):
            for node_id in self._spawn_entry_node_ids(entry):
                node = self._store.get_node(str(node_id or '').strip())
                if node is None:
                    continue
                if self._normalized_status(getattr(node, 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}:
                    pending.add(node.node_id)
        return pending

    def _spawn_round_paused_node_count(self, *, node_ids: set[str]) -> int:
        paused = 0
        for node_id in sorted(node_ids or set()):
            node = self._store.get_node(node_id)
            if node is not None and bool(getattr(node, 'is_paused', False)):
                paused += 1
        return paused

    def _spawn_round_liveness(self, *, task_id: str, node_id: str) -> bool | None:
        """问持有者这个节点此刻有没有人在驱动；探针没接线或自己抛＝说不出结论。"""
        probe = self.node_liveness_probe
        if not callable(probe):
            return None
        try:
            return bool(probe(task_id, node_id))
        except Exception:
            return None

    def _spawn_round_row_status(self, node_id: str) -> str | None:
        """节点行的归一状态。**读不到返回 None，不当成终态。**

        这条区分是整件事的关键：读失败是「不知道」，不是「已经结束了」。把 None 当终态，
        就是让一次存储抖动拥有宣判一个节点结束的权力。
        """
        try:
            node = self._store.get_node(str(node_id or '').strip())
        except Exception:
            return None
        if node is None:
            return None
        return self._normalized_status(getattr(node, 'status', ''))

    def _spawn_round_hold_probe(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
    ) -> dict[str, Any]:
        """本轮绑定节点的三态清点，且任何一条读失败都不许外抛。

        走到滞留道的原因往往就是库在坏。这里每处裸读的异常都会**顶替**原异常穿出
        `_spawn_children` 的 except 体，于是工具照样返回 `Error executing spawn_child_nodes`、
        轮次照样带着活孩子提前结束、批后帧写照样清掉重放意图、收尸器照样开杀——本道要防的
        事被本道自己重新做了一遍。所以：读得到就按读到的算，读不到一律计入 unknown。

        `driven`   持有者说有协程在跑 ⇒ 进度在发生，继续等。
        `converged` 节点行已终态 ⇒ 本轮真收敛了。
        `unknown`  读不到行，或持有者说停了而行仍非终态 ⇒ 无人推进且说不清会不会再推进。
        """
        driven: list[str] = []
        converged: list[str] = []
        unknown: list[str] = []
        try:
            entries = self._spawn_round_entries(parent_node_id=parent_node_id, cache_key=cache_key)
        except Exception as exc:
            logger.warning(
                'title=spawn_round_hold_probe_failed stage=entries cache_key={} error={}',
                str(cache_key or '').strip(),
                describe_exception(exc),
            )
            entries = []
        for entry in list(entries or []):
            if not isinstance(entry, dict):
                continue
            for node_id in self._spawn_entry_node_ids(entry):
                normalized = str(node_id or '').strip()
                if not normalized:
                    continue
                # 先认终态，再问活性：节点落了终态就是收敛了，此刻它的派发 entry 可能还没
                # 被摘掉（探针仍会说"在跑"）。反过来先问活性，一轮已完成的子节点会把滞留道
                # 永远吊住——实测就是这条循环不出。
                status = self._spawn_round_row_status(normalized)
                if status in {STATUS_SUCCESS, STATUS_FAILED}:
                    converged.append(normalized)
                    continue
                if self._spawn_round_liveness(task_id=task_id, node_id=normalized) is True:
                    driven.append(normalized)
                    continue
                unknown.append(normalized)
        return {
            'entries': list(entries or []),
            'driven': sorted(set(driven)),
            'converged': sorted(set(converged)),
            'unknown': sorted(set(unknown)),
        }

    def _spawn_round_hold_state_text(
        self,
        *,
        state: dict[str, Any],
        fault: BaseException | None = None,
        held_seconds: float = 0.0,
        stuck_seconds: float = 0.0,
    ) -> str:
        bits = [
            f'driven={len(state.get("driven") or [])}',
            f'unknown={len(state.get("unknown") or [])}',
            f'converged={len(state.get("converged") or [])}',
        ]
        if held_seconds:
            bits.append(f'held_seconds={held_seconds:.0f}')
        if stuck_seconds:
            bits.append(f'stuck_seconds={stuck_seconds:.0f}')
        unknown_ids = [str(item) for item in (state.get('unknown') or [])][:8]
        if unknown_ids:
            bits.append('unknown_nodes=' + ','.join(unknown_ids))
        if fault is not None:
            bits.append('fault=' + describe_exception_chain(fault))
        return ' '.join(bits)

    def _spawn_round_hold_stop_reason_safe(self, *, task_id: str, parent_node_id: str) -> str:
        """停手理由只认明确的终态；读不到＝还没读到，不是「可以继续也不是「已经结束」。"""
        try:
            return self._spawn_round_hold_stop_reason(task_id=task_id, parent_node_id=parent_node_id)
        except Exception as exc:
            logger.warning(
                'title=spawn_round_hold_probe_failed stage=stop_reason task={} error={}',
                str(task_id or '').strip(),
                describe_exception(exc),
            )
            return ''

    def _park_spawn_round_for_operator(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        state: dict[str, Any],
        fault: BaseException | None,
        held_seconds: float,
        stuck_seconds: float,
    ) -> None:
        """等不到任何可观测推进时让父节点**可见地**停下，而不是静默挂着。

        落点是既有的节点错误暂停车道：错误行进 `task_error_logs`、节点落
        `pause_reason='error'` 并带 remark，然后抛 `NodePausedError`——它在工具异常保护集里
        原样重抛，因此不消费 `tool_call_id`，重放通道完整保留，`resume_node` 后同 id 重入。

        这里只把现场摆出来，**不写任何节点判决**：unknown 的含义是"本进程说不出谁在驱动这些
        节点"，而"说不出"既不是"它们在跑"也不是"它们失败了"。要不要放弃这一批，归看完现场的
        会话 agent 或操作者。
        """
        detail = self._spawn_round_hold_state_text(
            state=state, held_seconds=held_seconds, stuck_seconds=stuck_seconds,
        )
        chain = describe_exception_chain(fault) if fault is not None else 'unknown'
        error_text = (
            f'spawn_round_stalled: 派生轮停等，没有任何绑定节点在被驱动，也说不清还有谁在驱动。'
            f' 一手故障: {chain}。'
            f' 处置: 确认这批节点的归属后用 manage_task_nodes 把本轮落 canceled，或'
            f' resume 本节点按同一 call id 重入；在此之前不要重发同一批（重发开局会把在飞子树判成 superseded）。'
            f' {detail}'
        )
        error_text = error_text[:_SPAWN_FAULT_HOLD_REPORT_TEXT_LIMIT]
        parked = self._park_parent_for_operator(
            task_id=task_id,
            parent_node_id=parent_node_id,
            error_text=error_text,
        )
        self._emit_spawn_diagnostic(
            title='spawn_round_parked_for_operator',
            task_id=task_id,
            parent_node_id=parent_node_id,
            cache_key=cache_key,
            index=None,
            entry=None,
            detail=f'{detail} persisted={"yes" if parked else "no"} reason=stuck_no_progress',
        )
        if not parked:
            # 现场没落库就不要把它伪装成一次已登记的暂停——继续等，让下一拍重投。
            return
        raise NodePausedError(task_id, parent_node_id)

    def _park_parent_for_operator(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        error_text: str,
    ) -> bool:
        """把现场写进既有的节点错误暂停车道，返回是否真的落了库；写失败绝不外抛。

        走到停车的原因常常正是库在坏，所以这两笔写本身也必须按可失败对待：写不进去就
        返回 False，由调用方决定「继续等下一拍重投」还是「原样把一手故障抛出去」。
        谎报一次已登记的暂停，比承认没登记更糟——外界会据此以为已经有人接手。
        """
        wrote = True
        try:
            self._log_service.append_task_error_log(
                task_id, parent_node_id, error_text=error_text, node_title='',
            )
        except Exception as exc:
            wrote = False
            logger.warning(
                'title=spawn_round_park_write_failed stage=error_log task={} node={} error={}',
                str(task_id or '').strip(), str(parent_node_id or '').strip(), describe_exception(exc),
            )
        try:
            self._mark_node_paused(task_id, parent_node_id, reason='error', remark=error_text)
        except Exception as exc:
            wrote = False
            logger.warning(
                'title=spawn_round_park_write_failed stage=node_pause task={} node={} error={}',
                str(task_id or '').strip(), str(parent_node_id or '').strip(), describe_exception(exc),
            )
        return wrote

    def _deliver_spawn_round_results(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        stage: str,
        entries: list[dict[str, Any]] | None = None,
        entries_loader: Callable[[], list[dict[str, Any]]] | None = None,
        specs: list[SpawnChildSpec] | None = None,
    ) -> list[SpawnChildResult]:
        """把已收敛轮次的 entry 交付给模型，读不出就**可见地停住**，不交出一句改写过的工具错误。

        子节点的结论这时已经全部落库、轮次 `completed=True`：丢的不是工作，是把工作送到
        模型眼前这一跳。而这一跳裸读的是 entry 表、节点行、交接件和错误行
        （`entries_loader` / `_rebuild_spawn_entry_result`），正好是当初把整轮打进滞留道的那
        同一个存储故障——不套住它，故障就会在"已经修好了"的位置再赢一次。

        落点选 `NodePausedError` 而不是 per-entry 降级：给读不出来的那条 entry 编一个 error
        结果就是不变量 I 禁止的"把『我不知道』解析成否定判决"；给读不出来的整轮交付一份
        **空**结果表同理（`entry_count=unreadable` 就是为了让这一跳可查）。而整体落
        `Error executing spawn_child_nodes` 更糟——它消费掉 `tool_call_id` 并触发批后帧写清掉
        pending_tool_calls，模型照旧重发同一批，新 call id 生成新 cache_key，
        `_completed_successful_spawn_results` 又只在**全部 entry 都是 success** 时才复用（一份
        error entry 就让它整轮重新派生）。停车把 `tool_call_id` 原样留给重放：`resume_node` 后
        同一轮直接从库里再取一次，交付的是真实结论。
        """
        stage = str(stage or '').strip() or 'unknown'
        resolved: list[Any] = list(entries or [])
        counted = entries is not None
        try:
            if entries_loader is not None:
                resolved = list(entries_loader() or [])
            if specs and not resolved:
                # 已完成轮次读回空 entry 表：这不是「0 个子节点」这个事实，只是这一跳读不回。
                # 把空表当结论交付会被下游读成「全部成功、无事可做」，所以按读失败处理，
                # 并把 entry_count 标成 unreadable（而不是实测的 0）。
                counted = False
                raise RuntimeError('spawn round ledger yielded no entries for a completed round')
            counted = True
            return self._spawn_round_results_from_entries(
                task_id=task_id,
                entries=[dict(item) for item in resolved if isinstance(item, dict)],
                specs=specs,
            )
        except (NodePausedError, TaskPausedError, asyncio.CancelledError):
            raise
        except Exception as exc:
            detail = f'stage={stage} entry_count={len(resolved) if counted else "unreadable"}'
            error_text = (
                f'spawn_round_results_unreadable: 派生轮已收敛，但按当前存储状态取不回结果。'
                f' 子节点的结论没有丢（都在库里），缺的只是交付这一跳。'
                f' 一手故障: {describe_exception_chain(exc)}。'
                f' 处置: resume 本节点按同一 call id 重放即可取回；在此之前不要重发同一批，'
                f' 重发开局会把这一轮的 entry 当成被取代的旧轮清扫掉。 {detail}'
            )[:_SPAWN_FAULT_HOLD_REPORT_TEXT_LIMIT]
            parked = self._park_parent_for_operator(
                task_id=task_id,
                parent_node_id=parent_node_id,
                error_text=error_text,
            )
            self._emit_spawn_diagnostic(
                title='spawn_round_converged_results_unreadable',
                task_id=task_id,
                parent_node_id=parent_node_id,
                cache_key=cache_key,
                index=None,
                entry=None,
                detail=(
                    f'{detail} persisted={"yes" if parked else "no"}'
                    f' fault={describe_exception_chain(exc)}'
                ),
            )
            if parked:
                raise NodePausedError(task_id, parent_node_id) from exc
            # 连暂停都写不进去：库已经不认任何一笔写。这时唯一诚实的动作是把一手故障原样抛出去，
            # 而不是在这儿再造一条判决。
            raise

    def _latest_spawn_round_payload(
        self,
        *,
        parent: NodeRecord,
        cache_key: str,
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        """批后重读只为拿到并发写入的 entry；读不到就用本进程手里那份。

        这一轮的 entry 本来就是这一轮的记账写进去的，`fallback` 与库里那份同源，用它交付不算
        伪造结果——伪造的是给读不出来的结论编一条判决。
        """
        try:
            latest_parent = self._store.get_node(parent.node_id) or parent
            latest = dict((latest_parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
        except Exception as exc:
            logger.warning(
                'title=spawn_round_result_reread_failed stage=post_convergence cache_key={} error={}',
                str(cache_key or '').strip(),
                describe_exception(exc),
            )
            return fallback
        return latest if isinstance(latest, dict) else fallback

    def _spawn_round_hold_stop_reason(self, *, task_id: str, parent_node_id: str) -> str:
        """只有明确的终态才算停手理由；查不到行不算（记账故障正是走到这里的原因）。"""
        task = self._store.get_task(task_id)
        if task is None:
            return ''
        return self._task_terminal_reason(task_id, task=task) or self._node_terminal_reason(
            self._store.get_node(parent_node_id),
            default_failed='parent node failed',
            default_success='parent node already completed',
        )

    def _replay_completed_spawn_round(
        self,
        *,
        task_id: str,
        parent: NodeRecord,
        cache_key: str,
        specs: list[SpawnChildSpec],
    ) -> list[SpawnChildResult]:
        """已完成轮次的重放只做**交付**：交出这一轮子节点的真实结论，绝不退回空回执。

        旧实现返回创建回执（state='created'、`node_output=''`），在轮次因交付读失败被
        停车、随后 resume 重放的形状里，模型拿到的是「孩子什么也没说」——这不是可观测的
        事实，只是这一跳没读出来。交付本身仍然易碎（entry 表 / 子节点行 / 交接件都要现读），
        所以统一走 `_deliver_spawn_round_results`：读失败就写错误行、落 `pause_reason='error'`
        并抛 `NodePausedError`，把同一 `tool_call_id` 留给下一次重放。

        这里先于 reconcile/settle：两者对 completed 轮都是空操作（reconcile 直接跳过、
        settle 排除当前 cache_key），却各自要先裸读节点行——正是收敛时坏掉的那条路。
        """
        if self._pending_notice_waits_for_children(node=parent):
            raise RuntimeError(
                'pending local notice must wait for the active child round to finish before starting a new spawn round'
            )
        return self._deliver_spawn_round_results(
            task_id=task_id,
            parent_node_id=parent.node_id,
            cache_key=cache_key,
            stage='replay_completed_round',
            entries_loader=lambda: self._read_spawn_round_ledger_entries(
                parent_node_id=parent.node_id,
                cache_key=cache_key,
            ),
            specs=specs,
        )

    async def _spawn_children_locked(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        specs: list[SpawnChildSpec],
        call_id: str | None,
    ) -> list[SpawnChildResult]:
        task = self._store.get_task(task_id)
        parent = self._store.get_node(parent_node_id)
        if task is None or parent is None:
            raise ValueError('parent task or node missing')
        if not parent.can_spawn_children:
            raise ValueError('spawn_child_nodes is not available for this node')
        cache_key = self._spawn_round_cache_key(specs=specs, call_id=call_id)
        cached = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
        if isinstance(cached, dict) and cached.get('completed'):
            return self._replay_completed_spawn_round(
                task_id=task.task_id,
                parent=parent,
                cache_key=cache_key,
                specs=specs,
            )
        self.reconcile_spawn_entry_child_bindings(
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            round_id=cache_key,
        )
        parent = self._store.get_node(parent_node_id) or parent
        if self._pending_notice_waits_for_children(node=parent):
            existing = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
            existing_incomplete_round = isinstance(existing, dict) and not bool(existing.get('completed'))
            if not existing_incomplete_round:
                raise RuntimeError(
                    'pending local notice must wait for the active child round to finish before starting a new spawn round'
                )
        await self._settle_superseded_spawn_operations(
            task=task,
            parent=parent,
            exclude_cache_key=cache_key,
            replacement_round_id=cache_key,
            replacing_goals={str(spec.goal or '').strip() for spec in specs},
        )
        parent = self._store.get_node(parent_node_id) or parent
        cached = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
        if isinstance(cached, dict) and cached.get('completed'):
            # 完成度从节点行派生、由调度器写在 `_spawn_operation_lock` 之外，所以 settle
            # 期间本轮可能刚好收敛；这时同样只能交付，不能落回物化路径。
            return self._replay_completed_spawn_round(
                task_id=task.task_id,
                parent=parent,
                cache_key=cache_key,
                specs=specs,
            )
        reused_results = self._completed_successful_spawn_results(
            parent=parent,
            specs=specs,
            exclude_cache_key=cache_key,
        )
        if reused_results is not None:
            return reused_results

        cached_payload = copy.deepcopy(cached) if isinstance(cached, dict) else {
            'specs': [item.model_dump(mode='json') for item in specs],
            'entries': [],
            'completed': False,
        }
        cached_payload['specs'] = [item.model_dump(mode='json') for item in specs]
        entries = list(cached_payload.get('entries') or [])
        while len(entries) < len(specs):
            entries.append({})
        cached_payload['entries'] = [
            self._normalize_spawn_entry(index=index, spec=spec, entry=entries[index])
            for index, spec in enumerate(specs)
        ]
        cached_payload['completed'] = False
        self._save_spawn_cache(task.task_id, parent.node_id, cache_key, cached_payload)

        spawn_review = await self._reuse_or_review_spawn_batch(
            task=task,
            parent=parent,
            specs=specs,
            cache_key=cache_key,
            cached_payload=cached_payload,
        )
        allowed_indexes = self._apply_spawn_review_results(
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            cache_key=cache_key,
            cached_payload=cached_payload,
            specs=specs,
            spawn_review=spawn_review,
        )
        allowed_indexes = self._apply_spawn_creation_gates(
            task=task,
            parent=parent,
            specs=specs,
            allowed_indexes=allowed_indexes,
            cached_payload=cached_payload,
        )

        if allowed_indexes:
            await self._admit_spawn_batch(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                spec_count=len(allowed_indexes),
            )
        self._materialize_spawn_batch_children(
            task=task,
            parent=parent,
            specs=specs,
            allowed_indexes=allowed_indexes,
            cache_key=cache_key,
            cached_payload=cached_payload,
        )

        entries = list(cached_payload.get('entries') or [])
        dispatch_callback = getattr(self, 'dispatch_node_callback', None)
        dispatched_ids: list[str] = []
        for index in range(len(specs)):
            entry = dict((entries[index] if index < len(entries) else {}) or {})
            child_node_id = str(entry.get('child_node_id') or '').strip()
            if not child_node_id:
                continue
            if str(entry.get('review_decision') or '').strip().lower() == 'blocked':
                continue
            requires_acceptance = bool(entry.get('requires_acceptance'))
            self._update_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                status='running',
                started_at=str(entry.get('started_at') or _now()),
                finished_at='',
                check_status='pending' if requires_acceptance else 'skipped',
            )
            if callable(dispatch_callback):
                try:
                    if dispatch_callback(task.task_id, child_node_id):
                        dispatched_ids.append(child_node_id)
                except Exception as exc:  # noqa: BLE001 - 派发失败由可派发扫描兜底
                    logger.warning(
                        'title=spawn_immediate_dispatch_failed task={} parent={} child={} error={}',
                        task.task_id,
                        parent.node_id,
                        child_node_id,
                        describe_exception(exc),
                    )
        # 拆掉阻塞承诺：这里只做创建 + 交棒给调度器 + 立即返回。轮次 completed 由
        # `reconcile_spawn_round_completion` 按节点行派生，不再由本协程宣布；子节点的
        # 结果走 `child_final` 消息，父节点用 `enter_wait(all_children)` 等它们。
        self._save_spawn_cache(task.task_id, parent.node_id, cache_key, cached_payload)
        receipts = self._spawn_creation_receipts(specs=specs, entries=list(cached_payload.get('entries') or []))
        self._emit_spawn_diagnostic(
            title='spawn_round_created_without_waiting',
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            cache_key=cache_key,
            index=None,
            entry=None,
            detail=f'created={len(receipts)} dispatched={len(dispatched_ids)}',
        )
        return receipts

    @staticmethod
    def _depth_allows_spawn(parent_depth: int, max_depth: int) -> bool:
        """深度闸门的唯一判据（派生闸门第 1 条）。

        语义沿用既有实现：`max_depth` 是允许的层级数，深 `d` 的节点能派生当且仅当
        `d + 1 < max_depth`。不要在别处再抄一份（旧代码在 `can_spawn_children`
        与工具摘除两处各写了一次）。
        """
        try:
            return int(parent_depth) + 1 < int(max_depth)
        except (TypeError, ValueError):
            return False

    def _apply_spawn_creation_gates(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        allowed_indexes: list[int],
        cached_payload: dict[str, Any],
    ) -> list[int]:
        """派生闸门的创建期判定：深度与本任务节点总数。

        与 `review_spawn_candidates`（第 8 条闸门）分开：这两条是机器可判的资源约束，
        不需要模型参与。被拒候选写入 entry 的 `creation_gate` / `creation_reject_reason`，
        由 `_spawn_creation_receipts` 转成 `rejected[]`；**不物化、不派发**。
        节点额度把验收节点算进去：它是资源约束的一部分，只是不参与完成判据。
        """
        if not allowed_indexes:
            return []
        entries = list(cached_payload.get('entries') or [])
        try:
            current_total = len(self._store.list_nodes(task.task_id) or [])
        except Exception:
            current_total = 0
        max_nodes = max(1, int(getattr(self, '_max_task_nodes', 200) or 200))
        budget = max_nodes - current_total
        depth_ok = self._depth_allows_spawn(parent.depth, int(getattr(task, 'max_depth', 0) or 0))
        depth_reason = (
            f'已达最大派生深度（max_depth={int(getattr(task, "max_depth", 0) or 0)}，'
            f'当前深度={parent.depth}）'
        )
        kept: list[int] = []
        for index in allowed_indexes:
            if index >= len(entries) or not isinstance(entries[index], dict):
                continue
            entry = entries[index]
            spec = specs[index] if index < len(specs) else None
            if not depth_ok:
                entry['creation_gate'] = 'depth'
                entry['creation_reject_reason'] = depth_reason
                continue
            requires_acceptance = bool(
                getattr(spec, 'requires_acceptance', None)
                if getattr(spec, 'requires_acceptance', None) is not None
                else str(getattr(spec, 'acceptance_prompt', '') or '').strip()
            )
            cost = 1 + (1 if requires_acceptance else 0)
            if cost > budget:
                entry['creation_gate'] = 'max_nodes'
                entry['creation_reject_reason'] = (
                    f'任务节点总数上限 {max_nodes} 已不足以容纳本批：'
                    f'剩余额度 {max(budget, 0)}，本候选需要 {cost} 个节点（含验收节点）。'
                )
                continue
            budget -= cost
            kept.append(index)
        cached_payload['entries'] = entries
        return kept

    def _materialized_spawn_round_review(self, *, specs: list[SpawnChildSpec]) -> dict[str, Any]:
        specs_list = list(specs or [])
        return {
            'reviewed_at': _now(),
            'requested_specs': [
                self._spawn_review_requested_spec_payload(index=index, spec=spec)
                for index, spec in enumerate(specs_list)
            ],
            'allowed_indexes': list(range(len(specs_list))),
            'blocked_specs': [],
            'error_text': '',
        }

    async def _reuse_or_review_spawn_batch(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        cache_key: str,
        cached_payload: dict[str, Any],
    ) -> dict[str, Any]:
        existing_review = cached_payload.get('spawn_review')
        if isinstance(existing_review, dict) and existing_review:
            return existing_review
        # A round that already owns child/acceptance bindings must never be
        # re-reviewed as if it were a fresh logical operation. Treat materialized
        # entries as allowed so recovery resumes and waits on the existing subtree.
        materialized = any(
            isinstance(entry, dict)
            and (
                str(entry.get('child_node_id') or '').strip()
                or str(entry.get('acceptance_node_id') or '').strip()
            )
            for entry in list(cached_payload.get('entries') or [])
        )
        if materialized:
            return self._materialized_spawn_round_review(specs=specs)
        return await self._review_spawn_batch(
            task=task,
            parent=parent,
            specs=specs,
            cache_key=cache_key,
        )

    async def _review_spawn_batch(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        cache_key: str,
    ) -> dict[str, Any]:
        if not specs:
            return {
                'reviewed_at': _now(),
                'requested_specs': [],
                'allowed_indexes': [],
                'blocked_specs': [],
                'error_text': '',
            }
        backend = getattr(self._react_loop, '_chat_backend', None)
        if backend is None or not callable(getattr(backend, 'chat', None)):
            return self._default_spawn_review_result(
                specs=specs,
                reason='RuntimeError: spawn review model chain is unavailable',
            )
        messages = self._spawn_review_messages(
            task=task,
            parent=parent,
            specs=specs,
            cache_key=cache_key,
        )
        # 评审车道不过节点 preflight，也没有 actual-request 工件；把正文规模记进
        # spawn_review 是本车道唯一可留存的体积证据（_record_spawn_review_token_usage
        # 只把 usage 并进父节点聚合，事后无法还原是哪一次评审花的）。
        review_request_chars = sum(len(str(item.get('content') or '')) for item in messages if isinstance(item, dict))
        tools = [self._spawn_review_tool_schema()]
        model_refs = list(self._acceptance_model_refs or self._execution_model_refs)
        if not model_refs:
            return self._default_spawn_review_result(
                specs=specs,
                reason='RuntimeError: spawn review inspection model chain is empty',
                review_request_chars=review_request_chars,
            )
        invalid_response_count = 0
        usage_attempts: list[Any] = []
        while True:
            request_messages = (
                [
                    *messages,
                    {'role': 'user', 'content': self._spawn_review_repair_message(attempt_count=invalid_response_count)},
                ]
                if invalid_response_count > 0
                else list(messages)
            )
            # Spawn review is an external inspection lane. It intentionally does not reuse
            # node send token preflight, which only applies to execution/acceptance sends
            # plus message_distribution control turns.
            try:
                response = await backend.chat(
                    messages=request_messages,
                    tools=tools,
                    model_refs=model_refs,
                )
            except Exception as exc:
                return self._default_spawn_review_result(
                    specs=specs,
                    reason=describe_exception(exc),
                    review_attempts=invalid_response_count + 1,
                    review_request_chars=review_request_chars,
                )
            usage_attempts.extend(list(response.attempts or []))
            parsed = self._parse_spawn_review_response(response, spec_count=len(specs))
            if parsed is not None:
                self._record_spawn_review_token_usage(task=task, parent=parent, usage_attempts=usage_attempts)
                return {
                    'reviewed_at': _now(),
                    'review_outcome': 'verdict',
                    'requested_specs': [self._spawn_review_requested_spec_payload(index=index, spec=spec) for index, spec in enumerate(specs)],
                    'review_attempts': invalid_response_count + 1,
                    'review_request_chars': review_request_chars,
                    **parsed,
                }
            invalid_response_count += 1
            if invalid_response_count >= _SPAWN_REVIEW_MAX_ATTEMPTS:
                return self._default_spawn_review_result(
                    specs=specs,
                    reason=(
                        f'RuntimeError: spawn review kept returning unparseable decisions '
                        f'({_SPAWN_REVIEW_MAX_ATTEMPTS} attempts)'
                    ),
                    review_attempts=invalid_response_count,
                    review_request_chars=review_request_chars,
                )
            await asyncio.sleep(_SPAWN_REVIEW_RETRY_DELAY_SECONDS)

    def _record_spawn_review_token_usage(self, *, task, parent: NodeRecord, usage_attempts: list[Any]) -> None:
        attempt_list = list(usage_attempts or [])
        if not attempt_list:
            return
        try:
            self._log_service.record_node_attempt_token_usage(task.task_id, parent.node_id, attempt_list)
        except Exception:
            pass

    @staticmethod
    def _spawn_review_tool_schema() -> dict[str, Any]:
        return {
            'type': 'function',
            'function': {
                'name': _SPAWN_REVIEW_TOOL_NAME,
                'description': '审查本次 spawn_child_nodes 请求，决定允许原样放行哪些 specs，并给出被拦截项的原因与建议。',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'allowed_indexes': {
                            'type': 'array',
                            'items': {'type': 'integer'},
                            'description': '允许原样放行的原始 spec 索引列表。',
                        },
                        'blocked_specs': {
                            'type': 'array',
                            'description': '被拦截的原始 spec 列表，每项都必须对应原始索引。',
                            'items': {
                                'type': 'object',
                                'properties': {
                                    'index': {'type': 'integer'},
                                    'reason': {'type': 'string'},
                                    'suggestion': {'type': 'string'},
                                },
                                'required': ['index', 'reason', 'suggestion'],
                                'additionalProperties': False,
                            },
                        },
                    },
                    'required': ['allowed_indexes', 'blocked_specs'],
                    'additionalProperties': False,
                },
            },
        }

    def _spawn_review_messages(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        cache_key: str,
    ) -> list[dict[str, Any]]:
        return [
            {'role': 'system', 'content': load_prompt('spawn_child_review.md').strip()},
            {
                'role': 'user',
                'content': json.dumps(
                    self._spawn_review_context(
                        task=task,
                        parent=parent,
                        specs=specs,
                        cache_key=cache_key,
                    ),
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ]

    def _spawn_review_context(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        cache_key: str,
    ) -> dict[str, Any]:
        root = self._store.get_node(task.root_node_id)
        return {
            'task_id': str(task.task_id or ''),
            'parent_node_id': str(parent.node_id or ''),
            'parent_goal': str(parent.goal or ''),
            'task_title': str(getattr(task, 'title', '') or ''),
            'user_request': str(getattr(task, 'user_request', '') or ''),
            'core_requirement': self._resolve_core_requirement(task),
            'root_prompt': str(getattr(root, 'prompt', '') or ''),
            'consumed_distribution_notices': self._spawn_review_consumed_distribution_notices(parent=parent),
            'path_tree_text': self._spawn_review_path_tree_text(task_id=task.task_id, parent=parent),
            'path_nodes': self._spawn_review_path_nodes(task_id=task.task_id, parent=parent),
            'tree_summary': self._spawn_review_tree_summary(task_id=task.task_id, parent=parent),
            'spawn_request': {
                'call_id': str(cache_key or ''),
                'requested_specs': [
                    self._spawn_review_requested_spec_payload(index=index, spec=spec)
                    for index, spec in enumerate(specs)
                ],
            },
        }

    @staticmethod
    def _spawn_review_consumed_distribution_notices(*, parent: NodeRecord) -> list[dict[str, Any]]:
        metadata = dict(parent.metadata or {}) if isinstance(parent.metadata, dict) else {}
        context = normalize_append_notice_context(metadata.get(APPEND_NOTICE_CONTEXT_KEY))
        return [dict(item) for item in list(context.get('notice_records') or []) if isinstance(item, dict)]

    def _spawn_review_path_tree_text(self, *, task_id: str, parent: NodeRecord) -> str:
        path_nodes: list[NodeRecord] = []
        seen: set[str] = set()
        current: NodeRecord | None = parent
        while current is not None:
            node_id = str(current.node_id or '').strip()
            if not node_id or node_id in seen:
                break
            seen.add(node_id)
            path_nodes.append(current)
            parent_id = str(current.parent_node_id or '').strip()
            current = self._store.get_node(parent_id) if parent_id else None
        path_nodes.reverse()
        lines: list[str] = []
        for depth, node in enumerate(path_nodes):
            goal = self._trim_diagnostic_text(node.goal, max_chars=80)
            lines.append(f'{"  " * depth}- ({node.node_id},{node.status},{goal})')
        return '\n'.join(lines) if lines else '(empty path)'

    def _spawn_review_path_nodes(self, *, task_id: str, parent: NodeRecord) -> list[dict[str, Any]]:
        path_nodes: list[NodeRecord] = []
        seen: set[str] = set()
        current: NodeRecord | None = parent
        while current is not None:
            node_id = str(current.node_id or '').strip()
            if not node_id or node_id in seen:
                break
            seen.add(node_id)
            path_nodes.append(current)
            parent_id = str(current.parent_node_id or '').strip()
            current = self._store.get_node(parent_id) if parent_id else None
        path_nodes.reverse()
        return [
            {
                'node_id': str(node.node_id or ''),
                'depth': int(node.depth or 0),
                'status': str(node.status or ''),
                'goal': self._trim_diagnostic_text(node.goal, max_chars=400),
                'prompt': self._trim_diagnostic_text(node.prompt, max_chars=1600),
                'recent_text': NodeRunner._spawn_review_recent_text(node),
            }
            for node in path_nodes
        ]

    def _spawn_review_tree_summary(self, *, task_id: str, parent: NodeRecord) -> dict[str, Any]:
        # 读投影表而不是运行时 `nodes`：这里只要"前 10 行分支文本 + 一个计数"，
        # 旧写法却把全任务 975 行 / 229 MB 建成 NodeRecord（长块榜点名：
        # `sqlite.query.fetchall:nodes[from=_spawn_review_tree_summary]` 1,715 ms）。
        # 投影行的 `title` 就是 `goal or node_id`、`sort_key` 就是 `created_at:node_id`，
        # 所以按 (depth, sort_key) 排出来的分支集合与原来一致。
        list_task_nodes = getattr(self._store, 'list_task_nodes', None)
        try:
            nodes = list(list_task_nodes(task_id) or []) if callable(list_task_nodes) else []
        except Exception:
            nodes = []
        path_ids: set[str] = set()
        current: NodeRecord | None = parent
        while current is not None:
            node_id = str(current.node_id or '').strip()
            if node_id:
                path_ids.add(node_id)
            parent_id = str(current.parent_node_id or '').strip()
            current = self._store.get_node(parent_id) if parent_id else None
        others = [
            node
            for node in nodes
            if str(getattr(node, 'node_id', '') or '').strip() not in path_ids
        ]
        others.sort(
            key=lambda item: (
                int(getattr(item, 'depth', 0) or 0),
                str(getattr(item, 'sort_key', '') or ''),
                str(getattr(item, 'node_id', '') or ''),
            )
        )
        lines: list[str] = []
        for node in others[:10]:
            recent_text = NodeRunner._spawn_review_recent_text(node)
            lines.append(
                '{} {}- ({},{},{},{})'.format(
                    '  ' * int(getattr(node, 'depth', 0) or 0),
                    int(getattr(node, 'depth', 0) or 0),
                    getattr(node, 'node_id', ''),
                    getattr(node, 'status', ''),
                    self._trim_diagnostic_text(
                        '' if str(getattr(node, 'title', '') or '') == str(getattr(node, 'node_id', '') or '')
                        else str(getattr(node, 'title', '') or ''),
                        max_chars=80,
                    ),
                    recent_text,
                )
            )
        return {
            'execution_node_count': len(nodes),
            'visible_other_branch_lines': lines,
        }

    @staticmethod
    def _spawn_review_recent_text(node: Any) -> str:
        for entry in reversed(list(getattr(node, 'output', None) or [])):
            text = str(getattr(entry, 'content', '') or '').strip()
            if text:
                return ' '.join(text.split())[:120]
        payload = getattr(node, 'payload', None)
        if isinstance(payload, dict):
            text = str(payload.get('latest_progress_text') or '').strip()
            if text:
                return ' '.join(text.split())[:120]
        return ''

    @staticmethod
    def _spawn_review_requested_spec_payload(*, index: int, spec: SpawnChildSpec) -> dict[str, Any]:
        """审查车道看到的候选 = 运行时将创建的节点集合，不是模型入参的字面平铺。

        `acceptance_prompt` 与 goal/prompt 同级时，评审会把它读成"验收内嵌、自产自销"并建议
        把生成与它的验收排进同批（事故 task:9b89e2cfc6a5），故按 `_ensure_spawn_acceptance_node`
        的真实物化结果展开。原始入参仍单独存于 `spawn_operations[round]['specs']`。
        """
        goal = str(spec.goal or '')
        requires_acceptance = _spec_requires_acceptance(spec)
        runtime_nodes: list[dict[str, Any]] = [{'node_kind': 'execution'}]
        if requires_acceptance:
            runtime_nodes.append(
                {
                    'node_kind': 'acceptance',
                    'goal': _spawn_acceptance_goal(goal),
                    'parent_node': '上一条 execution 节点（本候选自己的子节点，不是兄弟候选）',
                    'activation': '本候选整条子管线终态后才激活',
                    'acceptance_prompt': str(spec.acceptance_prompt or ''),
                }
            )
        return {
            'index': int(index),
            'goal': goal,
            'prompt': str(spec.prompt or ''),
            'execution_policy': normalize_execution_policy_metadata(spec.execution_policy.model_dump(mode='json')).model_dump(mode='json'),
            'requires_acceptance': requires_acceptance,
            'runtime_nodes': runtime_nodes,
        }

    @classmethod
    def _parse_spawn_review_response(cls, response: Any, *, spec_count: int) -> dict[str, Any] | None:
        tool_calls = list(getattr(response, 'tool_calls', []) or [])
        for call in tool_calls:
            name = ''
            arguments: Any = None
            if isinstance(call, dict):
                name = str(call.get('name') or '').strip()
                arguments = call.get('arguments')
            else:
                name = str(getattr(call, 'name', '') or '').strip()
                arguments = getattr(call, 'arguments', None)
            if name != _SPAWN_REVIEW_TOOL_NAME:
                continue
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    return None
            if not isinstance(arguments, dict):
                return None
            return cls._normalize_spawn_review_arguments(arguments, spec_count=spec_count)
        return cls._parse_spawn_review_content(getattr(response, 'content', None), spec_count=spec_count)

    @classmethod
    def _parse_spawn_review_content(cls, content: Any, *, spec_count: int) -> dict[str, Any] | None:
        text = str(content or '').strip()
        if not text:
            return None
        for candidate in cls._extract_json_object_candidates(text):
            try:
                payload = json.loads(candidate)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            normalized = cls._normalize_spawn_review_arguments(payload, spec_count=spec_count)
            if normalized is not None:
                return normalized
        return None

    @staticmethod
    def _extract_json_object_candidates(content: Any) -> list[str]:
        text = str(content or '')
        candidates: list[str] = []
        start_index: int | None = None
        depth = 0
        in_string = False
        escape = False
        for index, char in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif char == '\\':
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == '{':
                if depth == 0:
                    start_index = index
                depth += 1
                continue
            if char == '}' and depth > 0:
                depth -= 1
                if depth == 0 and start_index is not None:
                    candidates.append(text[start_index : index + 1])
                    start_index = None
        candidates.reverse()
        return candidates

    @staticmethod
    def _spawn_review_repair_message(*, attempt_count: int) -> str:
        return (
            f'{_SPAWN_REVIEW_REPAIR_PREFIX} '
            f'当前为第 {max(1, int(attempt_count) + 1)} 次修复尝试。'
            '不要输出解释性普通文本。'
            f'优先通过工具调用 `{_SPAWN_REVIEW_TOOL_NAME}` 返回；'
            '如果当前模型不支持工具调用，则只输出一个合法 JSON 对象，'
            '且必须严格包含 `allowed_indexes` 和 `blocked_specs` 两个字段。'
        )

    @classmethod
    def _normalize_spawn_review_arguments(cls, payload: dict[str, Any], *, spec_count: int) -> dict[str, Any] | None:
        raw_allowed = payload.get('allowed_indexes')
        raw_blocked = payload.get('blocked_specs')
        if not isinstance(raw_allowed, list) or not isinstance(raw_blocked, list):
            return None
        allowed_indexes: list[int] = []
        seen_allowed: set[int] = set()
        for item in raw_allowed:
            try:
                index = int(item)
            except (TypeError, ValueError):
                return None
            if index < 0 or index >= spec_count or index in seen_allowed:
                continue
            seen_allowed.add(index)
            allowed_indexes.append(index)
        blocked_by_index: dict[int, dict[str, Any]] = {}
        for item in raw_blocked:
            if not isinstance(item, dict):
                return None
            try:
                index = int(item.get('index'))
            except (TypeError, ValueError):
                return None
            if index < 0 or index >= spec_count or index in seen_allowed:
                continue
            blocked_by_index[index] = {
                'index': index,
                'reason': str(item.get('reason') or '').strip(),
                'suggestion': str(item.get('suggestion') or '').strip(),
            }
        blocked_specs: list[dict[str, Any]] = []
        for index in range(spec_count):
            if index in seen_allowed:
                continue
            blocked_payload = blocked_by_index.get(index) or {
                'index': index,
                'reason': _SPAWN_REVIEW_DEFAULT_BLOCK_REASON,
                'suggestion': _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
            }
            blocked_specs.append(
                {
                    'index': index,
                    'reason': str(blocked_payload.get('reason') or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_REASON,
                    'suggestion': str(blocked_payload.get('suggestion') or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
                }
            )
        return {
            'allowed_indexes': allowed_indexes,
            'blocked_specs': blocked_specs,
            'error_text': '',
        }

    def _default_spawn_review_result(
        self,
        *,
        specs: list[SpawnChildSpec],
        reason: str,
        review_attempts: int = 0,
        review_request_chars: int = 0,
    ) -> dict[str, Any]:
        normalized_reason = str(reason or 'RuntimeError: spawn review failed').strip() or 'RuntimeError: spawn review failed'
        return {
            'reviewed_at': _now(),
            'review_outcome': 'system_failure',
            'requested_specs': [self._spawn_review_requested_spec_payload(index=index, spec=spec) for index, spec in enumerate(specs)],
            'review_attempts': max(0, int(review_attempts or 0)),
            'review_request_chars': max(0, int(review_request_chars or 0)),
            'allowed_indexes': [],
            'blocked_specs': [
                {
                    'index': index,
                    'reason': normalized_reason,
                    'suggestion': _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
                }
                for index, _spec in enumerate(specs)
            ],
            'error_text': normalized_reason,
        }

    def _apply_spawn_review_results(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        cached_payload: dict[str, Any],
        specs: list[SpawnChildSpec],
        spawn_review: dict[str, Any],
    ) -> list[int]:
        allowed_indexes = [
            int(item)
            for item in list(spawn_review.get('allowed_indexes') or [])
            if 0 <= int(item) < len(specs)
        ]
        system_failure = str(spawn_review.get('review_outcome') or '').strip() == 'system_failure'
        allowed_set = set(allowed_indexes)
        blocked_by_index = {
            int(item.get('index') or 0): dict(item)
            for item in list(spawn_review.get('blocked_specs') or [])
            if isinstance(item, dict)
        }
        for index, spec in enumerate(list(specs or [])):
            if index in allowed_set:
                self._update_spawn_entry(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                    review_decision='allowed',
                    blocked_reason='',
                    blocked_suggestion='',
                    synthetic_result_summary='',
                    runtime_error_text='',
                )
                continue
            blocked_payload = blocked_by_index.get(index) or {
                'reason': _SPAWN_REVIEW_DEFAULT_BLOCK_REASON,
                'suggestion': _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
            }
            # 故障态下不能用"未批准该候选"兜底——那会把基建故障说成评审否决。
            default_reason = '检验派生车道故障，本批未经审查。' if system_failure else _SPAWN_REVIEW_DEFAULT_BLOCK_REASON
            result = self._spawn_review_blocked_result(
                spec,
                reason=str(blocked_payload.get('reason') or '').strip() or default_reason,
                suggestion=str(blocked_payload.get('suggestion') or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION,
                system_failure=system_failure,
            )
            entries_so_far = list(cached_payload.get('entries') or [])
            current_entry = dict(entries_so_far[index] or {}) if index < len(entries_so_far) else {}
            live_node_reason = self._spawn_entry_non_terminal_node(current_entry)
            if live_node_reason:
                # The blocked spec already owns a live execution/acceptance node.
                # Its review outcome must not overwrite the real pipeline status —
                # the materialized subtree still needs to run to a terminal state.
                self._warn_spawn_entry_review_over_live_node(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    index=index,
                    entry=current_entry,
                    reason=live_node_reason,
                )
                self._update_spawn_entry(
                    task_id=task_id,
                    parent_node_id=parent_node_id,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                    review_decision='blocked',
                    blocked_reason=str(blocked_payload.get('reason') or '').strip(),
                    blocked_suggestion=str(blocked_payload.get('suggestion') or '').strip(),
                    synthetic_result_summary=str(result.node_output_summary or ''),
                    runtime_error_text='',
                )
                continue
            self._update_spawn_entry(
                task_id=task_id,
                parent_node_id=parent_node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                status='success',
                started_at=str(spawn_review.get('reviewed_at') or _now()),
                finished_at=str(spawn_review.get('reviewed_at') or _now()),
                check_status='skipped',
                review_decision='blocked',
                blocked_reason=str(blocked_payload.get('reason') or '').strip(),
                blocked_suggestion=str(blocked_payload.get('suggestion') or '').strip(),
                synthetic_result_summary=str(result.node_output_summary or ''),
                runtime_error_text='',
            )
        cached_payload['spawn_review'] = {
            'round_id': str(cache_key or ''),
            'reviewed_at': str(spawn_review.get('reviewed_at') or _now()),
            'requested_specs': [dict(item) for item in list(spawn_review.get('requested_specs') or []) if isinstance(item, dict)],
            'allowed_indexes': list(allowed_indexes),
            'blocked_specs': [dict(item) for item in list(spawn_review.get('blocked_specs') or []) if isinstance(item, dict)],
            'error_text': str(spawn_review.get('error_text') or '').strip(),
            # 'verdict' = 审过并给出裁决；'system_failure' = 车道自身故障、本批未经审查。
            # 账本上两者过去同为 blocked + status=success，不分型就只能靠读 reason 文本猜。
            'review_outcome': 'system_failure' if system_failure else 'verdict',
            # 车道证据只有落进这份持久载荷才读得到（投影从 spawn_operations 重读），
            # 少写这两个键就等于评审请求多大、重发过几次仍然不可知。
            'review_attempts': int(spawn_review.get('review_attempts') or 0),
            'review_request_chars': int(spawn_review.get('review_request_chars') or 0),
        }
        self._save_spawn_cache(task_id, parent_node_id, cache_key, cached_payload)
        return allowed_indexes

    @staticmethod
    def _spawn_review_blocked_text(*, reason: str, suggestion: str, system_failure: bool = False) -> str:
        if system_failure:
            # 与"审了并被拒"分开：父节点若把基建故障当评审意见，会去改写 spec 而不是
            # 结束节点或等待恢复，所以这里直接给出可执行的终态调用（字段名照 schema 写）。
            failure_reason = str(reason or '').strip() or '检验派生车道故障，本批未经审查。'
            return (
                f'派生未被审查：检验车道自身故障，本批候选一律未放行（未创建、未执行）。错误信息：{failure_reason}。'
                '这不是评审否决，不要靠改写候选 spec 来规避。'
                '若你在不派生子节点的情况下无法继续完成任务，调用 '
                "submit_final_result(status='failed', delivery_status='blocked', "
                f"blocking_reason='派生审查车道故障：{failure_reason}') 结束本节点并说明原因"
                '（submit_final_result 必须是本回合唯一的工具调用）；'
                '否则请在当前父节点内自行完成这一步，或改为更小批次的派生。'
            )
        normalized_reason = str(reason or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_REASON
        normalized_suggestion = str(suggestion or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_SUGGESTION
        return f'派生已被拦截。原因：{normalized_reason}。建议：{normalized_suggestion}'

    @staticmethod
    def _spawn_review_blocked_summary(*, reason: str, system_failure: bool = False) -> str:
        if system_failure:
            return f'派生未审查：{str(reason or "").strip() or "检验派生车道故障。"}'
        normalized_reason = str(reason or '').strip() or _SPAWN_REVIEW_DEFAULT_BLOCK_REASON
        return f'派生拦截：{normalized_reason}'

    @classmethod
    def _spawn_review_blocked_result(
        cls,
        spec: SpawnChildSpec,
        *,
        reason: str,
        suggestion: str,
        system_failure: bool = False,
    ) -> SpawnChildResult:
        output_text = cls._spawn_review_blocked_text(
            reason=reason,
            suggestion=suggestion,
            system_failure=system_failure,
        )
        summary_text = cls._spawn_review_blocked_summary(reason=reason, system_failure=system_failure)
        return SpawnChildResult(
            goal=spec.goal,
            check_result=(
                _SPAWN_REVIEW_SYSTEM_FAILURE_CHECK_RESULT if system_failure else _SPAWN_REVIEW_BLOCKED_CHECK_RESULT
            ),
            node_output=output_text,
            node_output_summary=summary_text,
            node_output_ref='',
            failure_info=cls._runtime_spawn_failure_info(reason) if system_failure else None,
            review_blocked=True,
        )

    @classmethod
    def _spawn_review_blocked_result_by_goal(
        cls,
        *,
        goal: str,
        reason: str,
        suggestion: str,
        system_failure: bool = False,
    ) -> SpawnChildResult:
        output_text = cls._spawn_review_blocked_text(
            reason=reason,
            suggestion=suggestion,
            system_failure=system_failure,
        )
        summary_text = cls._spawn_review_blocked_summary(reason=reason, system_failure=system_failure)
        return SpawnChildResult(
            goal=str(goal or '').strip(),
            check_result=(
                _SPAWN_REVIEW_SYSTEM_FAILURE_CHECK_RESULT if system_failure else _SPAWN_REVIEW_BLOCKED_CHECK_RESULT
            ),
            node_output=output_text,
            node_output_summary=summary_text,
            node_output_ref='',
            failure_info=cls._runtime_spawn_failure_info(reason) if system_failure else None,
            review_blocked=True,
        )

    @staticmethod
    def _normalize_optional_limit(value: int | None | object, *, default: int | None) -> int | None:
        if value is _UNSET:
            value = default
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return max(0, int(value))

    @staticmethod
    def _parallel_slot_count(limit: int | None, item_count: int, *, enabled: bool) -> int:
        if not enabled or item_count <= 1:
            return 1
        if limit is None:
            return max(1, item_count)
        return max(1, int(limit) if int(limit) > 0 else 1)

    def _spawn_entry_node_ids(self, entry: dict[str, Any]) -> list[str]:
        node_ids: list[str] = []
        for field in ('child_node_id', 'acceptance_node_id'):
            node_id = str(entry.get(field) or '').strip()
            if node_id:
                node_ids.append(node_id)
        return node_ids

    def _spawn_entry_non_terminal_node(self, entry: dict[str, Any]) -> str:
        """Return why an entry's bound node is still non-terminal.

        Returns one of ``''``, ``'execution'``, or ``'acceptance'``. The real
        node status — not the synthetic entry status — is the authoritative
        signal that a materialized pipeline is still running.

        A missing row is skipped, and that is deliberately not a terminal
        verdict: within a live task a bound node id cannot go absent, because
        ``SqliteStore.delete_nodes`` has no callers in ``main/`` (every other
        delete is task-wide) and ``_fetch_one_on`` re-raises ``sqlite3.Error``
        instead of turning a read fault into ``None`` — so a failing store
        surfaces as an exception, never as a missing row. Do not "harden" this
        by reading a missing row as terminal, and do not add a runtime branch
        for it: it would be a guard that can never fire.
        """
        for field, reason in (('child_node_id', 'execution'), ('acceptance_node_id', 'acceptance')):
            node_id = str(entry.get(field) or '').strip()
            if not node_id:
                continue
            node = self._store.get_node(node_id)
            if node is None:
                continue
            if self._normalized_status(getattr(node, 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}:
                return reason
        return ''

    def _spawn_entry_activity(self, entry: dict[str, Any]) -> tuple[bool, str]:
        """Return ``(is_active, reason)`` for a spawn entry.

        ``reason`` distinguishes *why* the entry is active:

        * ``'entry'`` — the entry status itself is ``queued``/``running``;
        * ``'execution'`` — the bound execution node is non-terminal;
        * ``'acceptance'`` — the bound acceptance node is non-terminal.

        A ``review_decision=blocked`` entry that still owns a live execution or
        acceptance node is therefore treated as active, never as complete.
        """
        status = str(entry.get('status') or '').strip().lower()
        if status in {'queued', 'running'}:
            return True, 'entry'
        non_terminal = self._spawn_entry_non_terminal_node(entry)
        if non_terminal:
            return True, non_terminal
        return False, ''

    def _spawn_entry_is_active(self, entry: dict[str, Any]) -> bool:
        active, _reason = self._spawn_entry_activity(entry)
        return active

    def spawn_entry_settlement_reason(self, node_id: str) -> str:
        """该节点绑定的 spawn entry 是否已作为结果结清并交付给父节点。

        非空返回＝"复活它的工作不会被任何等待方采纳"：轮次 `completed` 意味着那份工具
        结果已经进了父节点的消息链（收不回），entry 记为 `error` 意味着父节点模型已经
        读到"这个子节点失败"。此时把节点从暂停里救活只会让下一次派生按
        `superseded by newer spawn round` 把它连同绑定的验收节点一起清扫掉。
        """
        normalized_node_id = str(node_id or '').strip()
        if not normalized_node_id:
            return ''
        node = self._store.get_node(normalized_node_id)
        if node is None:
            return ''
        metadata = getattr(node, 'metadata', None)
        if not isinstance(metadata, dict):
            return ''
        parent_node_id = str(metadata.get('spawn_owner_parent_node_id') or '').strip()
        owner_round_id = str(metadata.get('spawn_owner_round_id') or '').strip()
        if not parent_node_id or not owner_round_id:
            return ''
        parent = self._store.get_node(parent_node_id)
        if parent is None or not isinstance(parent.metadata, dict):
            return ''
        operations = parent.metadata.get('spawn_operations')
        if not isinstance(operations, dict):
            return ''
        payload = operations.get(owner_round_id)
        if not isinstance(payload, dict) or not bool(payload.get('completed')):
            return ''
        for entry in list(payload.get('entries') or []):
            if not isinstance(entry, dict):
                continue
            bound_ids = {
                str(entry.get('child_node_id') or '').strip(),
                str(entry.get('acceptance_node_id') or '').strip(),
            }
            if normalized_node_id not in bound_ids:
                continue
            if self._normalized_status(entry.get('status')) != 'error':
                return ''
            return f'spawn round {owner_round_id} already delivered entry[{entry.get("index")}] as error'
        return ''

    @staticmethod
    def _emit_spawn_diagnostic(
        *,
        title: str,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        index: int | None,
        entry: dict[str, Any] | None,
        detail: str,
    ) -> None:
        child_node_id = str((entry or {}).get('child_node_id') or '').strip()
        acceptance_node_id = str((entry or {}).get('acceptance_node_id') or '').strip()
        logger.warning(
            '[g3ku spawn diagnostic] '
            f'title={title} task_id={str(task_id or "").strip()} '
            f'parent_node_id={str(parent_node_id or "").strip()} round_id={str(cache_key or "").strip()} '
            f'entry_index={"" if index is None else str(index)} '
            f'entry_status={(entry or {}).get("status") or ""} '
            f'review_decision={(entry or {}).get("review_decision") or ""} '
            f'has_synthetic_result={bool((entry or {}).get("synthetic_result_summary"))} '
            f'child_node_id={child_node_id} acceptance_node_id={acceptance_node_id} '
            f'detail={str(detail or "").strip()}'
        )

    def _warn_spawn_entry_review_over_live_node(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        index: int,
        entry: dict[str, Any],
        reason: str,
    ) -> None:
        self._emit_spawn_diagnostic(
            title='spawn_blocked_review_over_materialized_round',
            task_id=task_id,
            parent_node_id=parent_node_id,
            cache_key=cache_key,
            index=index,
            entry=entry,
            detail=f'blocked review outcome did not terminate a live {reason} node',
        )

    def _warn_spawn_entry_inconsistent_terminal(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        index: int,
        entry: dict[str, Any],
        reason: str,
    ) -> None:
        self._emit_spawn_diagnostic(
            title='spawn_entry_terminal_with_live_node',
            task_id=task_id,
            parent_node_id=parent_node_id,
            cache_key=cache_key,
            index=index,
            entry=entry,
            detail=f'entry status is terminal/blocked while a bound {reason} node is non-terminal',
        )

    def _settle_spec_fault(
        self,
        *,
        task,
        parent: NodeRecord,
        cache_key: str,
        cached_payload: dict[str, Any],
        index: int,
        spec: SpawnChildSpec,
        exc: BaseException,
    ) -> SpawnChildResult | None:
        """子管线被异常打断时的唯一结算道：判决从绑定节点的真实状态派生。

        返回 `None` ＝ 不结算、原样重抛，整轮交给 `_hold_spawn_round_for_live_nodes` 等
        真实终态。`CancelledError` 与 `Exception` 两个 handler 都只调这一个入口：判据写成
        守卫分别挂在各自分支顶部时，下一个新增的 handler 一定忘。

        结清即交付：父节点据此认为整批已定，批后帧写清掉 `pending_tool_calls`，同 id 重放
        通道随之关闭；而孩子本身没被取消（它们由各自的派发 entry 驱动）。账本从那一刻起是
        假的，下一次 `run_task` 取任务时收尸器会照着这句假话把活口停等。

        绑定节点已经终态时交付的是**它的真实结论**，不是"这里抛过异常"：2026-10-15 那 12 条
        `Error: CancelledError` 里有 3 条对应的孩子其实早已 success——异常文本在这里从来不是
        事实的第二说法，它是唯一说法，而且常常是假的那一个。只有本轮根本没绑定到节点
        （评审或物化之前就坏了）时，异常文本才是这里唯一诚实的填法。
        """
        entries = list(cached_payload.get('entries') or [])
        entry = entries[index] if 0 <= index < len(entries) else None
        entry = dict(entry) if isinstance(entry, dict) else {}
        reason = self._spawn_entry_non_terminal_node(entry)
        if reason:
            self._emit_spawn_diagnostic(
                title='spawn_settlement_refused_live_node',
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                index=index,
                entry=entry,
                detail=f'bound {reason} node is non-terminal; fault={describe_exception_chain(exc)}',
            )
            return None
        if entry.get('child_node_id') or entry.get('acceptance_node_id'):
            result, status, check_status, child_check_result = self._rebuild_spawn_entry_result(
                task_id=task.task_id,
                entry=dict(entry),
                fallback_goal=str(getattr(spec, 'goal', '') or ''),
                fallback_reason=self._spawn_exception_text(exc),
                error_source='runtime',
            )
            child_node_id = str(entry.get('child_node_id') or '').strip()
            if child_check_result is not None and child_node_id:
                self._log_service.update_node_check_result(task.task_id, child_node_id, child_check_result)
            self._update_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                status=status,
                finished_at=_now(),
                check_status=check_status,
                result=result.model_dump(mode='json', exclude_none=True),
            )
            return result
        result = self._spawn_runtime_result(spec.goal, error_text=self._spawn_exception_text(exc))
        self._update_spawn_entry(
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            cache_key=cache_key,
            cached_payload=cached_payload,
            index=index,
            status='error',
            finished_at=_now(),
            check_status='failed',
            result=result.model_dump(mode='json', exclude_none=True),
        )
        return result

    def _active_prior_spawn_operations(
        self,
        *,
        parent: NodeRecord,
        exclude_cache_key: str,
        replacing_goals: set[str] | None = None,
    ) -> list[tuple[str, dict[str, Any], list[int]]]:
        operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(operations, dict):
            return []
        normalized_goals = {str(goal or '').strip() for goal in (replacing_goals or set()) if str(goal or '').strip()}
        active_operations: list[tuple[str, dict[str, Any], list[int]]] = []
        excluded = str(exclude_cache_key or '').strip()
        for cache_key, payload in operations.items():
            normalized_cache_key = str(cache_key or '').strip()
            if normalized_cache_key == excluded or not isinstance(payload, dict):
                continue
            payload_copy = copy.deepcopy(payload)
            active_indexes: list[int] = []
            for index, entry in enumerate(list(payload_copy.get('entries') or [])):
                if not isinstance(entry, dict) or not self._spawn_entry_is_active(entry):
                    continue
                # 拆掉阻塞承诺后，上一轮的子节点可能仍在跑。只有"新批次里出现了同一个
                # goal"才算重派、才允许作废旧分支；不同 goal 的批次是新增工作，不能
                # 因为父节点又派了一批就被连带清扫。
                if normalized_goals:
                    entry_goal = str(entry.get('goal') or '').strip()
                    if entry_goal not in normalized_goals:
                        continue
                active_indexes.append(index)
            if active_indexes:
                active_operations.append((normalized_cache_key, payload_copy, active_indexes))
        return active_operations

    def _collect_descendant_node_ids(self, root_node_ids: list[str] | set[str]) -> set[str]:
        pending = [str(node_id or '').strip() for node_id in list(root_node_ids or []) if str(node_id or '').strip()]
        collected: set[str] = set()
        while pending:
            node_id = pending.pop()
            if not node_id or node_id in collected:
                continue
            collected.add(node_id)
            for child in list(self._store.list_children(node_id) or []):
                child_id = str(child.node_id or '').strip()
                if child_id and child_id not in collected:
                    pending.append(child_id)
        return collected

    async def _cancel_spawn_subtrees(self, *, task_id: str, node_ids: set[str], reason_text: str = '') -> set[str]:
        """取消这批子树，并返回**本进程确实持有其派发 entry** 的节点 id。

        这个返回值是调用方唯一需要的活性结论，也是本方法存在的全部意义：派发器在里面已经
        等到协程真停、并把节点落定成 `reason_text`（调用方给不出就用 `canceled`）。调用方
        拿到的「没出现在返回值里」不是「还在跑」，而是「我不知道谁在驱动它」——两者处置不同，
        前者不需要再判，后者只许上报。
        """
        targets = sorted(str(node_id or '').strip() for node_id in list(node_ids or []) if str(node_id or '').strip())
        if not targets:
            return set()
        executor = self.cancel_node_subtree_executor
        if not callable(executor):
            # 没接线＝本进程给不出持有结论，一律按未持有处理：调用方只上报，不强判。
            return set()
        try:
            owned = await executor(task_id, targets, str(reason_text or '').strip())
        except TaskPausedError:
            raise
        except asyncio.CancelledError:
            raise
        return {str(item or '').strip() for item in set(owned or ()) if str(item or '').strip()}

    def _report_nodes_without_owner(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        reason_text: str,
        node_ids: set[str],
    ) -> None:
        """清扫时本进程没有这些节点的派发 entry：只上报，不代签终态。

        「本进程没人驱动它」不等于「它失败了」——它可能在别的 worker 里跑、可能在等一次
        释放，也可能早就停了我们只是读不到。派发器对它有结论才有 `canceled`；我们没有。
        旧实现在这里用 50 毫秒轮询重推一遍再强判 `failed`，而同一文件当时的注释就写着
        「一次模型回合 p50 15s / p90 60s，2s 等待常掐不住」——那个预算从来没成立过。

        上报项要带清扫前的真实状态：操作员据此决定是等、是 `resume_node`、还是按 §3.5
        的作废签发落 `canceled`。这个判断代码没有证据替人做。
        """
        for node_id in sorted(str(item or '').strip() for item in (node_ids or set()) if str(item or '').strip()):
            node = self._store.get_node(node_id)
            self._emit_spawn_diagnostic(
                title='spawn_supersede_left_to_owner',
                task_id=task_id,
                parent_node_id=parent_node_id,
                cache_key=cache_key,
                index=None,
                entry=None,
                detail=(
                    f'left_node_id={node_id} '
                    f'kind={str(getattr(node, "node_kind", "") or "")} '
                    f'depth={str(getattr(node, "depth", "") or "")} '
                    f'status={self._normalized_status(getattr(node, "status", ""))} '
                    f'reason={str(reason_text or "").strip()}'
                ),
            )

    def _find_spawn_entry_for_child(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        child_node_id: str,
    ) -> tuple[str, int, dict[str, Any], dict[str, Any]] | None:
        parent = self._store.get_node(parent_node_id)
        if parent is None or str(parent.task_id or '').strip() != str(task_id or '').strip():
            return None
        operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(operations, dict):
            return None
        normalized_child_id = str(child_node_id or '').strip()
        for cache_key, payload in operations.items():
            if not isinstance(payload, dict) or bool(payload.get('completed')):
                continue
            for index, entry in enumerate(list(payload.get('entries') or [])):
                if not isinstance(entry, dict):
                    continue
                if str(entry.get('child_node_id') or '').strip() != normalized_child_id:
                    continue
                return str(cache_key or '').strip(), index, dict(entry), copy.deepcopy(payload)
        return None

    def _rebuild_spawn_entry_result(
        self,
        *,
        task_id: str,
        entry: dict[str, Any],
        fallback_goal: str,
        fallback_reason: str,
        error_source: str = 'runtime',
    ) -> tuple[SpawnChildResult, str, str, str | None]:
        goal = str(entry.get('goal') or fallback_goal or '').strip()
        review_decision = str(entry.get('review_decision') or '').strip().lower()
        child_node_id = str(entry.get('child_node_id') or '').strip()
        acceptance_node_id = str(entry.get('acceptance_node_id') or '').strip()
        has_bound_node = bool(child_node_id or acceptance_node_id)
        if review_decision == 'blocked' and not has_bound_node:
            blocked_reason = str(entry.get('blocked_reason') or '').strip()
            blocked_suggestion = str(entry.get('blocked_suggestion') or '').strip()
            result = self._spawn_review_blocked_result_by_goal(
                goal=goal,
                reason=blocked_reason,
                suggestion=blocked_suggestion,
            )
            return result, 'success', 'skipped', _SPAWN_REVIEW_BLOCKED_CHECK_RESULT
        runtime_error_text = str(entry.get('runtime_error_text') or '').strip()
        if runtime_error_text:
            result = self._spawn_runtime_result(goal, error_text=runtime_error_text)
            return result, 'error', 'failed', runtime_error_text
        requires_acceptance = bool(entry.get('requires_acceptance'))
        child = self._store.get_node(child_node_id) if child_node_id else None
        acceptance = self._store.get_node(acceptance_node_id) if acceptance_node_id else None
        child_summary = ''
        child_ref = ''
        if child is not None:
            child_handoff = self._child_handoff_payload(
                task_id=task_id,
                node=child,
                fallback_output='',
            )
            child_summary = str(child_handoff.get('summary') or '')
            child_ref = str(child_handoff.get('output_ref') or '')

        if requires_acceptance:
            if acceptance is not None and self._normalized_status(getattr(acceptance, 'status', '')) == STATUS_SUCCESS:
                acceptance_record = self._result_from_record(acceptance) if acceptance is not None else None
                check_result = str(
                    (acceptance_record.summary if acceptance_record is not None else '')
                    or acceptance.final_output
                    or acceptance.failure_reason
                    or SKIPPED_CHECK_RESULT
                ).strip() or SKIPPED_CHECK_RESULT
                return (
                    SpawnChildResult(
                        goal=goal,
                        check_result=check_result,
                        node_output=child_summary,
                        node_output_summary=child_summary,
                        node_output_ref=child_ref,
                    ),
                    'success',
                    'passed',
                    check_result,
                )
            if acceptance is not None:
                acceptance_record = self._result_from_record(acceptance) if acceptance is not None else None
                check_result = str(
                    (acceptance_record.summary if acceptance_record is not None else '')
                    or acceptance.final_output
                    or acceptance.failure_reason
                    or fallback_reason
                ).strip() or fallback_reason
                return (
                    SpawnChildResult(
                        goal=goal,
                        check_result=check_result,
                        node_output=child_summary,
                        node_output_summary=child_summary,
                        node_output_ref=child_ref,
                        failure_info=self._spawn_failure_info_from_node(
                            task_id=task_id,
                            node=acceptance,
                            fallback_output=check_result,
                            source='acceptance',
                        ),
                    ),
                    'error',
                    'failed',
                    check_result,
                )
            fallback_error = str(fallback_reason or self._spawn_exception_text(RuntimeError('acceptance node missing'))).strip()
            result = self._spawn_runtime_result(goal, error_text=fallback_error)
            return result, 'error', 'failed', fallback_error

        if child is not None and self._normalized_status(getattr(child, 'status', '')) == STATUS_SUCCESS:
            return (
                SpawnChildResult(
                    goal=goal,
                    check_result=SKIPPED_CHECK_RESULT,
                    node_output=child_summary,
                    node_output_summary=child_summary,
                    node_output_ref=child_ref,
                ),
                'success',
                'skipped',
                SKIPPED_CHECK_RESULT,
            )
        if child is not None:
            return (
                SpawnChildResult(
                    goal=goal,
                    check_result=SKIPPED_CHECK_RESULT,
                    node_output=child_summary,
                    node_output_summary=child_summary,
                    node_output_ref=child_ref,
                    failure_info=self._spawn_failure_info_from_node(
                        task_id=task_id,
                        node=child,
                        fallback_output=fallback_reason,
                        source='execution',
                    ),
                ),
                'error',
                'skipped',
                SKIPPED_CHECK_RESULT,
            )
        fallback_error = str(fallback_reason or self._spawn_exception_text(RuntimeError('child node missing'))).strip()
        result = self._spawn_runtime_result(goal, error_text=fallback_error)
        return result, 'error', 'failed', fallback_error

    def _spawn_creation_receipts(
        self,
        *,
        specs: list[SpawnChildSpec],
        entries: list[dict[str, Any]],
    ) -> list[SpawnChildResult]:
        """创建回执：只说明"建了谁 / 谁被拒"，不含执行结果。

        结果由子节点终局时的 `child_final` 消息送达（P1 终局必上报）。这条路同时是
        重放路径的唯一输出：同一 call id 重放只返回同一批创建回执，不会重新物化。
        """
        receipts: list[SpawnChildResult] = []
        for index, spec in enumerate(list(specs or [])):
            entry = dict((entries[index] if index < len(entries) else {}) or {})
            goal = str(entry.get('goal') or spec.goal or '').strip()
            child_node_id = str(entry.get('child_node_id') or '').strip()
            acceptance_node_id = str(entry.get('acceptance_node_id') or '').strip()
            review_decision = str(entry.get('review_decision') or '').strip().lower()
            creation_gate = str(entry.get('creation_gate') or '').strip().lower()
            if creation_gate and not child_node_id:
                reason = str(entry.get('creation_reject_reason') or '').strip() or creation_gate
                receipts.append(
                    SpawnChildResult(
                        goal=goal,
                        check_result='派生被拒绝',
                        node_output=reason,
                        state='rejected',
                        gate=creation_gate,
                        reject_reason=reason,
                    )
                )
                continue
            if review_decision == 'blocked' and not child_node_id:
                reason = str(entry.get('blocked_reason') or '').strip()
                suggestion = str(entry.get('blocked_suggestion') or '').strip()
                receipts.append(
                    SpawnChildResult(
                        goal=goal,
                        check_result='派生已被拦截',
                        node_output=self._spawn_review_blocked_text(
                            reason=reason, suggestion=suggestion
                        ),
                        review_blocked=True,
                        state='rejected',
                        gate='review',
                        reject_reason=reason or 'spawn review blocked this candidate',
                    )
                )
                continue
            if not child_node_id:
                reason = str(entry.get('runtime_error_text') or '').strip() or (
                    'child node was not materialized'
                )
                receipts.append(
                    SpawnChildResult(
                        goal=goal,
                        check_result='派生未物化',
                        node_output=reason,
                        state='rejected',
                        gate='materialization',
                        reject_reason=reason,
                    )
                )
                continue
            receipts.append(
                SpawnChildResult(
                    goal=goal,
                    state='created',
                    child_node_id=child_node_id,
                    acceptance_node_id=acceptance_node_id,
                    seed_tools=self._seed_receipt_for_node(child_node_id),
                )
            )
        return receipts

    def reconcile_spawn_round_completion(self, task_id: str) -> bool:
        """把"所有绑定节点都已终态"的 spawn 轮标记为 completed。

        拆掉阻塞承诺后，轮次的完成不再由父协程的 gather 宣布，而是从节点行派生：
        非阻塞模型下 `completed=True` 只表示"这一轮的节点都收尾了"，不再是"结果已交付"
        （结果交付走信箱）。不标会让每一轮都永远"未完成"，孤儿收割与作废清扫都会
        把它当成仍在飞的子树。
        """
        changed_any = False
        try:
            nodes = list(self._store.list_nodes(task_id) or [])
        except Exception:
            return False
        for node in nodes:
            metadata = dict(getattr(node, 'metadata', None) or {}) if isinstance(getattr(node, 'metadata', None), dict) else {}
            operations = metadata.get('spawn_operations')
            if not isinstance(operations, dict) or not operations:
                continue
            changed = False
            for round_id, payload in list(operations.items()):
                if not isinstance(payload, dict) or bool(payload.get('completed')):
                    continue
                bound_ids: list[str] = []
                for item in list(payload.get('entries') or []):
                    if not isinstance(item, dict):
                        continue
                    for field in ('child_node_id', 'acceptance_node_id'):
                        node_id = str(item.get(field) or '').strip()
                        if node_id:
                            bound_ids.append(node_id)
                if not bound_ids:
                    continue
                settled = True
                for node_id in bound_ids:
                    bound = self._store.get_node(node_id)
                    if bound is None:
                        continue
                    if self._normalized_status(getattr(bound, 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}:
                        settled = False
                        break
                if not settled:
                    continue
                payload['completed'] = True
                payload['completed_at'] = _now()
                changed = True
            if not changed:
                continue
            self._store.update_node(
                node.node_id,
                lambda record, ops=dict(operations): record.model_copy(
                    update={'metadata': {**dict(record.metadata or {}), 'spawn_operations': ops}}
                ),
            )
            changed_any = True
        return changed_any

    def _spawn_round_results_from_entries(
        self,
        *,
        task_id: str,
        entries: list[dict[str, Any]],
        specs: list[SpawnChildSpec] | None = None,
    ) -> list[SpawnChildResult]:
        specs_list = list(specs or [])
        results: list[SpawnChildResult] = []
        for index, entry in enumerate(list(entries or [])):
            if not isinstance(entry, dict):
                continue
            fallback_goal = str(entry.get('goal') or '').strip()
            if not fallback_goal and index < len(specs_list):
                fallback_goal = str(specs_list[index].goal or '').strip()
            creation_gate = str(entry.get('creation_gate') or '').strip().lower()
            gated_child_id = str(entry.get('child_node_id') or '').strip()
            gated_acceptance_id = str(entry.get('acceptance_node_id') or '').strip()
            if creation_gate and not (gated_child_id or gated_acceptance_id):
                # 创建期闸门拒掉的候选从未物化，没有节点行可读；交付它的拒绝理由，
                # 不能落到下面 `_rebuild_spawn_entry_result` 的「child node missing」。
                reason = str(entry.get('creation_reject_reason') or '').strip() or creation_gate
                results.append(
                    SpawnChildResult(
                        goal=fallback_goal,
                        check_result='派生被拒绝',
                        node_output=reason,
                        state='rejected',
                        gate=creation_gate,
                        reject_reason=reason,
                    )
                )
                continue
            result, _status, _check_status, _child_check_result = self._rebuild_spawn_entry_result(
                task_id=task_id,
                entry=entry,
                fallback_goal=fallback_goal,
                fallback_reason=self._spawn_exception_text(RuntimeError('child node missing')),
                error_source='runtime',
            )
            # 回执从节点行现读而不是从 entry 记账里读：名单与判据的家在节点 metadata，
            # 交付这一跳只是把它转述给派发方。重放/恢复那一跳也走同一条口，不会给出两份答案。
            if result.seed_tools is None:
                result.seed_tools = self._seed_receipt_for_node(str(entry.get('child_node_id') or '').strip())
            child_node_id = str(entry.get('child_node_id') or '').strip()
            acceptance_node_id = str(entry.get('acceptance_node_id') or '').strip()
            review_decision = str(entry.get('review_decision') or '').strip().lower()
            if review_decision == 'blocked' and not child_node_id:
                result = result.model_copy(
                    update={
                        'state': 'rejected',
                        'gate': 'review',
                        'reject_reason': str(entry.get('blocked_reason') or '').strip()
                        or 'spawn review blocked this candidate',
                    }
                )
            elif child_node_id or acceptance_node_id:
                result = result.model_copy(
                    update={
                        'state': 'created',
                        'child_node_id': child_node_id,
                        'acceptance_node_id': acceptance_node_id,
                    }
                )
            results.append(result)
        return results

    async def _settle_superseded_spawn_operations(
        self,
        *,
        task,
        parent: NodeRecord,
        exclude_cache_key: str,
        replacement_round_id: str,
        replacing_goals: set[str] | None = None,
    ) -> None:
        active_operations = self._active_prior_spawn_operations(
            parent=parent,
            exclude_cache_key=exclude_cache_key,
            replacing_goals=replacing_goals,
        )
        if not active_operations:
            return
        reason_text = self._spawn_round_superseded_reason(replacement_round_id)
        active_root_node_ids: set[str] = set()
        preterminal_node_ids: set[str] = set()
        for _cache_key, payload, active_indexes in active_operations:
            for index in active_indexes:
                entries = list(payload.get('entries') or [])
                if index >= len(entries) or not isinstance(entries[index], dict):
                    continue
                entry = dict(entries[index] or {})
                for node_id in self._spawn_entry_node_ids(entry):
                    active_root_node_ids.add(node_id)
        subtree_node_ids = self._collect_descendant_node_ids(active_root_node_ids)
        for node_id in list(subtree_node_ids):
            node = self._store.get_node(node_id)
            if node is None:
                continue
            if self._normalized_status(getattr(node, 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}:
                preterminal_node_ids.add(node_id)

        owned_node_ids = await self._cancel_spawn_subtrees(
            task_id=task.task_id, node_ids=subtree_node_ids, reason_text=reason_text,
        )
        # 有 entry 的那批：派发器已经等到协程真停、并把未解析的 future 权威落成 `canceled`，
        # 这里不再判第二次。没有 entry 的那批：本进程给不出「它还在跑还是已经停了」的结论，
        # 只许上报，留给操作员或作废签发。旧实现在这两批上都用 50 毫秒轮询从库行重推一遍再
        # 强判 failed，而它自己的注释就写着 2 秒预算掐不住一次真实回合（p50 15s / p90 60s）。
        # `reason_text` 已带被取代的轮 id，操作员据此能读出「工作是被清扫打断的」。
        self._report_nodes_without_owner(
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            cache_key='',
            reason_text=reason_text,
            node_ids=preterminal_node_ids - owned_node_ids,
        )

        for cache_key, payload, active_indexes in active_operations:
            specs = list(payload.get('specs') or [])
            for index in active_indexes:
                spec_payload = specs[index] if index < len(specs) and isinstance(specs[index], dict) else {}
                result, status, check_status, child_check_result = self._rebuild_spawn_entry_result(
                    task_id=task.task_id,
                    entry=dict((list(payload.get('entries') or []) or [])[index] or {}),
                    fallback_goal=str(spec_payload.get('goal') or ''),
                    fallback_reason=self._spawn_exception_text(RuntimeError(reason_text)),
                    error_source='runtime',
                )
                child_node_id = str(((list(payload.get('entries') or []) or [])[index] or {}).get('child_node_id') or '').strip()
                if child_node_id and child_check_result is not None:
                    self._log_service.update_node_check_result(task.task_id, child_node_id, child_check_result)
                self._update_spawn_entry(
                    task_id=task.task_id,
                    parent_node_id=parent.node_id,
                    cache_key=cache_key,
                    cached_payload=payload,
                    index=index,
                    status=status,
                    finished_at=_now(),
                    check_status=check_status,
                    runtime_error_text=reason_text if status == 'error' else '',
                    result=result.model_dump(mode='json', exclude_none=True),
                )
            payload['completed'] = not any(
                self._spawn_entry_is_active(item)
                for item in list(payload.get('entries') or [])
                if isinstance(item, dict)
            )
            self._save_spawn_cache(task.task_id, parent.node_id, cache_key, payload)

    def _normalize_spawn_entry(self, *, index: int, spec: SpawnChildSpec, entry: dict[str, Any] | None) -> dict[str, Any]:
        payload = dict(entry or {})
        status = str(payload.get('status') or '').strip().lower()
        if status not in {'queued', 'running', 'success', 'error'}:
            status = 'queued'
        check_status = str(payload.get('check_status') or '').strip().lower()
        if check_status not in {'', 'pending', 'running', 'skipped', 'passed', 'failed'}:
            check_status = ''
        review_decision = str(payload.get('review_decision') or '').strip().lower()
        if review_decision not in {'', 'allowed', 'blocked'}:
            review_decision = ''
        return {
            'index': index,
            'goal': spec.goal,
            'requires_acceptance': bool(payload.get('requires_acceptance')) if 'requires_acceptance' in payload else self._requires_acceptance(spec),
            'status': status,
            'started_at': str(payload.get('started_at') or ''),
            'finished_at': str(payload.get('finished_at') or ''),
            'child_node_id': str(payload.get('child_node_id') or ''),
            'acceptance_node_id': str(payload.get('acceptance_node_id') or ''),
            'check_status': check_status,
            'review_decision': review_decision,
            'blocked_reason': str(payload.get('blocked_reason') or ''),
            'blocked_suggestion': str(payload.get('blocked_suggestion') or ''),
            'synthetic_result_summary': str(payload.get('synthetic_result_summary') or ''),
            'runtime_error_text': str(payload.get('runtime_error_text') or ''),
        }

    def _update_spawn_entry(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        cached_payload: dict[str, Any],
        index: int,
        **changes: Any,
    ) -> dict[str, Any]:
        entries = list(cached_payload.get('entries') or [])
        while len(entries) <= index:
            entries.append({})
        entry = dict(entries[index] or {})
        for key, value in changes.items():
            if value is None:
                continue
            entry[key] = copy.deepcopy(value)
        entries[index] = entry
        cached_payload['entries'] = entries
        self._save_spawn_cache(task_id, parent_node_id, cache_key, cached_payload)
        return entry

    def _save_spawn_cache(self, task_id: str, parent_node_id: str, cache_key: str, payload: dict[str, Any]) -> None:
        task = self._store.get_task(task_id)
        parent = self._store.get_node(parent_node_id)
        if task is None or parent is None:
            return
        if self._task_terminal_reason(task_id, task=task):
            return
        if self._node_terminal_reason(parent, default_failed='parent node failed', default_success='parent node already completed'):
            return
        payload_copy = copy.deepcopy(payload)

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            operations = dict(metadata.get('spawn_operations') or {})
            operations[cache_key] = payload_copy
            metadata['spawn_operations'] = operations
            return metadata

        self._log_service.update_node_metadata(parent_node_id, _mutate)
        self._sync_parent_child_pipelines_frame(task_id, parent_node_id)
        self._log_service.refresh_task_view(task_id, mark_unread=True)

    def reconcile_spawn_entry_child_bindings(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        round_id: str | None = None,
    ) -> bool:
        task = self._store.get_task(task_id)
        parent = self._store.get_node(parent_node_id)
        if task is None or parent is None or str(parent.task_id or '').strip() != str(task.task_id or '').strip():
            return False
        operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(operations, dict):
            return False
        normalized_round_filter = str(round_id or '').strip()
        changed = False
        next_operations = copy.deepcopy(operations)
        for raw_round_id, raw_payload in list(next_operations.items()):
            current_round_id = str(raw_round_id or '').strip()
            if normalized_round_filter and current_round_id != normalized_round_filter:
                continue
            if not isinstance(raw_payload, dict) or bool(raw_payload.get('completed')):
                continue
            entries = list(raw_payload.get('entries') or [])
            for entry_index, raw_entry in enumerate(entries):
                if not isinstance(raw_entry, dict):
                    continue
                entry = dict(raw_entry or {})
                if str(entry.get('review_decision') or '').strip().lower() == 'blocked':
                    continue
                normalized_entry_index = int(entry.get('index') or entry_index)
                candidates = self._spawn_owner_child_candidates(
                    task_id=task.task_id,
                    parent_node_id=parent.node_id,
                    round_id=current_round_id,
                    entry_index=normalized_entry_index,
                )
                if not candidates:
                    continue
                canonical = self._canonical_spawn_child_for_entry(
                    task_id=task.task_id,
                    entry=entry,
                    candidates=candidates,
                )
                if canonical is None:
                    continue
                if str(entry.get('child_node_id') or '').strip() != str(canonical.node_id or '').strip():
                    entry['child_node_id'] = canonical.node_id
                    entries[entry_index] = entry
                    raw_payload['entries'] = entries
                    changed = True
                for duplicate in candidates:
                    if str(duplicate.node_id or '').strip() == str(canonical.node_id or '').strip():
                        self._clear_duplicate_spawn_child_marker(duplicate.node_id)
                        continue
                    if self._mark_duplicate_spawn_child(duplicate.node_id, duplicate_of_node_id=canonical.node_id):
                        changed = True
        if changed:
            self._log_service.update_node_metadata(parent.node_id, lambda metadata: {**metadata, 'spawn_operations': next_operations})
            self._sync_parent_child_pipelines_frame(task.task_id, parent.node_id)
            self._log_service.refresh_task_view(task.task_id, mark_unread=True)
        return changed

    def _spawn_owner_child_candidates(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        round_id: str,
        entry_index: int,
    ) -> list[NodeRecord]:
        candidates: list[NodeRecord] = []
        for node in list(self._store.list_children(parent_node_id) or []):
            if str(node.task_id or '').strip() != str(task_id or '').strip():
                continue
            if str(node.node_kind or '').strip().lower() != KIND_EXECUTION:
                continue
            metadata = dict(node.metadata or {}) if isinstance(node.metadata, dict) else {}
            if str(metadata.get('spawn_owner_kind') or '').strip().lower() != 'child':
                continue
            if str(metadata.get('spawn_owner_parent_node_id') or '').strip() != str(parent_node_id or '').strip():
                continue
            if str(metadata.get('spawn_owner_round_id') or '').strip() != str(round_id or '').strip():
                continue
            try:
                owner_entry_index = int(metadata.get('spawn_owner_entry_index'))
            except (TypeError, ValueError):
                continue
            if owner_entry_index != int(entry_index):
                continue
            candidates.append(node)
        return candidates

    def _canonical_spawn_child_for_entry(
        self,
        *,
        task_id: str,
        entry: dict[str, Any],
        candidates: list[NodeRecord],
    ) -> NodeRecord | None:
        if not candidates:
            return None
        by_id = {str(item.node_id or '').strip(): item for item in candidates if str(item.node_id or '').strip()}
        bound_node_id = str(entry.get('child_node_id') or '').strip()
        bound = by_id.get(bound_node_id)
        if bound is not None and not self._duplicate_spawn_child_marker(bound):
            return bound

        def _score(node: NodeRecord) -> tuple[int, int, str, str, str]:
            node_id = str(node.node_id or '').strip()
            status = str(node.status or '').strip().lower()
            metadata = dict(node.metadata or {}) if isinstance(node.metadata, dict) else {}
            has_runtime_frame = 1 if self._store.has_task_runtime_frame(task_id, node_id) else 0
            is_nonterminal = 1 if status not in {STATUS_SUCCESS, STATUS_FAILED} else 0
            is_unmarked_duplicate = 1 if not bool(metadata.get('duplicate_spawn_child')) else 0
            return (has_runtime_frame, is_nonterminal, str(node.updated_at or ''), str(node.created_at or ''), node_id if is_unmarked_duplicate else '')

        return sorted(candidates, key=_score, reverse=True)[0]

    @staticmethod
    def _duplicate_spawn_child_marker(node: NodeRecord) -> bool:
        metadata = dict(node.metadata or {}) if isinstance(node.metadata, dict) else {}
        return bool(metadata.get('duplicate_spawn_child') or metadata.get('duplicate_spawn_child_of'))

    def _mark_duplicate_spawn_child(self, node_id: str, *, duplicate_of_node_id: str) -> bool:
        node = self._store.get_node(node_id)
        if node is None:
            return False
        metadata = dict(node.metadata or {}) if isinstance(node.metadata, dict) else {}
        if (
            bool(metadata.get('duplicate_spawn_child'))
            and str(metadata.get('duplicate_spawn_child_of') or '').strip() == str(duplicate_of_node_id or '').strip()
        ):
            return False

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            current['duplicate_spawn_child'] = True
            current['duplicate_spawn_child_of'] = str(duplicate_of_node_id or '').strip()
            current['duplicate_spawn_child_reason'] = 'duplicate spawn child for same parent round entry'
            return current

        self._log_service.update_node_metadata(node_id, _mutate)
        return True

    def _clear_duplicate_spawn_child_marker(self, node_id: str) -> bool:
        node = self._store.get_node(node_id)
        if node is None:
            return False
        metadata = dict(node.metadata or {}) if isinstance(node.metadata, dict) else {}
        if not any(key in metadata for key in ('duplicate_spawn_child', 'duplicate_spawn_child_of', 'duplicate_spawn_child_reason')):
            return False

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            current.pop('duplicate_spawn_child', None)
            current.pop('duplicate_spawn_child_of', None)
            current.pop('duplicate_spawn_child_reason', None)
            return current

        self._log_service.update_node_metadata(node_id, _mutate)
        return True

    def _sync_parent_child_pipelines_frame(self, task_id: str, parent_node_id: str) -> None:
        parent = self._store.get_node(parent_node_id)
        if parent is None:
            return
        child_pipelines, pending_specs, partial_results, has_active = self._parent_spawn_frame_state(parent)
        frame_exists = self._log_service.read_runtime_frame(task_id, parent_node_id) is not None
        if not frame_exists and not child_pipelines:
            return

        def _mutate(frame: dict[str, Any]) -> dict[str, Any]:
            next_frame = dict(frame or {})
            next_frame['node_id'] = parent.node_id
            next_frame['depth'] = int(parent.depth or 0)
            next_frame['node_kind'] = parent.node_kind
            next_frame['child_pipelines'] = child_pipelines
            next_frame['pending_child_specs'] = pending_specs
            next_frame['partial_child_results'] = partial_results
            if has_active:
                next_frame['phase'] = 'waiting_children'
            elif str(next_frame.get('phase') or '').strip() == 'waiting_children':
                next_frame['phase'] = 'waiting_tool_results' if list(next_frame.get('tool_calls') or []) else 'after_model'
            return next_frame

        self._log_service.update_frame(task_id, parent.node_id, _mutate, publish_snapshot=False)

    def _parent_spawn_frame_state(self, parent: NodeRecord) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
        spawn_operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(spawn_operations, dict):
            return [], [], [], False

        child_pipelines: list[dict[str, Any]] = []
        pending_specs: list[dict[str, Any]] = []

        for operation_index, (_operation_id, payload) in enumerate(spawn_operations.items()):
            if not isinstance(payload, dict):
                continue
            specs = [
                {
                    'goal': str(item.get('goal') or '').strip(),
                    'execution_policy': copy.deepcopy(item.get('execution_policy') or {}),
                    'requires_acceptance': bool(item.get('requires_acceptance')),
                }
                for item in list(payload.get('specs') or [])
                if isinstance(item, dict)
            ]
            if not bool(payload.get('completed')):
                pending_specs.extend(specs)
            for entry_index, entry in enumerate(list(payload.get('entries') or [])):
                if not isinstance(entry, dict):
                    continue
                status = str(entry.get('status') or '').strip().lower()
                if status not in {'queued', 'running', 'success', 'error'}:
                    status = 'queued'
                # A synthetic terminal status must not mask a live bound node:
                # display the pipeline as running until every bound node is terminal.
                if status in {'success', 'error'} and self._spawn_entry_non_terminal_node(entry):
                    status = 'running'
                child_pipelines.append(
                    {
                        'index': int(entry.get('index') or entry_index),
                        'goal': str(entry.get('goal') or ''),
                        'status': status,
                        'child_node_id': str(entry.get('child_node_id') or ''),
                        'acceptance_node_id': str(entry.get('acceptance_node_id') or ''),
                        'check_status': str(entry.get('check_status') or ''),
                        'started_at': str(entry.get('started_at') or ''),
                        'finished_at': str(entry.get('finished_at') or ''),
                        '_sort_key': (
                            str(entry.get('started_at') or ''),
                            operation_index,
                            int(entry.get('index') or entry_index),
                        ),
                    }
                )

        child_pipelines.sort(key=lambda item: item.get('_sort_key') or ('', 0, 0))
        for item in child_pipelines:
            item.pop('_sort_key', None)
        has_active = any(str(item.get('status') or '').strip().lower() in {'queued', 'running'} for item in child_pipelines)
        return child_pipelines, pending_specs, [], has_active

    async def _admit_spawn_batch(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        spec_count: int,
    ) -> None:
        controller = self._adaptive_tool_budget_controller
        if controller is None:
            return
        lease = await controller.acquire_work_slot(
            task_id=task_id,
            node_id=parent_node_id,
            work_kind='spawn_child_nodes',
            work_id=f'{cache_key}:batch:{max(0, int(spec_count))}',
        )
        controller.release_work_slot(lease)

    def _materialize_spawn_batch_children(
        self,
        *,
        task,
        parent: NodeRecord,
        specs: list[SpawnChildSpec],
        allowed_indexes: list[int],
        cache_key: str,
        cached_payload: dict[str, Any],
    ) -> None:
        stop_reason = self._task_terminal_reason(task.task_id, task=task) or self._node_terminal_reason(
            self._store.get_node(parent.node_id) or parent,
            default_failed='parent node failed',
            default_success='parent node already completed',
        )
        if stop_reason:
            return
        self._save_spawn_cache(task.task_id, parent.node_id, cache_key, cached_payload)
        self.reconcile_spawn_entry_child_bindings(
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            round_id=cache_key,
        )
        parent = self._store.get_node(parent.node_id) or parent
        cached_round = dict((parent.metadata or {}).get('spawn_operations') or {}).get(cache_key)
        if isinstance(cached_round, dict):
            cached_payload.clear()
            cached_payload.update(copy.deepcopy(cached_round))
        entries = list(cached_payload.get('entries') or [])
        allowed_set = {int(item) for item in list(allowed_indexes or [])}
        for index, spec in enumerate(list(specs or [])):
            if index >= len(entries):
                break
            if index not in allowed_set:
                continue
            entry = dict(entries[index] or {})
            child_id = str(entry.get('child_node_id') or '').strip()
            if child_id:
                continue
            child = self._find_reusable_execution_child(
                task=task,
                parent=parent,
                spec=spec,
                exclude_node_ids=self._claimed_spawn_node_ids(entries=entries, field='child_node_id', skip_index=index),
            )
            if child is None:
                child = self._create_execution_child(
                    task=task,
                    parent=parent,
                    spec=spec,
                    owner_round_id=cache_key,
                    owner_entry_index=index,
                )
            child = self._log_service.create_child_and_bind_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                entry_index=index,
                child=child,
            )
            self._update_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                child_node_id=child.node_id,
            )
            self._stamp_spawn_owner_metadata(
                node_id=child.node_id,
                parent_node_id=parent.node_id,
                owner_round_id=cache_key,
                owner_entry_index=index,
                owner_kind='child',
            )
            if self._requires_acceptance(spec):
                self._ensure_spawn_acceptance_node(
                    task=task,
                    parent=parent,
                    child=child,
                    spec=spec,
                    cache_key=cache_key,
                    cached_payload=cached_payload,
                    index=index,
                )
            entries = list(cached_payload.get('entries') or [])

    def _ensure_spawn_acceptance_node(
        self,
        *,
        task,
        parent: NodeRecord,
        child: NodeRecord,
        spec: SpawnChildSpec,
        cache_key: str,
        cached_payload: dict[str, Any],
        index: int,
    ) -> NodeRecord:
        entries = list(cached_payload.get('entries') or [])
        entry = dict((entries or [])[index] or {})
        acceptance_id = str(entry.get('acceptance_node_id') or '').strip()
        acceptance = self._store.get_node(acceptance_id) if acceptance_id else None
        if acceptance is not None:
            # 已绑定的验收节点直接复用。终态不再在这里作废：新的提交由
            # `_maybe_start_acceptance_handshake` 负责把终态验收节点重置回 in_progress；
            # 重放 spawn 轮只是重放创建账本，不能重新打开一份已经对当前提交下过的裁定
            # （否则 pass 之后会被再验一轮，甚至改判 reject）。
            pass
        if acceptance is None:
            acceptance_goal = _spawn_acceptance_goal(spec.goal)
            acceptance_prompt = str(spec.acceptance_prompt or '')
            claimed_acceptance_ids = self._claimed_spawn_node_ids(
                entries=entries,
                field='acceptance_node_id',
                skip_index=index,
            )
            acceptance = self._find_child_bound_acceptance_node(
                child=child,
                exclude_node_ids=claimed_acceptance_ids,
            )
            if acceptance is None:
                acceptance = self._find_reusable_acceptance_node(
                    task=task,
                    accepted_node=child,
                    goal=acceptance_goal,
                    acceptance_prompt=acceptance_prompt,
                    parent_node_id=child.node_id,
                    exclude_node_ids=claimed_acceptance_ids,
                )
            if acceptance is None:
                acceptance = self.create_acceptance_node(
                    task=task,
                    accepted_node=child,
                    goal=acceptance_goal,
                    acceptance_prompt=acceptance_prompt,
                    parent_node_id=child.node_id,
                    owner_parent_node_id=parent.node_id,
                    owner_round_id=cache_key,
                    owner_entry_index=index,
                )
            self._update_spawn_entry(
                task_id=task.task_id,
                parent_node_id=parent.node_id,
                cache_key=cache_key,
                cached_payload=cached_payload,
                index=index,
                acceptance_node_id=acceptance.node_id,
                check_status='pending',
            )
        self._stamp_spawn_owner_metadata(
            node_id=acceptance.node_id,
            parent_node_id=parent.node_id,
            owner_round_id=cache_key,
            owner_entry_index=index,
            owner_kind='acceptance',
        )
        return acceptance

    async def _admit_spawn_expansion(
        self,
        *,
        task_id: str,
        parent_node_id: str,
        cache_key: str,
        index: int,
        phase: str,
    ) -> None:
        controller = self._adaptive_tool_budget_controller
        if controller is None:
            return
        lease = await controller.acquire_work_slot(
            task_id=task_id,
            node_id=parent_node_id,
            work_kind='spawn_child_nodes',
            work_id=f'{cache_key}:{phase}:{int(index)}',
        )
        controller.release_work_slot(lease)

    def _requires_acceptance(self, spec: SpawnChildSpec) -> bool:
        return _spec_requires_acceptance(spec)

    @staticmethod
    def _claimed_spawn_node_ids(*, entries: list[dict[str, Any]], field: str, skip_index: int) -> set[str]:
        claimed: set[str] = set()
        for index, entry in enumerate(list(entries or [])):
            if index == int(skip_index):
                continue
            if not isinstance(entry, dict):
                continue
            node_id = str(entry.get(field) or '').strip()
            if node_id:
                claimed.add(node_id)
        return claimed

    def _find_reusable_execution_child(
        self,
        *,
        task,
        parent: NodeRecord,
        spec: SpawnChildSpec,
        exclude_node_ids: set[str],
    ) -> NodeRecord | None:
        expected_prompt = str(spec.prompt or '')
        expected_fingerprint = self._execution_child_recovery_fingerprint(
            parent_node_id=parent.node_id,
            goal=spec.goal,
            prompt=expected_prompt,
        )
        return self._find_reusable_node(
            parent_node_id=parent.node_id,
            node_kind=KIND_EXECUTION,
            expected_goal=spec.goal,
            expected_prompt=expected_prompt,
            expected_fingerprint=expected_fingerprint,
            exclude_node_ids=exclude_node_ids,
            metadata_filter=None,
        )

    def _find_child_bound_acceptance_node(
        self,
        *,
        child: NodeRecord,
        exclude_node_ids: set[str],
    ) -> NodeRecord | None:
        """被验执行节点已经挂着验收子节点时，那一份就是它的验收人，不看状态也不看指纹。

        一个执行节点只配一个验收节点，而这条配对平时只由 entry 的 `acceptance_node_id`
        承载——它是每轮一份的。新一轮派生复用同一个执行子节点时 entry 是空白的，只按
        `status == success` + 当轮提交指纹搜索，会把刚被上一轮清扫打成 failed 的那颗判成
        不可复用，于是给同一个执行节点再配一颗，旧那颗则永远留在树上。
        """
        candidates: list[NodeRecord] = []
        for node in list(self._store.list_children(child.node_id) or []):
            if str(node.node_id or '').strip() in exclude_node_ids:
                continue
            if str(node.node_kind or '').strip().lower() != KIND_ACCEPTANCE:
                continue
            metadata = dict(node.metadata or {})
            if str(metadata.get('accepted_node_id') or '').strip() != str(child.node_id or '').strip():
                continue
            candidates.append(node)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (str(item.updated_at or ''), str(item.created_at or ''), str(item.node_id or '')),
            reverse=True,
        )
        return candidates[0]

    def _find_reusable_acceptance_node(
        self,
        *,
        task,
        accepted_node: NodeRecord,
        goal: str,
        acceptance_prompt: str,
        parent_node_id: str,
        exclude_node_ids: set[str],
    ) -> NodeRecord | None:
        child_handoff = self._child_handoff_payload(
            task_id=task.task_id,
            node=accepted_node,
            fallback_output='',
        )
        expected_prompt = self._compose_acceptance_prompt(
            acceptance_prompt=acceptance_prompt,
            node_output=str(child_handoff.get('summary') or ''),
            node_output_ref=str(child_handoff.get('output_ref') or ''),
            result_payload_ref=str(child_handoff.get('result_payload_ref') or ''),
            evidence_summary=str(child_handoff.get('evidence_summary') or ''),
        )
        expected_fingerprint = self._acceptance_node_recovery_fingerprint(
            parent_node_id=parent_node_id,
            goal=goal,
            acceptance_prompt=acceptance_prompt,
            accepted_node_id=accepted_node.node_id,
            submission_ref=str(child_handoff.get('result_payload_ref') or ''),
        )
        return self._find_reusable_node(
            parent_node_id=parent_node_id,
            node_kind=KIND_ACCEPTANCE,
            expected_goal=goal,
            expected_prompt=expected_prompt,
            expected_fingerprint=expected_fingerprint,
            exclude_node_ids=exclude_node_ids,
            metadata_filter={'accepted_node_id': accepted_node.node_id},
        )

    def _find_reusable_node(
        self,
        *,
        parent_node_id: str,
        node_kind: str,
        expected_goal: str,
        expected_prompt: str,
        expected_fingerprint: str,
        exclude_node_ids: set[str],
        metadata_filter: dict[str, str] | None,
    ) -> NodeRecord | None:
        candidates: list[NodeRecord] = []
        for node in list(self._store.list_children(parent_node_id) or []):
            if node.node_id in exclude_node_ids:
                continue
            if str(node.node_kind or '').strip().lower() != str(node_kind or '').strip().lower():
                continue
            if str(node.status or '').strip().lower() != STATUS_SUCCESS:
                continue
            metadata = dict(node.metadata or {})
            if metadata_filter:
                mismatch = False
                for key, expected in metadata_filter.items():
                    if str(metadata.get(key) or '').strip() != str(expected or '').strip():
                        mismatch = True
                        break
                if mismatch:
                    continue
            fingerprint = str(metadata.get(_RECOVERY_FINGERPRINT_KEY) or '').strip()
            if fingerprint:
                if fingerprint != expected_fingerprint:
                    continue
            else:
                if str(node.goal or '') != str(expected_goal or ''):
                    continue
                if str(node.prompt or '') != str(expected_prompt or ''):
                    continue
            candidates.append(node)
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (str(item.updated_at or ''), str(item.created_at or ''), str(item.node_id or '')),
            reverse=True,
        )
        return candidates[0]

    @staticmethod
    def _recovery_fingerprint(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def _execution_child_recovery_fingerprint(self, *, parent_node_id: str, goal: str, prompt: str) -> str:
        return self._recovery_fingerprint(
            {
                'node_kind': KIND_EXECUTION,
                'parent_node_id': str(parent_node_id or '').strip(),
                'goal': str(goal or ''),
                'prompt': str(prompt or ''),
            }
        )

    def _acceptance_node_recovery_fingerprint(
        self,
        *,
        parent_node_id: str,
        goal: str,
        acceptance_prompt: str,
        accepted_node_id: str,
        submission_ref: str,
    ) -> str:
        # 指纹认的是「哪一份验收合同、判的是哪一次提交」：验收标准正文与提交载荷 ref
        # 足够定身份，提交**正文**绝不进指纹——否则每次重提交都会改动指纹，验收节点
        # 的 durable 身份随交付漂移，重启复用（`_find_reusable_acceptance_node`）也
        # 因此在唯一真正该复用的场景（同一份提交的重放）里失配。
        return self._recovery_fingerprint(
            {
                'node_kind': KIND_ACCEPTANCE,
                'parent_node_id': str(parent_node_id or '').strip(),
                'goal': str(goal or ''),
                'acceptance_prompt': str(acceptance_prompt or ''),
                'accepted_node_id': str(accepted_node_id or '').strip(),
                'submission_ref': str(submission_ref or '').strip(),
            }
        )

    def _resolve_core_requirement(self, task) -> str:
        metadata = task.metadata if isinstance(getattr(task, 'metadata', None), dict) else {}
        return str(metadata.get('core_requirement') or getattr(task, 'user_request', '') or getattr(task, 'title', '') or '').strip()

    def _resolve_file_targets(self, task) -> list[dict[str, str]]:
        metadata = task.metadata if isinstance(getattr(task, 'metadata', None), dict) else {}
        return normalize_create_async_task_file_targets(metadata.get('file_targets'))

    def _resolve_execution_policy(self, task, *, node: NodeRecord | None = None):
        task_metadata = task.metadata if isinstance(getattr(task, 'metadata', None), dict) else {}
        node_metadata = node.metadata if node is not None and isinstance(getattr(node, 'metadata', None), dict) else {}
        return normalize_execution_policy_metadata(
            node_metadata.get('execution_policy', task_metadata.get('execution_policy'))
        )

    def _seed_contract_metadata(self, *, task, node_kind: str, declared: list[str] | None) -> dict[str, Any]:
        """派发时算一次预置契约，把最终名单与回执写成要落进节点 metadata 的那一份。

        预置是优化不是前置条件：解析失败**不阻断派生**，此时整份回执缺席并留 `seed_error`，
        而不是替被丢的名字编一个原因。判据只有一份实现（服务层的
        `build_node_seed_metadata`），根节点与子节点走同一条口，否则两处会出现两套优先级。
        """
        builder = self._seed_contract_metadata_builder
        if not callable(builder):
            return {}
        return dict(builder(task=task, node_kind=node_kind, declared=declared) or {})

    def _seed_receipt_for_node(self, node_id: str) -> dict[str, Any] | None:
        """派发回执：只在派发方真的声明过名单时给，未声明时交 None 让字段整个消失。"""
        node = self._store.get_node(node_id) if str(node_id or '').strip() else None
        receipt = dict((getattr(node, 'metadata', None) or {}).get('seed_receipt') or {})
        if not receipt:
            return None
        return {
            'accepted': list(receipt.get('accepted') or []),
            'dropped': list(receipt.get('dropped') or []),
        }

    def _create_execution_child(
        self,
        *,
        task,
        parent: NodeRecord,
        spec: SpawnChildSpec,
        owner_round_id: str = '',
        owner_entry_index: int = 0,
    ) -> NodeRecord:
        execution_policy = normalize_execution_policy_metadata(spec.execution_policy)
        child_prompt = str(spec.prompt or '')
        metadata = {
            'execution_policy': execution_policy.model_dump(mode='json'),
            _RECOVERY_FINGERPRINT_KEY: self._execution_child_recovery_fingerprint(
                parent_node_id=parent.node_id,
                goal=spec.goal,
                prompt=child_prompt,
            ),
            'spawn_owner_parent_node_id': parent.node_id,
            'spawn_owner_round_id': str(owner_round_id or '').strip(),
            'spawn_owner_entry_index': int(owner_entry_index),
            'spawn_owner_kind': 'child',
            **self._seed_contract_metadata(task=task, node_kind=KIND_EXECUTION, declared=spec.seed_tools),
        }
        child = NodeRecord(
            node_id=new_node_id(),
            task_id=task.task_id,
            parent_node_id=parent.node_id,
            root_node_id=parent.root_node_id,
            depth=parent.depth + 1,
            node_kind=KIND_EXECUTION,
            status='in_progress',
            goal=spec.goal,
            prompt=child_prompt,
            input=child_prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=self._depth_allows_spawn(parent.depth, int(task.max_depth)),
            created_at=_now(),
            updated_at=_now(),
            token_usage=TokenUsageSummary(tracked=bool(getattr(task.token_usage, 'tracked', False))),
            token_usage_by_model=[],
            metadata=metadata,
        )
        created = self._log_service.create_node(task.task_id, child)
        return created

    def create_acceptance_node(
        self,
        *,
        task,
        accepted_node: NodeRecord,
        goal: str,
        acceptance_prompt: str,
        parent_node_id: str | None = None,
        owner_parent_node_id: str | None = None,
        owner_round_id: str = '',
        owner_entry_index: int = 0,
        metadata: dict[str, Any] | None = None,
    ) -> NodeRecord:
        execution_policy = self._resolve_execution_policy(task, node=accepted_node)
        child_handoff = self._child_handoff_payload(
            task_id=task.task_id,
            node=accepted_node,
            fallback_output='',
        )
        # 验收 bootstrap 在创建时定稿，之后不再被任何刷新改写：它进的是持久历史，
        # 每次重提交都重写它会把 scaffold 头探针的比对基准搬走（`fallback_seed_prefix_drift`），
        # 而交付正文本来就有交接通知这条 append 通道。见本文「验收」合同与
        # `context-and-cache-troubleshooting.md`「append-only 规则」。
        prompt = self._compose_acceptance_prompt(
            acceptance_prompt=str(acceptance_prompt or ''),
            node_output=str(child_handoff.get('summary') or ''),
            node_output_ref=str(child_handoff.get('output_ref') or ''),
            result_payload_ref=str(child_handoff.get('result_payload_ref') or ''),
            evidence_summary=str(child_handoff.get('evidence_summary') or ''),
        )
        base_metadata = {
            'accepted_node_id': accepted_node.node_id,
            'acceptance_prompt_template': str(acceptance_prompt or ''),
            'execution_policy': execution_policy.model_dump(mode='json'),
            _RECOVERY_FINGERPRINT_KEY: self._acceptance_node_recovery_fingerprint(
                parent_node_id=parent_node_id or accepted_node.node_id,
                goal=goal,
                acceptance_prompt=str(acceptance_prompt or ''),
                accepted_node_id=accepted_node.node_id,
                submission_ref=str(child_handoff.get('result_payload_ref') or ''),
            ),
            'spawn_owner_parent_node_id': str(owner_parent_node_id or '').strip(),
            'spawn_owner_round_id': str(owner_round_id or '').strip(),
            'spawn_owner_entry_index': int(owner_entry_index),
            'spawn_owner_kind': 'acceptance',
        }
        acceptance = NodeRecord(
            node_id=new_node_id(),
            task_id=task.task_id,
            parent_node_id=parent_node_id or accepted_node.node_id,
            root_node_id=accepted_node.root_node_id,
            depth=accepted_node.depth + 1,
            node_kind=KIND_ACCEPTANCE,
            status='in_progress',
            goal=goal,
            prompt=prompt,
            input=prompt,
            output=[],
            check_result='',
            final_output='',
            can_spawn_children=False,
            created_at=_now(),
            updated_at=_now(),
            token_usage=TokenUsageSummary(tracked=bool(getattr(task.token_usage, 'tracked', False))),
            token_usage_by_model=[],
            metadata={**base_metadata, **dict(metadata or {})},
        )
        return self._log_service.create_node(task.task_id, acceptance)

    def _refresh_acceptance_node_metadata(self, *, task, node: NodeRecord) -> NodeRecord:
        """刷新验收节点的**元数据**（验收标准模板回填 + 恢复指纹）；bootstrap 正文不动。

        历史上这个函数每次刷新都把最新一次交付的正文搬回 `prompt`/`input`，于是
        持久历史里最该稳定的那段（scaffold 头探针比对的 bootstrap）成了移动靶，而
        交付本来就有交接通知这条 append 通道可达。返回库内最新记录。
        """
        if str(getattr(node, 'node_kind', '') or '').strip().lower() != KIND_ACCEPTANCE:
            return node
        metadata = dict(getattr(node, 'metadata', {}) or {})
        accepted_node_id = str(metadata.get('accepted_node_id') or '').strip()
        if not accepted_node_id:
            return node
        accepted_node = self._store.get_node(accepted_node_id)
        if accepted_node is None or str(getattr(accepted_node, 'task_id', '') or '').strip() != str(task.task_id or '').strip():
            return node
        acceptance_prompt = str(metadata.get('acceptance_prompt_template') or '').strip()
        if not acceptance_prompt and bool(metadata.get('final_acceptance')):
            final_acceptance = normalize_final_acceptance_metadata((task.metadata or {}).get('final_acceptance'))
            acceptance_prompt = str(final_acceptance.prompt or '').strip()
        child_handoff = self._child_handoff_payload(
            task_id=task.task_id,
            node=accepted_node,
            fallback_output='',
        )
        next_metadata = {
            **metadata,
            'accepted_node_id': accepted_node.node_id,
            'acceptance_prompt_template': acceptance_prompt,
            _RECOVERY_FINGERPRINT_KEY: self._acceptance_node_recovery_fingerprint(
                parent_node_id=str(node.parent_node_id or accepted_node.node_id).strip() or accepted_node.node_id,
                goal=str(node.goal or ''),
                acceptance_prompt=acceptance_prompt,
                accepted_node_id=accepted_node.node_id,
                submission_ref=str(child_handoff.get('result_payload_ref') or ''),
            ),
        }
        if dict(metadata or {}) == next_metadata:
            return node
        updater = getattr(self._store, 'update_node', None)
        updated = None
        if callable(updater):
            updated = updater(
                node.node_id,
                lambda record: record.model_copy(
                    update={
                        'updated_at': _now(),
                        'metadata': next_metadata,
                    }
                ),
            )
        if updated is not None:
            sync_read_model = getattr(self._log_service, 'sync_node_read_model', None)
            if callable(sync_read_model):
                sync_read_model(task.task_id, node.node_id)
            self._log_service.refresh_task_view(task.task_id, mark_unread=True)
            return updated
        return node.model_copy(update={'metadata': next_metadata})

    def _stamp_spawn_owner_metadata(
        self,
        *,
        node_id: str,
        parent_node_id: str,
        owner_round_id: str,
        owner_entry_index: int,
        owner_kind: str,
    ) -> None:
        normalized_node_id = str(node_id or '').strip()
        if not normalized_node_id:
            return

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            metadata['spawn_owner_parent_node_id'] = str(parent_node_id or '').strip()
            metadata['spawn_owner_round_id'] = str(owner_round_id or '').strip()
            metadata['spawn_owner_entry_index'] = int(owner_entry_index)
            metadata['spawn_owner_kind'] = str(owner_kind or '').strip()
            return metadata

        self._log_service.update_node_metadata(normalized_node_id, _mutate)

    def _latest_incomplete_spawn_round(self, *, parent: NodeRecord) -> tuple[str, dict[str, Any]] | None:
        operations = (parent.metadata or {}).get('spawn_operations') if isinstance(parent.metadata, dict) else {}
        if not isinstance(operations, dict):
            return None
        for round_id, payload in reversed(list(operations.items())):
            if not isinstance(payload, dict):
                continue
            if bool(payload.get('completed')):
                continue
            return str(round_id or '').strip(), copy.deepcopy(payload)
        return None

    def live_distribution_child_node_ids(self, *, task_id: str, parent_node_id: str) -> list[str]:
        task = self._store.get_task(task_id)
        parent = self._store.get_node(parent_node_id)
        if task is None or parent is None or str(parent.task_id or '').strip() != str(task.task_id or '').strip():
            return []
        if str(parent.node_kind or '').strip().lower() != KIND_EXECUTION:
            return []
        latest_round = self._latest_incomplete_spawn_round(parent=parent)
        if latest_round is None:
            return []
        _round_id, payload = latest_round
        child_node_ids: list[str] = []
        seen: set[str] = set()
        for entry in list(payload.get('entries') or []):
            if not isinstance(entry, dict):
                continue
            if str(entry.get('review_decision') or '').strip().lower() == 'blocked':
                continue
            child_node_id = str(entry.get('child_node_id') or '').strip()
            if not child_node_id or child_node_id in seen:
                continue
            child = self._store.get_node(child_node_id)
            if child is None or str(child.node_kind or '').strip().lower() != KIND_EXECUTION:
                continue
            if not self.node_is_in_live_distribution_tree(task_id=task.task_id, node_id=child_node_id):
                continue
            seen.add(child_node_id)
            child_node_ids.append(child_node_id)
        return child_node_ids

    def node_is_in_live_distribution_tree(self, *, task_id: str, node_id: str) -> bool:
        task = self._store.get_task(task_id)
        node = self._store.get_node(node_id)
        if task is None or node is None or str(node.task_id or '').strip() != str(task.task_id or '').strip():
            return False
        if str(node.node_kind or '').strip().lower() == KIND_ACCEPTANCE:
            return False
        if str(node.node_id or '').strip() == str(task.root_node_id or '').strip():
            return True
        metadata = dict(node.metadata or {})
        if bool(metadata.get('duplicate_spawn_child') or metadata.get('duplicate_spawn_child_of')):
            return False
        if str(metadata.get('spawn_owner_kind') or '').strip().lower() != 'child':
            return False
        owner_parent_node_id = str(metadata.get('spawn_owner_parent_node_id') or '').strip()
        owner_round_id = str(metadata.get('spawn_owner_round_id') or '').strip()
        owner_entry_index = int(metadata.get('spawn_owner_entry_index') or 0)
        if not owner_parent_node_id or not owner_round_id:
            return False
        parent = self._store.get_node(owner_parent_node_id)
        if parent is None:
            return False
        latest_round = self._latest_incomplete_spawn_round(parent=parent)
        if latest_round is None:
            return False
        latest_round_id, payload = latest_round
        if latest_round_id != owner_round_id:
            return False
        entries = list(payload.get('entries') or [])
        if owner_entry_index < 0 or owner_entry_index >= len(entries):
            return False
        entry = dict(entries[owner_entry_index] or {}) if isinstance(entries[owner_entry_index], dict) else {}
        if str(entry.get('child_node_id') or '').strip() != str(node.node_id or '').strip():
            return False
        return self.node_is_in_live_distribution_tree(task_id=task.task_id, node_id=parent.node_id)

    def live_distribution_tree_node_ids(self, *, task_id: str) -> list[str]:
        task = self._store.get_task(task_id)
        if task is None:
            return []
        node_ids: list[str] = []
        for record in list(self._store.list_task_nodes(task_id) or []):
            node_id = str(getattr(record, 'node_id', '') or '').strip()
            if not node_id:
                continue
            if not self.node_is_in_live_distribution_tree(task_id=task_id, node_id=node_id):
                continue
            node_ids.append(node_id)
        root_node_id = str(task.root_node_id or '').strip()
        if root_node_id and root_node_id not in node_ids:
            node_ids.insert(0, root_node_id)
        return node_ids

    def _acceptance_notice_block(self, *, accepted_node: NodeRecord) -> str:
        # 汇总针对被检验执行节点（根节点）的追加通知：已消费归档 + 尚未消费的 pending。
        # 同一逻辑通知会以不同 id（notif:xxx 与 root-notice:...）双写落盘，故按 message 文本去重。
        metadata = dict(accepted_node.metadata or {}) if isinstance(getattr(accepted_node, 'metadata', None), dict) else {}
        consumed = normalize_append_notice_context(metadata.get(APPEND_NOTICE_CONTEXT_KEY)).get('notice_records') or []
        pending = normalize_pending_append_notice_records(metadata.get(PENDING_APPEND_NOTICE_RECORDS_KEY))
        entries: list[str] = []
        seen_texts: set[str] = set()
        for item in [*list(consumed), *list(pending)]:
            if not isinstance(item, dict):
                continue
            text = str(item.get('message') or '').strip()
            if not text or text in seen_texts:
                continue
            seen_texts.add(text)
            entries.append(f'- {text}')
        if not entries:
            return ''
        title = '追加任务要求（追加通知，晚于上方验收标准；若两者冲突，以本段要求为准）'
        return f'{title}\n' + '\n'.join(entries)

    def _compose_acceptance_prompt(
        self,
        *,
        acceptance_prompt: str,
        node_output: str,
        node_output_ref: str,
        result_payload_ref: str,
        evidence_summary: str,
    ) -> str:
        normalized_acceptance_prompt = str(acceptance_prompt or '').strip()
        normalized_output = node_message_body(node_output, ref=result_payload_ref or node_output_ref)
        return (
            f'{normalized_acceptance_prompt}\n\n'
            f'子节点输出摘要：\n{normalized_output or "(empty)"}\n\n'
            f'子节点输出 ref：{node_output_ref or "(none)"}\n'
            f'子节点结果载荷 ref：{result_payload_ref or "(none)"}\n'
            f'子节点证据摘要：\n{evidence_summary or "(none)"}\n'
        )

    def _acceptance_turn_tail(self, *, task, node: NodeRecord) -> str:
        """验收回合的只进本轮尾块：当前待验提交的指针 + 历次裁定 + 被验节点的追加要求。

        这些块每轮都在变。放进 bootstrap 等于把持久历史里最该稳定的首记录改成
        移动靶（scaffold 头探针比对的正是它，见
        `context-and-cache-troubleshooting.md`「append-only 规则」）；放在尾块里，
        bootstrap 一次定稿，而"这一轮该判哪份提交"仍然每轮重新按当前状态算出来。
        摘要整篇进尾块（仅超出内联额度时截断并保留 ref），核验方据此解析当前提交，
        而不是依赖上下文里残留的旧正文。
        """
        metadata = dict(getattr(node, 'metadata', {}) or {})
        accepted_node_id = str(metadata.get('accepted_node_id') or node.parent_node_id or '').strip()
        accepted_node = self._store.get_node(accepted_node_id) if accepted_node_id else None
        if accepted_node is None or str(accepted_node.task_id or '').strip() != str(task.task_id or '').strip():
            return ''
        handshake = normalize_acceptance_handshake((accepted_node.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY))
        lines: list[str] = []
        result_ref = str(handshake.get('latest_execution_result_ref') or '').strip()
        result_summary = str(handshake.get('latest_execution_result_summary') or '').strip()
        output_ref = str(getattr(accepted_node, 'final_output_ref', '') or '').strip()
        if result_ref or output_ref or result_summary:
            lines.append(f'待验提交结果载荷 ref：{result_ref or "(none)"}')
            lines.append(f'交付输出 ref：{output_ref or "(none)"}')
            if result_summary:
                lines.append(f'提交摘要：{node_message_body(result_summary, ref=result_ref or output_ref)}')
        history_block = self._acceptance_verdict_history_block(node=node)
        if history_block:
            lines.append(history_block)
        else:
            # 首轮没有可比对象：no_progress 的判据是"与上一次驳回相比无实质变化"，
            # 没有上一次就无从成立，提示词与运行时都禁用这一档。
            lines.append('【历次验收裁定】这是本节点的首次裁定，不得使用 reject_no_progress。')
        # 决策 J：被验节点的追加要求对任何层级都可见，不再只对根节点开放。
        notice_block = self._acceptance_notice_block(accepted_node=accepted_node)
        if notice_block:
            lines.append(notice_block)
        if not lines:
            return ''
        return '【本轮验收上下文（运行时注入，不是新的用户指令）】\n' + '\n'.join(lines)

    def _acceptance_verdict_history_block(self, *, node: NodeRecord) -> str:
        """从 `metadata.rejection_history` 派生的紧凑裁定清单（机器派生，不做压缩保护）。"""
        metadata = dict(getattr(node, 'metadata', {}) or {})
        raw_history = metadata.get(_REJECTION_HISTORY_KEY)
        if not isinstance(raw_history, list) or not raw_history:
            return ''
        entries: list[str] = []
        for index, item in enumerate(raw_history, start=1):
            if not isinstance(item, dict):
                continue
            status = str(item.get('status') or '').strip().lower()
            verdict = 'pass' if status == STATUS_SUCCESS else ('reject' if status == STATUS_FAILED else '')
            text = str(item.get('final_output') or item.get('failure_reason') or '').strip()
            if not text:
                continue
            if len(text) > 400:
                text = text[:400] + '…'
            label = verdict or status or '裁定'
            entries.append(f'#{index} [{label}] {text}')
        if not entries:
            return ''
        body = '\n'.join(entries)
        if len(body) > _NODE_MESSAGE_INLINE_CHAR_LIMIT:
            body = body[:_NODE_MESSAGE_INLINE_CHAR_LIMIT] + '\n…（更早的裁定已省略）'
        return (
            '【历次验收裁定（供本次自比；措辞变化、格式调整、补充无关细节都不算实质改变）】\n'
            + body
        )

    def _child_handoff_payload(self, *, task_id: str, node: NodeRecord, fallback_output: str) -> dict[str, str]:
        latest = self._log_service.ensure_node_output_externalized(task_id, node.node_id) or self._store.get_node(node.node_id) or node
        latest = self._log_service.ensure_node_result_payload_externalized(task_id, latest.node_id) or self._store.get_node(latest.node_id) or latest
        result_payload = normalize_result_payload((latest.metadata or {}).get('result_payload'))
        summary = str(getattr(latest, 'final_output', '') or fallback_output or getattr(latest, 'failure_reason', '') or '').strip()
        output_ref = str(getattr(latest, 'final_output_ref', '') or '').strip()
        result_payload_ref = str((latest.metadata or {}).get('result_payload_ref') or '').strip()
        evidence_summary = '\n'.join(f'- {line}' for line in list(result_payload.evidence_summary() if result_payload is not None else []))
        return {
            'summary': summary,
            'output_ref': output_ref,
            'result_payload_ref': result_payload_ref,
            'evidence_summary': evidence_summary,
        }

    def _spawn_failure_info_from_node(
        self,
        *,
        task_id: str,
        node: NodeRecord,
        fallback_output: str,
        source: str,
    ) -> SpawnChildFailureInfo:
        latest = self._log_service.ensure_node_output_externalized(task_id, node.node_id) or self._store.get_node(node.node_id) or node
        latest = self._log_service.ensure_node_result_payload_externalized(task_id, latest.node_id) or self._store.get_node(latest.node_id) or latest
        result_payload = normalize_result_payload((latest.metadata or {}).get('result_payload'))
        summary = str(getattr(latest, 'final_output', '') or fallback_output or getattr(latest, 'failure_reason', '') or '').strip()
        output_ref = str(getattr(latest, 'final_output_ref', '') or '').strip()
        result_payload_ref = str((latest.metadata or {}).get('result_payload_ref') or '').strip()
        delivery_status = 'blocked'
        blocking_reason = ''
        remaining_work: list[str] = []
        if result_payload is not None:
            summary = str(result_payload.summary or result_payload.output or summary or blocking_reason).strip()
            delivery_status = 'blocked' if str(result_payload.delivery_status or '').strip() == 'blocked' else 'final'
            blocking_reason = str(result_payload.blocking_reason or '').strip()
            remaining_work = [str(item).strip() for item in list(result_payload.remaining_work or []) if str(item).strip()]
        return SpawnChildFailureInfo(
            source=str(source or 'runtime').strip() or 'runtime',
            summary=summary,
            delivery_status=delivery_status,
            blocking_reason=blocking_reason,
            remaining_work=remaining_work,
            output_ref=output_ref,
            result_payload_ref=result_payload_ref,
        )

    @staticmethod
    def _runtime_spawn_failure_info(error_text: str) -> SpawnChildFailureInfo:
        text = str(error_text or '错误：子节点流水线失败').strip() or '错误：子节点流水线失败'
        return SpawnChildFailureInfo(
            source='runtime',
            summary=text,
            delivery_status='blocked',
            blocking_reason=text,
            remaining_work=[],
            output_ref='',
            result_payload_ref='',
        )

    async def _submit_final_result(self, *, task, node: NodeRecord, payload: dict[str, Any]) -> dict[str, Any]:
        """提交回执：模型看到的是 outcome/submission_id/acceptance_node_id，
        真正的载荷由 turn 参数一路带回（见 react_loop `_handle_final_result_tool_turn`）。

        submission_id 在这里签发并写回载荷：验收裁决与回执都按它对齐同一次提交。
        """
        submitted = dict(payload or {})
        normalized_kind = str(getattr(node, 'node_kind', '') or '').strip().lower()
        if normalized_kind == KIND_ACCEPTANCE:
            verdict = str(submitted.get('verdict') or '').strip().lower()
            history = (node.metadata or {}).get(_REJECTION_HISTORY_KEY)
            if verdict == 'reject_no_progress' and not (isinstance(history, list) and history):
                # 首轮没有可比对象：这一档的判据是「与上次驳回相比无实质变化」，
                # 运行时直接拒绝，别让模型在第一次裁定就替人按下暂停。
                raise ValueError(
                    'reject_no_progress is unavailable on the first verdict: '
                    'no previous rejection to compare against'
                )
            execution = self._accepted_execution_node(task_id=task.task_id, acceptance=node)
            handshake = normalize_acceptance_handshake(
                ((execution.metadata or {}).get(ACCEPTANCE_HANDSHAKE_KEY) if execution is not None else {})
            )
            submission_id = str(handshake.get('latest_execution_submission_id') or '').strip()
            submitted['submission_id'] = submission_id
            return {
                'outcome': str(submitted.get('verdict') or '').strip().lower() or 'reject',
                'submission_id': submission_id,
                'acceptance_node_id': str(node.node_id or '').strip(),
            }
        submission_id = new_command_id().replace('command:', 'submission:', 1)
        submitted['submission_id'] = submission_id
        acceptance = self._required_acceptance_node(task=task, node=node)
        acceptance_node_id = str(getattr(acceptance, 'node_id', '') or '').strip()
        return {
            'outcome': 'pending_acceptance' if acceptance_node_id else 'submitted',
            'submission_id': submission_id,
            'acceptance_node_id': acceptance_node_id,
        }

    def _forget_node_route_binding(self, node_id: str) -> None:
        """节点落终态时清掉负载均衡的粘滞绑定，避免绑定表随节点数无界增长。"""
        forget = getattr(self._react_loop, 'forget_node_route_binding', None)
        if not callable(forget):
            return
        try:
            forget(str(node_id or '').strip())
        except Exception:
            pass

    def _mark_finished(
        self,
        task_id: str,
        node_id: str,
        result: NodeFinalResult,
        *,
        notify_parent: bool = True,
    ) -> NodeFinalResult:
        task = self._store.get_task(task_id)
        node = self._store.get_node(node_id)
        if node is not None and self._normalized_status(getattr(node, 'status', '')) in {
            STATUS_SUCCESS,
            STATUS_FAILED,
        }:
            # 终态吸收：迟到的在飞回合不得改写已落定的行。实盘 task:a0d1bd9bc9fa：
            # 15:53:09 最终验收通过、15:53:10 任务 success，而 15:48 resume 出来的根
            # 回合在 15:53:15 才走到检查点，一句 "task already completed" 把根写成
            # failed，refresh_task_view 随即把成功任务翻成失败。终态只由裁决/终局写一次。
            return self._result_from_record(node)
        self._forget_node_route_binding(node_id)
        if (
            task is not None
            and str(task.root_node_id or '').strip() == str(node_id or '').strip()
            and result.status == STATUS_FAILED
        ):
            capture_retry_snapshot = getattr(self._log_service, 'capture_retry_resume_snapshot', None)
            if callable(capture_retry_snapshot):
                try:
                    capture_retry_snapshot(task_id, node_id, failure_reason=result.failure_text)
                except Exception:
                    pass
        if (
            node is not None
            and str(getattr(node, 'node_kind', '') or '').strip().lower() == KIND_EXECUTION
            and result.status == STATUS_FAILED
        ):
            self._cancel_waiting_acceptance_for_execution_failure(
                task_id=task_id,
                execution_node_id=node_id,
                reason=result.failure_text,
            )
        status = STATUS_SUCCESS if result.status == STATUS_SUCCESS else STATUS_FAILED
        if status == STATUS_SUCCESS:
            clear_retry_snapshot = getattr(self._log_service, 'clear_retry_resume_snapshot', None)
            if callable(clear_retry_snapshot):
                try:
                    clear_retry_snapshot(task_id)
                except Exception:
                    pass
            self._log_service.remove_frame(task_id, node_id, publish_snapshot=False)
        self._persist_result_payload(task_id, node_id, result)
        self._log_service.update_node_status(
            task_id,
            node_id,
            status=status,
            final_output=result.output,
            failure_reason='' if status == STATUS_SUCCESS else result.failure_text,
        )
        self._log_service.ensure_node_result_payload_externalized(task_id, node_id)
        if notify_parent:
            self.schedule_terminal_child_message(task_id, node_id)
        latest = self._store.get_node(node_id)
        return self._result_from_record(latest) if latest is not None else result

    def schedule_terminal_child_message(self, task_id: str, node_id: str) -> None:
        """终局 → 父节点 `child_final`：唯一上报点，异步但不丢。

        P1「终局必上报」要求每个执行节点的终局都有一条机器消息抵达父节点；这是
        `all_children` 唤醒判据与父节点"子节点到底怎么了"的唯一来源，所以只在这一处
        产出，模型既伪造不了也漏发不了。验收节点不发（T15：它随被验收节点终局）。
        父节点自己发起的 terminate 由调用方传 `notify_parent=False` 抑制（设计 §4.4：
        终止者在同一个工具返回里已经知道了结果）。

        投递任务登记在 `_pending_terminal_messages` 里，`run_task` 的空转判定要等它
        跑完——否则消息还在路上，任务演员就收工关掉了 dispatcher。
        """
        sink = getattr(self, 'child_final_sink', None)
        if not callable(sink):
            return
        try:
            node = self._store.get_node(node_id)
        except Exception:
            return
        if node is None:
            return
        if str(getattr(node, 'node_kind', '') or '').strip().lower() != KIND_EXECUTION:
            return
        if not str(getattr(node, 'parent_node_id', '') or '').strip():
            return
        if self._normalized_status(getattr(node, 'status', '')) not in {STATUS_SUCCESS, STATUS_FAILED}:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        task = loop.create_task(
            self._emit_terminal_child_message(task_id=task_id, node_id=node_id),
            name=f'node-child-final:{task_id}:{node_id}',
        )
        self._pending_terminal_messages.add(task)
        task.add_done_callback(self._pending_terminal_messages.discard)

    async def _emit_terminal_child_message(self, *, task_id: str, node_id: str) -> None:
        sink = getattr(self, 'child_final_sink', None)
        if not callable(sink):
            return
        try:
            await sink(task_id=task_id, node_id=node_id)
        except Exception as exc:  # noqa: BLE001 - 消息送不出去不能回滚终局
            # 静默失败会让父节点永久等一个不会来的终局——正是 R13 那类"零 ERROR 悬空"。
            logger.warning(
                'title=child_final_delivery_failed task={} node={} error={}',
                task_id,
                node_id,
                describe_exception(exc),
            )

    def has_pending_terminal_messages(self) -> bool:
        return any(not task.done() for task in list(self._pending_terminal_messages))

    async def wait_pending_terminal_messages(self) -> None:
        while True:
            pending = [task for task in list(self._pending_terminal_messages) if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def finalize_inspected_execution_node(
        self,
        *,
        task_id: str,
        node_id: str,
        failure_reason: str = '',
    ) -> NodeRecord | None:
        """验收裁决落地后给被验收的执行节点签发终态。

        旧模型里这一步由 `_run_child_pipeline` 隐式完成（它把子节点的"终态"记在
        spawn entry 上，节点行反而留在 in_progress）；新模型必须显式写节点行，否则
        子节点会永远停在 `waiting_acceptance`：零 ERROR、零告警。失败原因非空表示
        验收判了"不可修复"，否则按执行节点自己提交的结果载荷收尾。
        """
        node = self._store.get_node(str(node_id or '').strip())
        if node is None:
            return None
        if self._normalized_status(getattr(node, 'status', '')) in {STATUS_SUCCESS, STATUS_FAILED}:
            return node
        if failure_reason:
            self._mark_failed(task_id, node.node_id, reason=failure_reason)
            return self._store.get_node(node.node_id)
        payload = normalize_result_payload((node.metadata or {}).get('result_payload'))
        if payload is None:
            return None
        self._mark_finished(task_id, node.node_id, payload)
        return self._store.get_node(node.node_id)

    def _mark_failed(
        self,
        task_id: str,
        node_id: str,
        *,
        reason: str,
        notify_parent: bool = True,
    ) -> NodeFinalResult:
        self._forget_node_route_binding(node_id)
        text = str(reason or 'node failed').strip() or 'node failed'
        return self._mark_finished(
            task_id,
            node_id,
            NodeFinalResult(
                status=STATUS_FAILED,
                delivery_status='blocked',
                summary=text,
                answer='',
                evidence=[],
                remaining_work=[],
                blocking_reason=text,
            ),
            notify_parent=notify_parent,
        )

    def _node_pause_requested(self, task_id: str, node_id: str) -> bool:
        node = self._store.get_node(node_id)
        if node is None or str(getattr(node, 'task_id', '') or '').strip() != str(task_id or '').strip():
            return False
        return bool(getattr(node, 'pause_requested', False)) or bool(getattr(node, 'is_paused', False))

    def _persist_error_and_pause_best_effort(self, *, task_id: str, node_id: str, text: str, node_goal: str) -> None:
        """尽力落盘节点错误记录与暂停状态；任何一步失败都不外抛。

        顺序决策：先 error_log（携带完整异常文本、单行小写、磁盘满时成功率更高），
        后 pause 记录（控制流不依赖它——NodePausedError 照抛，task_actor_service
        进入 control-only 返回，不会重跑节点）。两者都失败时文本进内存有界队列
        并留一条 warning 日志，不静默。

        兜底**按"写失败"分流，不按"为什么失败"分流**：曾经只有被分类成磁盘已满的失败才缓冲
        并告警，其余（包括这台机器上真正在发生的那一类——读侧 `SQLITE_IOERR_READ` 顶着
        `disk I/O error` 的文本）会被这个 `except` 静静吞掉，现场一条都不留。
        """
        error_log_persisted = False
        try:
            self._log_service.append_task_error_log(task_id, node_id, error_text=text, node_title=node_goal)
            error_log_persisted = True
        except Exception as exc:
            self._unpersisted_error_logs.append((task_id, node_id, text))
            logger.warning(
                'title=node_fault_report_write_failed stage=error_log task={} node={} buffered=yes '
                'disk_full={} error={}',
                task_id,
                node_id,
                is_disk_full_error(exc),
                describe_exception_chain(exc),
            )
        try:
            self._mark_node_paused(task_id, node_id, reason='error', remark=text)
        except Exception as exc:
            logger.warning(
                'title=node_fault_report_write_failed stage=pause_record task={} node={} '
                'error_log_persisted={} disk_full={} error={}',
                task_id,
                node_id,
                error_log_persisted,
                is_disk_full_error(exc),
                describe_exception_chain(exc),
            )

    def _mark_node_paused(self, task_id: str, node_id: str, *, reason: str = '', remark: str = '') -> NodeRecord | None:
        node = self._store.get_node(node_id)
        normalized_reason = str(reason or getattr(node, 'pause_reason', '') if node is not None else '').strip().lower() or 'manual'
        existing = self._store.get_task_node_pause(node_id)
        if not remark and existing is not None:
            remark = str(existing.remark or '')
        return self._log_service.set_node_pause_state(
            task_id,
            node_id,
            pause_requested=True,
            is_paused=True,
            pause_reason=normalized_reason,
            remark=remark,
            delivered=bool(existing.delivered) if existing is not None else False,
        )

    def fail_paused_node(
        self,
        task_id: str,
        node_id: str,
        reason: str = '',
        *,
        notify_parent: bool = True,
    ) -> NodeFinalResult:
        node = self._store.get_node(node_id)
        if node is None or str(node.task_id or '').strip() != str(task_id or '').strip():
            raise ValueError(f'node not found: {node_id}')
        if node.status in {STATUS_SUCCESS, STATUS_FAILED}:
            return self._result_from_record(node)
        text = str(reason or node.failure_reason or 'failed by operator').strip() or 'failed by operator'
        return self._mark_failed(task_id, node_id, reason=text, notify_parent=notify_parent)

    def _pause_requested(self, task_id: str) -> bool:
        task = self._store.get_task(task_id)
        if task is None:
            return False
        return bool(task.pause_requested) and not bool(task.cancel_requested)

    def _persist_result_payload(self, task_id: str, node_id: str, result: NodeFinalResult) -> None:
        payload = result.payload_dict()

        def _mutate(metadata: dict[str, Any]) -> dict[str, Any]:
            previous = metadata.get('result_payload')
            metadata['result_schema_version'] = RESULT_SCHEMA_VERSION
            metadata['result_payload'] = payload
            if not isinstance(previous, dict) or previous != payload:
                # 打回重做后的重提交会覆写结果载荷：载荷内容变化时必须清空已外部化的
                # 载荷 ref 与摘要，让 ensure_node_result_payload_externalized 针对最新
                # 载荷重新外部化，否则验收/握手读取方会永远拿到首次提交载荷的过期
                # ref（事故复盘：验收节点据此复核已被取代的阻塞占位载荷）。
                metadata.pop('result_payload_ref', None)
                metadata.pop('result_payload_summary', None)
            return metadata

        self._log_service.update_node_metadata(node_id, _mutate)

    @staticmethod
    def _result_from_record(node: NodeRecord) -> NodeFinalResult:
        payload = normalize_result_payload((node.metadata or {}).get('result_payload'))
        if payload is not None and str(payload.status or '').strip().lower() == str(node.status or '').strip().lower():
            return payload
        status = STATUS_SUCCESS if node.status == STATUS_SUCCESS else STATUS_FAILED
        final_output = str(node.final_output or '').strip()
        failure_reason = str(node.failure_reason or '').strip()
        return NodeFinalResult(
            status=status,
            delivery_status=('final' if status == STATUS_FAILED and (final_output or str(node.check_result or '').strip()) else 'blocked') if status == STATUS_FAILED else 'final',
            summary=failure_reason or final_output or 'node finished',
            answer=final_output if status == STATUS_SUCCESS else final_output,
            evidence=[],
            remaining_work=[],
            blocking_reason=failure_reason if status == STATUS_FAILED else '',
        )


def _now() -> str:
    from main.protocol import now_iso

    return now_iso()
